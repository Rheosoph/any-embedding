"""Build-time model download."""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
from pathlib import Path

from huggingface_hub import snapshot_download


def _load_secret(name: str) -> str:
    """Read a BuildKit secret mounted at /run/secrets/<name>, fall back to env."""
    path = f"/run/secrets/{name}"
    if os.path.isfile(path):
        with open(path) as f:
            return f.read().strip()
    return os.environ.get(name, "")


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
    if not code_repo or not re.fullmatch(r"[0-9a-f]{40}", code_revision):
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


def main() -> None:
    model_name = os.environ.get("MODEL_NAME", "")
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

    if (code_repo or code_revision) and (
        not code_repo or not re.fullmatch(r"[0-9a-f]{40}", code_revision)
    ):
        raise ValueError("MODEL_CODE_REPO and a full 40-character MODEL_CODE_REVISION commit are required together")
    print(f"Downloading model: {model_name}")
    snapshot_path = snapshot_download(
        repo_id=model_name,
        cache_dir=cache_folder,
        token=hf_token or None,
        local_files_only=False,
    )
    if model_path.is_dir():
        shutil.rmtree(model_path)
    shutil.copytree(snapshot_path, model_path)
    if code_repo:
        localize_remote_code(model_path, code_repo, code_revision, cache_folder, hf_token)
    print(f"Model cached: {model_name} -> {snapshot_path}")
    print(f"Model materialized for runtime: {model_name} -> {model_path}")


if __name__ == "__main__":
    main()
