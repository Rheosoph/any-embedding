#!/usr/bin/env python3
"""Build, push, and deploy all any-embedding services to GCP Cloud Run.

Reads model definitions from config.yaml and registry settings from
terraform.tfvars, then builds + pushes all Docker images and runs
terraform apply — single command, zero manual wiring.

Every image is content-addressed: a hash over its Dockerfile, the files the
Dockerfile copies, and its build args becomes a second tag (":h-<hash>"). When
that tag already exists in the registry the build is skipped and the existing
digest is deployed, so a redeploy with unchanged inputs touches no Cloud Run
revision.

Usage:
    python deployment/gcp/deploy.py               # full deploy
    python deployment/gcp/deploy.py --plan        # build images, then tf plan
    python deployment/gcp/deploy.py --skip-build  # terraform only (images exist)
    python deployment/gcp/deploy.py --force-build # rebuild even if unchanged
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
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GCP_DIR = os.path.join(REPO_ROOT, "deployment", "gcp")
GCP_GATEWAY_DOCKERFILE = os.path.join(REPO_ROOT, "Dockerfile.gateway")
GCP_WORKER_DOCKERFILES = {
    False: os.path.join(REPO_ROOT, "Dockerfile.worker"),
    True: os.path.join(REPO_ROOT, "Dockerfile.worker-gpu"),
}
HF_MODEL_FILE_URL = "https://huggingface.co/{}/resolve/main/config.json"
CLOUD_RUN_GPU_REGIONS = {
    "asia-southeast1",
    "europe-west1",
    "europe-west4",
    "us-central1",
    "us-east4",
}

# Bump when the hashing rules change so stale ":h-<hash>" tags are never reused.
HASH_SCHEME = "1"
SHARED_APP_DIR = "app/shared"
# Exactly the files the Dockerfiles copy (plus every file under app/shared).
WORKER_IMAGE_INPUTS = [
    ".dockerignore",
    "app/__init__.py",
    "app/gcp/__init__.py",
    "app/gcp/worker.py",
    "app/gcp/download_model.py",
    "pyproject.toml",
]
GATEWAY_IMAGE_INPUTS = [
    ".dockerignore",
    "app/__init__.py",
    "app/gcp/__init__.py",
    "app/gcp/gateway.py",
    "pyproject.toml",
    "config.yaml",
]
REGISTRY_LOOKUP_WORKERS = 6
DEFAULT_PARALLEL_BUILDS = 4
DEFAULT_TF_PARALLELISM = 1


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


# ── Image identity ───────────────────────────────────────────────────────────


def shared_app_files(root: str | None = None) -> list[str]:
    """Return every file under app/shared as sorted repo-relative paths."""
    root = root or REPO_ROOT
    base = os.path.join(root, SHARED_APP_DIR)
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in filenames:
            files.append(os.path.relpath(os.path.join(dirpath, filename), root))
    return sorted(files)


def image_input_files(static_inputs: list[str], root: str | None = None) -> list[str]:
    """Combine a Dockerfile's static COPY list with the app/shared tree."""
    return sorted(set(static_inputs) | set(shared_app_files(root)))


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_input_hash(
    dockerfile: str,
    files: list[str],
    build_args: dict[str, str],
    root: str | None = None,
) -> str:
    """Hash everything that determines an image: Dockerfile, copied files, build args.

    Empty build args are ignored because build_and_push does not pass them either.
    Every field is length-prefixed so concatenations cannot collide.
    """
    root = root or REPO_ROOT
    digest = hashlib.sha256()

    def feed(part: str | bytes) -> None:
        data = part.encode() if isinstance(part, str) else part
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)

    feed(HASH_SCHEME)
    with open(dockerfile, "rb") as f:
        feed(f.read())
    for relative_path in sorted(files):
        feed(relative_path)
        feed(_file_sha256(os.path.join(root, relative_path)))
    for key, value in sorted(build_args.items()):
        if value:
            feed(key)
            feed(value)
    return digest.hexdigest()[:12]


def image_repository(image_reference: str) -> str:
    """Strip the tag or digest from an image reference."""
    repository = image_reference.split("@", 1)[0]
    prefix, separator, image_name = repository.rpartition("/")
    return prefix + separator + image_name.split(":", 1)[0]


@dataclass(frozen=True)
class ImageSpec:
    """One image to build: what it is called and what goes into it."""

    name: str
    primary_tag: str
    dockerfile: str
    files: tuple[str, ...]
    build_args: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)

    @property
    def repository(self) -> str:
        return image_repository(self.primary_tag)

    @property
    def input_hash(self) -> str:
        return image_input_hash(self.dockerfile, list(self.files), self.build_args)

    @property
    def hashed_tag(self) -> str:
        return f"{self.repository}:h-{self.input_hash}"


def worker_image_tag(image_registry: str, model_name: str) -> str:
    return f"{image_registry}-{model_name.replace('.', '-')}:latest"


def image_specs(
    gateway_image: str, image_registry: str, models: list[dict], hf_token: str
) -> list[ImageSpec]:
    """Describe the gateway and every worker image from config.yaml."""
    specs = [
        ImageSpec(
            name="gateway",
            primary_tag=gateway_image,
            dockerfile=GCP_GATEWAY_DOCKERFILE,
            files=tuple(image_input_files(GATEWAY_IMAGE_INPUTS)),
        )
    ]
    worker_files = tuple(image_input_files(WORKER_IMAGE_INPUTS))
    for m in models:
        specs.append(
            ImageSpec(
                name=m["name"],
                primary_tag=worker_image_tag(image_registry, m["name"]),
                dockerfile=GCP_WORKER_DOCKERFILES[m.get("gpu", False)],
                files=worker_files,
                build_args={
                    "MODEL_NAME": m["model"],
                    "MODEL_REVISION": m.get("model_revision", ""),
                    "SENTENCE_TRANSFORMERS_VERSION": m.get("sentence_transformers_version", ""),
                    "TRANSFORMERS_VERSION": m.get("transformers_version", ""),
                    "MODEL_CODE_REPO": m.get("model_code_repo", ""),
                    "MODEL_CODE_REVISION": m.get("model_code_revision", ""),
                },
                secrets={"HF_TOKEN": hf_token},
            )
        )
    return specs


# ── Build and registry ───────────────────────────────────────────────────────


def check_builder_supports_amd64(builder: str) -> bool:
    """Warn (never fail) when the buildx builder does not list linux/amd64 at all.

    `docker buildx inspect` cannot tell native from emulated platforms (its
    asterisk marks platforms fixed with --platform, not native ones), so this
    only catches builders that would refuse the build outright.
    """
    cmd = ["docker", "buildx", "inspect"]
    if builder:
        cmd += ["--builder", builder]
    try:
        result = run(cmd, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"WARNING: could not inspect the buildx builder ({exc}); continuing anyway.")
        return False
    supported = any(
        re.search(r"\blinux/amd64\b", line)
        for line in result.stdout.splitlines()
        if line.lstrip().startswith("Platforms:")
    )
    if not supported:
        label = builder or "the default docker builder"
        print(
            f"WARNING: {label} does not list linux/amd64 among its platforms; "
            "the build may fail or run under emulation. Set --builder (or BUILDX_BUILDER) "
            "to a builder with an amd64 node."
        )
    return supported


def read_build_metadata_digest(metadata_path: str) -> str:
    """Return the 'containerimage.digest' written by buildx, or '' if unavailable."""
    try:
        with open(metadata_path) as f:
            metadata = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return ""
    digest = metadata.get("containerimage.digest", "") if isinstance(metadata, dict) else ""
    return digest if isinstance(digest, str) else ""


def build_and_push(
    tags: list[str],
    dockerfile: str,
    build_args: dict[str, str],
    secrets: dict[str, str],
    context: str,
    builder: str = "",
) -> str:
    """Build and push a single Docker image under every tag in ``tags``.

    Returns the immutable ``repository@sha256:...`` reference. ``tags[0]`` is
    the content-addressed tag and is used to verify (or, if buildx wrote no
    metadata, look up) the digest.
    """
    with tempfile.TemporaryDirectory() as tmp:
        metadata_path = os.path.join(tmp, "metadata.json")
        cmd = ["docker", "buildx", "build"]
        if builder:
            cmd += ["--builder", builder]
        cmd += [
            "--platform",
            "linux/amd64",
            "--provenance=false",
            "--sbom=false",
            "--push",
            "--metadata-file",
            metadata_path,
            "-f",
            dockerfile,
        ]
        for tag in tags:
            cmd += ["-t", tag]
        for k, v in build_args.items():
            if v:
                cmd += ["--build-arg", f"{k}={v}"]
        for k, v in secrets.items():
            if v:
                cmd += ["--secret", f"id={k},env={k}"]
        cmd.append(context)
        run(cmd)
        digest = read_build_metadata_digest(metadata_path)

    identity_tag = tags[0]
    if not digest:
        return resolve_image_digest(identity_tag)
    return validate_image_digest(identity_tag, f"{canonical_repository(identity_tag)}@{digest}")


def canonical_repository(image_reference: str) -> str:
    """Repository path as Artifact Registry reports it (migrated gcr.io URLs included)."""
    repository = image_repository(image_reference)
    registry, _, path = repository.partition("/")
    # Artifact Registry's describe command canonicalizes migrated gcr.io URLs.
    gcr_locations = {
        "gcr.io": "us", "us.gcr.io": "us", "eu.gcr.io": "europe", "asia.gcr.io": "asia",
    }
    if registry in gcr_locations:
        project, _, image_path = path.partition("/")
        repository = f"{gcr_locations[registry]}-docker.pkg.dev/{project}/{registry}/{image_path}"
    return repository


def validate_image_digest(image_reference: str, resolved: str) -> str:
    """Check that ``resolved`` is an immutable digest reference for ``image_reference``."""
    match = re.fullmatch(
        r"(?P<repository>(?:[a-z0-9-]+-docker\.pkg\.dev|(?:[a-z]+\.)?gcr\.io)"
        r"/[a-zA-Z0-9._/-]+)@sha256:[0-9a-f]{64}",
        resolved,
    )
    if not match or match.group("repository") != canonical_repository(image_reference):
        raise ValueError(f"Registry did not return a valid immutable digest for {image_reference}")
    if "@" in image_reference and resolved.rsplit("@", 1)[1] != image_reference.rsplit("@", 1)[1]:
        raise ValueError(f"Registry returned a different digest for {image_reference}")
    return resolved


def resolve_image_digest(image_reference: str) -> str:
    """Resolve a registry tag to the immutable image Terraform must deploy."""
    result = run(
        [
            "gcloud", "artifacts", "docker", "images", "describe", image_reference,
            "--format=value(image_summary.fully_qualified_digest)",
        ],
        capture_output=True,
        text=True,
    )
    return validate_image_digest(image_reference, result.stdout.strip())


def resolve_image_digests(references: list[str]) -> dict[str, str]:
    """Resolve many tags concurrently; registry lookups are independent and slow."""
    with ThreadPoolExecutor(max_workers=REGISTRY_LOOKUP_WORKERS) as pool:
        return dict(zip(references, pool.map(resolve_image_digest, references)))


def find_existing_images(references: list[str]) -> dict[str, str]:
    """Return {tag: digest} for the tags present in the registry; absent tags are omitted."""

    def lookup(reference: str) -> str | None:
        try:
            return resolve_image_digest(reference)
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr or ""
            if "Image not found" in stderr:
                return None
            # Expired credentials or a registry outage must not degrade into
            # a full rebuild of every image.
            raise RuntimeError(
                f"Registry lookup failed for {reference}:\n{stderr.strip()}"
            ) from exc

    with ThreadPoolExecutor(max_workers=REGISTRY_LOOKUP_WORKERS) as pool:
        found = list(zip(references, pool.map(lookup, references)))
    return {reference: digest for reference, digest in found if digest}


def resolve_deployment_images(specs: list[ImageSpec]) -> dict:
    """Pin the --skip-build images before Terraform planning.

    The content-addressed tag for the current inputs wins when it exists, so
    --skip-build deploys the same image a build run would have skipped to;
    otherwise the primary tag (normally :latest) is used.
    """
    hashed_tags = {spec.name: spec.hashed_tag for spec in specs}
    digests = find_existing_images(list(hashed_tags.values()))
    by_name = {name: digests[tag] for name, tag in hashed_tags.items() if tag in digests}
    fallback = {spec.name: spec.primary_tag for spec in specs if spec.name not in by_name}
    if fallback:
        resolved = resolve_image_digests(list(fallback.values()))
        for name, tag in fallback.items():
            print(f"  {name}: no image for the current inputs, using {tag}")
            by_name[name] = resolved[tag]
    for name in hashed_tags:
        if name not in fallback:
            print(f"  {name}: {hashed_tags[name].rsplit(':', 1)[1]} (unchanged inputs)")
    return {
        "gateway_image": by_name["gateway"],
        "worker_images": {spec.name: by_name[spec.name] for spec in specs if spec.name != "gateway"},
    }


def build_images(specs: list[ImageSpec], parallel: int, force_build: bool, builder: str) -> dict[str, str]:
    """Build (or reuse) every image; returns {spec name: immutable digest reference}."""
    hashed_tags = {spec.name: spec.hashed_tag for spec in specs}
    digests: dict[str, str] = {}
    if not force_build:
        print("Checking the registry for images with unchanged inputs...")
        existing = find_existing_images(list(hashed_tags.values()))
        for spec in specs:
            digest = existing.get(hashed_tags[spec.name])
            if digest:
                digests[spec.name] = digest
                print(f"↷ {spec.name} unchanged ({hashed_tags[spec.name].rsplit(':', 1)[1]}), skipping build")

    to_build = [spec for spec in specs if spec.name not in digests]
    if to_build:
        print(f"\n{'=' * 60}")
        print(f"Building {len(to_build)} of {len(specs)} images")
        print(f"{'=' * 60}\n")

    failed = False
    futures: dict = {}
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for spec in to_build:
            fut = pool.submit(
                build_and_push,
                [hashed_tags[spec.name], spec.primary_tag],
                spec.dockerfile,
                spec.build_args,
                spec.secrets,
                REPO_ROOT,
                builder,
            )
            futures[fut] = spec.name

        for fut in as_completed(futures):
            name = futures[fut]
            try:
                digests[name] = fut.result()
                print(f"✓ {name} → {digests[name]}")
            except subprocess.CalledProcessError as exc:
                print(f"✗ {name} failed (exit {exc.returncode})")
                failed = True
            except ValueError as exc:
                print(f"✗ {name} failed: {exc}")
                failed = True

    print(f"\nImages: {len(to_build)} built, {len(specs) - len(to_build)} skipped")
    if failed:
        print("\nERROR: one or more image builds failed")
        sys.exit(1)
    return digests


# ── Terraform ────────────────────────────────────────────────────────────────


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


def run_terraform(
    tf_cmd: str,
    tfstate_bucket: str,
    region: str,
    deployment_images: dict,
    tf_parallelism: int,
) -> None:
    """terraform init + plan/apply with the resolved image digests as an override."""
    # A shared provider cache avoids re-downloading the google provider on every
    # deploy; init runs without -upgrade so the committed lock file stays authoritative.
    plugin_cache = os.environ.setdefault(
        "TF_PLUGIN_CACHE_DIR", os.path.expanduser("~/.terraform.d/plugin-cache")
    )
    os.makedirs(plugin_cache, exist_ok=True)

    run(
        [
            "terraform",
            "init",
            "-input=false",
            f"-backend-config=bucket={tfstate_bucket}",
        ],
        cwd=GCP_DIR,
    )
    if tf_cmd == "apply":
        unlock_stale_cloud_run_deletion_protection(region)
    tf_args = ["terraform", tf_cmd, "-input=false"]
    if tf_cmd == "apply":
        tf_args += [f"-parallelism={tf_parallelism}", "-auto-approve"]
    # Explicit -var-file overrides terraform.tfvars (TF_VAR_* would not), and a
    # changed digest creates a new Cloud Run revision even when :latest is reused.
    with tempfile.NamedTemporaryFile("w", suffix=".tfvars.json") as image_tfvars:
        json.dump(deployment_images, image_tfvars)
        image_tfvars.flush()
        run([*tf_args, f"-var-file={image_tfvars.name}"], cwd=GCP_DIR)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
        "--force-build",
        action="store_true",
        help="Rebuild every image even when an image with identical inputs is already in the registry",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=DEFAULT_PARALLEL_BUILDS,
        help=(
            f"Max parallel image builds (default: {DEFAULT_PARALLEL_BUILDS}). BuildKit shares "
            "identical in-flight steps across concurrent builds, so this is cheap to raise."
        ),
    )
    parser.add_argument(
        "--tf-parallelism",
        type=int,
        default=DEFAULT_TF_PARALLELISM,
        help=(
            f"terraform apply -parallelism (default: {DEFAULT_TF_PARALLELISM}). Cloud Run starts "
            "one instance of every new revision to verify readiness; those count against the "
            "regional GPU quota (3 L4 GPUs in europe-west1) together with any still-serving GPU "
            "instances, so raise this only when you know how many GPU instances are up "
            "(e.g. 4 for a CPU-only or gateway-only change)."
        ),
    )
    parser.add_argument(
        "--builder",
        default=os.environ.get("BUILDX_BUILDER", ""),
        help="docker buildx builder to use (default: $BUILDX_BUILDER, else the docker default builder)",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()

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
    tfstate_bucket = tfvars.get("tfstate_bucket", "")

    if not gateway_image or not image_registry:
        print(
            "ERROR: gateway_image and image_registry must be set in "
            "deployment/gcp/terraform.tfvars"
        )
        sys.exit(1)

    if not region:
        print("ERROR: region must be set in deployment/gcp/terraform.tfvars")
        sys.exit(1)

    if not tfstate_bucket:
        print("ERROR: tfstate_bucket must be set in deployment/gcp/terraform.tfvars")
        sys.exit(1)

    validate_gcp_region(models, region)

    hf_token = os.environ.get("HF_TOKEN", "")
    os.environ["TF_VAR_hf_token"] = hf_token

    if not args.skip_build:
        validate_gated_models(models, hf_token)

        # Ensure BuildKit is enabled (required for --secret)
        os.environ["DOCKER_BUILDKIT"] = "1"
        # Pin the image config timestamp so a fully cache-hit rebuild reproduces
        # the same digest and therefore does not roll a new Cloud Run revision.
        os.environ["SOURCE_DATE_EPOCH"] = "0"

        # Authenticate docker with the registry
        registry_host = gateway_image.split("/")[0]  # e.g. gcr.io or us-docker.pkg.dev
        print(f"\nAuthenticating docker with {registry_host}...")
        run(["gcloud", "auth", "configure-docker", registry_host, "--quiet"])
        check_builder_supports_amd64(args.builder)

        specs = image_specs(gateway_image, image_registry, models, hf_token)
        print(f"\nImages: 1 gateway + {len(models)} workers")
        digests = build_images(specs, args.parallel, args.force_build, args.builder)
        deployment_images = {
            "gateway_image": digests["gateway"],
            "worker_images": {m["name"]: digests[m["name"]] for m in models},
        }
    else:
        print("Skipping image build/push; Terraform will reuse the images already present in the registry.")
        print("If you changed app code or a Dockerfile, rerun deploy without --skip-build so Cloud Run gets a new image.")
        print("\nResolving registry images to immutable digests...")
        deployment_images = resolve_deployment_images(
            image_specs(gateway_image, image_registry, models, hf_token)
        )

    # ── Terraform ────────────────────────────────────────────────────
    tf_cmd = "plan" if args.plan else "apply"
    print(f"\n{'=' * 60}")
    print(f"Running terraform {tf_cmd}")
    print(f"{'=' * 60}\n")
    run_terraform(tf_cmd, tfstate_bucket, region, deployment_images, args.tf_parallelism)

    print(f"\n✓ deploy:gcp {'(plan)' if args.plan else ''} complete")


if __name__ == "__main__":
    main()
