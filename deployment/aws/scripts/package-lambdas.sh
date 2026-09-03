#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${script_dir}/lib/common.sh"
deployment_root="$(cd "${script_dir}/.." && pwd)"
repository_root="$(cd "${deployment_root}/../.." && pwd)"
source_dir="${1:-${repository_root}/app/aws}"
gateway_artifact="${2:-${deployment_root}/terraform/build/gateway.zip}"
lifecycle_artifact="${3:-${deployment_root}/terraform/build/lifecycle.zip}"

absolute_path() {
  local candidate="$1"
  if [[ "${candidate}" == /* ]]; then
    printf '%s\n' "${candidate}"
  else
    printf '%s\n' "${PWD}/${candidate}"
  fi
}

source_dir="$(absolute_path "${source_dir}")"
gateway_artifact="$(absolute_path "${gateway_artifact}")"
lifecycle_artifact="$(absolute_path "${lifecycle_artifact}")"

if [[ "${gateway_artifact}" == "${lifecycle_artifact}" ]]; then
  echo "Gateway and lifecycle artifacts must use different output paths." >&2
  exit 2
fi

required_files=(
  "package.json"
  "package-lock.json"
  "tsconfig.json"
  "gateway/index.ts"
  "lifecycle/index.ts"
)
for required_file in "${required_files[@]}"; do
  if [[ ! -f "${source_dir}/${required_file}" ]]; then
    echo "Expected ${source_dir}/${required_file}." >&2
    exit 1
  fi
done

# Build from the committed lock, then require the same checks used in CI before
# creating deployment artifacts. esbuild bundles runtime dependencies, so the
# archives contain only one deterministic JavaScript entry point apiece.
npm --prefix "${source_dir}" ci --ignore-scripts
npm --prefix "${source_dir}" run check

for component in gateway lifecycle; do
  if [[ ! -f "${source_dir}/dist/${component}/index.js" ]]; then
    echo "Build did not produce dist/${component}/index.js." >&2
    exit 1
  fi
done

staging_dir="$(mktemp -d "${TMPDIR:-/tmp}/any-embedding-lambda.XXXXXX")"
cleanup() {
  rm -rf "${staging_dir}"
}
trap cleanup EXIT

build_artifact() {
  local component="$1"
  local artifact="$2"
  local temporary_artifact="${artifact}.tmp"

  mkdir -p "${staging_dir}/${component}" "$(dirname "${artifact}")"
  cp "${source_dir}/dist/${component}/index.js" "${staging_dir}/${component}/index.js"
  chmod 0644 "${staging_dir}/${component}/index.js"

  # Stable timestamps plus zip -X make an unchanged build hash-identical and
  # prevent Terraform from publishing needless Lambda revisions.
  rm -f "${temporary_artifact}"
  (
    cd "${staging_dir}"
    export TZ=UTC
    touch -t 198001010000 "${component}/index.js"
    LC_ALL=C zip -q -X "${temporary_artifact}" "${component}/index.js"
  )
  mv "${temporary_artifact}" "${artifact}"

  echo "Built ${artifact}"
  sha256_files "${artifact}"
}

build_artifact "gateway" "${gateway_artifact}"
build_artifact "lifecycle" "${lifecycle_artifact}"
