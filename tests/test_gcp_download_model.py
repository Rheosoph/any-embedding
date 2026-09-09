"""Build-time custom-code bundling tests without model downloads."""

from __future__ import annotations

import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.gcp import download_model


CODE_REPO = "Alibaba-NLP/new-impl"
CODE_REVISION = "40ced75c3017eb27626c9d4ea981bde21a2662f4"


class ModelDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.snapshot = self.root / "snapshot"
        self.code = self.root / "code"
        self.target = self.root / "model"
        self.snapshot.mkdir()
        self.code.mkdir()
        self.config = {
            "model_type": "new",
            "torch_dtype": "float16",
            "description": f"Unrelated text containing {CODE_REPO}-- must not change",
            "auto_map": {
                "AutoConfig": f"{CODE_REPO}--configuration.NewConfig",
                "AutoModel": f"{CODE_REPO}--modeling.NewModel",
                "AutoTokenizer": [f"{CODE_REPO}--tokenization.Tokenizer", None],
            },
            "nested": {"auto_map": "leave this untouched"},
        }
        (self.snapshot / "config.json").write_text(json.dumps(self.config))
        (self.snapshot / "model.safetensors").write_bytes(b"unchanged model weight bytes\x00\xff")
        (self.code / "configuration.py").write_text("class NewConfig: pass\n")
        (self.code / "modeling.py").write_text(
            "from .configuration import NewConfig\n"
            "from .layers import Layer\n"
            "class NewModel: pass\n"
        )
        (self.code / "layers.py").write_text("from .helpers import helper\nclass Layer: pass\n")
        (self.code / "helpers.py").write_text("helper = 1\n")
        (self.code / "tokenization.py").write_text("class Tokenizer: pass\n")

    def run_download(self, code_env: dict[str, str] | None = None):
        env = {
            "MODEL_NAME": "Alibaba-NLP/gte-multilingual-base",
            "HF_HOME": str(self.root / "cache"),
            "MODEL_PATH": str(self.target),
            **(code_env or {}),
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(download_model, "_load_secret", return_value=""),
            patch.object(download_model, "snapshot_download", side_effect=[
                str(self.snapshot), str(self.code),
            ]) as snapshot_download,
        ):
            download_model.main()
        return snapshot_download

    def code_env(self):
        return {"MODEL_CODE_REPO": CODE_REPO, "MODEL_CODE_REVISION": CODE_REVISION}

    def test_models_without_opt_in_keep_original_files_and_download_behavior(self) -> None:
        snapshot = self.run_download()

        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(
            (self.target / "config.json").read_bytes(),
            (self.snapshot / "config.json").read_bytes(),
        )
        self.assertEqual(
            (self.target / "model.safetensors").read_bytes(),
            (self.snapshot / "model.safetensors").read_bytes(),
        )
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), ["config.json", "model.safetensors"])

    def test_pinned_code_and_transitive_imports_are_baked_without_weight_or_other_config_changes(self) -> None:
        snapshot = self.run_download(self.code_env())

        self.assertEqual(snapshot.call_count, 2)
        self.assertEqual(snapshot.call_args_list[1].kwargs, {
            "repo_id": CODE_REPO,
            "revision": CODE_REVISION,
            "cache_dir": str(self.root / "cache"),
            "token": None,
            "local_files_only": False,
            "allow_patterns": ("*.py",),
        })
        expected = {**self.config, "auto_map": {
            "AutoConfig": "configuration.NewConfig",
            "AutoModel": "modeling.NewModel",
            "AutoTokenizer": ["tokenization.Tokenizer", None],
        }}
        self.assertEqual(json.loads((self.target / "config.json").read_text()), expected)
        self.assertEqual(json.loads((self.snapshot / "config.json").read_text()), self.config)
        self.assertEqual(
            (self.target / "model.safetensors").read_bytes(),
            (self.snapshot / "model.safetensors").read_bytes(),
        )
        for filename in ("configuration.py", "modeling.py", "layers.py", "helpers.py", "tokenization.py"):
            self.assertEqual((self.target / filename).read_bytes(), (self.code / filename).read_bytes())

    def test_missing_transitive_dependency_fails_build(self) -> None:
        (self.code / "helpers.py").unlink()
        with self.assertRaisesRegex(ValueError, "Missing custom-code dependency: helpers.py"):
            self.run_download(self.code_env())
        self.assertEqual(json.loads((self.target / "config.json").read_text()), self.config)

    def test_package_relative_dependency_fails_instead_of_shipping_incomplete_loader_tree(self) -> None:
        for statement in ("from .package.helpers import helper", "from ..helpers import helper", "from . import helpers"):
            with self.subTest(statement=statement):
                (self.code / "layers.py").write_text(statement + "\nclass Layer: pass\n")
                with self.assertRaisesRegex(ValueError, "Unsupported .*relative import"):
                    self.run_download(self.code_env())

    def test_wrong_code_repo_or_additional_remote_repo_fails_build(self) -> None:
        for repo in ("Other/repo", CODE_REPO):
            with self.subTest(repo=repo):
                config = json.loads((self.snapshot / "config.json").read_text())
                config["auto_map"]["AutoConfig"] = "Other/repo--configuration.NewConfig"
                (self.snapshot / "config.json").write_text(json.dumps(config))
                with self.assertRaisesRegex(ValueError, "external repository other than MODEL_CODE_REPO"):
                    self.run_download({"MODEL_CODE_REPO": repo, "MODEL_CODE_REVISION": CODE_REVISION})

    def test_missing_or_unpinned_code_revision_fails_before_downloading(self) -> None:
        for env in (
            {"MODEL_CODE_REPO": CODE_REPO},
            {"MODEL_CODE_REVISION": CODE_REVISION},
            {"MODEL_CODE_REPO": CODE_REPO, "MODEL_CODE_REVISION": "main"},
        ):
            with (
                self.subTest(env=env),
                patch.dict(os.environ, {"MODEL_NAME": "example/model", **env}, clear=True),
                patch.object(download_model, "_load_secret", return_value=""),
                patch.object(download_model, "snapshot_download") as snapshot,
            ):
                with self.assertRaisesRegex(ValueError, "40-character MODEL_CODE_REVISION"):
                    download_model.main()
                snapshot.assert_not_called()

    def test_no_model_name_skips_download(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(download_model, "_load_secret", return_value=""),
            patch.object(download_model, "snapshot_download") as snapshot,
        ):
            download_model.main()
        snapshot.assert_not_called()

    def test_legacy_entry_point_still_invokes_download(self) -> None:
        with patch.object(download_model, "main") as main:
            runpy.run_module("app.download_model", run_name="__main__")
        main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
