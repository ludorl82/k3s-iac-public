# Sanitized snapshot

This is a **sanitized, read-only snapshot** of the private repository that
holds my homelab's k3s workload manifests, published as the companion to
labodeludo.dev articles:

- ["Unplugging the NAS for science"](https://labodeludo.dev/en/blog/debrancher-le-nas-pour-la-science/)
  ([français](https://labodeludo.dev/blog/debrancher-le-nas-pour-la-science/)) —
  the liveness-probe work: see the probe blocks and their comments in
  `logging/loki.yaml`, `unifi/deployment.yaml`, `numeriseur/deployment.yaml`,
  `cronicle/deployment.yaml`, `logging/grafana.yaml`,
  `labodeludo/dev-labodeludo.yaml`, `kuma/deployment.yaml`,
  `ntfy/deployment.yaml`.
- ["Nine machines, zero USB sticks"](https://labodeludo.dev/en/blog/migrer-tout-mon-homelab-vers-nixos/)
  ([français](https://labodeludo.dev/blog/migrer-tout-mon-homelab-vers-nixos/)).

Hostnames, addresses (RFC 5737 / RFC 3849), SSH keys, certificates, push
tokens and tunnel ids are all fictional; every operational comment is real.
It is published to be read, not deployed — the manifests will not apply
cleanly against your cluster as-is, and the repository is not maintained in
sync with the private original.
