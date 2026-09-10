#!/usr/bin/env python3
"""Generate docker-compose.yaml from config.yaml.

Run: python generate_compose.py
This reads config.yaml and produces a docker-compose.yaml with one worker
per model + the gateway, all wired together.
"""

import os

import yaml

GCP_GATEWAY_DOCKERFILE = "Dockerfile.gateway"
GCP_WORKER_DOCKERFILES = {
    False: "Dockerfile.worker",
    True: "Dockerfile.worker-gpu",
}

# Build args forwarded verbatim from config.yaml when set (config key -> ARG).
WORKER_BUILD_ARGS = {
    "model_revision": "MODEL_REVISION",
    "sentence_transformers_version": "SENTENCE_TRANSFORMERS_VERSION",
    "transformers_version": "TRANSFORMERS_VERSION",
    "model_code_repo": "MODEL_CODE_REPO",
    "model_code_revision": "MODEL_CODE_REVISION",
}

# Worker env forwarded verbatim from config.yaml when set (config key -> env var).
# Mirrors deployment/gcp/main.tf; unset keys fall back to the worker's own defaults.
WORKER_ENV_PASSTHROUGH = {
    "batch_size": "MODEL_BATCH_SIZE",
    "warmup": "MODEL_WARMUP",
    "dtype": "MODEL_DTYPE",
    "matmul_precision": "MODEL_MATMUL_PRECISION",
    "max_tokens": "MODEL_MAX_SEQ_LENGTH",
    "prefetch": "MODEL_PREFETCH",
    "encode_parallelism": "WORKER_ENCODE_PARALLELISM",
}
DEFAULT_WORKER_CONCURRENCY = 4


def sanitize_env_key(name: str) -> str:
    return "WORKER_URL_" + name.replace("-", "_").replace(".", "_").upper()


def sanitize_service_name(name: str) -> str:
    return "worker-" + name.replace(".", "-").lower()


def env_value(value: object) -> str:
    # Booleans become "true"/"false" as worker.py expects; everything else str().
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def worker_build_args(m: dict) -> dict[str, str]:
    args = {"MODEL_NAME": m["model"]}
    for key, arg in WORKER_BUILD_ARGS.items():
        if m.get(key):
            args[arg] = str(m[key])
    return args


def worker_environment(m: dict) -> dict[str, str]:
    env = {
        "MODEL_NAME": m["model"],
        "MODEL_TYPE": m.get("type", "text"),
    }
    for key, var in WORKER_ENV_PASSTHROUGH.items():
        if m.get(key) is not None:
            env[var] = env_value(m[key])
    env["WORKER_CONCURRENCY"] = str(m.get("concurrency") or DEFAULT_WORKER_CONCURRENCY)
    # The images default to offline=1; Terraform defaults to offline=false, so
    # always emit the resolved value to keep compose and Cloud Run in sync.
    offline = "1" if m.get("offline", False) else "0"
    env["HF_HUB_OFFLINE"] = offline
    env["TRANSFORMERS_OFFLINE"] = offline
    return env


def main() -> None:
    with open("config.yaml") as f:
        config = yaml.safe_load(f)

    models = config.get("models", [])
    hf_token = os.environ.get("HF_TOKEN", "")

    services: dict = {}
    gateway_env: dict = {
        "API_KEY": "test-key",
        "CONFIG_PATH": "config.yaml",
    }
    gateway_depends: list[str] = []

    for i, m in enumerate(models):
        svc_name = sanitize_service_name(m["name"])
        port = 8090 + i
        uses_gpu = m.get("gpu", False)

        svc: dict = {
            "build": {
                "context": ".",
                "dockerfile": GCP_WORKER_DOCKERFILES[uses_gpu],
                "args": worker_build_args(m),
                **({"secrets": ["hf_token"]} if hf_token else {}),
            },
            "environment": worker_environment(m),
            "ports": [f"{port}:8080"],
            "healthcheck": {
                "test": ["CMD", "curl", "-f", "http://localhost:8080/health"],
                "interval": "10s",
                "timeout": "5s",
                "retries": 30,
                "start_period": "120s",
            },
        }

        if uses_gpu:
            svc["deploy"] = {
                "resources": {
                    "reservations": {
                        "devices": [
                            {
                                "driver": "nvidia",
                                "count": 1,
                                "capabilities": ["gpu"],
                            }
                        ]
                    }
                }
            }

        services[svc_name] = svc

        env_key = sanitize_env_key(m["name"])
        gateway_env[env_key] = f"http://{svc_name}:8080"
        gateway_depends.append(svc_name)

    services["gateway"] = {
        "build": {"context": ".", "dockerfile": GCP_GATEWAY_DOCKERFILE},
        "environment": gateway_env,
        "ports": ["8080:8080"],
        "depends_on": {
            dep: {"condition": "service_healthy"} for dep in gateway_depends
        },
    }

    compose = {"services": services}

    if hf_token:
        compose["secrets"] = {
            "hf_token": {"environment": "HF_TOKEN"},
        }

    with open("docker-compose.yaml", "w") as f:
        yaml.dump(compose, f, default_flow_style=False, sort_keys=False)

    print("Generated docker-compose.yaml")
    print(f"  Gateway: http://localhost:8080")
    for i, m in enumerate(models):
        print(f"  Worker {m['name']}: http://localhost:{8090 + i}")


if __name__ == "__main__":
    main()
