#!/usr/bin/env python3
"""Build, push, and deploy all any-embedding services to GCP Cloud Run.

Reads model definitions from config.yaml and registry settings from
terraform.tfvars, then builds + pushes all Docker images and runs
terraform apply — single command, zero manual wiring.

Usage:
    python deployment/gcp/deploy.py              # full deploy
    python deployment/gcp/deploy.py --plan       # build images, then tf plan
    python deployment/gcp/deploy.py --skip-build # terraform only (images exist)
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GCP_DIR = os.path.join(REPO_ROOT, "deployment", "gcp")
HF_MODEL_FILE_URL = "https://huggingface.co/{}/resolve/main/config.json"
CLOUD_RUN_GPU_REGIONS = {
    "asia-southeast1",
    "europe-west1",
    "europe-west4",
    "us-central1",
    "us-east4",
}


def load_dotenv(path: str) -> None:
    """Load KEY=value pairs from a .env file into os.environ (no overwrite)."""
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def parse_tfvars(path: str) -> dict[str, str]:
    """Parse a simple terraform.tfvars file (key = "value" lines)."""
    values: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r'(\w+)\s*=\s*"([^"]*)"', line)
            if m:
                values[m.group(1)] = m.group(2)
    return values


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"▸ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kwargs)


def _extract_hf_error(body: str) -> str:
    if not body:
        return ""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body.strip()
    if isinstance(payload, dict):
        error_message = payload.get("error") or payload.get("message")
        if isinstance(error_message, str):
            return error_message.strip()
    return body.strip()


def check_hf_model_access(model_name: str, hf_token: str) -> tuple[bool, str]:
    """Validate file download access to a Hugging Face model using the token."""
    request = Request(
        HF_MODEL_FILE_URL.format(quote(model_name, safe="/")),
        headers={"Authorization": f"Bearer {hf_token}"},
    )
    try:
        with urlopen(request, timeout=15):
            return True, ""
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        reason = _extract_hf_error(body) or f"HTTP {exc.code}"
        return False, reason
    except URLError as exc:
        return False, str(exc.reason)


def validate_gated_models(models: list[dict], hf_token: str) -> None:
    """Fail fast when gated models cannot be fetched with the configured token."""
    gated_models = [m for m in models if m.get("gated", False)]
    if not gated_models:
        return

    if not hf_token:
        print("ERROR: HF_TOKEN is required for gated models:")
        for model in gated_models:
            print(f"  - {model['name']} ({model['model']})")
        print("\nSet HF_TOKEN in your shell or .env before running deploy:gcp.")
        sys.exit(1)

    print("Checking Hugging Face access for gated models...")
    failures: list[tuple[str, str, str]] = []
    for model in gated_models:
        ok, reason = check_hf_model_access(model["model"], hf_token)
        if ok:
            print(f"✓ gated access OK: {model['name']}")
            continue
        failures.append((model["name"], model["model"], reason))

    if failures:
        print("\nERROR: Hugging Face access check failed for gated models:")
        for name, model_id, reason in failures:
            print(f"  - {name} ({model_id}): {reason}")
        print(
            "\nRequired fix: accept the model license on Hugging Face and ensure "
            "your HF_TOKEN can access public gated repositories."
        )
        sys.exit(1)


def validate_gcp_region(models: list[dict], region: str) -> None:
    """Fail fast when GPU workers are configured for an unsupported Cloud Run region."""
    gpu_models = [m["name"] for m in models if m.get("gpu", False)]
    if not gpu_models:
        return

    if region in CLOUD_RUN_GPU_REGIONS:
        return

    print("ERROR: Cloud Run GPUs are not supported in the configured region.")
    print(f"  region: {region}")
    print(f"  gpu models: {', '.join(gpu_models)}")
    print(
        "  supported regions: "
        + ", ".join(sorted(CLOUD_RUN_GPU_REGIONS))
    )
    print(
        "\nUse a supported region in deployment/gcp/terraform.tfvars or mark those models "
        "with gpu: false before deploying."
    )
    sys.exit(1)


def build_and_push(
    tag: str,
    dockerfile: str,
    build_args: dict[str, str],
    secrets: dict[str, str],
    context: str,
) -> str:
    """Build and push a single Docker image. Returns the tag."""
    cmd = [
        "docker",
        "buildx",
        "build",
        "--platform",
        "linux/amd64",
        "--provenance=false",
        "--sbom=false",
        "--push",
        "-f",
        dockerfile,
        "-t",
        tag,
    ]
    for k, v in build_args.items():
        if v:
            cmd += ["--build-arg", f"{k}={v}"]
    for k, v in secrets.items():
        if v:
            cmd += ["--secret", f"id={k},env={k}"]
    cmd.append(context)
    run(cmd)
    return tag


def worker_service_name(model_name: str) -> str:
    """Keep Python deploy naming aligned with Terraform worker service names."""
    sanitized = model_name.replace(".", "-")
    candidate = f"ae-w-{sanitized}"
    if len(candidate) < 50:
        return candidate
    digest = hashlib.md5(model_name.encode(), usedforsecurity=False).hexdigest()[:6]
    return f"ae-w-{sanitized[:37]}-{digest}"


def expected_service_name(resource_name: str, instance: dict) -> str:
    """Return the Cloud Run service name Terraform now expects for a resource."""
    if resource_name == "gateway":
        return "any-embedding-gateway"
    if resource_name == "worker":
        index_key = instance.get("index_key", "")
        if isinstance(index_key, str) and index_key:
            return worker_service_name(index_key)
    return ""


def unlock_stale_cloud_run_deletion_protection(target_region: str) -> None:
    """Clear Terraform's local deletion protection flag for stale replace targets.

    Cloud Run deletion protection is enforced by the Terraform provider before
    destroy. If a service is tainted or must be replaced because its region or
    generated name changed, Terraform needs the state flag cleared first.
    """
    result = subprocess.run(
        ["terraform", "state", "pull"],
        check=True,
        cwd=GCP_DIR,
        capture_output=True,
        text=True,
    )
    state = json.loads(result.stdout)
    unlocked: list[str] = []

    for resource in state.get("resources", []):
        if resource.get("mode") != "managed":
            continue
        if resource.get("type") != "google_cloud_run_v2_service":
            continue

        resource_name = resource.get("name", "")
        for instance in resource.get("instances", []):
            attrs = instance.get("attributes") or {}
            if not attrs.get("deletion_protection"):
                continue

            current_name = attrs.get("name", "")
            current_region = attrs.get("location", "")
            expected_name_value = expected_service_name(resource_name, instance)
            needs_replace = (
                instance.get("status") == "tainted"
                or (current_region and current_region != target_region)
                or (
                    expected_name_value
                    and current_name
                    and current_name != expected_name_value
                )
            )
            if not needs_replace:
                continue

            attrs["deletion_protection"] = False
            index_key = instance.get("index_key")
            resource_id = resource_name if index_key is None else f'{resource_name}["{index_key}"]'
            unlocked.append(f"{resource_id} -> {current_region}/{current_name}")

    if not unlocked:
        return

    if isinstance(state.get("serial"), int):
        state["serial"] += 1

    with tempfile.NamedTemporaryFile("w", suffix=".tfstate", delete=False) as tmp:
        json.dump(state, tmp)
        tmp.write("\n")
        tmp_path = tmp.name

    try:
        print("Clearing Terraform Cloud Run deletion protection for replace targets:")
        for item in unlocked:
            print(f"  - {item}")
        run(["terraform", "state", "push", tmp_path], cwd=GCP_DIR)
    finally:
        os.remove(tmp_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy any-embedding to GCP")
    parser.add_argument(
        "--plan", action="store_true", help="Run terraform plan instead of apply"
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip image build/push, only run terraform",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=2,
        help="Max parallel image builds (default: 2)",
    )
    args = parser.parse_args()

    config_path = os.path.join(REPO_ROOT, "config.yaml")
    tfvars_path = os.path.join(GCP_DIR, "terraform.tfvars")

    # Load .env from repo root (won't overwrite existing env vars)
    load_dotenv(os.path.join(REPO_ROOT, ".env"))

    with open(config_path) as f:
        config = yaml.safe_load(f)
    models = config.get("models", [])

    tfvars = parse_tfvars(tfvars_path)
    gateway_image = tfvars.get("gateway_image", "")
    image_registry = tfvars.get("image_registry", "")
    region = tfvars.get("region", "")

    if not gateway_image or not image_registry:
        print(
            "ERROR: gateway_image and image_registry must be set in "
            "deployment/gcp/terraform.tfvars"
        )
        sys.exit(1)

    if not region:
        print("ERROR: region must be set in deployment/gcp/terraform.tfvars")
        sys.exit(1)

    validate_gcp_region(models, region)

    hf_token = os.environ.get("HF_TOKEN", "")
    os.environ["TF_VAR_hf_token"] = hf_token

    if not args.skip_build:
        validate_gated_models(models, hf_token)

        # Ensure BuildKit is enabled (required for --secret)
        os.environ["DOCKER_BUILDKIT"] = "1"

        # Authenticate docker with the registry
        registry_host = gateway_image.split("/")[0]  # e.g. gcr.io or us-docker.pkg.dev
        print(f"\nAuthenticating docker with {registry_host}...")
        run(["gcloud", "auth", "configure-docker", registry_host, "--quiet"])

        total = len(models) + 1
        print(f"\n{'=' * 60}")
        print(f"Building {total} images (1 gateway + {len(models)} workers)")
        print(f"{'=' * 60}\n")

        # ── Gateway ──────────────────────────────────────────────────
        build_and_push(
            tag=gateway_image,
            dockerfile=os.path.join(REPO_ROOT, "Dockerfile.gateway"),
            build_args={},
            secrets={},
            context=REPO_ROOT,
        )
        print(f"✓ gateway → {gateway_image}\n")

        # ── Workers (parallel) ───────────────────────────────────────
        failed = False
        futures: dict = {}
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            for m in models:
                name = m["name"].replace(".", "-")
                tag = f"{image_registry}-{name}:latest"
                uses_gpu = m.get("gpu", False)
                dockerfile = os.path.join(
                    REPO_ROOT,
                    "Dockerfile.worker-gpu" if uses_gpu else "Dockerfile.worker",
                )
                build_args = {
                    "MODEL_NAME": m["model"],
                    "SENTENCE_TRANSFORMERS_VERSION": m.get("sentence_transformers_version", ""),
                    "TRANSFORMERS_VERSION": m.get("transformers_version", ""),
                }
                secrets = {"HF_TOKEN": hf_token}
                fut = pool.submit(
                    build_and_push, tag, dockerfile, build_args, secrets, REPO_ROOT
                )
                futures[fut] = m["name"]

            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    tag = fut.result()
                    print(f"✓ {name} → {tag}")
                except subprocess.CalledProcessError as exc:
                    print(f"✗ {name} failed (exit {exc.returncode})")
                    failed = True

        if failed:
            print("\nERROR: one or more image builds failed")
            sys.exit(1)
    else:
        print("Skipping image build/push; Terraform will reuse the images already present in the registry.")
        print("If you changed app code or a Dockerfile, rerun deploy without --skip-build so Cloud Run gets a new image.")

    # ── Terraform ────────────────────────────────────────────────────
    tf_cmd = "plan" if args.plan else "apply"
    print(f"\n{'=' * 60}")
    print(f"Running terraform {tf_cmd}")
    print(f"{'=' * 60}\n")

    run(["terraform", "init", "-input=false", "-upgrade"], cwd=GCP_DIR)
    if tf_cmd == "apply":
        unlock_stale_cloud_run_deletion_protection(region)
    tf_args = ["terraform", tf_cmd]
    if tf_cmd == "apply":
        tf_args += ["-parallelism=1"]
        tf_args.append("-auto-approve")
    run(tf_args, cwd=GCP_DIR)

    print(f"\n✓ deploy:gcp {'(plan)' if args.plan else ''} complete")


if __name__ == "__main__":
    main()
