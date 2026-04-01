#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

export AE_SCRIPT_DIR="$SCRIPT_DIR"
export AE_REPO_ROOT="$REPO_ROOT"

exec uv run python - "$@" <<'PY'
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml


SCRIPT_DIR = Path(os.environ["AE_SCRIPT_DIR"])
REPO_ROOT = Path(os.environ["AE_REPO_ROOT"])
TF_DIR = SCRIPT_DIR
TFVARS_FILE = TF_DIR / "terraform.tfvars"
CONFIG_FILE = REPO_ROOT / "config.yaml"
SMOKE_TIMEOUT = float(os.environ.get("AE_SMOKE_TIMEOUT", "300"))
SMALL_RED_DOT_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEklEQVR4nGP8z4AdMOEQH6QSAM1BAQ/oQeJvAAAAAElFTkSuQmCC"
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"


@dataclass
class ProbeResult:
    ok: bool
    latency_ms: float
    status_code: int
    response_json: dict[str, Any] | None
    error: str | None


@dataclass
class ModelResult:
    name: str
    model_type: str
    expected_dims: int
    registered: bool
    first: ProbeResult
    second: ProbeResult

    @property
    def actual_dims(self) -> int | None:
        for probe in (self.second, self.first):
            if not probe.ok or not probe.response_json:
                continue
            data = probe.response_json.get("data", [])
            if data and isinstance(data[0], dict):
                embedding = data[0].get("embedding", [])
                if isinstance(embedding, list):
                    return len(embedding)
        return None

    @property
    def delta_ms(self) -> float:
        return self.first.latency_ms - self.second.latency_ms

    @property
    def cold_start(self) -> str:
        if not self.first.ok or not self.second.ok or self.second.latency_ms <= 0:
            return "n/a"
        ratio = self.first.latency_ms / self.second.latency_ms
        delta = self.delta_ms
        if self.first.latency_ms >= 5000 and ratio >= 1.75 and delta >= 1500:
            return "likely"
        if self.first.latency_ms >= 2500 and ratio >= 1.30 and delta >= 700:
            return "possible"
        return "no"

    @property
    def perf_band(self) -> str:
        probe = self.second if self.second.ok else self.first
        if not probe.ok:
            return "error"
        if probe.latency_ms < 800:
            return "fast"
        if probe.latency_ms < 2500:
            return "steady"
        if probe.latency_ms < 7000:
            return "slow"
        return "very-slow"

    @property
    def passed(self) -> bool:
        return bool(
            self.registered
            and self.first.ok
            and self.second.ok
            and self.actual_dims
            and (not self.expected_dims or self.actual_dims == self.expected_dims)
        )

    @property
    def note(self) -> str:
        if not self.registered:
            return "missing from /v1/models"
        if not self.first.ok:
            return (self.first.error or f"HTTP {self.first.status_code}")[:120]
        if not self.second.ok:
            return (self.second.error or f"HTTP {self.second.status_code}")[:120]
        if self.actual_dims and self.expected_dims and self.actual_dims != self.expected_dims:
            return f"dims {self.actual_dims} != expected {self.expected_dims}"
        return "ok"


def color(text: str, code: str) -> str:
    return f"{code}{text}{RESET}"


def run_command(command: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def parse_tfvars(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    text = path.read_text() if path.exists() else ""
    for match in re.finditer(r'^([A-Za-z0-9_]+)\s*=\s*"([^"]*)"\s*$', text, re.M):
        values[match.group(1)] = match.group(2)
    return values


def discover_gateway(cli_value: str | None) -> tuple[str, str]:
    if cli_value:
        return cli_value.rstrip("/"), "cli"
    for env_name in ("ANY_EMBEDDING_GATEWAY", "GATEWAY_URL"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value.rstrip("/"), f"env:{env_name}"
    return run_command(["terraform", "output", "-raw", "gateway_url"], cwd=TF_DIR).rstrip("/"), "terraform output"


def discover_api_key(cli_value: str | None, tfvars: dict[str, str]) -> tuple[str, str]:
    if cli_value:
        return cli_value, "cli"
    for env_name in ("ANY_EMBEDDING_API_KEY", "API_KEY"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value, f"env:{env_name}"
    if tfvars.get("api_key"):
        return tfvars["api_key"], "terraform.tfvars"
    project_id = tfvars.get("project_id", "")
    if project_id:
        try:
            secret = run_command(
                [
                    "gcloud",
                    "secrets",
                    "versions",
                    "access",
                    "latest",
                    "--secret=any-embedding-api-key",
                    f"--project={project_id}",
                ]
            )
        except Exception:
            secret = ""
        if secret:
            return secret, "Secret Manager"
    raise SystemExit("Could not determine API key from args, env, terraform.tfvars, or Secret Manager")


def load_models(config_path: Path) -> list[dict[str, Any]]:
    config = yaml.safe_load(config_path.read_text()) or {}
    models = []
    for model in config.get("models", []):
        models.append(
            {
                "name": model["name"],
                "type": model.get("type", "text"),
                "dimensions": int(model.get("dimensions", 0)),
            }
        )
    return models


def json_request(url: str, headers: dict[str, str], payload: dict[str, Any] | None, timeout: float) -> ProbeResult:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method="GET" if payload is None else "POST")
    for key, value in headers.items():
        request.add_header(key, value)
    if payload is not None:
        request.add_header("Content-Type", "application/json")

    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            body = json.loads(raw) if raw else None
            return ProbeResult(
                ok=200 <= response.status < 300,
                latency_ms=(time.perf_counter() - started) * 1000,
                status_code=response.status,
                response_json=body,
                error=None,
            )
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        body = None
        error = raw.strip() or exc.reason
        try:
            body = json.loads(raw) if raw else None
            if isinstance(body, dict) and body.get("detail"):
                error = str(body["detail"])
        except json.JSONDecodeError:
            pass
        return ProbeResult(
            ok=False,
            latency_ms=(time.perf_counter() - started) * 1000,
            status_code=exc.code,
            response_json=body,
            error=error,
        )
    except URLError as exc:
        return ProbeResult(
            ok=False,
            latency_ms=(time.perf_counter() - started) * 1000,
            status_code=0,
            response_json=None,
            error=str(exc.reason),
        )


def build_payload(model: dict[str, Any]) -> dict[str, Any]:
    if model["type"] == "image":
        return {
            "model": model["name"],
            "input": {
                "type": "image",
                "image": {
                    "type": "image_base64",
                    "image_base64": SMALL_RED_DOT_PNG,
                },
            },
        }
    return {
        "model": model["name"],
        "input": "The quick brown fox jumps over the lazy dog.",
    }


def format_ms(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:7.0f}"


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def render_table(results: list[ModelResult]) -> None:
    headers = ["Model", "Type", "Exp", "Act", "1st ms", "2nd ms", "Delta", "Cold", "Perf", "Status"]
    rows: list[list[str]] = []
    for result in results:
        status = color("PASS", GREEN) if result.passed else color("FAIL", RED)
        cold = result.cold_start
        cold_label = {
            "likely": color(cold, YELLOW),
            "possible": color(cold, BLUE),
            "no": color(cold, GREEN),
        }.get(cold, cold)
        rows.append(
            [
                result.name,
                result.model_type,
                str(result.expected_dims or "-"),
                str(result.actual_dims or "-"),
                format_ms(result.first.latency_ms),
                format_ms(result.second.latency_ms),
                format_ms(result.delta_ms),
                cold_label,
                result.perf_band,
                status,
            ]
        )

    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(strip_ansi(value)))

    def format_row(values: list[str]) -> str:
        padded: list[str] = []
        for index, value in enumerate(values):
            padding = widths[index] - len(strip_ansi(value))
            padded.append(value + (" " * padding))
        return " | ".join(padded)

    print(format_row(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(format_row(row))


def print_section(title: str) -> None:
    print()
    print(color(f"=== {title} ===", CYAN))


def print_progress(index: int, total: int, model: dict[str, Any]) -> None:
    print(
        color(
            f"[{index}/{total}] probing {model['name']} ({model['type']}, expected dims {model['dimensions']})",
            YELLOW,
        )
    )


def summarize(results: list[ModelResult]) -> None:
    passed = sum(1 for result in results if result.passed)
    failed = len(results) - passed
    likely_cold = sum(1 for result in results if result.cold_start == "likely")
    warm_latencies = [result.second.latency_ms for result in results if result.second.ok]

    print()
    print(color("Summary", CYAN))
    print(f"  Passed:      {color(str(passed), GREEN)}")
    print(f"  Failed:      {color(str(failed), RED if failed else GREEN)}")
    print(f"  Cold likely: {color(str(likely_cold), YELLOW if likely_cold else GREEN)}")
    if warm_latencies:
        print(f"  Warm p50 ms: {statistics.median(warm_latencies):.0f}")
        percentile_index = max(0, math.ceil(len(warm_latencies) * 0.95) - 1)
        p95 = sorted(warm_latencies)[percentile_index]
        print(f"  Warm p95 ms: {p95:.0f}")

    failures = [result for result in results if not result.passed]
    if failures:
        print()
        print(color("Failures", RED))
        for result in failures:
            print(f"  - {result.name}: {result.note}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the deployed GCP backend")
    parser.add_argument("gateway", nargs="?", help="Override gateway URL")
    parser.add_argument("--api-key", dest="api_key", help="Override API key")
    args = parser.parse_args()

    if not CONFIG_FILE.exists():
        raise SystemExit(f"config.yaml not found at {CONFIG_FILE}")
    if not TFVARS_FILE.exists():
        raise SystemExit(f"terraform.tfvars not found at {TFVARS_FILE}")

    tfvars = parse_tfvars(TFVARS_FILE)
    gateway, gateway_source = discover_gateway(args.gateway)
    api_key, api_key_source = discover_api_key(args.api_key, tfvars)
    models = load_models(CONFIG_FILE)

    print_section("Deployment")
    print(f"Gateway:        {gateway}")
    print(f"Gateway source: {gateway_source}")
    print(f"API key source: {api_key_source}")
    print(f"Models in cfg:  {len(models)}")
    print(f"Timeout:        {SMOKE_TIMEOUT:.0f}s")

    print_section("Gateway")
    health = json_request(f"{gateway}/health", headers={}, payload=None, timeout=10)
    if not health.ok:
        print(color(f"Health check failed: {health.error or health.status_code}", RED))
        return 1
    print(color(f"Health OK in {health.latency_ms:.0f} ms", GREEN))

    models_probe = json_request(
        f"{gateway}/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        payload=None,
        timeout=30,
    )
    if not models_probe.ok or not models_probe.response_json:
        print(color(f"Model listing failed: {models_probe.error or models_probe.status_code}", RED))
        return 1

    available_models = {
        item.get("id")
        for item in models_probe.response_json.get("data", [])
        if isinstance(item, dict)
    }
    print(color(f"Model list OK: {len(available_models)} registered", GREEN))

    print_section("Per-Model Dashboard")
    results: list[ModelResult] = []
    headers = {"Authorization": f"Bearer {api_key}"}
    total_models = len(models)
    for index, model in enumerate(models, start=1):
        print_progress(index, total_models, model)
        payload = build_payload(model)
        first = json_request(f"{gateway}/v1/embeddings", headers=headers, payload=payload, timeout=SMOKE_TIMEOUT)
        second = json_request(f"{gateway}/v1/embeddings", headers=headers, payload=payload, timeout=SMOKE_TIMEOUT)
        results.append(
            ModelResult(
                name=model["name"],
                model_type=model["type"],
                expected_dims=model["dimensions"],
                registered=model["name"] in available_models,
                first=first,
                second=second,
            )
        )

    render_table(results)
    summarize(results)
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
PY
