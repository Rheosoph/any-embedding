"""GCP image deployment tests; no registry or Cloud Run calls are made."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deployment.gcp import deploy


REGISTRY = "europe-west1-docker.pkg.dev/example/embedding"
DIGEST = "sha256:" + "a" * 64


class ImageDigestTests(unittest.TestCase):
    def test_tag_is_resolved_to_validated_immutable_reference(self) -> None:
        tagged = f"{REGISTRY}/gateway:latest"
        immutable = f"{REGISTRY}/gateway@{DIGEST}"
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, stdout=immutable + "\n",
        )) as run:
            self.assertEqual(deploy.resolve_image_digest(tagged), immutable)

        run.assert_called_once_with(
            ["gcloud", "artifacts", "docker", "images", "describe", tagged,
             "--format=value(image_summary.fully_qualified_digest)"],
            check=True, capture_output=True, text=True,
        )

    def test_migrated_gcr_reference_accepts_canonical_artifact_registry_uri(self) -> None:
        for registry, location in {
            "gcr.io": "us", "us.gcr.io": "us", "eu.gcr.io": "europe", "asia.gcr.io": "asia",
        }.items():
            with self.subTest(registry=registry):
                immutable = f"{location}-docker.pkg.dev/example/{registry}/gateway@{DIGEST}"
                with patch.object(deploy, "run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout=immutable,
                )):
                    self.assertEqual(
                        deploy.resolve_image_digest(f"{registry}/example/gateway:latest"), immutable,
                    )

    def test_empty_malformed_or_unrelated_registry_result_is_rejected(self) -> None:
        for value in (
            "", f"{REGISTRY}/gateway:latest", f"{REGISTRY}/gateway@sha256:abc",
            f"{REGISTRY}/different-image@{DIGEST}",
            f"{REGISTRY}/gateway@{DIGEST}\nother-output",
            f"https://{REGISTRY}/gateway@{DIGEST}",
        ):
            with self.subTest(result=value):
                with patch.object(deploy, "run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout=value,
                )):
                    with self.assertRaisesRegex(ValueError, "valid immutable digest"):
                        deploy.resolve_image_digest(f"{REGISTRY}/gateway:latest")

    def test_explicit_digest_cannot_silently_change(self) -> None:
        with patch.object(deploy, "run", return_value=subprocess.CompletedProcess(
            [], 0, stdout=f"{REGISTRY}/gateway@{DIGEST}",
        )):
            with self.assertRaisesRegex(ValueError, "different digest"):
                deploy.resolve_image_digest(f"{REGISTRY}/gateway@sha256:" + "b" * 64)

    def test_registry_lookup_failure_is_not_ignored(self) -> None:
        with patch.object(deploy, "run", side_effect=subprocess.CalledProcessError(1, "gcloud")):
            with self.assertRaises(subprocess.CalledProcessError):
                deploy.resolve_image_digest(f"{REGISTRY}/gateway:latest")


class DeploymentImageTests(unittest.TestCase):
    def test_full_and_skip_build_pass_digests_as_explicit_terraform_override(self) -> None:
        for skip_build in (False, True):
            with self.subTest(skip_build=skip_build), tempfile.TemporaryDirectory() as directory:
                Path(directory, "config.yaml").write_text(json.dumps({
                    "models": [{
                        "name": "gte.v1", "model": "example/gte",
                        "model_code_repo": "example/code",
                        "model_code_revision": "b" * 40,
                    }],
                }))
                gateway_image = f"{REGISTRY}/gateway:latest"
                worker_registry = f"{REGISTRY}/worker"
                expected = {
                    "gateway_image": f"{REGISTRY}/gateway@{DIGEST}",
                    "worker_images": {"gte.v1": f"{REGISTRY}/worker-gte-v1@{DIGEST}"},
                }
                recorded_files = []

                def run(command, **kwargs):
                    if command[:2] == ["terraform", "plan"]:
                        overrides = [arg for arg in command if arg.startswith("-var-file=")]
                        self.assertEqual(len(overrides), 1)
                        path = overrides[0].split("=", 1)[1]
                        self.assertTrue(path.endswith(".tfvars.json"))
                        self.assertEqual(json.loads(Path(path).read_text()), expected)
                        recorded_files.append(path)
                    return subprocess.CompletedProcess(command, 0)

                argv = ["deploy.py", "--plan"] + (["--skip-build"] if skip_build else [])
                with (
                    patch.object(deploy, "REPO_ROOT", directory),
                    patch.object(deploy, "GCP_DIR", directory),
                    patch.object(deploy, "parse_tfvars", return_value={
                        "gateway_image": gateway_image,
                        "image_registry": worker_registry,
                        "region": "europe-west1",
                        "tfstate_bucket": "example-state",
                    }),
                    patch.object(deploy, "resolve_image_digest", side_effect=[
                        expected["gateway_image"], expected["worker_images"]["gte.v1"],
                    ]) as resolve,
                    patch.object(deploy, "build_and_push", return_value="built") as build,
                    patch.object(deploy, "run", side_effect=run),
                    patch.dict(os.environ, {}, clear=True),
                    patch("sys.argv", argv),
                ):
                    deploy.main()

                self.assertEqual(build.call_count, 0 if skip_build else 2)
                if not skip_build:
                    worker_args = build.call_args_list[1].args[2]
                    self.assertEqual(worker_args["MODEL_CODE_REPO"], "example/code")
                    self.assertEqual(worker_args["MODEL_CODE_REVISION"], "b" * 40)
                self.assertEqual(
                    [call.args[0] for call in resolve.call_args_list],
                    [gateway_image, f"{worker_registry}-gte-v1:latest"],
                )
                self.assertEqual(len(recorded_files), 1)
                self.assertFalse(Path(recorded_files[0]).exists())


if __name__ == "__main__":
    unittest.main()
