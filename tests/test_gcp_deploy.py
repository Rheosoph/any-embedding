"""GCP image deployment tests; no registry, docker, or Cloud Run calls are made."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from deployment.gcp import deploy


REGISTRY = "europe-west1-docker.pkg.dev/example/embedding"
DIGEST = "sha256:" + "a" * 64
WORKER_DIGEST = "sha256:" + "c" * 64
GATEWAY_IMAGE = f"{REGISTRY}/gateway:latest"
WORKER_REGISTRY = f"{REGISTRY}/worker"
BUILDX_INSPECT_NATIVE = "Name: hybrid\nNodes:\nPlatforms: linux/amd64, linux/amd64/v2, linux/386\n"
BUILDX_INSPECT_NO_AMD64 = "Name: default\nNodes:\nPlatforms: linux/arm64*, linux/arm/v7\n"
MODEL = {
    "name": "gte.v1", "model": "example/gte",
    "model_revision": "d" * 40,
    "model_code_repo": "example/code",
    "model_code_revision": "b" * 40,
}


def write_fake_repo(root: str) -> None:
    """Create every file the image hashes read, with distinct contents."""
    files = {
        "app/__init__.py": "", "app/gcp/__init__.py": "",
        "app/gcp/worker.py": "worker", "app/gcp/gateway.py": "gateway",
        "app/gcp/download_model.py": "download",
        ".dockerignore": "**/__pycache__",
        "app/shared/__init__.py": "", "app/shared/models.py": "models",
        "app/shared/__pycache__/models.cpython-312.pyc": "bytecode",
        "pyproject.toml": "[project]", "config.yaml": json.dumps({"models": [MODEL]}),
        "Dockerfile.gateway": "FROM gateway", "Dockerfile.worker": "FROM worker",
        "Dockerfile.worker-gpu": "FROM worker-gpu",
    }
    for relative, content in files.items():
        path = Path(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def patch_repo(stack: ExitStack, root: str) -> None:
    stack.enter_context(patch.object(deploy, "REPO_ROOT", root))
    stack.enter_context(patch.object(deploy, "GCP_DIR", root))
    stack.enter_context(patch.object(deploy, "GCP_GATEWAY_DOCKERFILE", os.path.join(root, "Dockerfile.gateway")))
    stack.enter_context(patch.object(deploy, "GCP_WORKER_DOCKERFILES", {
        False: os.path.join(root, "Dockerfile.worker"),
        True: os.path.join(root, "Dockerfile.worker-gpu"),
    }))


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

    def test_skip_build_falls_back_to_latest_when_no_hashed_tag_exists(self) -> None:
        models = [{"name": "gte.v1", "model": "a/gte"}, {"name": "bge", "model": "a/bge"}]

        def resolve(reference: str) -> str:
            if ":h-" in reference:
                raise subprocess.CalledProcessError(1, "gcloud", stderr="ERROR: (gcloud.artifacts.docker.images.describe) Image not found.\n")
            return reference.replace(":latest", f"@{DIGEST}")

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            write_fake_repo(directory)
            patch_repo(stack, directory)
            specs = deploy.image_specs(GATEWAY_IMAGE, WORKER_REGISTRY, models, "")
            with patch.object(deploy, "resolve_image_digest", side_effect=resolve):
                resolved = deploy.resolve_deployment_images(specs)
        self.assertEqual(resolved, {
            "gateway_image": f"{REGISTRY}/gateway@{DIGEST}",
            "worker_images": {
                "gte.v1": f"{WORKER_REGISTRY}-gte-v1@{DIGEST}",
                "bge": f"{WORKER_REGISTRY}-bge@{DIGEST}",
            },
        })

    def test_skip_build_prefers_the_image_built_for_the_current_inputs(self) -> None:
        models = [{"name": "gte.v1", "model": "a/gte"}]
        other = "sha256:" + "c" * 64

        def resolve(reference: str) -> str:
            repository, _, tag = reference.rpartition(":")
            if tag.startswith("h-"):
                return f"{repository}@{DIGEST}"
            return f"{repository}@{other}"

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            write_fake_repo(directory)
            patch_repo(stack, directory)
            specs = deploy.image_specs(GATEWAY_IMAGE, WORKER_REGISTRY, models, "")
            with patch.object(deploy, "resolve_image_digest", side_effect=resolve) as lookup:
                resolved = deploy.resolve_deployment_images(specs)
        self.assertEqual(resolved, {
            "gateway_image": f"{REGISTRY}/gateway@{DIGEST}",
            "worker_images": {"gte.v1": f"{WORKER_REGISTRY}-gte-v1@{DIGEST}"},
        })
        self.assertTrue(all(":h-" in c.args[0] for c in lookup.call_args_list))

    def test_missing_hashed_tags_are_reported_as_absent_not_errors(self) -> None:
        present = f"{REGISTRY}/gateway:h-000000000000"
        missing = f"{WORKER_REGISTRY}-gte-v1:h-111111111111"

        def resolve(reference: str) -> str:
            if reference == present:
                return f"{REGISTRY}/gateway@{DIGEST}"
            raise subprocess.CalledProcessError(1, "gcloud", stderr="ERROR: (gcloud.artifacts.docker.images.describe) Image not found.\n")

        with patch.object(deploy, "resolve_image_digest", side_effect=resolve):
            self.assertEqual(
                deploy.find_existing_images([present, missing]),
                {present: f"{REGISTRY}/gateway@{DIGEST}"},
            )

    def test_other_registry_errors_fail_instead_of_forcing_a_rebuild(self) -> None:
        denied = subprocess.CalledProcessError(
            1, "gcloud", stderr="ERROR: (gcloud.artifacts.docker.images.describe) PERMISSION_DENIED: token expired",
        )
        with patch.object(deploy, "resolve_image_digest", side_effect=denied):
            with self.assertRaisesRegex(RuntimeError, "PERMISSION_DENIED"):
                deploy.find_existing_images([f"{REGISTRY}/gateway:h-000000000000"])

    def test_malformed_registry_output_for_hashed_tag_still_fails(self) -> None:
        with patch.object(deploy, "resolve_image_digest", side_effect=ValueError("bad")):
            with self.assertRaises(ValueError):
                deploy.find_existing_images([f"{REGISTRY}/gateway:h-000000000000"])


class ImageInputHashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = self.directory.name
        write_fake_repo(self.root)
        self.dockerfile = os.path.join(self.root, "Dockerfile.worker")
        self.files = deploy.image_input_files(deploy.WORKER_IMAGE_INPUTS, self.root)
        self.build_args = {"MODEL_NAME": "example/gte", "MODEL_REVISION": ""}

    def hash(self, **overrides) -> str:
        kwargs = {
            "dockerfile": self.dockerfile, "files": self.files,
            "build_args": self.build_args, "root": self.root,
        }
        kwargs.update(overrides)
        return deploy.image_input_hash(**kwargs)

    def test_shared_files_are_included_and_pycache_is_not(self) -> None:
        self.assertIn("app/shared/models.py", self.files)
        self.assertIn("app/gcp/download_model.py", self.files)
        self.assertNotIn("app/gcp/gateway.py", self.files)
        self.assertFalse([f for f in self.files if "__pycache__" in f])

    def test_hash_is_deterministic_and_12_hex(self) -> None:
        first = self.hash()
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        self.assertEqual(first, self.hash(files=list(reversed(self.files))))
        self.assertEqual(first, self.hash(build_args=dict(reversed(self.build_args.items()))))

    def test_empty_build_args_are_ignored(self) -> None:
        self.assertEqual(self.hash(), self.hash(build_args={"MODEL_NAME": "example/gte"}))
        self.assertEqual(
            self.hash(), self.hash(build_args={**self.build_args, "TRANSFORMERS_VERSION": ""}),
        )

    def test_hash_changes_with_build_arg_file_or_dockerfile(self) -> None:
        baseline = self.hash()
        self.assertNotEqual(baseline, self.hash(build_args={**self.build_args, "MODEL_REVISION": "e" * 40}))
        self.assertNotEqual(baseline, self.hash(build_args={"MODEL_NAME": "example/other"}))

        Path(self.root, "app/shared/models.py").write_text("models v2")
        changed_shared = self.hash()
        self.assertNotEqual(baseline, changed_shared)

        Path(self.root, "Dockerfile.worker").write_text("FROM worker v2")
        changed_dockerfile = self.hash()
        self.assertNotEqual(changed_shared, changed_dockerfile)

        # .dockerignore decides what COPY app/shared actually ships.
        Path(self.root, ".dockerignore").write_text("**/__pycache__\n**/*.md\n")
        self.assertNotEqual(changed_dockerfile, self.hash())

    def test_hash_changes_when_the_scheme_changes(self) -> None:
        baseline = self.hash()
        with patch.object(deploy, "HASH_SCHEME", "999"):
            self.assertNotEqual(baseline, self.hash())

    def test_gateway_spec_uses_gateway_inputs_and_workers_get_model_revision(self) -> None:
        with ExitStack() as stack:
            patch_repo(stack, self.root)
            gateway, worker = deploy.image_specs(GATEWAY_IMAGE, WORKER_REGISTRY, [MODEL], "tok")
        self.assertIn("config.yaml", gateway.files)
        self.assertIn("app/gcp/gateway.py", gateway.files)
        self.assertNotIn("config.yaml", worker.files)
        self.assertEqual(worker.build_args["MODEL_REVISION"], "d" * 40)
        self.assertEqual(worker.build_args["MODEL_CODE_REPO"], "example/code")
        self.assertEqual(worker.secrets, {"HF_TOKEN": "tok"})
        self.assertEqual(worker.primary_tag, f"{WORKER_REGISTRY}-gte-v1:latest")
        self.assertRegex(worker.hashed_tag, rf"^{WORKER_REGISTRY}-gte-v1:h-[0-9a-f]{{12}}$")
        self.assertRegex(gateway.hashed_tag, rf"^{REGISTRY}/gateway:h-[0-9a-f]{{12}}$")


class BuildAndPushTests(unittest.TestCase):
    TAGS = [f"{REGISTRY}/gateway:h-0123456789ab", GATEWAY_IMAGE]

    def run_build(self, metadata: dict | None, builder: str = "", resolve=None) -> tuple[str, list[str]]:
        commands: list[list[str]] = []

        def run(command, **kwargs):
            commands.append(command)
            if metadata is not None:
                metadata_path = command[command.index("--metadata-file") + 1]
                Path(metadata_path).write_text(json.dumps(metadata))
            return subprocess.CompletedProcess(command, 0)

        with (
            patch.object(deploy, "run", side_effect=run),
            patch.object(deploy, "resolve_image_digest", side_effect=resolve or []) as resolve_mock,
        ):
            digest = deploy.build_and_push(
                self.TAGS, "/repo/Dockerfile.gateway", {"A": "1", "EMPTY": ""},
                {"HF_TOKEN": "tok"}, "/repo", builder=builder,
            )
        self.resolve_mock = resolve_mock
        return digest, commands

    def test_both_tags_metadata_file_and_builder_are_passed(self) -> None:
        digest, commands = self.run_build({"containerimage.digest": DIGEST}, builder="hybrid")
        self.assertEqual(digest, f"{REGISTRY}/gateway@{DIGEST}")
        self.assertEqual(len(commands), 1)
        cmd = commands[0]
        self.assertEqual(cmd[:5], ["docker", "buildx", "build", "--builder", "hybrid"])
        self.assertEqual([cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-t"], self.TAGS)
        self.assertIn("--metadata-file", cmd)
        self.assertIn("--push", cmd)
        self.assertEqual(cmd[cmd.index("--platform") + 1], "linux/amd64")
        self.assertEqual(cmd[cmd.index("-f") + 1], "/repo/Dockerfile.gateway")
        self.assertEqual([cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--build-arg"], ["A=1"])
        self.assertEqual(cmd[cmd.index("--secret") + 1], "id=HF_TOKEN,env=HF_TOKEN")
        self.assertEqual(cmd[-1], "/repo")
        self.resolve_mock.assert_not_called()

    def test_no_builder_flag_without_builder(self) -> None:
        _, commands = self.run_build({"containerimage.digest": DIGEST})
        self.assertNotIn("--builder", commands[0])

    def test_missing_metadata_digest_falls_back_to_registry_lookup(self) -> None:
        for metadata in (None, {}, {"image.name": "x"}):
            with self.subTest(metadata=metadata):
                digest, _ = self.run_build(metadata, resolve=[f"{REGISTRY}/gateway@{DIGEST}"])
                self.assertEqual(digest, f"{REGISTRY}/gateway@{DIGEST}")
                self.resolve_mock.assert_called_once_with(self.TAGS[0])

    def test_invalid_metadata_digest_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid immutable digest"):
            self.run_build({"containerimage.digest": "sha256:abc"})

    def test_failed_build_propagates(self) -> None:
        with patch.object(deploy, "run", side_effect=subprocess.CalledProcessError(1, "docker")):
            with self.assertRaises(subprocess.CalledProcessError):
                deploy.build_and_push(self.TAGS, "Dockerfile", {}, {}, "/repo")


class BuilderCheckTests(unittest.TestCase):
    def test_native_amd64_builder_produces_no_warning(self) -> None:
        with (
            patch.object(deploy, "run", return_value=subprocess.CompletedProcess([], 0, stdout=BUILDX_INSPECT_NATIVE)) as run,
            patch("builtins.print") as printed,
        ):
            self.assertTrue(deploy.check_builder_supports_amd64("hybrid"))
        run.assert_called_once_with(
            ["docker", "buildx", "inspect", "--builder", "hybrid"], capture_output=True, text=True,
        )
        self.assertFalse([c for c in printed.call_args_list if "WARNING" in str(c)])

    def test_builder_without_amd64_warns_without_failing(self) -> None:
        with (
            patch.object(deploy, "run", return_value=subprocess.CompletedProcess([], 0, stdout=BUILDX_INSPECT_NO_AMD64)) as run,
            patch("builtins.print") as printed,
        ):
            self.assertFalse(deploy.check_builder_supports_amd64(""))
        run.assert_called_once_with(["docker", "buildx", "inspect"], capture_output=True, text=True)
        self.assertTrue([c for c in printed.call_args_list if "WARNING" in str(c)])

    def test_inspect_failure_warns_without_failing(self) -> None:
        with (
            patch.object(deploy, "run", side_effect=subprocess.CalledProcessError(1, "docker")),
            patch("builtins.print") as printed,
        ):
            self.assertFalse(deploy.check_builder_supports_amd64("missing"))
        self.assertTrue([c for c in printed.call_args_list if "WARNING" in str(c)])


@dataclass
class MainRun:
    """Everything observed while running deploy.main() against a fake repo."""

    commands: list[list[str]] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    builds: dict[str, dict] = field(default_factory=dict)
    tfvars: dict | None = None
    tfvars_path: str = ""
    plugin_cache_exists: bool = False


class WorkerImageEnvironmentTests(unittest.TestCase):
    """Guard image-level settings that only fail once a model is served."""

    DOCKERFILES = ("Dockerfile.worker", "Dockerfile.worker-gpu")

    def dockerfile(self, name: str) -> str:
        return Path(deploy.REPO_ROOT, name).read_text()

    def test_worker_images_point_caches_at_a_writable_directory(self) -> None:
        # The app user is a system user whose passwd home is /nonexistent, so
        # torch's bundled triton could not create its JIT cache and every GPU
        # request failed with PermissionError.
        for name in self.DOCKERFILES:
            text = self.dockerfile(name)
            for variable in ("HOME", "TRITON_CACHE_DIR", "TORCHINDUCTOR_CACHE_DIR"):
                with self.subTest(dockerfile=name, variable=variable):
                    match = re.search(rf"^\s*{variable}=(\S+)", text, re.M)
                    self.assertIsNotNone(match, f"{name} must set {variable}")
                    self.assertTrue(
                        match.group(1).startswith("/tmp/"),
                        f"{name} must point {variable} at the writable tmpfs",
                    )


class DeployMainTests(unittest.TestCase):
    """Run main() end to end with docker, gcloud, and terraform replaced by fakes."""

    GATEWAY_DIGEST = f"{REGISTRY}/gateway@{DIGEST}"
    WORKER_DIGEST_REF = f"{WORKER_REGISTRY}-gte-v1@{WORKER_DIGEST}"

    def run_main(
        self,
        argv: list[str],
        present: set[str] | None = None,
        inspect_output: str = BUILDX_INSPECT_NATIVE,
        state: dict | None = None,
    ) -> MainRun:
        """Run main(); `present` names image kinds ("gateway", "gte.v1") whose hashed tag exists."""
        present = present or set()
        observed = MainRun()
        main_thread = threading.current_thread()

        def run(command, **kwargs):
            observed.commands.append(command)
            if command[:3] == ["docker", "buildx", "inspect"]:
                return subprocess.CompletedProcess(command, 0, stdout=inspect_output)
            if command[:2] in (["terraform", "plan"], ["terraform", "apply"]):
                overrides = [arg for arg in command if arg.startswith("-var-file=")]
                self.assertEqual(len(overrides), 1)
                observed.tfvars_path = overrides[0].split("=", 1)[1]
                self.assertTrue(observed.tfvars_path.endswith(".tfvars.json"))
                observed.tfvars = json.loads(Path(observed.tfvars_path).read_text())
            return subprocess.CompletedProcess(command, 0)

        def resolve(reference: str) -> str:
            observed.resolved.append(reference)
            repository, _, tag = reference.rpartition(":")
            kind = "gateway" if repository.endswith("/gateway") else "gte.v1"
            if tag == "latest" or kind in present:
                digest = DIGEST if kind == "gateway" else WORKER_DIGEST
                return f"{repository}@{digest}"
            raise subprocess.CalledProcessError(1, "gcloud", stderr="ERROR: (gcloud.artifacts.docker.images.describe) Image not found.\n")

        def build(tags, dockerfile, build_args, secrets, context, builder=""):
            kind = "gateway" if tags[0].startswith(f"{REGISTRY}/gateway:") else "gte.v1"
            observed.builds[kind] = {
                "tags": tags, "dockerfile": dockerfile, "build_args": build_args,
                "secrets": secrets, "context": context, "builder": builder,
                "source_date_epoch": os.environ.get("SOURCE_DATE_EPOCH"),
                "in_pool": threading.current_thread() is not main_thread,
            }
            return self.GATEWAY_DIGEST if kind == "gateway" else self.WORKER_DIGEST_REF

        def subprocess_run(command, **kwargs):
            self.assertEqual(command, ["terraform", "state", "pull"])
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(state or {"resources": []}))

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            write_fake_repo(directory)
            patch_repo(stack, directory)
            stack.enter_context(patch.object(deploy, "parse_tfvars", return_value={
                "gateway_image": GATEWAY_IMAGE,
                "image_registry": WORKER_REGISTRY,
                "region": "europe-west1",
                "tfstate_bucket": "example-state",
            }))
            stack.enter_context(patch.object(deploy, "resolve_image_digest", side_effect=resolve))
            stack.enter_context(patch.object(deploy, "build_and_push", side_effect=build))
            stack.enter_context(patch.object(deploy, "run", side_effect=run))
            stack.enter_context(patch.object(subprocess, "run", side_effect=subprocess_run))
            stack.enter_context(patch.dict(os.environ, {
                "TF_PLUGIN_CACHE_DIR": os.path.join(directory, "plugin-cache"),
            }, clear=True))
            stack.enter_context(patch("sys.argv", ["deploy.py", *argv]))
            deploy.main()
            observed.plugin_cache_exists = os.path.isdir(os.path.join(directory, "plugin-cache"))
        return observed

    def expected_tfvars(self) -> dict:
        return {
            "gateway_image": self.GATEWAY_DIGEST,
            "worker_images": {"gte.v1": self.WORKER_DIGEST_REF},
        }

    def terraform_commands(self, observed: MainRun) -> dict[str, list[str]]:
        return {cmd[1]: cmd for cmd in observed.commands if cmd[0] == "terraform"}

    def test_full_build_pushes_both_tags_and_pins_built_digests(self) -> None:
        observed = self.run_main(["--plan"])

        self.assertEqual(set(observed.builds), {"gateway", "gte.v1"})
        gateway, worker = observed.builds["gateway"], observed.builds["gte.v1"]
        self.assertRegex(gateway["tags"][0], rf"^{REGISTRY}/gateway:h-[0-9a-f]{{12}}$")
        self.assertEqual(gateway["tags"][1], GATEWAY_IMAGE)
        self.assertRegex(worker["tags"][0], rf"^{WORKER_REGISTRY}-gte-v1:h-[0-9a-f]{{12}}$")
        self.assertEqual(worker["tags"][1], f"{WORKER_REGISTRY}-gte-v1:latest")
        self.assertEqual(worker["build_args"]["MODEL_NAME"], "example/gte")
        self.assertEqual(worker["build_args"]["MODEL_REVISION"], "d" * 40)
        self.assertEqual(worker["build_args"]["MODEL_CODE_REPO"], "example/code")
        self.assertEqual(worker["build_args"]["MODEL_CODE_REVISION"], "b" * 40)
        self.assertTrue(worker["dockerfile"].endswith("Dockerfile.worker"))
        self.assertTrue(gateway["dockerfile"].endswith("Dockerfile.gateway"))
        # Gateway and workers share one pool; SOURCE_DATE_EPOCH keeps rebuilds digest-stable.
        self.assertTrue(gateway["in_pool"] and worker["in_pool"])
        self.assertEqual(gateway["source_date_epoch"], "0")
        self.assertEqual(worker["source_date_epoch"], "0")
        # Only the hashed tags were probed, never :latest.
        self.assertEqual(sorted(observed.resolved), sorted([gateway["tags"][0], worker["tags"][0]]))

        self.assertEqual(observed.tfvars, self.expected_tfvars())
        self.assertFalse(Path(observed.tfvars_path).exists())
        self.assertIn(["gcloud", "auth", "configure-docker", "europe-west1-docker.pkg.dev", "--quiet"], observed.commands)

    def test_unchanged_images_are_skipped_and_their_digest_reused(self) -> None:
        observed = self.run_main(["--plan"], present={"gateway"})
        self.assertEqual(set(observed.builds), {"gte.v1"})
        self.assertEqual(observed.tfvars, self.expected_tfvars())

        observed = self.run_main(["--plan"], present={"gateway", "gte.v1"})
        self.assertEqual(observed.builds, {})
        self.assertEqual(observed.tfvars, self.expected_tfvars())

    def test_force_build_rebuilds_without_probing_the_registry(self) -> None:
        observed = self.run_main(["--plan", "--force-build"], present={"gateway", "gte.v1"})
        self.assertEqual(set(observed.builds), {"gateway", "gte.v1"})
        self.assertEqual(observed.resolved, [])
        self.assertEqual(observed.tfvars, self.expected_tfvars())

    def test_skip_build_probes_hashed_tags_then_falls_back_to_latest(self) -> None:
        observed = self.run_main(["--plan", "--skip-build"])
        self.assertEqual(observed.builds, {})
        hashed = [r for r in observed.resolved if ":h-" in r]
        latest = [r for r in observed.resolved if r.endswith(":latest")]
        self.assertEqual(len(hashed), 2)
        self.assertEqual(sorted(latest), sorted([GATEWAY_IMAGE, f"{WORKER_REGISTRY}-gte-v1:latest"]))
        self.assertEqual(observed.tfvars, self.expected_tfvars())
        self.assertFalse([c for c in observed.commands if c[0] in ("docker", "gcloud")])

    def test_skip_build_uses_hashed_images_when_present(self) -> None:
        observed = self.run_main(["--plan", "--skip-build"], present={"gateway", "gte.v1"})
        self.assertEqual(observed.builds, {})
        self.assertFalse([r for r in observed.resolved if r.endswith(":latest")])
        self.assertEqual(observed.tfvars, self.expected_tfvars())

    def test_builder_is_forwarded_to_inspect_and_builds(self) -> None:
        observed = self.run_main(["--plan", "--builder", "hybrid"])
        self.assertIn(["docker", "buildx", "inspect", "--builder", "hybrid"], observed.commands)
        self.assertEqual({b["builder"] for b in observed.builds.values()}, {"hybrid"})

        observed = self.run_main(["--plan"])
        self.assertIn(["docker", "buildx", "inspect"], observed.commands)
        self.assertEqual({b["builder"] for b in observed.builds.values()}, {""})

    def test_builder_defaults_to_buildx_builder_env(self) -> None:
        with patch.dict(os.environ, {"BUILDX_BUILDER": "from-env"}):
            args = deploy.parse_args(["--plan"])
        self.assertEqual(args.builder, "from-env")
        self.assertEqual(args.parallel, 4)
        self.assertEqual(args.tf_parallelism, 1)
        self.assertFalse(args.force_build)

    def test_builder_without_amd64_only_warns(self) -> None:
        with patch("builtins.print") as printed:
            observed = self.run_main(["--plan"], inspect_output=BUILDX_INSPECT_NO_AMD64)
        self.assertEqual(set(observed.builds), {"gateway", "gte.v1"})
        self.assertTrue([c for c in printed.call_args_list if "WARNING" in str(c)])

    def test_terraform_init_plan_and_apply_flags(self) -> None:
        observed = self.run_main(["--plan"])
        tf = self.terraform_commands(observed)
        self.assertEqual(tf["init"], ["terraform", "init", "-input=false", "-backend-config=bucket=example-state"])
        self.assertEqual(tf["plan"][:3], ["terraform", "plan", "-input=false"])
        self.assertNotIn("apply", tf)
        self.assertTrue(observed.plugin_cache_exists)

        observed = self.run_main([])
        tf = self.terraform_commands(observed)
        self.assertNotIn("-upgrade", tf["init"])
        self.assertEqual(tf["apply"][:5], ["terraform", "apply", "-input=false", "-parallelism=1", "-auto-approve"])
        self.assertEqual(observed.tfvars, self.expected_tfvars())

        observed = self.run_main(["--tf-parallelism", "4"])
        self.assertIn("-parallelism=4", self.terraform_commands(observed)["apply"])


if __name__ == "__main__":
    unittest.main()
