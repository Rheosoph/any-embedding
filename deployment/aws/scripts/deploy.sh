#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
deployment_root="$(cd "${script_dir}/.." && pwd)"
terraform_dir="${deployment_root}/terraform"
artifact_dir="${deployment_root}/artifacts"
# shellcheck source=lib/common.sh
source "${script_dir}/lib/common.sh"
command_name="${1:-plan}"

absolute_path() {
  local candidate="$1"
  if [[ "${candidate}" == /* ]]; then
    printf '%s\n' "${candidate}"
  else
    printf '%s\n' "${PWD}/${candidate}"
  fi
}

terraform_path() {
  local candidate="$1"
  if [[ "${candidate}" == /* ]]; then
    printf '%s\n' "${candidate}"
  else
    printf '%s/%s\n' "${terraform_dir}" "${candidate}"
  fi
}

tfvars_file="$(absolute_path "${2:-${terraform_dir}/terraform.tfvars}")"
plan_file="$(absolute_path "${3:-${artifact_dir}/terraform/deployment.tfplan}")"
plan_manifest="${plan_file}.sha256"
applying_manifest="${plan_manifest}.applying"
applied_manifest="${plan_manifest}.applied"

terraform_args=("-input=false")
if [[ -f "${tfvars_file}" ]]; then
  terraform_args+=("-var-file=${tfvars_file}")
elif [[ "${command_name}" == "plan" ]]; then
  echo "No ${tfvars_file}; planning with variable defaults and TF_VAR_* values." >&2
fi

write_plan_manifest() {
  local artifact_list
  local artifact_path
  local artifact_count=0
  local temporary_manifest="${plan_manifest}.tmp"

  if ! command -v jq >/dev/null 2>&1; then
    echo "jq is required to bind a Terraform plan to its Lambda artifacts." >&2
    return 127
  fi

  artifact_list="$(mktemp "${TMPDIR:-/tmp}/any-embedding-plan-artifacts.XXXXXX")"
  if ! terraform -chdir="${terraform_dir}" show -json "${plan_file}" | jq -er '
    [
      .resource_changes[]
      | select(
          .address == "aws_lambda_function.gateway"
          or .address == "aws_lambda_function.lifecycle"
        )
      | .change.after.filename
    ] as $paths
    | if (
        ($paths | length) == 2
        and ($paths | all(type == "string" and length > 0))
        and ($paths | unique | length) == 2
      )
      then $paths | unique[]
      else error("saved plan must contain distinct gateway and lifecycle artifacts")
      end
  ' >"${artifact_list}"; then
    rm -f "${artifact_list}"
    echo "Could not inspect Lambda artifacts in ${plan_file}." >&2
    return 1
  fi

  rm -f "${temporary_manifest}"
  sha256_files "${plan_file}" >"${temporary_manifest}"
  while IFS= read -r artifact_path; do
    artifact_path="$(terraform_path "${artifact_path}")"
    if [[ ! -f "${artifact_path}" ]]; then
      rm -f "${artifact_list}" "${temporary_manifest}"
      echo "Planned Lambda artifact does not exist: ${artifact_path}" >&2
      return 1
    fi
    sha256_files "${artifact_path}" >>"${temporary_manifest}"
    artifact_count=$((artifact_count + 1))
  done <"${artifact_list}"
  rm -f "${artifact_list}"

  if [[ "${artifact_count}" -ne 2 ]]; then
    rm -f "${temporary_manifest}"
    echo "Saved plan did not resolve two distinct Lambda artifacts." >&2
    return 1
  fi
  mv "${temporary_manifest}" "${plan_manifest}"
}

case "${command_name}" in
  package)
    exec "${script_dir}/package-lambdas.sh"
    ;;
  package-microvm)
    exec "${script_dir}/package-microvm.sh"
    ;;
  init)
    exec terraform -chdir="${terraform_dir}" init -lockfile=readonly
    ;;
  fmt)
    exec terraform -chdir="${terraform_dir}" fmt -recursive -check
    ;;
  validate)
    terraform -chdir="${terraform_dir}" init -backend=false -lockfile=readonly
    exec terraform -chdir="${terraform_dir}" validate
    ;;
  plan)
    # Starting a new review cycle invalidates any previously approved plan,
    # even if packaging or planning fails before a replacement is written.
    rm -f "${plan_manifest}" "${applying_manifest}"
    "${script_dir}/package-lambdas.sh"
    terraform -chdir="${terraform_dir}" init -lockfile=readonly
    mkdir -p "$(dirname "${plan_file}")"
    terraform -chdir="${terraform_dir}" plan "${terraform_args[@]}" -out="${plan_file}"
    write_plan_manifest
    echo "Saved reviewed plan to ${plan_file}"
    echo "Bound plan and Lambda artifacts in ${plan_manifest}"
    ;;
  apply)
    if [[ ! -f "${plan_file}" ]]; then
      echo "Refusing to apply without the saved plan ${plan_file}. Run ${0} plan first." >&2
      exit 1
    fi
    if [[ ! -f "${plan_manifest}" ]]; then
      echo "Refusing to apply an unbound or legacy plan without ${plan_manifest}. Run ${0} plan again." >&2
      exit 1
    fi
    if ! sha256_check_manifest "${plan_manifest}"; then
      echo "Refusing to apply because the saved plan or a planned Lambda artifact changed. Run ${0} plan again." >&2
      exit 1
    fi
    mv "${plan_manifest}" "${applying_manifest}"
    echo "Applying the exact saved Terraform plan: ${plan_file}"
    if terraform -chdir="${terraform_dir}" apply -input=false "${plan_file}"; then
      mv "${applying_manifest}" "${applied_manifest}"
      echo "Consumed the saved plan approval; run ${0} plan before another apply."
    else
      echo "Apply did not complete. The approval remains invalidated; review state and run ${0} plan again." >&2
      exit 1
    fi
    ;;
  inventory)
    exec "${script_dir}/export-inventory.sh"
    ;;
  *)
    echo "Usage: $0 {package|package-microvm|init|fmt|validate|plan|apply|inventory} [tfvars-file] [plan-file]" >&2
    exit 2
    ;;
esac
