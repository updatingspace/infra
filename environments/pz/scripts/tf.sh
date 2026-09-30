#!/usr/bin/env bash
set -euo pipefail
umask 077
root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
export YC_TOKEN="$(yc iam create-token)"
exec "${TERRAFORM_BIN:-terraform}" -chdir="$root/terraform" "$@"
