#!/usr/bin/env bash
set -euo pipefail

issue_no="${1:-manual}"
branch="$(git rev-parse --abbrev-ref HEAD)"
short_sha="$(git rev-parse --short=8 HEAD)"
ts="$(date +%Y%m%d-%H%M%S)"

tag="rb-${branch}-${ts}-issue${issue_no}-${short_sha}"
msg="Rollback snapshot on ${branch}, issue=${issue_no}, sha=${short_sha}"

git tag -a "${tag}" -m "${msg}"
printf '%s\n' "${tag}"
