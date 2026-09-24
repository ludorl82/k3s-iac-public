#!/usr/bin/env python3
"""iac-gate: the post-merge test for Renovate commits on k3s-iac.

Nobody merges by hand any more (2026-09-16). Renovate automerges image
bumps by tier (renovate.json); Argo CD deploys main. This gate is what
makes that safe: every 10 minutes it clones main, finds the Renovate
commits of the last WINDOW hours that nothing has reverted, and for each
one asks two questions once SETTLE seconds have passed since the commit:

  1. Did Argo CD deploy it, and is every app it touched healthy?
     (status.sync.revision contains the commit, health not Degraded /
     Missing / still Progressing, last sync operation not Error/Failed)
  2. Are the app's Kuma monitors (apps.json) green on a beat newer than
     the commit?

An app that is merely still Progressing (a 10 GB image pull) is not red
until PROGRESSING_GRACE; only Degraded/Missing, a failed sync, or a DOWN
monitor are judged at SETTLE.

A "no" reverts the commit on main with the deploy key, appends an
"iac-gate auto-block until <date>" packageRule to renovate.json so the same
tag is not proposed again for BLOCK_DAYS, and says so on ntfy. A revert that
conflicts is recorded with an Iac-Gate-Skip trailer (so it is not retried
every 10 minutes) and paged instead. A commit Argo has not picked up after
PICKUP_ALERT seconds is paged once, not reverted: if Argo is not syncing,
a revert would not deploy either.

Only commits whose author matches WATCH_AUTHORS or whose subject matches
WATCH_SUBJECT (Renovate's "chore(deps): " prefix — a squash merge is authored
by the token's owner) are judged. The gate's own
commits carry Iac-Gate-Revert / Iac-Gate-Skip trailers naming the commit
they answer; a commit named by such a trailer is settled and left alone.

Every run ends with a Kuma push (KUMA_PUSH_URL): up with a summary, down
with the exception. The push monitor fires on absence, so a gate that
stops running is itself an alert. That is also why a clone GitHub refuses
(retried CLONE_ATTEMPTS times) neither pushes down nor pages: a blip is
gone by the next run, and an outage becomes Kuma's absence alert.
"""
import datetime as dt
import json
import os
import re
import ssl
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request

REMOTE = os.environ.get("GIT_REMOTE", "git@github.com:ludorl82/k3s-iac.git")
BRANCH = os.environ.get("GIT_BRANCH", "main")
WORK = os.environ.get("WORK_DIR", "/work/repo")
WATCH_AUTHORS = [a.strip() for a in os.environ.get("WATCH_AUTHORS", "Renovate").split(",") if a.strip()]
# A squash-merged Renovate PR lands on main authored by the token's owner, not
# by Renovate (seen 2026-09-16: "Ludovic Lamarre | chore(deps): update ... (#32)"),
# so the author alone would match nothing. The subject prefix is Renovate's
# commitMessagePrefix and is what actually identifies a bump.
WATCH_SUBJECT = re.compile(os.environ.get("WATCH_SUBJECT", r"^chore\(deps\): "))
SETTLE = int(os.environ.get("SETTLE_SECONDS", "900"))
# "Still Progressing" is what a big image pull looks like — Frigate's tensorrt
# image is ~10 GB and took longer than SETTLE on its first automerged bump
# (2026-09-16). Degraded/Failed/DOWN are judged at SETTLE; Progressing alone
# gets this much longer before it counts as red.
PROGRESSING_GRACE = int(os.environ.get("PROGRESSING_GRACE_SECONDS", "3600"))
WINDOW = int(os.environ.get("WINDOW_SECONDS", str(6 * 3600)))
PICKUP_ALERT = int(os.environ.get("PICKUP_ALERT_SECONDS", "1800"))
CLONE_ATTEMPTS = int(os.environ.get("CLONE_ATTEMPTS", "3"))
CLONE_BACKOFF = int(os.environ.get("CLONE_BACKOFF_SECONDS", "20"))
RUN_INTERVAL = int(os.environ.get("RUN_INTERVAL_SECONDS", "600"))
BLOCK_DAYS = int(os.environ.get("BLOCK_DAYS", "30"))
APPS_JSON = os.environ.get("APPS_JSON", "/etc/iac-gate/apps.json")
KUMA_STATUS_URL = os.environ.get("KUMA_STATUS_URL", "http://uptime-kuma.kuma.svc/api/status-page/heartbeat/gate")
KUMA_PUSH_URL = os.environ.get("KUMA_PUSH_URL", "")
NTFY_URL = os.environ.get("NTFY_URL", "http://ntfy.ntfy.svc.cluster.local/alerts")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")
GIT_NAME = os.environ.get("GIT_AUTHOR_NAME", "iac-gate (k3s)")
GIT_EMAIL = os.environ.get("GIT_AUTHOR_EMAIL", "alerts@example.com")
ARGOCD_NS = os.environ.get("ARGOCD_NAMESPACE", "argocd")
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false")  # judge and log, never push or page
SA = "/var/run/secrets/kubernetes.io/serviceaccount"

NOW = dt.datetime.now(dt.timezone.utc)


def log(*a):
    print(dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


def sh(*cmd, cwd=None, check=True, capture=True):
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=capture)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} -> {r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


def git(*args, check=True):
    return sh("git", "-C", WORK, *args, check=check)


# --------------------------------------------------------------------------- notify

def ntfy(title, body, priority="default", tags=""):
    if not NTFY_TOKEN or DRY_RUN:
        log("ntfy (no token):", title, "|", body)
        return
    req = urllib.request.Request(NTFY_URL, data=body.encode(), method="POST")
    req.add_header("Authorization", f"Bearer {NTFY_TOKEN}")
    req.add_header("X-Title", title)
    req.add_header("X-Priority", priority)
    if tags:
        req.add_header("X-Tags", tags)
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # a failed page must not stop the gate
        log("ntfy failed:", e)


def kuma_push(status, msg):
    if not KUMA_PUSH_URL or DRY_RUN:
        log("kuma push (no url):", status, msg)
        return
    q = urllib.parse.urlencode({"status": status, "msg": msg[:250]})
    try:
        urllib.request.urlopen(f"{KUMA_PUSH_URL}?{q}", timeout=15).read()
    except Exception as e:
        log("kuma push failed:", e)


# --------------------------------------------------------------------------- inputs

def k8s_get(path):
    if not os.path.exists(f"{SA}/token"):
        # outside the cluster (a dry run from the console): borrow kubectl's context
        return json.loads(sh("kubectl", "get", "--raw", path).stdout)
    token = open(f"{SA}/token").read()
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    req = urllib.request.Request(f"https://kubernetes.default.svc{path}")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        return json.load(r)


def argo_app(name):
    try:
        return k8s_get(f"/apis/argoproj.io/v1alpha1/namespaces/{ARGOCD_NS}/applications/{name}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def kuma_beats():
    """monitor id -> (status, beat time) of the latest heartbeat on the gate page."""
    try:
        with urllib.request.urlopen(KUMA_STATUS_URL, timeout=20) as r:
            data = json.load(r)
    except Exception as e:
        log("kuma status page unreadable, judging on Argo alone:", e)
        return {}
    out = {}
    for mid, beats in data.get("heartbeatList", {}).items():
        if not beats:
            continue
        b = beats[-1]
        t = dt.datetime.fromisoformat(str(b["time"]).replace(" ", "T"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        out[int(mid)] = (b["status"], t)
    return out


def app_paths():
    """Argo app name -> repo path, from argocd/apps/*.yaml in the clone."""
    import yaml
    apps = {"root": "argocd/apps"}
    d = os.path.join(WORK, "argocd", "apps")
    for f in sorted(os.listdir(d)):
        if not f.endswith(".yaml"):
            continue
        for doc in yaml.safe_load_all(open(os.path.join(d, f))):
            if doc and doc.get("kind") == "Application":
                src = doc["spec"].get("source") or {}
                if src.get("path"):
                    apps[doc["metadata"]["name"]] = src["path"].strip("/")
                else:
                    # A Helm chart (cert-manager, 2026-09-23 -- the first one
                    # here) or a multi-source app has no directory in this
                    # repo. The one file that drives it is its own Application
                    # manifest, so that is its path: touched_apps() matches a
                    # file exactly, and a chart bump is then judged on that
                    # app's health. Before this, the first Helm app made every
                    # run crash on KeyError: 'path' and the gate paged
                    # "crashed" every ten minutes while judging nothing.
                    apps[doc["metadata"]["name"]] = os.path.join("argocd", "apps", f)
    return apps


class GitUnavailable(Exception):
    """GitHub would not serve the clone: nothing was judged, nothing is wrong yet."""


def clone():
    # 2026-09-16 20:50 UTC: GitHub refused publickey for a few seconds to both
    # this deploy key and Argo CD's (a different key), and the gate paged
    # "crashed". The next run was green.
    for attempt in range(1, CLONE_ATTEMPTS + 1):
        if os.path.isdir(WORK):
            sh("rm", "-rf", WORK)
        os.makedirs(os.path.dirname(WORK), exist_ok=True)
        r = sh("git", "clone", "-q", "--depth", "500", "--branch", BRANCH, REMOTE, WORK, check=False)
        if r.returncode == 0:
            break
        log(f"clone attempt {attempt}/{CLONE_ATTEMPTS} failed:", r.stderr.strip()[-200:])
        if attempt < CLONE_ATTEMPTS:
            time.sleep(CLONE_BACKOFF * attempt)
    else:
        raise GitUnavailable(r.stderr.strip()[-300:])
    git("config", "user.name", GIT_NAME)
    git("config", "user.email", GIT_EMAIL)


def commits():
    """Recent commits on main: [(sha, time, author, subject)], plus the set of
    shas already answered by a gate trailer (reverted or skipped)."""
    fmt = "%H%x1f%at%x1f%an%x1f%s%x1f%(trailers:key=Iac-Gate-Revert,valueonly)%x1f%(trailers:key=Iac-Gate-Skip,valueonly)"
    since = (NOW - dt.timedelta(seconds=WINDOW)).isoformat()
    out = git("log", f"--since={since}", f"--format={fmt}").stdout
    rows, answered = [], set()
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, at, author, subject, rev, skip = (line.split("\x1f") + [""] * 6)[:6]
        for s in (rev + " " + skip).split():
            answered.add(s.strip())
        rows.append((sha, dt.datetime.fromtimestamp(int(at), dt.timezone.utc), author, subject))
    return rows, answered


def touched_apps(sha, paths):
    files = git("show", "--name-only", "--format=", sha).stdout.split()
    apps = set()
    for f in files:
        for app, p in paths.items():
            if f == p or f.startswith(p + "/"):
                apps.add(app)
    return sorted(apps), files


def is_ancestor(sha, rev):
    if not rev or not re.fullmatch(r"[0-9a-f]{40}", rev):
        return False
    r = git("merge-base", "--is-ancestor", sha, rev, check=False)
    return r.returncode == 0


def image_bumps(sha):
    """[(image, new_tag)] from the '+' image lines of the commit."""
    out = git("show", "--format=", "--unified=0", sha).stdout
    bumps = []
    for line in out.splitlines():
        m = re.match(r"^\+\s*-?\s*image:\s*['\"]?([^\s'\"]+)", line)
        if m:
            ref = m.group(1)
            if ":" in ref.rsplit("/", 1)[-1]:
                image, tag = ref.rsplit(":", 1)
                bumps.append((image, tag))
        m = re.match(r"^\+.*[?&]ref=(v?[0-9][^\s&\"']*)", line)
        if m and "argo-cd" in line:
            bumps.append(("argoproj/argo-cd", m.group(1)))
    return bumps


# --------------------------------------------------------------------------- judge

def judge(sha, when, app_name, app, beats, monitors):
    """Return list of reasons the commit is bad for this app, or [] if fine.
    Returns None when it cannot be judged yet (not deployed / too early)."""
    st = (app or {}).get("status", {})
    rev = st.get("sync", {}).get("revision", "")
    if app is None:
        return [f"{app_name}: Application not found in Argo CD"]
    if not is_ancestor(sha, rev):
        return None  # not deployed yet
    age = (NOW - when).total_seconds()
    if age < SETTLE:
        return None
    reasons = []
    health = st.get("health", {}).get("status", "Unknown")
    if health in ("Degraded", "Missing"):
        reasons.append(f"{app_name}: health {health}")
    elif health == "Progressing":
        if age >= PROGRESSING_GRACE:
            reasons.append(f"{app_name}: still Progressing {int(age // 60)} min after the commit")
        elif not reasons:
            return None  # still rolling out (image pull); judge again next tick
    op = st.get("operationState") or {}
    if op.get("phase") in ("Error", "Failed"):
        reasons.append(f"{app_name}: sync {op.get('phase')} -- {str(op.get('message', ''))[:160]}")
    for mid in monitors.get(app_name, []):
        if mid not in beats:
            continue
        status, t = beats[mid]
        if t > when and status == 0:
            reasons.append(f"{app_name}: Kuma monitor {mid} DOWN")
    return reasons


# --------------------------------------------------------------------------- act

def add_block_rules(bumps, sha, reasons):
    path = os.path.join(WORK, "renovate.json")
    cfg = json.load(open(path))
    until = (NOW + dt.timedelta(days=BLOCK_DAYS)).date().isoformat()
    for image, tag in bumps:
        cfg.setdefault("packageRules", []).append({
            "description": f"iac-gate auto-block until {until}: {image}:{tag} reverted ({sha[:8]}) -- " + "; ".join(reasons)[:200],
            "matchPackageNames": [image],
            "allowedVersions": "!/^" + re.escape(tag) + "$/",
        })
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")


def prune_blocks():
    """Drop 'iac-gate auto-block until <date>' rules whose date has passed and
    push the housekeeping, so the fixed release upstream is proposed again."""
    path = os.path.join(WORK, "renovate.json")
    cfg = json.load(open(path))
    today = NOW.date()
    keep, dropped = [], []
    for rule in cfg.get("packageRules", []):
        m = re.match(r"iac-gate auto-block until (\d{4}-\d{2}-\d{2})", rule.get("description", ""))
        (dropped if m and dt.date.fromisoformat(m.group(1)) < today else keep).append(rule)
    if not dropped:
        return 0
    if DRY_RUN:
        log(f"DRY_RUN: would drop {len(dropped)} expired auto-block(s)")
        return 0
    cfg["packageRules"] = keep
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    git("add", "renovate.json")
    git("commit", "-q", "-m", f"iac-gate: drop {len(dropped)} expired auto-block(s)",
        "-m", "\n".join(r["description"] for r in dropped))
    push_with_retry()
    log(f"pruned {len(dropped)} expired auto-block(s)")
    return len(dropped)


def push_with_retry():
    for attempt in (1, 2):
        r = git("push", "-q", "origin", f"HEAD:{BRANCH}", check=False)
        if r.returncode == 0:
            return
        log("push rejected, rebasing and retrying:", r.stderr.strip()[-200:])
        git("fetch", "-q", "origin", BRANCH)
        git("rebase", "-q", f"origin/{BRANCH}")
    raise RuntimeError("push failed twice")


def revert(sha, subject, reasons):
    bumps = image_bumps(sha)
    what = ", ".join(f"{i}:{t}" for i, t in bumps) or subject
    if DRY_RUN:
        log(f"DRY_RUN: would revert {sha[:8]} ({what}) and block {bumps}")
        return "dry-run"
    r = git("revert", "--no-edit", "--no-commit", sha, check=False)
    if r.returncode != 0:
        git("revert", "--abort", check=False)
        git("reset", "-q", "--hard", f"origin/{BRANCH}")
        git("commit", "-q", "--allow-empty",
            "-m", f"iac-gate: cannot revert {sha[:8]} ({what}), conflict -- needs a hand",
            "-m", "Reasons: " + "; ".join(reasons),
            "-m", f"Iac-Gate-Skip: {sha}")
        push_with_retry()
        ntfy("iac-gate: revert CONFLICT", f"{what}\n{sha[:8]} {subject}\n" + "\n".join(reasons) + "\nRevert conflicted; left in place. Fix by hand.",
             priority="urgent", tags="rotating_light")
        return "conflict"
    if bumps:
        add_block_rules(bumps, sha, reasons)
        git("add", "renovate.json")
    git("commit", "-q",
        "-m", f"revert: {what}",
        "-m", f"Reverts {sha[:8]} \"{subject}\" after the post-merge gate went red:\n" + "\n".join("  - " + r for r in reasons)
              + (f"\n\nrenovate.json: auto-block for {BLOCK_DAYS} days so the same tag is not re-proposed." if bumps else ""),
        "-m", f"Iac-Gate-Revert: {sha}")
    push_with_retry()
    ntfy("iac-gate: reverted", f"{what}\n{sha[:8]} {subject}\n" + "\n".join(reasons) + f"\nBlocked for {BLOCK_DAYS} days.",
         priority="high", tags="rewind")
    return "reverted"


# --------------------------------------------------------------------------- main

def main():
    monitors = {k: v for k, v in json.load(open(APPS_JSON)).items() if not k.startswith("_")}
    clone()
    pruned = prune_blocks()
    paths = app_paths()
    rows, answered = commits()
    beats = kuma_beats()
    watched = [(s, t, a, subj) for s, t, a, subj in rows
               if (any(w.lower() in a.lower() for w in WATCH_AUTHORS) or WATCH_SUBJECT.search(subj))
               and s not in answered]
    log(f"{len(rows)} commits in window, {len(watched)} watched, {len(answered)} answered, {len(beats)} kuma beats")
    summary = []
    for sha, when, author, subject in watched:
        apps, files = touched_apps(sha, paths)
        age = int((NOW - when).total_seconds())
        if not apps:
            log(f"{sha[:8]} touches no app ({', '.join(files)[:80]}), nothing to judge")
            continue
        reasons, pending = [], []
        for app_name in apps:
            r = judge(sha, when, app_name, argo_app(app_name), beats, monitors)
            if r is None:
                pending.append(app_name)
            else:
                reasons.extend(r)
        if reasons:
            log(f"{sha[:8]} RED:", "; ".join(reasons))
            summary.append(f"{revert(sha, subject, reasons)} {sha[:8]}")
            continue
        if pending:
            if age < SETTLE:
                log(f"{sha[:8]} {age}s old, settling ({', '.join(pending)})")
            else:
                log(f"{sha[:8]} not deployed yet in {', '.join(pending)} after {age}s")
                if PICKUP_ALERT <= age < PICKUP_ALERT + RUN_INTERVAL:
                    ntfy("iac-gate: not deployed", f"{sha[:8]} {subject}\nArgo CD has not synced {', '.join(pending)} {age // 60} min after the merge.",
                         priority="high", tags="hourglass")
            summary.append(f"pending {sha[:8]}")
            continue
        log(f"{sha[:8]} green in {', '.join(apps)}")
        summary.append(f"ok {sha[:8]}")
    msg = f"{len(watched)} watched: " + (", ".join(summary) if summary else "nothing to judge") + (f"; {pruned} block(s) expired" if pruned else "")
    log(msg)
    kuma_push("up", msg)


if __name__ == "__main__":
    try:
        main()
    except GitUnavailable as e:
        # No down push, no page: the push monitor (30 min) fires if this lasts.
        log("GitHub unavailable, skipping this run:", e)
        sys.exit(1)
    except Exception as e:
        traceback.print_exc()
        kuma_push("down", f"gate crashed: {e}")
        ntfy("iac-gate: crashed", str(e)[:500], priority="high", tags="warning")
        sys.exit(1)
