# shorts: эксплуатация

## Архитектура

```
CronJob shorts-pipeline (5×/сутки 9–21 Asia/Tbilisi, по ролику за запуск, Forbid, backoffLimit 0)
  └─ shorts run ── lease "pipeline" в PostgreSQL (одно видео одновременно)
       discover (YouTube Data API) → analyze (Replicate text, ReferenceMediaSource)
       → script (Replicate Qwen3) → generate (Replicate Wan 2.2) → TTS (Replicate MiniMax)
       → render (FFmpeg) → validate (ffprobe/QC)
       → upload (YouTube resumable) → publish (processing status)
StatefulSet shorts-postgres (1 реплика, local-path PVC на rpi-node-3)
PVC shorts-artifacts 20Gi (rpi-node-3): /data/storage/jobs/<job>/…
CronJob shorts-purge; shorts-db-backup → PVC shorts-backups на rpi2
```

Нет Redis/Kafka/Temporal/Argo Workflows: очередь — таблица `jobs`, блокировка — таблица `locks`,
учёт внешних операций — `external_ops`, бюджет — `budget_ledger`.

## Состояния задания

| Состояние | Значение | Что делать |
|---|---|---|
| `discovered` … `validated` | этап завершён, следующий ещё нет | ничего: следующий запуск продолжит |
| `uploaded` | видео загружено, ждём обработки YouTube | ничего |
| `published` | обработка успешна, приватность применена | — |
| `done_dry_run` | DRY_RUN: всё, кроме загрузки | проверить `shorts status <job>` |
| `review_required` | неясная AI-классификация, отказ проверки, отклонённый сценарий | `shorts review`, затем approve / retry / reject |
| `generation_unknown` | платный запрос мог выполниться, результата нет | `shorts reconcile <job>`; если не помогло — проверить провайдера и `shorts resolve-op` |
| `upload_unknown` | загрузка прервана, исход неизвестен | проверить YouTube Studio, затем `shorts retry <job> --stage upload` и `shorts run --job <job> --new-upload-session` |
| `failed` | отклонено оператором или нет данных кандидата | — |

Повтор этапа: `shorts retry <job> --stage analyze|script|generate|render|validate|upload|publish`,
затем `shorts run --job <job>`. Успешно сгенерированные сцены (`external_ops.status=succeeded` + файл в хранилище артефактов)
не генерируются повторно. После `uploaded` повтор ранних этапов запрещён (защита от дубликата).

## Бюджет

* Лимиты в `BUDGET_*` (ConfigMap), учёт в `budget_ledger` по суткам: NVIDIA — UTC, YouTube — Pacific Time.
* Единица резервируется **до** запроса и не возвращается при ошибке (консервативно).
* Генерация видео выключена, пока `BUDGET_VIDEO_REQUESTS_PER_DAY=0` (по умолчанию).
* Расчёт на ролик: 15–30 с / ≤ 7,6 с на сцену (Wan 2.2: 121 кадр при 16 fps) → 3–5 запросов генерации
  + ≤ 1 повтор на сцену → `BUDGET_VIDEO_REQUESTS_PER_JOB=6`; на сутки при 5 роликах — `BUDGET_VIDEO_REQUESTS_PER_DAY=30`
  (≈ $3 максимум при $0.10/клип 720p).
* YouTube на ролик: `search.list` × (ключевые слова × страницы), `videos.list` 1–2 units, `videos.insert` 1 вызов,
  `channels.list`/`videos.list` для проверки канала и статуса — единицы.
* Стоимость кредитов NVIDIA в деньгах не известна коду; смотрите кабинет NVIDIA.
* `shorts status` показывает расход за текущие сутки.

## Ресурсы Raspberry Pi 5 (4 ГБ)

Измерено 2026-10-11 (`kubectl top nodes`): rpi1 1634Mi (k3s server, srvmon), rpi2 806Mi (Argo CD),
rpi-node-3 481Mi. Поэтому PostgreSQL и пайплайн — на rpi-node-3 (pipeline — preferred, не на control plane).

| Под | requests | limits | Обоснование |
|---|---|---|---|
| shorts-postgres | 64Mi | 256Mi | `shared_buffers=32MB`, `max_connections=20`, одна запись за раз |
| shorts-pipeline | 512Mi / 500m | 1536Mi / 2 CPU / 5Gi ephemeral | x264 2 потока 720×1280 + декодирование base64 одной сцены ≤ 96 MiB |
| purge/backup | 96Mi | 256Mi | |

Худший случай на rpi-node-3: ~1,8 Gi сверх текущих ~0,5 Gi — остаётся ≥ 1,5 Gi для k3s agent и системы.
Это расчёт, а не измерение: первое реальное выполнение проверить `kubectl top pod -n shorts`.
Аппаратного H.264-кодировщика у Pi 5 нет; используется libx264 `-preset veryfast`, `-threads 2`.
Временные файлы: emptyDir `/work` ≤ 4Gi, приложение отказывается работать при `MAX_WORK_DIR_BYTES` (3 GiB).

## PostgreSQL и артефакты: привязка, отсутствие HA, бэкап

* PostgreSQL и `shorts-artifacts` — `local-path` на SD-карте `rpi-node-3`; StatefulSet и CronJob'ы
  pipeline/purge закреплены `nodeSelector`. S3 не используется (его нет); код поддерживает S3 на будущее.
* При отказе rpi-node-3 пайплайн стоит до восстановления ноды. Без HA — осознанно.
* `shorts-db-backup` ежедневно 03:10: `pg_dump -Fc` → проверка `pg_restore --list` → PVC `shorts-backups`
  на **rpi2** (другая нода), хранение 14 дней. Потеря при отказе rpi-node-3 — до 24 ч состояния; ролики
  в `shorts-artifacts` (готовые видео — уже на YouTube) теряются вместе с нодой.
* Защиты от отказа SD-карты rpi2 нет; для внешней копии периодически копируйте файл к себе:
  `kubectl -n shorts cp` из пода бэкапа или `ssh luphu@192.168.100.72` + `/var/lib/rancher/k3s/storage/`.

Восстановление БД на новую ноду:

```bash
export KUBECONFIG=~/.kube/homelab.yaml
kubectl -n shorts scale statefulset shorts-postgres --replicas=0
kubectl -n shorts delete pvc data-shorts-postgres-0          # ТОЛЬКО если старая нода потеряна
# при необходимости поменять nodeSelector в deploy/shorts/overlays/k3s, merge → Argo CD создаст пустую БД
kubectl -n shorts scale statefulset shorts-postgres --replicas=1
# под с доступом к бэкапам (на rpi2):
kubectl -n shorts run restore --rm -it --image=postgres:17.6-alpine --overrides='{"spec":{"nodeSelector":{"kubernetes.io/hostname":"rpi2"},"volumes":[{"name":"b","persistentVolumeClaim":{"claimName":"shorts-backups"}}],"containers":[{"name":"restore","image":"postgres:17.6-alpine","stdin":true,"tty":true,"command":["sh"],"volumeMounts":[{"name":"b","mountPath":"/backup"}],"envFrom":[{"secretRef":{"name":"shorts-db"}}]}]}}'
#   внутри: pg_restore --clean --if-exists --no-owner -d "$DATABASE_URL" /backup/shorts-<date>.dump
```

После восстановления — `scripts/shorts-job.sh review`: задания в `generation_unknown`/`upload_unknown`
разрешаются вручную; `uploaded` после бэкапа может отсутствовать в БД — проверьте канал перед повтором загрузки.

## Секреты

Механизм репозитория — Secret'ы вне Git (`scripts/create-*.sh`). Для shorts: `scripts/create-shorts-secrets.sh
secrets/shorts.env`. Пароль PostgreSQL и `UPLOAD_URI_KEY` генерируются один раз и сохраняются при повторах.
Следующий шаг для всего кластера — Sealed Secrets (один контроллер ~30 Mi) — пока не внедрён.

Защита в приложении: `SecretStr`, редактирование логов (ключи `nvapi-`, Bearer, refresh/access tokens,
`upload_id`, пароли в URL), session URI загрузки — AES-GCM в БД, refresh token — только в Secret, OAuth
`auth init` пишет токен в файл 0600 и не печатает его.

## Устранение ошибок

| Симптом | Причина / действие |
|---|---|
| `video generator endpoint not configured` / `text provider not configured` | нет `REPLICATE_API_TOKEN` (или `NIM_VIDEO_URL` при VIDEO_PROVIDER=nvidia) |
| `replicate …: output already deleted (kept 1 hour)` | восстановление позже часа невозможно: `resolve-op … --note` и повтор (платно) |
| `budget for nim_video is 0` | задайте `BUDGET_VIDEO_REQUESTS_PER_DAY` |
| `HTTP 401/403 (check API key / entitlement)` | ключ NVIDIA, доступ к модели; не повторяется автоматически |
| `HTTP 404` от NIM | модель/URL недоступны для ключа; `shorts doctor --live` |
| `deferred: …HTTP 429` | лимит провайдера; следующий запуск продолжит этап |
| `OAuth refresh failed (invalid_grant)` | токен отозван/истёк (Testing-режим OAuth — 7 дней); `shorts auth init` |
| `token belongs to channel …` | OAuth выдан не тому каналу — загрузка заблокирована |
| `quality check failed: black_frames…` | плохая сцена: `shorts retry <job> --stage generate --regenerate-scenes 1` (платно), затем `shorts run --job <job>` |
| `MAX_WORK_DIR_BYTES` | `shorts purge`, проверить emptyDir |
| `pipeline is locked` | другой запуск активен; lease истекает через `JOB_LEASE_SECONDS` |
