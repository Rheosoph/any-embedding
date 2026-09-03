#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${script_dir}/lib/common.sh"
deployment_root="$(cd "${script_dir}/.." && pwd)"
repository_root="$(cd "${deployment_root}/../.." && pwd)"
source_dir="${1:-${repository_root}/app/aws/microvm}"
artifact="${2:-${deployment_root}/artifacts/microvm/source.zip}"

if [[ "${source_dir}" != /* ]]; then
  source_dir="${PWD}/${source_dir}"
fi
if [[ "${artifact}" != /* ]]; then
  artifact="${PWD}/${artifact}"
fi

required_files=(Dockerfile download_model.py pyproject.toml server.py uv.lock)
for required_file in "${required_files[@]}"; do
  if [[ ! -f "${source_dir}/${required_file}" ]]; then
    echo "Expected ${required_file} under ${source_dir}." >&2
    exit 1
  fi
done

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to validate the MicroVM dependency lock." >&2
  exit 1
fi
uv lock --project "${source_dir}" --check

staging_dir="$(mktemp -d "${TMPDIR:-/tmp}/any-embedding-microvm.XXXXXX")"
cleanup() {
  rm -rf "${staging_dir}"
}
trap cleanup EXIT

mkdir -p "$(dirname "${artifact}")"
for required_file in "${required_files[@]}"; do
  cp "${source_dir}/${required_file}" "${staging_dir}/${required_file}"
  chmod 0644 "${staging_dir}/${required_file}"
done

temporary_artifact="${artifact}.tmp"
rm -f "${temporary_artifact}"
(
  cd "${staging_dir}"
  export TZ=UTC
  touch -t 198001010000 "${required_files[@]}"
  zip -q -X "${temporary_artifact}" "${required_files[@]}"
)
mv "${temporary_artifact}" "${artifact}"

echo "Built ${artifact}"
sha256_files "${artifact}"
