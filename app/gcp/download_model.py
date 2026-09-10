"""Build-time model download."""

from __future__ import annotations

import ast
import json
import os
import posixpath
import re
import shutil
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

# Alternative export formats and documentation assets that the PyTorch runtime
# never reads. Hub repositories frequently ship every format side by side, which
# would otherwise triple the size of the weight layer.
ALWAYS_IGNORE = (
    "onnx/*", "onnx/**", "openvino/*", "openvino/**", "*.onnx", "*.onnx_data",
    "coreml/**", "*.mlpackage/**", "*.gguf", "*.tflite", "*.msgpack", "*.h5", "*.ot",
    "images/*", "images/**", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ipynb", ".gitattributes",
)
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth", ".ckpt")
# Pickle weight formats that transformers only loads when no safetensors file
# exists in the same directory.
PICKLE_WEIGHT_SUFFIXES = (".bin", ".pt", ".pth", ".ckpt")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")


def _load_secret(name: str) -> str:
    """Read a BuildKit secret mounted at /run/secrets/<name>, fall back to env."""
    path = f"/run/secrets/{name}"
    if os.path.isfile(path):
        with open(path) as f:
            return f.read().strip()
    return os.environ.get(name, "")


def build_ignore_patterns(files: list[str]) -> list[str]:
    """Return snapshot_download ignore patterns for a listed repository.

    Pickle weights are skipped only where a safetensors file sits in the same
    directory, so sub-modules that ship nothing else (e.g. ``2_Dense/``) keep
    their weights.
    """
    patterns = list(ALWAYS_IGNORE)
    safetensors_dirs = {posixpath.dirname(path) for path in files if path.endswith(".safetensors")}
    for path in files:
        if path.endswith(PICKLE_WEIGHT_SUFFIXES) and posixpath.dirname(path) in safetensors_dirs:
            patterns.append(path)
    return patterns


def inspect_repo(model_name: str, revision: str, hf_token: str) -> tuple[str, list[str]]:
    """Resolve the commit and file listing of a Hub repository with one request."""
    info = HfApi(token=hf_token or None).model_info(model_name, revision=revision or None)
    return info.sha or "", sorted(sibling.rfilename for sibling in info.siblings or [])


def list_weight_files(model_path: Path) -> list[Path]:
    """Return every weight file under the model directory."""
    return sorted(
        path for path in model_path.rglob("*")
        if path.is_file() and path.name.endswith(WEIGHT_SUFFIXES)
    )


def report_files(model_path: Path) -> int:
    """Print the materialized files with sizes and return the total in bytes."""
    total = 0
    for path in sorted(p for p in model_path.rglob("*") if p.is_file()):
        size = path.stat().st_size
        total += size
        print(f"  {path.relative_to(model_path).as_posix()}  {size / 2**20:.1f} MiB")
    print(f"Model files total: {total / 2**20:.1f} MiB")
    return total


def warn_about_unpinned_remote_code(model_path: Path) -> bool:
    """Warn when auto_map points at another repository that is not baked in."""
    config_path = model_path / "config.json"
    if not config_path.is_file():
        return False
    try:
        auto_map = json.loads(config_path.read_text()).get("auto_map")
    except (json.JSONDecodeError, AttributeError):
        return False
    if not isinstance(auto_map, dict):
        return False
    references = [
        item for value in auto_map.values()
        for item in (value if isinstance(value, list) else [value])
        if isinstance(item, str) and "--" in item
    ]
    if not references:
        return False
    print(
        "WARNING: config.json auto_map references external custom code "
        f"({', '.join(sorted(set(references)))}). It will be fetched from the Hub at runtime; "
        "set model_code_repo and model_code_revision in config.yaml to bake it into the image."
    )
    return True


def _validate_local_modules(model_path: Path, modules: set[str]) -> None:
    """Check the complete relative-import chain without executing model code.

    The pinned Transformers dynamic loader supports sibling Python modules.
    Package-relative imports are rejected explicitly rather than producing an
    image that will discover missing dependencies during offline startup.
    """
    pending = list(modules)
    seen: set[str] = set()
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        if not module.isidentifier():
            raise ValueError(f"Unsupported custom-code module path: {module}")
        source = model_path / f"{module}.py"
        if not source.is_file():
            raise ValueError(f"Missing custom-code dependency: {source.name}")
        seen.add(module)
        for node in ast.walk(ast.parse(source.read_text(), filename=str(source))):
            if not isinstance(node, ast.ImportFrom) or node.level == 0:
                continue
            if node.level != 1 or (node.module and not node.module.isidentifier()):
                raise ValueError(f"Unsupported package-relative import in {source.name}")
            if node.module is None:
                # Transformers' relative-import discovery cannot reliably copy
                # dependencies written as `from . import sibling`.
                raise ValueError(f"Unsupported relative import without module in {source.name}")
            pending.append(node.module)


def localize_remote_code(
    model_path: Path, code_repo: str, code_revision: str, cache_folder: str, hf_token: str,
) -> None:
    """Bake an explicitly pinned external auto_map repository into the model."""
    if not code_repo or not COMMIT_PATTERN.fullmatch(code_revision):
        raise ValueError("MODEL_CODE_REPO and a full 40-character MODEL_CODE_REVISION commit are required together")
    config_path = model_path / "config.json"
    config = json.loads(config_path.read_text())
    auto_map = config.get("auto_map")
    if not isinstance(auto_map, dict):
        raise ValueError("Custom-code localization requires config.json auto_map")
    prefix = f"{code_repo}--"
    modules: set[str] = set()
    replacements = 0

    def local_reference(value):
        nonlocal replacements
        if value is None:
            return None
        if isinstance(value, list):
            return [local_reference(item) for item in value]
        if not isinstance(value, str):
            raise ValueError("Unsupported auto_map reference")
        if value.startswith(prefix):
            value = value[len(prefix):]
            replacements += 1
        elif "--" in value:
            raise ValueError("auto_map refers to an external repository other than MODEL_CODE_REPO")
        parts = value.split(".")
        if len(parts) != 2 or not all(part.isidentifier() for part in parts):
            raise ValueError(f"Unsupported auto_map class reference: {value}")
        modules.add(parts[0])
        return value

    localized_auto_map = {key: local_reference(value) for key, value in auto_map.items()}
    if replacements == 0:
        raise ValueError("MODEL_CODE_REPO does not match any auto_map reference")
    code_path = Path(snapshot_download(
        repo_id=code_repo,
        revision=code_revision,
        cache_dir=cache_folder,
        token=hf_token or None,
        local_files_only=False,
        allow_patterns=("*.py",),
    ))
    # Include all Python modules, not only configuration.py and modeling.py:
    # transitive relative imports must also be available without a Hub request.
    for source in code_path.rglob("*.py"):
        destination = model_path / source.relative_to(code_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    _validate_local_modules(model_path, modules)
    config["auto_map"] = localized_auto_map
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"Custom model code materialized: {code_repo}@{code_revision}")


def download_weights(model_name: str, model_revision: str, model_path: Path, hf_token: str) -> None:
    """Download the runtime subset of a model repository straight into model_path."""
    commit, files = inspect_repo(model_name, model_revision, hf_token)
    ignore_patterns = build_ignore_patterns(files)
    print(f"Downloading model: {model_name}@{commit or model_revision or 'main'}")
    if model_path.is_dir():
        shutil.rmtree(model_path)
    snapshot_download(
        repo_id=model_name,
        revision=model_revision or None,
        local_dir=str(model_path),
        token=hf_token or None,
        ignore_patterns=ignore_patterns,
        max_workers=8,
    )
    # huggingface_hub keeps download metadata next to the files; it is dead weight at runtime.
    shutil.rmtree(model_path / ".cache", ignore_errors=True)
    if not list_weight_files(model_path):
        raise ValueError(
            f"no model weights were downloaded for {model_name}: expected a file matching "
            f"{', '.join('*' + suffix for suffix in WEIGHT_SUFFIXES)} under {model_path}"
        )
    print(f"Model files for {model_name}:")
    report_files(model_path)


def main() -> None:
    model_name = os.environ.get("MODEL_NAME", "")
    model_revision = os.environ.get("MODEL_REVISION", "")
    cache_folder = os.environ.get("HF_HOME", "/app/models")
    model_path = Path(os.environ.get("MODEL_PATH", "/app/model"))
    code_repo = os.environ.get("MODEL_CODE_REPO", "")
    code_revision = os.environ.get("MODEL_CODE_REVISION", "")
    hf_token = _load_secret("HF_TOKEN")
    if hf_token:
        os.environ["HF_TOKEN"] = hf_token
        os.environ["HUGGINGFACE_HUB_TOKEN"] = hf_token
        os.environ["HUGGING_FACE_HUB_TOKEN"] = hf_token

    if not model_name:
        print("No MODEL_NAME set, skipping download")
        return

    if model_revision and not COMMIT_PATTERN.fullmatch(model_revision):
        raise ValueError("MODEL_REVISION must be a full 40-character commit hash")
    if (code_repo or code_revision) and (
        not code_repo or not COMMIT_PATTERN.fullmatch(code_revision)
    ):
        raise ValueError("MODEL_CODE_REPO and a full 40-character MODEL_CODE_REVISION commit are required together")
    download_weights(model_name, model_revision, model_path, hf_token)
    if code_repo:
        localize_remote_code(model_path, code_repo, code_revision, cache_folder, hf_token)
    else:
        warn_about_unpinned_remote_code(model_path)
    print(f"Model materialized for runtime: {model_name} -> {model_path}")


if __name__ == "__main__":
    main()
