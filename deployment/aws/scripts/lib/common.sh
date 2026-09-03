#!/usr/bin/env bash

# Shared, side-effect-free helpers for AWS packaging and deployment scripts.

sha256_files() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$@"
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$@"
  else
    echo "A SHA-256 utility is required (sha256sum or shasum)." >&2
    return 127
  fi
}

sha256_check_manifest() {
  local manifest="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -c "${manifest}"
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 -c "${manifest}"
  else
    echo "A SHA-256 utility is required (sha256sum or shasum)." >&2
    return 127
  fi
}
