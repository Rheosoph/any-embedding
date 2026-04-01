"""Local development entrypoint.

Usage:
    python main.py gateway    # run the API gateway
    python main.py worker     # run a model worker (set MODEL_NAME env var)
"""

import sys

import uvicorn


def main() -> None:
    role = sys.argv[1] if len(sys.argv) > 1 else "gateway"

    if role == "gateway":
        uvicorn.run("app.gateway:app", host="0.0.0.0", port=8080, reload=True)
    elif role == "worker":
        uvicorn.run("app.worker:app", host="0.0.0.0", port=8081, reload=True)
    else:
        print(f"Unknown role: {role}. Use 'gateway' or 'worker'.")
        sys.exit(1)


if __name__ == "__main__":
    main()
