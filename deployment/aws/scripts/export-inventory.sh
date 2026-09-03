#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
deployment_root="$(cd "${script_dir}/.." && pwd)"
terraform_dir="${deployment_root}/terraform"
output_path="${1:-${deployment_root}/artifacts/inventories/resource-inventory.json}"

mkdir -p "$(dirname "${output_path}")"
terraform -chdir="${terraform_dir}" output -json resource_inventory | tee "${output_path}"
echo
echo "Wrote ${output_path}"
