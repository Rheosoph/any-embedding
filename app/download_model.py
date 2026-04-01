"""Build-time model download."""

import os
import shutil
import sys

from huggingface_hub import snapshot_download


def _load_secret(name: str) -> str:
    """Read a BuildKit secret mounted at /run/secrets/<name>, fall back to env."""
    path = f"/run/secrets/{name}"
    if os.path.isfile(path):
        with open(path) as f:
            return f.read().strip()
    return os.environ.get(name, "")


model_name = os.environ.get("MODEL_NAME", "")
cache_folder = os.environ.get("HF_HOME", "/app/models")
model_path = os.environ.get("MODEL_PATH", "/app/model")
hf_token = _load_secret("HF_TOKEN")
if hf_token:
    os.environ["HF_TOKEN"] = hf_token
    os.environ["HUGGINGFACE_HUB_TOKEN"] = hf_token
    os.environ["HUGGING_FACE_HUB_TOKEN"] = hf_token

if not model_name:
    print("No MODEL_NAME set, skipping download")
    sys.exit(0)

print(f"Downloading model: {model_name}")
snapshot_path = snapshot_download(
    repo_id=model_name,
    cache_dir=cache_folder,
    token=hf_token or None,
    local_files_only=False,
)
if os.path.isdir(model_path):
    shutil.rmtree(model_path)
shutil.copytree(snapshot_path, model_path)
print(f"Model cached: {model_name} -> {snapshot_path}")
print(f"Model materialized for runtime: {model_name} -> {model_path}")
