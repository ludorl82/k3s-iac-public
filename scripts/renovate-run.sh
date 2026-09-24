#!/usr/bin/env bash
# Daily self-hosted Renovate run over this repo. Runs in the console
# container on console-vm (invoked by nixos-iac scripts/weekly-iac-updates.sh,
# the forced command of the Cronicle "Daily IaC updates" event).
#
# Renovate reads renovate.json at the repo root, opens one tag-bump PR per
# image, and AUTOMERGES them itself once their tier's release age has
# passed (renovate.json). Merging is the deploy: Argo CD syncs main, and the
# iac-gate CronJob (iac-gate/) reverts anything that goes red within 15 min
# and blocks the tag for 30 days. The gate also expires its own blocks.
#
# Two things Renovate will not do on its own, so this script does them:
#
#  1. SILENCE BEFORE MERGING. A rollout takes the service down for a few
#     minutes (Frigate pulls a 10 GB image), and Kuma paged twice at 13:38 on
#     the first automerged burst (2026-09-16). So the first Renovate pass runs
#     with automerge forced OFF (CLI flags beat renovate.json): it opens or
#     refreshes the PRs and merges nothing. Every open Renovate PR is then a
#     PR that WILL merge in the next pass (minimumReleaseAge holds an update
#     back before it even becomes a PR), so its files name the Argo apps
#     about to roll, iac-gate/silence.json names their monitors, and
#     scripts/kuma-silence.py puts those under a timed Kuma maintenance
#     window. The gate ignores a monitor under maintenance and judges on
#     Argo health; when the window ends Kuma pages for real if the service
#     is still down. A failed silence is logged and the merge goes ahead --
#     an upgrade that pages beats an upgrade that does not happen.
#
#  2. LOOP. Renovate automerges at most a few PRs per run, and never in the
#     run that opened them. So: run, and run again while the previous run
#     merged something, up to MAX_PASSES.
#
# The GitHub token comes from the gh CLI's own auth -- nothing stored beyond
# what gh already holds. Exit codes: 0 = run completed, else broken.
set -euo pipefail

REPO=ludorl82/k3s-iac
MAX_PASSES="${MAX_PASSES:-8}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"   # the clone this runs from = k3s-iac main
export RENOVATE_TOKEN="$(gh auth token)"
export RENOVATE_GIT_AUTHOR="Renovate (console-vm) <alerts@example.com>"
export LOG_LEVEL="${LOG_LEVEL:-info}"

# --- 1. open the PRs without merging, then silence what they will roll ------
echo "=== renovate pass 0 (automerge off: open PRs, merge nothing) ==="
npx --yes renovate@41 --platform=github --automerge=false "$REPO" 2>&1 | grep -E "PR (created|updated)|Branch (created|updated)|ERROR|WARN" || true

# The PR list travels in an env var, not a pipe: `python3 -` reads its
# program from stdin, so a heredoc program and piped data cannot share it.
PRS_JSON="$(gh pr list -R "$REPO" --label renovate --state open --json number,files)"
silence=$(PRS_JSON="$PRS_JSON" python3 - "$HERE" <<'PY'
import glob, json, os, sys, yaml
repo = sys.argv[1]
prs = json.loads(os.environ.get("PRS_JSON") or "[]")
paths = {"root": "argocd/apps"}
for f in glob.glob(os.path.join(repo, "argocd", "apps", "*.yaml")):
    for d in yaml.safe_load_all(open(f)):
        if d and d.get("kind") == "Application":
            paths[d["metadata"]["name"]] = d["spec"]["source"]["path"].strip("/")
cfg = json.load(open(os.path.join(repo, "iac-gate", "silence.json")))
default = cfg.get("_default_minutes", 45)
apps, ids, minutes = set(), set(), 0
for pr in prs:
    for fl in pr.get("files", []):
        for app, p in paths.items():
            if fl["path"] == p or fl["path"].startswith(p + "/"):
                apps.add(app)
for app in sorted(apps):
    s = cfg.get(app)
    if not s or app.startswith("_"):
        continue
    ids.update(s.get("monitors", []))
    minutes = max(minutes, s.get("minutes", default))
if ids:
    print(f"{minutes} {' '.join(str(i) for i in sorted(ids))} # {', '.join(sorted(apps))}")
PY
)
if [ -n "$silence" ]; then
  apps="${silence#*# }"; args="${silence%% #*}"
  echo "silencing Kuma monitors for: $apps (${args%% *} min)"
  if mid=$(python3 "$HERE/scripts/kuma-silence.py" "Upgrade: $apps" $args 2>&1); then
    echo "kuma maintenance $mid opened"
  else
    echo "WARNING: could not silence Kuma, merging anyway: $mid"
  fi
else
  echo "no PR names a silenced app; nothing to silence"
fi

# --- 2. merge, and merge again while something merged ------------------------
pass=0
while :; do
  pass=$((pass + 1))
  echo "=== renovate pass $pass ==="
  # npm cache lives in the (roomy) home volume; pin the major so a breaking
  # renovate release does not surprise a cron run. Bump deliberately.
  log=$(npx --yes renovate@41 --platform=github "$REPO" 2>&1 | tee /dev/stderr) || exit 1
  if ! grep -q "automerged" <<<"$log"; then
    echo "pass $pass: nothing merged, done"
    break
  fi
  if [ "$pass" -ge "$MAX_PASSES" ]; then
    echo "pass $pass: still merging at MAX_PASSES=$MAX_PASSES, the rest waits for tomorrow"
    break
  fi
done
