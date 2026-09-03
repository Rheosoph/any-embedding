"""Materialize a reproducible, offline gte-multilingual-base model tree."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from huggingface_hub import snapshot_download


MODEL_ID = "Alibaba-NLP/gte-multilingual-base"
MODEL_REVISION = "9bbca17d9273fd0d03d5725c7a4b0f6b45142062"
TRUSTED_CODE_ID = "Alibaba-NLP/new-impl"
TRUSTED_CODE_REVISION = "40ced75c3017eb27626c9d4ea981bde21a2662f4"
MODEL_PATH = Path(os.environ.get("MODEL_PATH", "/opt/model"))


def main() -> None:
    MODEL_PATH.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=MODEL_ID,
        revision=MODEL_REVISION,
        local_dir=MODEL_PATH,
        local_dir_use_symlinks=False,
        ignore_patterns=("*.bin", "*.h5", "*.msgpack", "*.onnx"),
    )

    with tempfile.TemporaryDirectory() as temporary_directory:
        code_path = Path(
            snapshot_download(
                repo_id=TRUSTED_CODE_ID,
                revision=TRUSTED_CODE_REVISION,
                local_dir=temporary_directory,
                local_dir_use_symlinks=False,
                allow_patterns=("configuration.py", "modeling.py"),
            )
        )
        for filename in ("configuration.py", "modeling.py"):
            shutil.copy2(code_path / filename, MODEL_PATH / filename)

    config_path = MODEL_PATH / "config.json"
    config = json.loads(config_path.read_text())
    config["auto_map"] = {
        key: value.replace(f"{TRUSTED_CODE_ID}--", "")
        for key, value in config["auto_map"].items()
    }
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    manifest = {
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "trusted_code_id": TRUSTED_CODE_ID,
        "trusted_code_revision": TRUSTED_CODE_REVISION,
    }
    (MODEL_PATH / "microvm-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    print(json.dumps({"event": "model_materialized", **manifest}), flush=True)


if __name__ == "__main__":
    main()
