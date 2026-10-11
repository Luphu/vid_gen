---
name: k3s-arm64-gitops
description: Use when adding or changing Kubernetes manifests, Kustomize overlays, Argo CD Applications, container images or GitHub Actions in this repo (k3s on 3× Raspberry Pi 5, 4 GB, arm64, Argo CD app-of-apps). Covers resource sizing, node placement, secrets outside Git, image digests and manifest validation.
---

# k3s + arm64 + Argo CD in this repo

## When to use
- Editing `apps/*.yaml`, `bootstrap/`, `charts/`, `deploy/**`, `.github/workflows/*`, any Dockerfile.
- Choosing requests/limits, node placement, storage, or adding a new service to the cluster.

## Verified facts (2026-10-11, `kubectl get nodes`, `kubectl top nodes`)
- Nodes: `rpi1` control-plane (k3s server, srvmon, ~1.6 Gi used), `rpi2` worker (Argo CD, ~0.8 Gi),
  `rpi-node-3` worker (~0.5 Gi). Allocatable ≈ 3.9 Gi each. Labels: `homelab/role=worker|control`.
- k3s v1.36, containerd, StorageClass `local-path` (default, WaitForFirstConsumer). Traefik, metrics-server.
- Argo CD 10.10.2 chart, app-of-apps: `bootstrap/root-app.yaml` → `apps/`. Argo Workflows is NOT installed.
- Secrets are created outside Git by `scripts/create-*.sh`; Argo CD does not manage them.
- CI builds natively on `ubuntu-24.04-arm` and pushes to `ghcr.io/luphu/nim/*`; pull secret `ghcr-pull`.

## Rules
1. Every image must have a linux/arm64 variant — check `docker manifest inspect <img> | grep arm64`.
2. Own images in manifests are pinned by `@sha256:` digest; CI opens a PR to bump it. Never `:latest`.
3. Every container has a memory limit. Size per node, not per cluster: a pod must fit beside k3s +
   existing pods with ≥ 1 Gi headroom. Heavy batch work goes to `rpi-node-3`, never to `rpi1`.
4. Stateful pods on `local-path` are pinned with `nodeSelector kubernetes.io/hostname`; document that there is
   no HA and how to back up/restore. No SQLite on network storage.
5. Batch work = CronJob with `concurrencyPolicy: Forbid`, `activeDeadlineSeconds`, `ttlSecondsAfterFinished`,
   bounded `emptyDir.sizeLimit`, `readOnlyRootFilesystem`, non-root, `capabilities.drop: [ALL]`.
6. CI never gets a kubeconfig and never deploys; Argo CD syncs merged Git state.
7. Don't add Redis/Kafka/Temporal/Elasticsearch/monitoring stacks without a concrete need.
8. No plaintext secrets in Git; `.env.example` holds placeholders only.

## How to verify
```bash
kubectl kustomize deploy/shorts/overlays/k3s > /tmp/r.yaml
docker run --rm -v /tmp/r.yaml:/m.yaml ghcr.io/yannh/kubeconform:v0.6.7 -strict -summary /m.yaml
grep -n 'image:' /tmp/r.yaml          # own images must contain @sha256:
KUBECONFIG=~/.kube/homelab.yaml kubectl top nodes   # re-measure before changing limits (read-only)
docker build --platform linux/arm64 --target runtime -t x shorts && docker run --rm --memory=1536m x --help
```
Report which checks ran; cluster changes are only "verified" after Argo CD sync and a successful Job.
