#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<EOF
Usage: $0 [--region REGION] [--account-id ID] [--build-role-name NAME] [--mode create|update] s3://bucket/key release-id
EOF
}

region="${AWS_REGION:-eu-west-1}"
account_id=""
build_role_name=""
mode="create"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)
      region="${2:?--region requires a value}"
      shift 2
      ;;
    --account-id)
      account_id="${2:?--account-id requires a value}"
      shift 2
      ;;
    --build-role-name)
      build_role_name="${2:?--build-role-name requires a value}"
      shift 2
      ;;
    --mode)
      mode="${2:?--mode requires create or update}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --*)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
    *)
      break
      ;;
  esac
done

if [[ $# -ne 2 ]]; then
  usage
  exit 2
fi
artifact_uri="$1"
release_id="$2"
if [[ ! "${release_id}" =~ ^[A-Za-z0-9_-]{1,32}$ ]]; then
  echo "release-id must be 1-32 letters, numbers, underscores, or hyphens." >&2
  exit 2
fi
if [[ "${mode}" != "create" && "${mode}" != "update" ]]; then
  echo "--mode must be create or update." >&2
  exit 2
fi
if [[ -z "${account_id}" ]]; then
  account_id="$(aws sts get-caller-identity --query Account --output text)"
fi
if [[ -z "${build_role_name}" ]]; then
  build_role_name="any-embedding-lambda-microvms-build-role-${region}"
fi
base_image_arn="arn:aws:lambda:${region}:aws:microvm-image:al2023-1"
build_role_arn="arn:aws:iam::${account_id}:role/${build_role_name}"
internet_egress_arn="arn:aws:lambda:${region}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
log_group="/aws/lambda/microvms/any-embedding-gte-multilingual-base"

images=(
  "any-embedding-gte-multilingual-base:2048:4:production-build-2g"
  "any-embedding-gte-multilingual-base-4g:4096:8:production-build-4g"
  "any-embedding-gte-multilingual-base-8g:8192:16:production-build-8g"
)

hooks='{"port":8080,"microvmHooks":{"run":"ENABLED","runTimeoutInSeconds":60,"resume":"ENABLED","resumeTimeoutInSeconds":60,"suspend":"ENABLED","suspendTimeoutInSeconds":60,"terminate":"ENABLED","terminateTimeoutInSeconds":60},"microvmImageHooks":{"ready":"ENABLED","readyTimeoutInSeconds":3600,"validate":"ENABLED","validateTimeoutInSeconds":3600}}'

# Resolve every target before creating a version. Without a complete preflight,
# a conflict or IAM/network failure on a later image could leave an avoidable
# partial release. Treat only the service's explicit not-found response as
# absence; create must fail closed on every other lookup error.
preflight_dir="$(mktemp -d "${TMPDIR:-/tmp}/any-embedding-image-preflight.XXXXXX")"
cleanup() {
  rm -rf "${preflight_dir}"
}
trap cleanup EXIT
preflight_failed=false

for specification in "${images[@]}"; do
  IFS=: read -r image_name _memory_mib _threads _log_stream <<<"${specification}"
  image_arn="arn:aws:lambda:${region}:${account_id}:microvm-image:${image_name}"
  error_file="${preflight_dir}/${image_name}.stderr"

  if aws lambda-microvms get-microvm-image \
    --region "${region}" \
    --image-identifier "${image_arn}" >/dev/null 2>"${error_file}"; then
    if [[ "${mode}" == "create" ]]; then
      echo "Refusing create: ${image_arn} already exists. Use --mode update deliberately." >&2
      preflight_failed=true
    fi
  elif [[ "${mode}" == "create" ]] && grep -q "ResourceNotFoundException" "${error_file}"; then
    : # Expected: this name is available for creation.
  else
    echo "Preflight failed for ${image_arn}; no image was changed." >&2
    sed -n '1,4p' "${error_file}" >&2
    preflight_failed=true
  fi
done

if [[ "${preflight_failed}" == "true" ]]; then
  echo "Image release preflight failed; no create or update request was submitted." >&2
  exit 1
fi

for specification in "${images[@]}"; do
  IFS=: read -r image_name memory_mib threads log_stream <<<"${specification}"
  image_arn="arn:aws:lambda:${region}:${account_id}:microvm-image:${image_name}"
  environment_variables="{\"OMP_NUM_THREADS\":\"${threads}\",\"OPENBLAS_NUM_THREADS\":\"${threads}\",\"MKL_NUM_THREADS\":\"${threads}\",\"TORCH_NUM_THREADS\":\"${threads}\",\"DYNAMIC_BATCH_WINDOW_MS\":\"4\",\"MODEL_BATCH_SIZE\":\"32\",\"JOB_TIMEOUT_SECONDS\":\"840\"}"

  image_arguments=(
    --base-image-arn "${base_image_arn}"
    --base-image-version "1"
    --build-role-arn "${build_role_arn}"
    --description "gte-multilingual-base production worker (${memory_mib} MiB baseline, ${threads} Torch threads)"
    --code-artifact "uri=${artifact_uri}"
    --logging "{\"cloudWatch\":{\"logGroup\":\"${log_group}\",\"logStream\":\"${log_stream}\"}}"
    --egress-network-connectors "${internet_egress_arn}"
    --cpu-configurations architecture=ARM_64
    --resources "minimumMemoryInMiB=${memory_mib}"
    --hooks "${hooks}"
    --environment-variables "${environment_variables}"
    --client-token "any-embedding-${release_id}-${memory_mib}"
  )

  if [[ "${mode}" == "create" ]]; then
    aws lambda-microvms create-microvm-image \
      --region "${region}" \
      --name "${image_name}" \
      "${image_arguments[@]}"
  else
    aws lambda-microvms update-microvm-image \
      --region "${region}" \
      --image-identifier "${image_arn}" \
      "${image_arguments[@]}"
  fi
done

echo "Triggered ${mode} builds in ${region} from ${artifact_uri}. No image or version was deleted."
