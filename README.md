# k8s-iac

## Control plane (single-node, cloud-01 — since 2026-07-23; was 3-node HA 2026-07-22 through 2026-07-23)

`cp-1` (EC2, 203.0.113.130), `cloud-01` (EC2, 198.51.100.7) and `cp-3` (VM on gpu-01, 192.0.2.136) — all `control-plane,etcd`. Converted from single-server sqlite by adding `cluster-init: true` to cp-1's `/etc/rancher/k3s/config.yaml` and restarting (automatic sqlite→etcd migration; pre-conversion sqlite backup at `cp-1:/root/k3s-sqlite-backup-20260722.tgz`). cloud-01 and cp-3 joined with `server --server https://203.0.113.130:6443`, both tainted `dedicated=controlplane:NoSchedule` (alloy's DaemonSet toleration covers them; general workloads stay off — both EC2 boxes were 2GB t3a.smalls running ~1.3GB used). cp-3 is a fresh Debian 13 cloud-image VM built like vm-01 (cloud-init seed ISO, macvtap on gpu-01's vlan.10) — created specifically so vm-01 could stay an agent and keep its retirement option. That option was exercised on 2026-07-24: vm-01 was drained, removed from the cluster and its VM destroyed on gpu-01 (NetBox Postgres and a valkey replica moved to gpu-01 first).

**2026-07-23: consolidating to cloud-01 as sole control-plane.** Plan: resize cloud-01 up (headroom for both control-plane + its legacy Docker stack), repoint every `lab.example` name that was resolving to cp-1's IP over to cloud-01, then remove `cp-1` and `cp-3` (etcd quorum holds throughout since 2 of the 3 members stay up until the last step). cloud-01 was stopped and resized `t3a.small` → `t3a.medium` first (656MB→3.1GB available RAM) — this is deliberately *not* the "keep 2 cloud etcd members so a home outage doesn't take quorum with it" design given up lightly: that property was for the control-plane's own availability, whereas the earlier discussion about kuma/ntfy alerting on a home-internet outage is a separate concern that already requires an external (cloud) vantage point regardless of how many etcd members exist locally — collapsing to one cloud control-plane node doesn't reopen that gap, it just removes control-plane HA, accepted here as a homelab-appropriate tradeoff.

Doing this exposed a port conflict: cloud-01's legacy Docker `traefik` (see `cloudflared/` below) had `0.0.0.0:80/443` published on the host, colliding with k3s's `svclb-traefik` DaemonSet trying to do the same for in-cluster ingress once the `dedicated=controlplane` taint got a toleration. Fixed by rebinding Docker `traefik` to cloud-01's own WireGuard IP (`198.18.0.2`, already routable from the LAN — no new AWS ENI IP needed) instead of `0.0.0.0`, freeing `198.51.100.7:80/443` for the k3s ingress. The toleration itself is set declaratively via `traefik/helmchartconfig.yaml` (a `HelmChartConfig` overlay on the built-in `traefik` `HelmChart` addon, using the `svccontroller.k3s.cattle.io/tolerations` Service annotation) rather than editing the auto-generated Service/DaemonSet directly, which k3s's helm controller would just revert.

Net split of the 9 `lab.example` private names that used to point at `cp-1`/cloud-01 docker-traefik:
- **→ cloud-01 primary IP `198.51.100.7`** (k3s ingress, has an in-cluster `IngressRoute`): `frigate`, `cronicle`, `loki`, `n8n`, `grafana`, `numeriseur`, `unifi` (unifi's route added 2026-07-23, see `unifi/` below — previously routed only through cloud-01 Docker Traefik straight to vm-02:8443). `netalertx` and `netbox` were on this list until both apps were decommissioned 2026-07-27 (their Unbound overrides need pruning on router).
- **→ cloud-01 wg0 IP `198.18.0.2`** (legacy Docker Traefik, no in-cluster equivalent): `plex`, `traefik`. `kuma` and `ntfy` were on this list until 2026-07-25, when they moved into the cluster and their overrides were repointed to `198.51.100.7` — see `ntfy/ + kuma/`.

Also cleaned up while doing this: `cronicle`/`loki`/`n8n`/`frigate`.lab.example had leftover Cloudflare A records (pointing at the old cp-1 IP) shadowed-but-not-replaced by their local pfSense Unbound overrides — deleted from Cloudflare per policy (no `lab.example` entries should exist there; `lab.example` is LAN-private, `pub.example.com` is the public zone, see `dns-domain-split-public-private` in Claude's memory). `cp-1.lab.example`/`cloud-01.lab.example` host-identity overrides (not service routes) are untouched and will just get pruned once `cp-1` is actually decommissioned.

Gotcha hit mid-migration: after the cloud-01 stop/resize cycle and the taint/toleration change, Traefik's Kubernetes CRD provider got stuck (`apiserver not ready` reflector errors during the disruption never fully recovered) and silently stopped matching *any* new `IngressRoute`, returning Traefik's generic `404 page not found` for `unifi` even though the Service/Endpoints were healthy and other pre-existing routers kept working. Fix was `kubectl rollout restart deployment/traefik -n kube-system`. If a freshly-applied `IngressRoute` 404s despite correct Service/Endpoints, suspect a stuck CRD informer before debugging the manifest itself — especially after any control-plane node was recently stopped/restarted.

**2026-07-23: cp-1 and cp-3 removed, cloud-01 is now the sole control-plane/etcd node.** Removal order matters a lot with etcd — the safe sequence per node is (1) `kubectl delete node <name>` **while that node's k3s/etcd process is still running**, so k3s's node-lifecycle controller can cleanly remove it from etcd's raft membership while the cluster is still quorate, then (2) `systemctl stop k3s` on that node afterward, once it's already out of the membership list and no longer contributes to quorum math. Doing it backwards (stopping the process first, deleting the Node object second) bit us on the second removal: with only cloud-01+cp-3 left, etcd's majority requirement for a 2-member cluster is 2-of-2 (zero fault tolerance) — stopping cp-3 before removing it from membership broke the whole etcd cluster's quorum immediately, taking cloud-01's own API down with it (`kubectl` returned `unexpected EOF`). Recovery was just `systemctl start k3s` on cp-3 again to restore the pair, then redoing the removal in the correct order. Lesson: **any transition through a 2-member etcd cluster is fragile** — the first removal (3→2 members) tolerates one node being down, but the second (2→1) does not, so the "delete Node object first, stop the process second" order isn't optional for that last step.

Pre-removal safety net: an on-demand etcd snapshot (`k3s etcd-snapshot save --name pre-cp-1-removal-20260723`) was taken on cloud-01 immediately before starting, on top of the existing default 12h/keep-5 schedule — cheap insurance given this was collapsing from 3 etcd members down to 1, with no HA left to fall back on afterward.

Post-removal cleanup: `cp-1.lab.example`/`cp-3.lab.example` pfSense Unbound host-identity overrides (not service routes, just "this hostname is this box") deleted since both hosts no longer exist — `cp-1` (EC2 `i-09cbcaa1f3007cfe0`) terminated, `cp-3` (libvirt VM on gpu-01) destroyed with `virsh undefine --remove-all-storage` (disks gone too).

**Tradeoff accepted going forward:** no control-plane HA. If cloud-01 goes down (reboot, EC2 maintenance, OOM), the k3s API/scheduler is unavailable until it's back — existing pods keep serving via their own kubelets, but nothing can be deployed/scaled/rescheduled during that window. Judged acceptable for a homelab; the etcd snapshot schedule is now cloud-01's only recovery path if its disk itself fails, so don't let that lapse.

Two cross-site gotchas cost the join several attempts — **etcd's peer transport verifies the connecting source IP against the peer's cert SANs**, which no other k3s traffic does:

1. Locally-originated connections from the EC2 side to LAN rode wg0 with the WG interface address (`198.18.0.x`) as source. Fix: source-hint the route — `ip route change 192.0.2.0/23 dev wg0 scope link src <node-ip>`, persisted as `PostUp` in `/etc/wireguard/wg0.conf` on cp-1 and cloud-01.
2. pfSense **outbound-NATs** LAN→tunnel traffic to its own tunnel address (automatic NAT, because the WG interfaces have gateways). cp-3's connections arrived at cp-1 as `198.18.0.5`. Fix: two manual no-NAT rules (hybrid mode) on `router` — src `192.0.2.0/23` → dst `203.0.113.0/24` on opt3, → dst `198.51.100.0/24` on opt1.

Also needed: SG `cloud-01-main` rules from `198.51.100.0/24` for 6443, 9345, 2379-2380, 10250/tcp + 8472/udp (cp-1↔cloud-01 is VPC-native, so the SG applies; LAN↔EC2 rides WG and bypasses it). etcd snapshots: k3s now takes them on schedule (default 12h/keep 5) plus `conversion-done` on-demand one. Agents still register against `https://203.0.113.130:6443`; they learn all three servers after joining — a fixed registration address (DNS) is a possible future nicety.

Kubernetes manifests for the homelab k3s cluster (cp-1 + pi-01-5), applied via `kubectl apply -f <dir>`.

Secrets are **not** stored in this repo. They're created out-of-band with `kubectl create secret ...` directly on the cluster and referenced by name from the manifests here.

## numeriseur/

Scanner ingestion pipeline: sftpgo with S3-backed virtual users, so scans never touch local storage. **Stock `drakkan/sftpgo` image since 2026-08-05** — there is no image to build any more.

What used to make it custom was the post-upload hook (imagemagick, rclone, awscli, openssh-client) plus the `envsubst` templating that fed it. The hook is now an EventBridge-driven Lambda in `cloud-01-iac` (`live/numeriseur.tf`): sftpgo writes to S3, S3 emits an event, the Lambda processes and pushes to Google Drive. The templating is an initContainer running the same stock image, because it only needs `bash`.

**The workload is stateless.** The 200Mi NFS PVC and its nightly backup CronJob are gone: the hook's scratch space and rclone's rewritable config went with the hook, the host keys are a Secret, and the sqlite provider is rebuilt from `users.json` on every start (`--loaddata-mode 0`). What was actually surviving restarts was ~19 MB of logs.

Three things worth knowing before touching it:

- **The health probe depends on an unobvious config detail.** `/healthz` is only served if `enable_rest_api` is `true` on the httpd binding. Turning all three of `enable_web_admin` / `enable_web_client` / `enable_rest_api` off makes sftpgo skip starting the HTTP server entirely, and the probes then fail with no clue why. The web UI routes stay unregistered (404) and the REST API returns 401 with no admin account existing, so binding on `0.0.0.0` exposes nothing but `/healthz`.
- **`fsGroup: 1000` is load-bearing.** The stock image runs as uid 1000 and the host-key Secret is mounted `0640`; without `fsGroup` the process cannot read its own host keys.
- **Editing the ConfigMap does not restart anything.** The initContainer renders config at pod start only, so a ConfigMap or Secret change needs `kubectl -n numeriseur rollout restart deployment/numeriseur-sftpgo`.

Secrets expected in the `numeriseur` namespace before applying:
- `s3-credentials` (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`)
- `sftp-user-passwords` (`ludo`, `lea`, `ludoetlea`, `photos`, `cartes`)
- `printer-public-key` (key `key`)
- `numeriseur-sftpgo-hostkeys` (`id_rsa`, `id_rsa.pub`, `id_ed25519`, `id_ed25519.pub` — the scanner pins the server key, so never regenerate these casually)

`rclone-config` is no longer used and can be deleted once the cutover is confirmed. The Google credentials now live in AWS Secrets Manager (`numeriseur/google-drive`), not in the cluster.

## n8n/

Workflow automation (posting to LinkedIn, etc.), single-pod, SQLite-backed on a local-path PVC. Exposed internally as `n8n.lab.example` and publicly as `n8n.pub.example.com` (Cloudflare Access OTP-gated), same Tunnel+Access pattern as netbox/netalertx.

Secrets expected in the `n8n` namespace before applying:
- `n8n-secrets` (`encryption-key` — random value, generated once with `openssl rand -hex 32`; losing it makes stored credentials unrecoverable)

## cronicle/

Job scheduler + dashboard for scheduled actions across the lab (LinkedIn post triggers via n8n webhooks, backup jobs, system upgrade runs). Single-pod (`soulteary/cronicle` image), **stateless since 2026-08-07**: all durable data (events, users, API keys, history, completed job logs) lives in S3 via Cronicle's bundled `pixl-server-storage` S3 engine — bucket `cronicle-data-example-com` (cloud-01-iac; versioned, 30d noncurrent expiry as the recovery mechanism, no separate backup job). `config.json` is owned declaratively by the `cronicle-config` ConfigMap (`config-configmap.yaml`) because Cronicle 0.9.x has no env-var config overrides; everything except the `Storage` block is a verbatim copy of the image default. Logs and queue are emptyDir scratch — losing them on pod churn only costs in-flight job logs. The old NFS PVC (`cronicle-data-nfs`, `pvc.yaml`) is kept only as a rollback path during the soak period. Exposed internally as `cronicle.lab.example` and publicly as `cronicle.pub.example.com` (Cloudflare Access OTP-gated), same Tunnel+Access pattern as n8n. Default login on first boot is `admin`/`admin` — change immediately.

Secrets expected in the `cronicle` namespace before applying (beyond the SSH keys below):
- `cronicle-s3-credentials` (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_REGION`) — IAM user `cronicle-s3` (cloud-01-iac), scoped to the data bucket only; consumed via the SDK default credential chain so the ConfigMap holds no credential.

Every event that needs SSH into a host uses a narrowly-scoped, forced-command-restricted key — one dedicated keypair per (script, host) pair, each `Secret` name following `cronicle-ssh-<purpose>[-<host>]`. Each key's public half is added to that host's `authorized_keys` with `command="<exact script path>",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty` — so even a leaked key can only ever run its one designated script on its one designated host, nothing else. Connections use the real hostname (`*.lab.example`/`*.pub.example.com`) now that cluster DNS is fixed (see `cluster-dns/` below); `UserKnownHostsFile=/keys/known_hosts` pins the host key from the shared `console-vm-known-hosts` ConfigMap (misleadingly named — it now holds every backed-up host's key, not just console-vm's).

Two things that bit while wiring the 2026-08-09 events, both worth knowing before adding another key here. **known_hosts records a non-default port as `[host]:port`**, so the `console-vm.lab.example,console-vm` entry only ever covered port 22 (the VM); the console *container* on 2222 has its own host key and was not pinned at all, which is why the events dialling it had quietly been using `StrictHostKeyChecking=accept-new` against a throwaway file — trust-on-first-use every run, not what this paragraph claims. `[console-vm.lab.example]:2222` is pinned now. And **the ConfigMap is mounted with `subPath`, so it does not hot-reload** — editing it requires a pod restart, not just an Argo sync.

For the console container specifically, the public half goes in `hosts/console-vm/configuration.nix`, **not** in the box's `authorized_keys` by hand: `modules/console-container.nix` rewrites that file from the declared list on every switch. Appending works until the next comin deploy silently drops it, which is exactly what happened mid-verification on 2026-08-09 — two keys that had just passed their tests stopped authenticating a few minutes later.

Events currently configured:
- **LinkedIn post trigger** — one-time (`years` pinned), fires an n8n webhook over the in-cluster service address (`n8n.n8n.svc.cluster.local`), no SSH involved. Spent one-time events are **deleted once they fire**; a permanent disabled `LinkedIn post (TEMPLATE - copy me)` event carries the current payload skeleton to copy from (see `net-cfgs/linkedin-publishing.md`).
- **Homelab Backup** — one event per host (console-vm, pi-01, pi-02, vm-03, cp-1), daily 2:15am, each running that host's own `~/scripts/homelab-backup.sh` via its own forced-command key. vm-01's and vm-02's events, their `cronicle-ssh-backup-worker{3,4}` keys and their Kuma push monitors were removed 2026-07-24 — vm-01 when its VM was retired, vm-02 as leftover cleanup (that host had been gone for some time, so its job had been failing nightly).
- **Weekly System Updates (console-vm)** — Sunday 7am, runs console-vm's `weekly-updates.sh` (itself a headless Claude Code call that updates the rest of the fleet, including a `kubectl -n unifi rollout restart deployment/unifi` step run via `ssh cp-1` — fixed 2026-07-22, it previously targeted a stale `pi-01` docker-compose path that no longer exists; the UniFi Network Application is actually a k3s Deployment on vm-02).
- **UniFi Mongo Backup** — daily 2:30am, same suspended-CronJob-via-`kubectl` pattern as netbox/netalertx (see below).
- **KeePass Backup (cloud-01)** — daily 1:05am, `/opt/scripts/backup_keepass.py`.
- **The diagram pipeline's morning chain (2026-08-09)** — Cronicle is now the *only* scheduler for it, and the chain finishes before 06:00. In order: **IaC Drift Checks (cloud-01 + cloudflare)** → **nixos-iac Drift Check** → **Nightly Diagram Sync** → **Architecture Refresh (prod)**. Since 2026-08-09 that order is enforced by Cronicle's own **chaining** rather than by four separate clock times: only the first event has `timing` (04:30), and each sets `chain` *and* `chain_error` to the next so the chain advances whatever the outcome (stopping on a red drift check would freeze the prod badge on the very morning it should read `drift`). Finishes ~04:50 instead of 05:32, and the variable-length sync no longer has to fit a fixed 45-minute window. Caveat: an **aborted** job chains to nothing — `chain_error` does not fire on `abort_reason` — so a timeout breaks the chain silently and the downstream Kuma absence monitors are what catch it. The first and last only *trigger* a GitHub workflow and wait for it (`gh run watch --exit-status`, so a failed workflow turns the Cronicle run red instead of the automatic green a bare dispatch gives); the `schedule:` blocks were deleted from `cloud-01-iac/drift.yml`, `cloudflare-iac/drift.yml` and `labodeludo.dev/architecture.yml` the same day. This fixed a real ordering bug: `architecture.yml`'s cron was `15 9 * * *` = 09:15 UTC = **05:15 local**, i.e. *before* the drift checks it sealed and *before* the sync whose components it rendered. GitHub cron is UTC-only and also slipped an hour between EDT and EST; Cronicle schedules in local time.
- **Public IaC Snapshots (manual)** — no timing, run on demand. Runs all four `sanitize-public.sh` (fail-closed gates), then force-pushes each sanitized tree as a single-commit orphan to its `*-public` repo, bundling the previous public HEAD into `~/.local/state/publish-snapshots/rescue/` first. It **refuses a repo whose working tree is dirty** — those checkouts are shared and the sanitizer reads the tree, not `HEAD`, so a dirty tree would publish someone's in-flight edit. It deliberately **does not move `article/*` tags**: each pins the tree as its article described it (they legitimately point at older commits), and moving them would repoint every published article at today's tree. The "delete and recreate the tags" line in `net-cfgs/history.md` describes the 2026-08-07 leak *remediation*, not the standing rule.

**Policy revision (later 2026-07-22): Cronicle only *triggers* daily/weekly jobs.** The sub-daily jobs originally migrated here (WAN IP Cloudflare Sync on console-vm; both Frigate syncs on cloud-01; Frigate NAS Backup / Lowres Prune / Lowres Feed Watchdog / Recording Watchdog on gpu-02) were deliberately moved **back to their hosts' crontabs** to keep the execution history readable. Their Cronicle events remain as **disabled, no-timing placeholders** (filed under the **Runs elsewhere** category since 2026-08-09, with the marker and the real crontab line in each event's `notes` rather than in its title) so the dashboard stays a complete inventory of the lab's scheduling surface — see `net-cfgs/credentials.md` for the full convention and the per-job crontab lines. Don't re-enable a placeholder without removing the matching host crontab entry first, or it runs twice (that exact mixup happened once with the WAN IP sync).

**Dashboard curation (2026-08-09):** the event list is grouped into five categories — **Backups** (blue), **Diagram pipeline** (green), **Maintenance** (orange), **Publishing** (purple), **Runs elsewhere** (plain) — with `General` left as the fallback for anything unfiled. Use the Schedule page's *Group by Category* toggle. Every event carries `notes` (what it does, which Kuma monitor alarms, the runbook path, and for the chain events its position in it); titles stay short. Creating a category needs an **admin** account — the `Cronicle (claudecode)` API key is not one, so categories are made in the UI while assignment is a plain `update_event`. Full convention in `net-cfgs/credentials.md`.

KeePass Backup runs as root on its host via a forced command prefixed `sudo` (as did the Frigate jobs when they lived here). Gotcha hit doing this: `sudo <cmd> >> /var/log/foo.log 2>&1` does **not** work — the `>>` redirect is parsed by the outer (non-root) shell before `sudo` ever runs, so it fails with `Permission denied` against a root-owned log file. Fix: wrap the whole thing so the redirect happens *inside* the elevated shell — `sudo sh -c 'cmd >> /var/log/foo.log 2>&1'`.

Not migrated, deliberately: `router`'s `ipv6_pd_watchdog.sh` (a network watchdog whose scheduling shouldn't depend on the network it's watching) and `nas`'s `disk_health_kuma_push.sh`/`plex_watchdog.sh` (same watcher-shouldn't-depend-on-network logic, plus QNAP's cron is vendor-managed and easy to break by editing externally).

pi-02 is a special case: it had no backup script, no `cloud-01` CLI, and no `kp-get` before this — it was net-new setup, not just a scheduling migration. It shares the fleet's IAM credentials (copied from vm-01, not host-unique) and fetches the backup passphrase from console-vm the same indirect way the other non-console-vm hosts do (a dedicated forced-command key that runs `kp-get` *on console-vm*, since only console-vm holds KeePass access directly). pi-02 also has its own unrelated DNS bug — a stale/duplicate NetworkManager profile makes it prefer ISP nameservers over the LAN resolver, breaking hostname resolution *from* pi-02 outward — deliberately left untouched (real risk to touch network config on a live cluster node for something out of scope) and worked around by having pi-02's script connect to console-vm by IP instead of hostname. It also has no Kuma push monitor yet, unlike the other hosts' backup jobs — not yet added.

Note: this image self-identifies internally as hostname `main` regardless of the pod's actual hostname — event `target` must be set to `"main"`, not the pod's real hostname.

### Triggering native K8s CronJobs (suspended-CronJob pattern)

Backup CronJobs that need in-cluster access (PVCs, in-namespace services, namespace-scoped Secrets) that Cronicle's pod doesn't and shouldn't have directly are set to `spec.suspend: true` (scheduling logic stays in place as the Job *template*, but Kubernetes' own controller never auto-fires them) and Cronicle triggers them via `kubectl create job --from=cronjob/<name>`, polls until the Job completes, prints its logs, then deletes it. This originally covered `netbox-postgres-backup`, `netbox-media-backup`, and `netalertx-backup`; both apps were decommissioned 2026-07-27 (final backups in S3), leaving `unifi-mongo-backup` and the other suspended backups as the users of the pattern.

This needs `kubectl` in the Cronicle pod (installed via the same `postStart` hook as the SSH client, architecture-aware since the cluster mixes amd64/arm64 nodes) and a dedicated `ServiceAccount` (`cronicle-job-trigger`, see `rbac.yaml`) bound via `Role`/`RoleBinding` to `create`/`get`/`list`/`delete` on `jobs`, `get` on `cronjobs`, and `get`/`list` on `pods` + `get` on `pods/log` (needed for `kubectl logs` to find the Job's pod) — scoped per-namespace (see `rbac.yaml` for the current list), nothing cluster-wide, no Secret/PVC access.

If you add another suspended-CronJob-via-Cronicle target in a new namespace, it needs its own `Role`/`RoleBinding` pair added to `rbac.yaml` (namespaced Roles don't apply automatically).

## unifi/ backup CronJob

`unifi-mongo-backup` is `suspend: true` — see the Cronicle section above for why. The schedule/timing on the `CronJob` resource is now purely documentation of the original intended cadence; Cronicle's event `timing` is authoritative. (`netbox/` and `netalertx/` had the same pattern until both apps were decommissioned on 2026-07-27.)

`unifi/mongo-backup-cronjob.yaml` existed live in the cluster before this repo tracked it (applied out-of-band at some point) — the file here was written from and verified against the live resource, not the other way around.

## unifi/ingressroute.yaml

Added 2026-07-23. `unifi.lab.example` previously reached the UniFi Network Application (hostNetwork Deployment pinned to vm-02, see `unifi/deployment.yaml`) only via a hop through cloud-01's legacy Docker Traefik (`https://192.0.2.134:8443`, `insecureSkipVerify` — UniFi's cert is self-signed). This adds the in-cluster equivalent: a plain `Service` (selector `app: unifi`, port 8443 — works fine against a hostNetwork pod, since the Pod IP *is* the node IP), a `ServersTransport` with `insecureSkipVerify: true` for the same self-signed-cert reason, and `web`/`websecure` `IngressRoute`s following the frigate/cronicle pattern. `unifi.lab.example` now points straight at cloud-01's k3s ingress (`198.51.100.7`) instead of the cloud-01-Traefik hop. The public `unifi.pub.example.com` twin is untouched — it still goes straight to vm-02:8443 via the `k3s` cloudflared tunnel (see `cloudflared/` below), no Traefik hop either way.

## traefik/helmchartconfig.yaml

Added 2026-07-23, alongside consolidating the control plane onto cloud-01 (see top of this README). A `HelmChartConfig` (not a raw edit of the built-in `traefik` `HelmChart`/Service/DaemonSet, which k3s's helm controller would revert) that adds `svccontroller.k3s.cattle.io/tolerations` to the Traefik Service's annotations, letting the auto-generated `svclb-traefik` DaemonSet schedule onto nodes tainted `dedicated=controlplane:NoSchedule` (cloud-01 and cp-3). Needed so cloud-01's k3s ingress (`198.51.100.7:80/443`) could come up at all post-taint; see the port-conflict note above for why cloud-01's legacy Docker Traefik had to move off those same ports first.

## cloudflared/

Second Cloudflare tunnel (`k3s`, id `00000000-0000-0000-0000-000000000000`), in-cluster since 2026-07-22, carrying every cluster-backed public hostname: the `.pub.example.com` twins (cronicle, frigate, grafana, n8n, netalertx, netbox → in-cluster Traefik with `noTLSVerify`; unifi → straight to vm-02:8443). It also carried `dev.labodeludo.dev` (→ Traefik's web entrypoint, the only rule that used it) until 2026-08-07, when staging moved to Cloudflare Pages and stopped having an origin at all. Removes the old three-hop path (edge → cloud-01 cloudflared → cloud-01 Traefik → `insecureSkipVerify` → cp-1) and its dependency on the cloud-01 host — done deliberately *before* the planned 3-cp-1 control-plane conversion.

Design notes:
- **HA = replicas of one tunnel, not two tunnels per name.** A hostname CNAMEs to exactly one `<tunnel-id>.cfargotunnel.com`; multi-tunnel per hostname needs the paid Load Balancer product. Two replicas with required anti-affinity give edge-side failover for free.
- Tunnel **ingress rules are remote-managed** (Cloudflare API `cfd_tunnel/<id>/configurations`, PUT replaces the whole array) — the Deployment here only runs the connector. `TUNNEL_TOKEN` Secret is imperative (`cfd_tunnel/<id>/token` API → env-file pipe).
- Cloudflare **Access apps are hostname-based** and survived the move untouched.
- ~~The original cloud-01 tunnel (`22222222`) keeps the non-cluster names: `vault.family.example`, `ha-01.family.example`, plex/traefik/router `.pub.example.com`.~~ **The `keepass-webdav` tunnel was retired 2026-08-06** — there is now exactly one tunnel. See `keepass-webdav/` and `plex/` for where its last five hostnames went. The retired cloud-01 Traefik dynamic files are parked in `cloud-01:~/web-docker/retired-20260722-k3s-tunnel/`; `unifi.yml` was trimmed to its `.lab.example`-only router.
- **A third connector (`deployment-cloud-01.yaml`) is pinned to the `cloud-01` node** (nodeSelector + controlplane toleration), added 2026-07-25 with the ntfy/kuma migration. Anti-affinity can only forbid co-location, not guarantee placement — both original replicas landed on home nodes (pi-02/vm-02), which would have made the alerting stack unreachable during exactly the home outage it exists to report. Same tunnel, same Secret; Cloudflare just sees a third connector.

## keepass-webdav/ + plex/

Added 2026-08-06, retiring the legacy `keepass-webdav` tunnel (`22222222`) and with it the whole `cloud-01:~/web-docker` Docker stack — Traefik, the `httpd` WebDAV container and the out-of-cluster `cloudflared` connector. The lab is down to **one** Cloudflare tunnel.

Five hostnames rode that tunnel, and they split into three shapes:

- **Pure pass-throughs** — `router.pub.example.com` (pfSense) and `ha-01.family.example` (Home Assistant) were only ever `insecureSkipVerify` proxies to a VLAN10 address. Their tunnel ingress rules now dial `https://192.0.2.254` / `https://192.0.2.34` directly, with `noTLSVerify` and the hostname as `originServerName`. No in-cluster object exists for either; Traefik was a hop with nothing to contribute.
- **`plex.pub.example.com` / `plex.lab.example`** — Plex itself stays on the QNAP. `plex/` is routing only: a selector-less Service plus a hand-written EndpointSlice pointing at `192.0.2.65:32400`, so in-cluster Traefik can reach it like any other backend. The `.lab.example` name needed this because Docker Traefik was the only thing serving it; its Unbound override moved `198.18.0.2` → `198.51.100.7`. **Nothing maintains that EndpointSlice** — if the QNAP's IP changes, `plex/service.yaml` is the one place to fix and nothing self-heals.
- **`traefik.pub.example.com` / `traefik.lab.example`** — the Docker Traefik's own dashboard. Retired outright rather than repointed: the in-cluster Traefik deliberately exposes no dashboard. DNS record, Cloudflare Access app and Unbound override all deleted.

`vault.family.example` is the interesting one and the reason this took manifests instead of five API calls.

**The WebDAV Deployment is pinned to the `cloud-01` node and must stay there.** The `.kdbx` files it serves are the live KeePass databases, and `keepass-watch.service` *on the cloud-01 host* watches that exact directory with inotifywait (`IN_CLOSE_WRITE` + `IN_MOVED_TO`) to fire the whole KeePass sync pipeline on every save — the `IN_MOVED_TO` specifically so WebDAV's atomic temp-file+rename saves trigger it too. Serving the files from an NFS PVC, or from any other node, would have severed that trigger *silently*: WebDAV would keep working, saves would keep succeeding, and the sync would simply stop happening. So the pod goes to the data, not the reverse — `nodeSelector` + a `hostPath` onto `/var/lib/keepass`, declared in `nixos-iac` by a tmpfiles `d` rule so it exists before the pod binds to it. (Until 2026-08-07 this was a docker volume path, `numeriseur-docker_keepass2-home`; see [net-cfgs history](https://github.com/ludorl82/net-cfgs) for the move — and for why `kubectl apply` silently lost to ArgoCD partway through it.)

Consequences worth knowing:

- `replicas: 1` + `strategy: Recreate`. Two httpd pods writing the same `.kdbx` over WebDAV would race, and a rolling update would briefly double-mount.
- `runAsUser/runAsGroup: 1006` — matches the on-disk ownership, same uid the Docker container used. httpd must be able to write, not just read.
- The liveness probe is `tcpSocket`, **not** `httpGet`. `DocumentRoot` is `Require all denied`, so a perfectly healthy server answers `GET /` with 403 — and kubelet only counts 200–399 as success, so an httpGet probe there would crashloop the pod. Auth is at the Traefik edge, so there is no unauthenticated 2xx path to probe instead.
- `httpd.conf` is a ConfigMap copied verbatim from the retired Docker stack, including the `RequestHeader edit Destination` rewrite — WebDAV clients send `Destination: https://vault.family.example/...` but httpd listens on plain `:8080`, and mod_dav returns 502 on the scheme/port mismatch.
- Basic auth moved from a Traefik file-provider middleware to a `basicAuth` Middleware + `webdav-auth` Secret. The **same bcrypt hash** was carried over rather than reissued, so every mobile KeePass client kept working without reconfiguration. Credential is the KeePass entry "Keepass Server".
- No rate-limit middleware here on purpose: that moved to a Cloudflare edge rule (20 req/10s) back when the Traefik `webdav-ratelimit` middleware was dropped. Don't re-add a Traefik-level limiter without checking the edge rule first.

Cloudflare Access apps and the WAF rules (the `vault.family.example` Canada-only geo-block, the shared plex/ntfy one) are hostname-based, so they followed the move untouched — only the `traefik.pub.example.com` app was deleted, deliberately.

## ntfy/ + kuma/

The alerting stack — Uptime Kuma (dashboard/monitors) and ntfy (push delivery) — migrated out of Docker on `cloud-01` (`~/uptime-kuma-docker`, `~/ntfy-docker`) into k3s on 2026-07-25. Exposed internally as `ntfy.lab.example` / `kuma.lab.example` (pfSense Unbound overrides repointed `198.18.0.2` → `198.51.100.7`) and publicly as `ntfy.pub.example.com` / `kuma.pub.example.com` on the `k3s` tunnel.

**Both are deliberately pinned to the `cloud-01` node** (`nodeSelector: kubernetes.io/hostname: cloud-01` + the `dedicated=controlplane:NoSchedule` toleration), and this is the whole point of the directory — not an incidental placement:

- The original design put Kuma on the cloud box specifically so a home network / VLAN10 outage would not take down the thing whose job is to alert about it. Letting the scheduler place these pods on a home node would silently undo that property while everything still *looked* healthy.
- Their PVCs are `local-path`, not `nfs-client`: both keep SQLite state (`kuma.db` + WAL; ntfy's `auth.db`/`cache.db`), and SQLite over NFS has the same locking/corruption hazard that keeps n8n on local-path. local-path bakes a nodeAffinity into the PV, which reinforces the pin — but it also means a future node move needs the same scale-to-0 + copy dance n8n's PVC comment describes. The NFS server is a home host anyway, so an NFS-backed PVC would have re-introduced the home dependency through the back door.
- **Their tunnel ingress rules point straight at the in-cluster Services** (`http://ntfy.ntfy.svc.cluster.local:80`, `http://uptime-kuma.kuma.svc.cluster.local:80`), *not* at `traefik.kube-system` like the other `.pub.example.com` twins. Traefik has no cloud-01-resident pod, so routing through it would have put a home-node hop back in the path. Same reasoning drove the cloud-01-pinned third cloudflared connector (see `cloudflared/`). The `IngressRoute`s here still exist and serve the `.lab.example` names via Traefik — that path is LAN-only, and during a home outage there is no LAN client to serve anyway.

Kuma mounts the private CA root (`kuma/private-ca.yaml`) at `/certs` with `NODE_EXTRA_CA_CERTS`, so its `*.lab.example` HTTP monitors validate properly instead of being downgraded to `ignoreTls` — the in-cluster equivalent of the Docker setup's bind-mounted cert.

ntfy's `server.yml` is a ConfigMap (`base-url: https://ntfy.pub.example.com`, `behind-proxy: true`, `auth-default-access: deny-all`). Users/ACLs live in `auth.db` on the PVC, not in config — that file was copied over with the migration, so the Kuma → ntfy credentials kept working untouched.

No Secrets are needed by either manifest set. Kuma's ntfy credentials live inside `kuma.db` (the notification provider config), not in the cluster.

Migration procedure used (worth repeating for the next Docker→k3s move): `docker compose stop` first so SQLite checkpoints its WAL into the main DB file (a clean stop leaves no `-wal`/`-shm` — if it does, copy them too), apply namespace + PVC + Deployment so local-path provisions the PV directory on the right node, scale to 0, `rsync` the Docker volume into `/var/lib/rancher/k3s/storage/<pvc>_<ns>_<name>/`, then scale back to 1. The old Docker volumes (`uptime-kuma-docker_kuma-data`, `ntfy-docker_ntfy-data`) were **kept** as a rollback path; the retired cloud-01 Traefik dynamic files are parked in `cloud-01:~/web-docker/retired-20260725-kuma-ntfy-k3s/`.

Gotcha hit on cutover: after repointing the `.pub.example.com` CNAMEs to the k3s tunnel, `kuma.pub.example.com` worked immediately but `ntfy.pub.example.com` returned the tunnel catch-all `404` for a few minutes — the edge was still routing it to the old tunnel, which no longer claimed the hostname. It resolved on its own; don't go debugging the origin (the in-cluster Service answered correctly with the real `Host` header the whole time). Also note `ntfy.pub.example.com` sits behind a Cloudflare "Canada only" custom firewall rule shared with `plex.pub.example.com` — an unauthenticated publish returns ntfy's own JSON `403` (`code 40301`), which is *not* the geo-block; a geo-block returns Cloudflare's HTML block page instead.

## logging/

The whole centralized logging stack, fully in-cluster since 2026-07-22:

- **alloy.yaml** — DaemonSet collector on every node (journal + syslog intake + pod logs), pushes to the in-cluster Loki Service. Has a blanket toleration for `dedicated`/NoSchedule taints so it covers gpu-02. The only per-host alloy left is the docker one on `cloud-01` (not a cluster node yet); it pushes to `http://loki.lab.example` (Cloudflare A → cp-1, plain-HTTP IngressRoute — the cloud-01→cp-1:80 hop needed a new SG rule, TCP 80 from `198.51.100.0/24`, same gotcha as the 443 one).
- **loki.yaml** — migrated from docker on cloud-01. S3-backed (bucket `loki-logs-example-com`), so the move was a writer swap: graceful-stop the cloud-01 container (flushes WAL to S3), start this one on the same bucket; alloy clients buffered through the gap. PVC is scratch only. Pinned to gpu-01. `loki-s3` secret created imperatively (env-file piped into kubectl).
- **grafana.yaml** — migrated from docker on cloud-01; the data volume (dashboards/users sqlite) was tarred across into the local-path PVC dir on gpu-01. `grafana.lab.example` is a pfSense/Unbound host override → cp-1; `grafana.pub.example.com` OTP twin hops via cloud-01 Traefik → cp-1 (netbox.yml pattern, old config at `cloud-01:~/web-docker/grafana.yml.bak-20260722-pre-k3s`).

Post-cutover gotcha (second time it bit in one day): cloud-01's systemd-resolved had cached the old `*.lab.example` answers — Kuma monitors 404'd against cloud-01 Traefik until `resolvectl flush-caches`. The Kuma "Loki" monitor was also still polling `pi-01:3100` (stale since an even earlier move) — now `http://loki.lab.example/ready`.

## frigate/

Frigate NVR (0.16.3-tensorrt, ONNX detector w/ yolov9-t-320), migrated 2026-07-22 from a plain docker container on gpu-02. gpu-02 joined the cluster the same day as a dedicated node (taint `dedicated=frigate:NoSchedule`, label `gpu=nvidia`, node name kept as `gpu-02` — it's pinned hardware, not an interchangeable workerN).

Deliberately *not* a normal cluster workload:

- **Pinned to gpu-02** (`nodeSelector` on hostname): the RTX 3060 is there, the camera VLAN is reachable from there, and `/home/ludorl82/opt/frigate` (config + ~78G recordings) is local disk.
- **hostPath, not PVC**: the Cronicle jobs (frigate_nas_backup, lowres prune/sync, recording watchdog) SSH into gpu-02 and operate on those exact host paths; keeping them means zero changes to that pipeline.
- **hostNetwork**: keeps 1984/5000/8554/8555 on gpu-02's own IP, so the recording watchdog (`127.0.0.1:5000`), HA and RTSP/WebRTC consumers kept working unmodified through the migration.
- **`runtimeClassName: nvidia`** (see `runtimeclass.yaml`): k3s auto-detected the nvidia-container-runtime already present on gpu-02; the RuntimeClass object itself is manual. No device-plugin/`nvidia.com/gpu` resource — the image's own `NVIDIA_VISIBLE_DEVICES=all` does the exposure, same as the old docker `--gpus` setup.

`frigate-credentials` secret (RTSP user/password + Plus API key) was created imperatively by piping the old container's env straight into `kubectl` — values live in KeePass and the cluster only, never in a file.

**firewalld gotcha (cost an outage on migration day):** gpu-02 runs firewalld, and docker's firewalld integration had been silently punching through the published ports — k3s does no such thing, so after cutover every remote source got connection-refused on 5000/8554/8555/1984 (Kuma alerts + 502 from the old cloud-01-Traefik front). Local curls kept working, which made it look healthy from the node. Fix: 5000/1984/8554/8555(tcp+udp) + 8472/udp (flannel vxlan) opened in the `public` zone, cluster CIDRs (10.42.0.0/16, 10.43.0.0/16) added as `trusted` sources. Any future service on a firewalld host needs this treatment when it moves from docker to k3s.

**Access paths (since 2026-07-22):** `frigate.lab.example` → cp-1 → in-cluster IngressRoute (`ingressroute.yaml`, cronicle-style) — note this name is a pfSense/Unbound **host override**, not a Cloudflare record (unlike cronicle/n8n which are Cloudflare A records; the lab.example zone is a mix of both). The `frigate.pub.example.com` OTP twin keeps its tunnel path but cloud-01 Traefik now hops into k3s (`https://203.0.113.130`, netbox.yml-style) instead of hitting gpu-02 directly; old cloud-01 config backed up at `cloud-01:~/web-docker/frigate.yml.bak-20260722-pre-k3s-ingress`. RTSP (8554) and WebRTC (8555) consumers still hit gpu-02's host IP directly — hostNetwork, no ingress involved.

Disk on gpu-02 is the thing to watch: recordings share the 98G root FS with containerd images (the tensorrt image alone is ~10G). The old docker image copy and build cache were purged during migration; if the node ever hits kubelet's eviction thresholds, recordings retention (7d) is the lever.

## access-audit/

Hourly `CronJob` (`cloudflare-access-gating-audit`, not suspended — runs natively via k8s's own controller, no Cronicle involved) that queries the Cloudflare API for every Tunnel-proxied (`proxied: true`, CNAME → `*.cfargotunnel.com`) hostname in the `pub.example.com` zone, diffs it against the zone's configured Access Apps, and reports pass/fail to a Kuma push monitor ("Cloudflare Access Gating Audit"). Catches a newly added `pub.example.com` service that forgets to get an Access app wired up.

Exceptions (hostnames deliberately not Access-gated, hardcoded in the script): `plex.pub.example.com` (own auth + geo-block WAF instead) and `ntfy.pub.example.com` (needs unauthenticated API access for push). `cloud-01.pub.example.com`/`cp-1.pub.example.com` aren't candidates at all — they're plain unproxied A/AAAA host records, not Tunnel-routed web services.

Secret expected in the `access-audit` namespace before applying: `cloudflare-access-audit-token` (key `token`) — a dedicated, read-only Cloudflare API token (Zone:DNS:Read + Zone:Access Apps and Policies:Read, scoped to the `pub.example.com` zone only; KeePass "Cloudflare Access Audit Token"), not the shared general-purpose token.

## labodeludo/

Just the self-hosted GitHub Actions runner for `ludorl82/labodeludo.dev` now. It was declared here on 2026-08-07; before that the directory held the staging site and the runner that deployed it was live-only, created imperatively on 2026-07-17 and never committed.

**Staging moved off the cluster 2026-08-07.** `dev.labodeludo.dev` was an `nginx:alpine` Deployment serving `/usr/share/nginx/html` off `dev-labodeludo-data` — a 5Gi RWX claim against a hand-made NFS PV, the cluster's **only** static PV and **only** RWX claim — and the deploy job wiped the mount and `kubectl cp`'d a fresh `dist/` in on every push. The content is static output; it needed neither the volume, nor the pod, nor a node. It is a Cloudflare Pages project (`labodeludo-dev`, production branch `staging`) now, published straight from CI. Deleted with it: the Deployment, Service, `dev-labodeludo-nginx` ConfigMap, the `dev-labodeludo` IngressRoute (which had also only ever existed live, never in git), the PVC and the PV.

The PV was `Retain`, so `/k8s/dev-labodeludo-data` on the NAS still holds the last build. Nothing reads it.

The runner's RBAC went too. `runner-sa` was bound to `runner-role`, granting get/list/watch/patch/update/create on deployments, pods, `pods/exec` and `pods/log` — exactly what `kubectl exec` + `kubectl cp` needed. Only the staging deploy used it; `deploy-prod` builds and talks to S3. The Deployment names `serviceAccountName: default` explicitly rather than dropping the field, because an omitted field is not a removal — `kubectl diff` reported *no change at all* when the line was simply deleted, leaving `runner-sa` bound in place.

**Watch out when deleting things from this directory.** Removing the live objects before removing the manifests does not work: the app is `automated` with `prune: false` **and** `selfHeal: false`, and Argo re-created the whole staging Deployment ~90 seconds after `kubectl delete`. That is the unexplained self-heal-without-selfHeal behaviour already logged in `net-cfgs/backlog.md`. Order that works: delete the files, push, let the app sync, *then* `kubectl delete` the orphans — `prune: false` means Argo will never remove them for you.

## cluster-dns/

Fixes a cluster-wide gap where no pod could resolve any `*.lab.example` hostname (CoreDNS forwarded to whatever the underlying node's own `/etc/resolv.conf` had, which varies per node — e.g. `cp-1` is an AWS EC2 instance using AWS's VPC resolver, with no knowledge of the LAN-only Unbound overrides some `.lab.example` names rely on). Pins CoreDNS's upstream forward to `192.0.2.254` (pfSense/Unbound) instead, which resolves both Cloudflare-published and LAN-only `.lab.example` records correctly from every node.

This ConfigMap is normally managed by k3s's own `coredns` Addon — a k3s upgrade could revert it back to `forward . /etc/resolv.conf`. If `.lab.example` resolution from inside pods breaks again after a cluster upgrade, re-apply this file and delete the CoreDNS pod to force a fresh reload (`reload` plugin live-reload was observed to not reliably pick up the ConfigMap change on its own).

## hooks/

`hooks/pre-commit` — same hook as the `cloud-01-iac` / `cloudflare-iac` repos. Enable it once per clone:

```sh
git config core.hooksPath hooks
```

There is no flake here to do it for you, so a fresh clone has the hook **off** until you run that.

It checks staged content for files that should never be committed (state files, `*.pem`, `id_rsa`, kubeconfigs), credential shapes (AWS keys, Google/GitHub tokens, PEM private keys, Cloudflare tokens), and YAML/JSON/shell syntax. The Terraform checks are inert here.

YAML syntax checking needs one of python3+pyyaml, ruby, or yq on PATH. If none is present the hook says so rather than passing quietly — a silent skip on a repo that is almost entirely manifests would be worse than no check at all.

Bypass with `git commit --no-verify`.
