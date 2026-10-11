# nim — домашний k3s-кластер (GitOps)

Три Raspberry Pi 5 (4 ГБ, SD, Wi-Fi), k3s + Argo CD. Все приложения описаны в этом репозитории, Argo CD синхронизирует кластер с веткой `main`.

| Нода | IP | Роль |
|---|---|---|
| rpi1 | 192.168.100.71 | k3s server (SQLite), srvmon |
| rpi2 | 192.168.100.72 | k3s agent, Argo CD |
| rpi-node-3 | 192.168.100.73 | k3s agent |

## Почему k3s + Argo CD

* **k3s** — сертифицированный Kubernetes в одном бинарнике; на одном server с SQLite вместо etcd меньше нагрузка на RAM и SD. Есть Traefik, local-path, metrics-server и NetworkPolicy. kubeadm с etcd на SD и MicroK8s (snap, dqlite) тяжелее. Nomad и Swarm не учат Kubernetes.
* **Argo CD** — GitOps-контроллер с UI. Здесь урезан: без Dex, notifications и ApplicationSet. Flux легче примерно вдвое, но выбран Argo CD.

## Структура

```
bootstrap/        установка Argo CD (Helm values) и корневое Application
apps/             Application'ы (app-of-apps): argocd (самоуправление), srvmon
charts/srvmon/    Helm-чарт мониторинга сервера в Yandex Cloud
images/           Dockerfile'ы tunnel (autossh) и report (python), собираются CI в GHCR
scripts/          create-secrets.sh — Secret'ы вне Git
```

## srvmon

```
YC server: vmagent (буфер 512 МБ) ─► 127.0.0.1:18428 ══ssh -R══ pod srvmon-tunnel ─► svc srvmon-vm:8428
                                                              │
                         vmalert ──► Alertmanager ──► Telegram │  VictoriaMetrics (1y, PVC на rpi1)
                         CronJob srvmon-report 19:59 Tbilisi ─► xlsx в Telegram
                         DaemonSet node-exporter на всех нодах (job=rpi-node)
```

## Развёртывание с нуля

```bash
# 1. k3s
ssh luphu@192.168.100.71 'curl -sfL https://get.k3s.io | sudo sh -s - server --node-ip=192.168.100.71 --flannel-iface=wlan0 --tls-san=192.168.100.71'
# agents: K3S_URL=https://192.168.100.71:6443 K3S_TOKEN=<node-token> ... agent --node-ip=<ip> --flannel-iface=wlan0 --node-label=homelab/role=worker
# 2. Argo CD
helm upgrade --install argocd argo/argo-cd --version 10.10.2 -n argocd --create-namespace -f bootstrap/argocd-values.yaml
# 3. Доступ Argo CD к репо: Secret с deploy key (read-only), label argocd.argoproj.io/secret-type=repository
# 4. Секреты приложений
GHCR_TOKEN=<PAT read:packages> scripts/create-secrets.sh
# 5. Всё остальное — из Git
kubectl apply -f bootstrap/root-app.yaml
```

UI: http://argocd.192.168.100.71.sslip.io (admin, пароль в `argocd-initial-admin-secret`).

## Эксплуатация

```bash
export KUBECONFIG=~/.kube/homelab.yaml
kubectl -n srvmon get pods,cronjob,pvc
kubectl -n srvmon logs deploy/srvmon-tunnel
kubectl -n srvmon create job --from=cronjob/srvmon-report report-manual   # отчёт вручную
kubectl -n srvmon port-forward svc/srvmon-vm 8428                          # VictoriaMetrics (basic auth)
```

## Известные ограничения

* Нет HA: при отказе rpi1 недоступны control plane и srvmon (данные на её SD).
* Секреты вне Git; следующий шаг — Sealed Secrets или SOPS.
* node-exporter слушает LAN-IP нод на :9100 (hostNetwork).
* Сетевой доступ к srvmon ограничен NetworkPolicy (только внутри namespace).
