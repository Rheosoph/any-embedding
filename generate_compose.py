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


def sanitize_env_key(name: str) -> str:
    return "WORKER_URL_" + name.replace("-", "_").replace(".", "_").upper()


def sanitize_service_name(name: str) -> str:
    return "worker-" + name.replace(".", "-").lower()


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
        model_type = m.get("type", "text")
        uses_gpu = m.get("gpu", False)

        svc: dict = {
            "build": {
                "context": ".",
                "dockerfile": GCP_WORKER_DOCKERFILES[uses_gpu],
                "args": {
                    "MODEL_NAME": m["model"],
                    **({"SENTENCE_TRANSFORMERS_VERSION": m["sentence_transformers_version"]} if m.get("sentence_transformers_version") else {}),
                    **({"TRANSFORMERS_VERSION": m["transformers_version"]} if m.get("transformers_version") else {}),
                },
                **({"secrets": ["hf_token"]} if hf_token else {}),
            },
            "environment": {
                "MODEL_NAME": m["model"],
                "MODEL_TYPE": model_type,
            },
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
