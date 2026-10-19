# argocd/

Argo CD deploys this repo: merge to `main` is the deploy. PR review is the
only gate — there is deliberately no second approval after merge.

## Shape

- `install/` — Argo CD itself, kustomize over the pinned upstream
  `cluster-install` manifests. Bootstrap: `kubectl apply -k argocd/install`.
  After bootstrap Argo manages its own upgrades via `apps/argocd.yaml`.
- `root-app.yaml` — the app-of-apps, applied by hand exactly once
  (`kubectl apply -f argocd/root-app.yaml`). It syncs `apps/` automatically.
- `apps/` — one `Application` per top-level directory of this repo.

## Doctrine (why the sync policy looks like this)

- **`automated: {prune: false, selfHeal: false}`** — sync happens when git
  changes and only then. Live drift is *shown* (replacing the nightly
  `kubectl diff` half of iac-drift, Kuma 41) but never auto-reverted:
  operator state is not drift. Nothing is ever pruned; deletions are done
  deliberately by a human.
- **`ignoreDifferences` on `spec.replicas`** (+ `RespectIgnoreDifferences`) —
  a scaled-to-zero workload is maintenance intent. Even a merge touching that
  app must not scale it back up.
- Secrets stay out-of-band (`kubectl create secret`), same as always — Argo
  never sees them, apps reference them by name.

## Repo access

Argo reads the (private) repo over SSH with a read-only GitHub deploy key.
The key lives only in the `argocd` namespace, out-of-band like every secret:

```
kubectl -n argocd apply -f - <<'EOF'   # after generating a keypair; public
                                       # half added as a read-only deploy key
apiVersion: v1
kind: Secret
metadata:
  name: repo-k3s-iac
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
stringData:
  type: git
  url: git@github.com:ludorl82/k3s-iac.git
  sshPrivateKey: |
    <private key>
EOF
```

## Rollout state

Child apps are currently **manual sync** (no `automated` block): first pass
is reviewing each app's live-vs-git diff in the UI and syncing by hand. Flip
apps to `automated: {prune: false, selfHeal: false}` one at a time once their
diffs are clean/understood. UI via `kubectl -n argocd port-forward svc/argocd-server 8080:443`
(no IngressRoute yet — deliberate until the rollout settles).
