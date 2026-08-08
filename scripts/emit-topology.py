#!/usr/bin/env python3
"""Emit topology.json for the public snapshot (cluster layer).

Runs against the ALREADY-SANITIZED tree (sanitize-public.sh calls this after
every substitution, before the verification gate), so everything it can read
is already fictional — and the gate re-scans its output like any other file
in the snapshot.

Walks the Argo CD Applications (argocd/apps/*.yaml) and each app's manifest
directory: workloads (Deployment/StatefulSet/DaemonSet/CronJob) with their
node pins, and IngressRoutes with their hostnames. Fail-closed: zero apps,
zero workloads, or an Application whose source path has no manifests kills
the run rather than emitting a partial layer.

Contract: labodeludo.dev scripts/topology/README.md (topologyVersion 1).
Requires PyYAML (pip install --user --break-system-packages pyyaml).
"""
import glob
import json
import os
import re
import sys

try:
    import yaml
except ModuleNotFoundError:
    print("emit-topology: PyYAML missing — pip install --user "
          "--break-system-packages pyyaml", file=sys.stderr)
    sys.exit(1)

def die(msg):
    print(f"emit-topology: {msg}", file=sys.stderr)
    sys.exit(1)

if len(sys.argv) != 2:
    die("usage: emit-topology.py <sanitized-tree>")
ROOT = sys.argv[1]

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "CronJob"}

def load_docs(path):
    with open(path, encoding="utf-8") as f:
        try:
            return [d for d in yaml.safe_load_all(f) if isinstance(d, dict)]
        except yaml.YAMLError as e:
            die(f"unparseable YAML in {path}: {e}")

def pod_spec(doc):
    spec = doc.get("spec", {})
    if doc.get("kind") == "CronJob":
        spec = spec.get("jobTemplate", {}).get("spec", {})
    return spec.get("template", {}).get("spec", {})

nodes = [{"id": "cluster:k3s", "kind": "cluster", "label": "k3s",
          "layer": "cluster", "source": "argocd/README.md", "meta": {}}]
edges = []
nas_users = set()  # app ids that lean on the NAS (NFS PVCs, plex endpoint)

S3_URI = re.compile(r"s3://([a-z0-9][a-z0-9.-]{2,62})")
# matches bucket-naming keys in every serialization the manifests embed:
# "Bucket": "x" (JSON-in-ConfigMap, arrives as \"Bucket\": \"x\" after
# json.dumps), bucketnames: x (YAML-in-string, loki), BUCKET=x (env-ish)
BUCKET_KEY = re.compile(
    r'bucket\w*\\?"?\s*[:=]\s*\\?"?([a-z0-9][a-z0-9.-]{2,62})', re.I)

def s3_refs(doc):
    """Bucket names a manifest justifies: s3:// URIs, "Bucket": "..." keys
    (covers config.json embedded in ConfigMaps), and *BUCKET* env vars.
    The join only turns these into edges when the cloud-01 layer actually
    declares a bucket with that name — no match, no edge."""
    text = json.dumps(doc)
    refs = set(S3_URI.findall(text)) | set(
        m.lower() for m in BUCKET_KEY.findall(text))

    def walk(obj):
        if isinstance(obj, dict):
            if ("BUCKET" in str(obj.get("name", "")).upper()
                    and isinstance(obj.get("value"), str)):
                refs.add(obj["value"].strip().lower())
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
    walk(doc)
    return refs

apps = sorted(glob.glob(os.path.join(ROOT, "argocd", "apps", "*.yaml")))
if not apps:
    die("no Argo CD Applications found under argocd/apps/")

n_workloads = 0
for app_file in apps:
    rel_app = os.path.relpath(app_file, ROOT)
    docs = [d for d in load_docs(app_file)
            if d.get("kind") == "Application"]
    if len(docs) != 1:
        die(f"{rel_app}: expected exactly one Application, got {len(docs)}")
    app = docs[0]
    name = app["metadata"]["name"]
    src_path = app["spec"]["source"]["path"].strip("/")
    namespace = app["spec"]["destination"].get("namespace", "")

    app_node = {"id": f"app:{name}", "kind": "app", "label": name,
                "layer": "cluster", "source": rel_app,
                "meta": {"namespace": namespace, "path": src_path}}
    nodes.append(app_node)
    edges.append({"from": f"app:{name}", "to": "cluster:k3s",
                  "kind": "part-of"})
    app_s3 = set()

    app_dir = os.path.join(ROOT, src_path)
    if not os.path.isdir(app_dir):
        die(f"{rel_app}: source path {src_path} is not a directory")
    manifests = sorted(glob.glob(os.path.join(app_dir, "**", "*.yaml"),
                                 recursive=True))
    if not manifests:
        die(f"{rel_app}: no manifests under {src_path}/")

    for mf in manifests:
        rel_mf = os.path.relpath(mf, ROOT)
        for doc in load_docs(mf):
            kind = doc.get("kind")
            meta_name = doc.get("metadata", {}).get("name", "")
            app_s3 |= s3_refs(doc)
            if kind in WORKLOAD_KINDS:
                ps = pod_spec(doc)
                meta = {"kind": kind}
                if ps.get("hostNetwork"):
                    meta["hostNetwork"] = True
                wid = f"workload:{name}/{meta_name}"
                dup = next((n for n in nodes if n["id"] == wid), None)
                if dup:
                    # same workload defined in two files (unifi's
                    # backup-cronjob.yaml vs mongo-backup-cronjob.yaml):
                    # later file wins, warn loudly — the repo should not
                    # carry duplicates at all
                    print(f"emit-topology: WARNING duplicate {wid} "
                          f"({dup['source']} superseded by {rel_mf})",
                          file=sys.stderr)
                    nodes.remove(dup)
                    edges[:] = [e for e in edges if e["from"] != wid]
                else:
                    n_workloads += 1
                nodes.append({"id": wid, "kind": "workload",
                              "label": meta_name, "layer": "cluster",
                              "source": rel_mf, "meta": meta})
                edges.append({"from": wid, "to": f"app:{name}",
                              "kind": "part-of"})
                pin = (ps.get("nodeSelector") or {}).get(
                    "kubernetes.io/hostname")
                if pin:
                    edges.append({"from": wid, "to": f"host:{pin}",
                                  "kind": "pinned-to"})
            elif kind == "PersistentVolumeClaim":
                # nfs-client is the fleet NFS export on the NAS (the class's
                # own comment says so) — hors-IaC hardware this app leans on
                if doc.get("spec", {}).get("storageClassName") == "nfs-client":
                    nas_users.add(f"app:{name}")
            elif kind == "EndpointSlice":
                # a hand-written EndpointSlice is by definition an
                # out-of-cluster backend; the only one today is plex on the
                # NAS (the Service's comment names it)
                if doc.get("endpoints"):
                    nas_users.add(f"app:{name}")
            elif kind == "IngressRoute":
                hosts = sorted(set(re.findall(
                    r"Host\(`([^`]+)`\)", json.dumps(doc))))
                if not hosts:
                    continue
                rid = f"route:{name}/{meta_name}"
                if any(n["id"] == rid for n in nodes):
                    continue  # http+https pairs share a name across docs
                nodes.append({"id": rid, "kind": "route", "label": meta_name,
                              "layer": "cluster", "source": rel_mf,
                              "meta": {"hosts": hosts}})
                edges.append({"from": rid, "to": f"app:{name}",
                              "kind": "part-of"})

    if app_s3:
        # candidate S3 buckets this app's manifests name; the join promotes
        # them to `uses` edges only when the cloud-01 layer declares the bucket
        app_node["meta"]["s3Refs"] = sorted(app_s3)

if n_workloads == 0:
    die("extracted zero workloads — parser broken or tree wrong")

# hors-IaC hardware, derived only from what the manifests reference — the
# join merges this node with the nixos layer's identical id
if nas_users:
    nodes.append({"id": "external:nas", "kind": "external", "label": "nas",
                  "layer": "external", "source": "plex/service.yaml",
                  "meta": {"via": "nfs-client PVCs, plex EndpointSlice"}})
    for uid in sorted(nas_users):
        edges.append({"from": uid, "to": "external:nas", "kind": "uses"})

out = {"topologyVersion": 1, "repo": "k3s-iac-public", "layer": "cluster",
       "nodes": nodes, "edges": edges}
with open(os.path.join(ROOT, "topology.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, sort_keys=True)
    f.write("\n")
print(f"emit-topology: {len(nodes)} nodes ({n_workloads} workloads), "
      f"{len(edges)} edges")
