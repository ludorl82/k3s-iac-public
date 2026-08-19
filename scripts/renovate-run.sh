#!/usr/bin/env bash
# Weekly self-hosted Renovate run over this repo. Runs in the console
# container on console-vm (invoked by ~/scripts/weekly-iac-updates.sh, the
# forced command of the Cronicle "Weekly IaC update PRs" event).
#
# Renovate reads renovate.json at the repo root and opens tag-bump PRs for
# the container images in the manifests. It only ever writes branches and
# PRs; merging one is the deploy (Argo CD syncs main). The GitHub token
# comes from the gh CLI's own auth — nothing stored beyond what gh already
# holds.
#
# Exit codes: 0 = run completed (PRs opened or not), else broken.
set -euo pipefail

export RENOVATE_TOKEN="$(gh auth token)"
export RENOVATE_GIT_AUTHOR="Renovate (console-vm) <alerts@example.com>"
export LOG_LEVEL="${LOG_LEVEL:-info}"

# npm cache lives in the (roomy) home volume; pin the major so a breaking
# renovate release does not surprise a cron run. Bump deliberately.
npx --yes renovate@41 \
  --platform=github \
  ludorl82/k3s-iac
