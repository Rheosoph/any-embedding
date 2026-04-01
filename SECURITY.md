# Security Policy

## Supported Versions

Security fixes are applied to the current default branch and the latest published deployment configuration.

If you are running a fork or an older snapshot, reproduce the issue on the current `main` branch before reporting it.

## Reporting a Vulnerability

Do not open public GitHub issues for suspected vulnerabilities.

Use one of these private channels instead:

- Email: `contact@flow-like.com`
- GitHub security advisory reporting, if enabled for this repository

When you report a vulnerability, include:

- a short description of the issue
- affected files, endpoints, or deployment steps
- reproduction steps or a proof of concept
- impact assessment if known
- any suggested mitigation or fix

If the issue involves secrets, infrastructure, or customer-facing deployments, say so explicitly in the report.

## What To Expect

- We will review reports privately.
- We may ask for more details or a smaller reproduction case.
- We will coordinate disclosure after a fix or mitigation is ready.

Please avoid sharing exploit details publicly until we confirm the issue has been addressed.

## Scope

This project includes application code, local container workflows, and the Google Cloud deployment configuration under `deployment/gcp/`.

Examples of in-scope issues include:

- authentication bypass
- secret exposure
- SSRF, RCE, or container breakout paths
- unsafe worker-to-gateway trust boundaries
- Terraform or deployment misconfigurations with real security impact
- unsafe default settings that expose data or infrastructure

Examples that are usually out of scope unless chained to a real impact:

- version bumps without a demonstrated exploit path
- theoretical issues with no practical abuse case
- reports against third-party services not controlled by this repository