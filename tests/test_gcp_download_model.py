"""Build-time model download and custom-code bundling tests without network access."""

from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.gcp import download_model


CODE_REPO = "Alibaba-NLP/new-impl"
CODE_REVISION = "40ced75c3017eb27626c9d4ea981bde21a2662f4"
MODEL_REVISION = "a5beb1e3e68b9ab74eb54cfd186867f64f240e1a"
REPO_FILES = ["README.md", "config.json", "model.safetensors", "pytorch_model.bin", "onnx/model.onnx"]

BGE_FILES = [
    ".gitattributes", "1_Pooling/config.json", "README.md", "config.json", "config_sentence_transformers.json",
    "model.safetensors", "modules.json", "onnx/config.json", "onnx/model.onnx", "onnx/tokenizer.json",
    "pytorch_model.bin", "sentence_bert_config.json", "special_tokens_map.json", "tokenizer.json",
    "tokenizer_config.json", "vocab.txt",
]
E5_FILES = [
    ".gitattributes", "README.md", "config.json", "model.safetensors", "onnx/model.onnx",
    "onnx/model_O1.onnx", "openvino/openvino_model.bin", "openvino/openvino_model.xml",
    "pytorch_model.bin", "tokenizer.json", "tokenizer_config.json", "vocab.txt",
]
EMBEDDINGGEMMA_FILES = [
    "1_Pooling/config.json", "2_Dense/config.json", "2_Dense/model.safetensors", "3_Dense/config.json",
    "3_Dense/model.safetensors", "4_Normalize/README.md", "README.md", "config.json",
    "config_sentence_transformers.json", "model.safetensors", "modules.json", "tokenizer.json",
    "tokenizer.model", "tokenizer_config.json",
]
JINA_FILES = [
    ".gitattributes", "README.md", "config.json", "model.safetensors", "onnx/model.onnx",
    "onnx/model_fp16.onnx", "pytorch_model.bin", "tokenizer.json", "tokenizer_config.json",
]


class IgnorePatternTests(unittest.TestCase):
    def extra_patterns(self, files: list[str]) -> list[str]:
        patterns = download_model.build_ignore_patterns(files)
        self.assertEqual(tuple(patterns[: len(download_model.ALWAYS_IGNORE)]), download_model.ALWAYS_IGNORE)
        return patterns[len(download_model.ALWAYS_IGNORE):]

    def test_duplicate_pickle_weights_next_to_safetensors_are_skipped(self) -> None:
        self.assertEqual(self.extra_patterns(BGE_FILES), ["pytorch_model.bin"])
        self.assertEqual(self.extra_patterns(E5_FILES), ["pytorch_model.bin"])
        self.assertEqual(self.extra_patterns(JINA_FILES), ["pytorch_model.bin"])

    def test_sub_module_weights_are_kept(self) -> None:
        self.assertEqual(self.extra_patterns(EMBEDDINGGEMMA_FILES), [])
        # A dense sub-module that only ships pickle weights must keep them even
        # though the root directory has safetensors.
        files = ["model.safetensors", "pytorch_model.bin", "2_Dense/pytorch_model.bin", "2_Dense/config.json"]
        self.assertEqual(self.extra_patterns(files), ["pytorch_model.bin"])

    def test_pickle_only_repositories_keep_their_weights(self) -> None:
        self.assertEqual(self.extra_patterns(["config.json", "pytorch_model.bin", "onnx/model.onnx"]), [])

    def test_every_pickle_suffix_is_covered_per_directory(self) -> None:
        files = [
            "model.safetensors", "a.bin", "b.pt", "c.pth", "d.ckpt", "training_args.bin",
            "sub/model.safetensors", "sub/x.pt", "other/y.pt",
        ]
        self.assertEqual(
            self.extra_patterns(files), ["a.bin", "b.pt", "c.pth", "d.ckpt", "training_args.bin", "sub/x.pt"]
        )

    def test_inspect_repo_resolves_commit_and_files_with_one_request(self) -> None:
        info = SimpleNamespace(sha=MODEL_REVISION, siblings=[
            SimpleNamespace(rfilename="model.safetensors"), SimpleNamespace(rfilename="config.json"),
        ])
        with patch.object(download_model, "HfApi") as api:
            api.return_value.model_info.return_value = info
            self.assertEqual(
                download_model.inspect_repo("example/model", MODEL_REVISION, "secret"),
                (MODEL_REVISION, ["config.json", "model.safetensors"]),
            )
        api.assert_called_once_with(token="secret")
        api.return_value.model_info.assert_called_once_with("example/model", revision=MODEL_REVISION)

        with patch.object(download_model, "HfApi") as api:
            api.return_value.model_info.return_value = info
            download_model.inspect_repo("example/model", "", "")
        api.assert_called_once_with(token=None)
        api.return_value.model_info.assert_called_once_with("example/model", revision=None)


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
        self.repo_files = list(REPO_FILES)

    def fake_snapshot_download(self, **kwargs) -> str:
        """Materialize the fake repository like huggingface_hub's local_dir mode."""
        if "local_dir" not in kwargs:
            return str(self.code)
        local_dir = Path(kwargs["local_dir"])
        shutil.copytree(self.snapshot, local_dir, dirs_exist_ok=True)
        (local_dir / ".cache" / "huggingface").mkdir(parents=True)
        (local_dir / ".cache" / "huggingface" / "download.metadata").write_text("x")
        return str(local_dir)

    def run_download(self, code_env: dict[str, str] | None = None):
        env = {
            "MODEL_NAME": "Alibaba-NLP/gte-multilingual-base",
            "HF_HOME": str(self.root / "cache"),
            "MODEL_PATH": str(self.target),
            **(code_env or {}),
        }
        self.output = io.StringIO()
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(download_model, "_load_secret", return_value=""),
            patch.object(download_model, "inspect_repo", return_value=(MODEL_REVISION, self.repo_files)) as inspect,
            patch.object(download_model, "snapshot_download", side_effect=self.fake_snapshot_download) as snapshot,
            contextlib.redirect_stdout(self.output),
        ):
            download_model.main()
        self.inspect_repo = inspect
        return snapshot

    def code_env(self):
        return {"MODEL_CODE_REPO": CODE_REPO, "MODEL_CODE_REVISION": CODE_REVISION}

    def test_models_without_opt_in_keep_original_files_and_download_behavior(self) -> None:
        snapshot = self.run_download()

        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(snapshot.call_args.kwargs, {
            "repo_id": "Alibaba-NLP/gte-multilingual-base",
            "revision": None,
            "local_dir": str(self.target),
            "token": None,
            "ignore_patterns": download_model.build_ignore_patterns(REPO_FILES),
            "max_workers": 8,
        })
        self.inspect_repo.assert_called_once_with("Alibaba-NLP/gte-multilingual-base", "", "")
        self.assertEqual(
            (self.target / "config.json").read_bytes(),
            (self.snapshot / "config.json").read_bytes(),
        )
        self.assertEqual(
            (self.target / "model.safetensors").read_bytes(),
            (self.snapshot / "model.safetensors").read_bytes(),
        )
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), ["config.json", "model.safetensors"])

    def test_stale_model_directory_is_replaced_and_hub_metadata_removed(self) -> None:
        self.target.mkdir()
        (self.target / "stale.bin").write_bytes(b"old")
        self.run_download()
        self.assertFalse((self.target / "stale.bin").exists())
        self.assertFalse((self.target / ".cache").exists())

    def test_pinned_model_revision_is_passed_through_and_printed(self) -> None:
        snapshot = self.run_download({"MODEL_REVISION": MODEL_REVISION})
        self.assertEqual(snapshot.call_args.kwargs["revision"], MODEL_REVISION)
        self.inspect_repo.assert_called_once_with("Alibaba-NLP/gte-multilingual-base", MODEL_REVISION, "")
        self.assertIn(f"Alibaba-NLP/gte-multilingual-base@{MODEL_REVISION}", self.output.getvalue())

    def test_unpinned_model_revision_fails_before_downloading(self) -> None:
        for revision in ("main", MODEL_REVISION[:7], MODEL_REVISION.upper()):
            with (
                self.subTest(revision=revision),
                patch.dict(os.environ, {"MODEL_NAME": "example/model", "MODEL_REVISION": revision}, clear=True),
                patch.object(download_model, "_load_secret", return_value=""),
                patch.object(download_model, "inspect_repo") as inspect,
                patch.object(download_model, "snapshot_download") as snapshot,
            ):
                with self.assertRaisesRegex(ValueError, "MODEL_REVISION must be a full 40-character commit"):
                    download_model.main()
                inspect.assert_not_called()
                snapshot.assert_not_called()

    def test_download_without_weights_fails_build(self) -> None:
        (self.snapshot / "model.safetensors").unlink()
        with self.assertRaisesRegex(ValueError, "no model weights were downloaded"):
            self.run_download()

    def test_weights_in_sub_modules_satisfy_the_weight_check(self) -> None:
        (self.snapshot / "model.safetensors").unlink()
        (self.snapshot / "2_Dense").mkdir()
        (self.snapshot / "2_Dense" / "pytorch_model.bin").write_bytes(b"dense")
        self.run_download()
        self.assertIn("2_Dense/pytorch_model.bin  0.0 MiB", self.output.getvalue())
        self.assertIn("Model files total: 0.0 MiB", self.output.getvalue())

    def test_unpinned_external_code_prints_warning_instead_of_failing(self) -> None:
        self.run_download()
        output = self.output.getvalue()
        self.assertIn("WARNING", output)
        self.assertIn(f"{CODE_REPO}--modeling.NewModel", output)
        self.assertIn("model_code_repo", output)
        self.assertEqual(json.loads((self.target / "config.json").read_text()), self.config)

    def test_local_auto_map_or_missing_config_prints_no_warning(self) -> None:
        for config in ({"auto_map": {"AutoModel": "modeling.NewModel"}}, {"model_type": "bert"}, None):
            with self.subTest(config=config):
                if config is None:
                    (self.snapshot / "config.json").unlink()
                else:
                    (self.snapshot / "config.json").write_text(json.dumps(config))
                self.run_download()
                self.assertNotIn("WARNING", self.output.getvalue())

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
        self.assertNotIn("WARNING", self.output.getvalue())

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
                patch.object(download_model, "inspect_repo") as inspect,
                patch.object(download_model, "snapshot_download") as snapshot,
            ):
                with self.assertRaisesRegex(ValueError, "40-character MODEL_CODE_REVISION"):
                    download_model.main()
                inspect.assert_not_called()
                snapshot.assert_not_called()

    def test_no_model_name_skips_download(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(download_model, "_load_secret", return_value=""),
            patch.object(download_model, "inspect_repo") as inspect,
            patch.object(download_model, "snapshot_download") as snapshot,
        ):
            download_model.main()
        inspect.assert_not_called()
        snapshot.assert_not_called()

    def test_legacy_entry_point_still_invokes_download(self) -> None:
        with patch.object(download_model, "main") as main:
            runpy.run_module("app.download_model", run_name="__main__")
        main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
