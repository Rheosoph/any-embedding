# Contributing

Thanks for contributing to any-embedding.

The goal of this repository is straightforward: one OpenAI-compatible embeddings endpoint, many workers, predictable deployment, and minimal operational surprises. Keep changes aligned with that.

## Before You Start

- Check existing issues before opening a new one.
- Keep pull requests focused. Small, reviewable changes move faster.
- If your change affects deployment behavior, update the relevant documentation in `README.md` and `deployment/gcp/`.

## Local Setup

Recommended toolchain:

- Python 3.12
- `uv`
- `mise`
- Docker
- Terraform, if you are changing the GCP deployment path

Install dependencies:

```bash
mise install
mise run install
```

If you do not use `mise`:

```bash
uv pip install -e '.[gateway,worker]'
```

## Common Workflows

Run the full local integration stack:

```bash
./test.sh
```

Start the stack without tearing it down:

```bash
./test.sh --up
```

Run the gateway locally:

```bash
mise run gateway
```

Run a worker locally:

```bash
MODEL_NAME="BAAI/bge-large-en-v1.5" mise run worker
```

If you work on deployment automation, the main entry points are:

```bash
mise run deploy:gcp:plan
mise run deploy:gcp:tf-only
```

## Contribution Guidelines

- Keep `config.yaml` as the source of truth for model definitions.
- Prefer small, explicit configuration over implicit magic.
- Do not commit secrets, `.env`, generated Terraform state, or `terraform.tfvars`.
- Document user-facing behavior changes.
- Add or update tests when behavior changes.
- Preserve the OpenAI-compatible API surface unless the change clearly requires otherwise.

## Pull Request Checklist

Before opening a pull request, make sure you have:

- explained the problem and the change clearly
- run relevant tests locally
- updated docs for changed behavior
- called out any security or deployment impact
- noted follow-up work separately instead of bundling it into the same PR

## Good First Contributions

Good contributions usually look like:

- tightening validation or error handling
- improving docs and deployment instructions
- adding test coverage for gateway or worker behavior
- improving Cloud Run or Docker ergonomics without changing the architecture

Large architectural rewrites are much less likely to be accepted than focused improvements.