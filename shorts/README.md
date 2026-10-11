# shorts — оригинальные YouTube Shorts по темам трендов

MVP-пайплайн: найти недавние популярные кандидаты в AI Shorts (YouTube Data API) → проанализировать
доступные материалы → написать **новый** сценарий на похожую тему → сгенерировать сцены через NVIDIA
(Cosmos3 Generator) → собрать вертикальное видео FFmpeg → проверить → загрузить на свой канал.

Перед настройкой прочитайте [docs/capabilities.md](../docs/capabilities.md): **hosted-endpoint генерации
cosmos3-nano не задокументирован** — без `NIM_VIDEO_URL` этап генерации не работает (ошибка, а не mock).
Эксплуатация, бюджет, бэкап: [docs/shorts-operations.md](../docs/shorts-operations.md).

## Что безопасно по умолчанию

`DRY_RUN=true`, `AUTO_PUBLISH=false`, `BUDGET_VIDEO_REQUESTS_PER_DAY=0`, `PUBLISH_MADE_FOR_KIDS` не задан,
CronJob `suspend: true`. Production (`APP_ENV=prod`) отказывается запускаться с mock-провайдерами или SQLite.

## Локальная разработка

```bash
cd shorts
python3.13 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check src tests && .venv/bin/pytest -q          # без ffmpeg e2e-тесты пропускаются
# полный набор (ffmpeg + PostgreSQL) в linux/arm64-контейнере:
docker build --platform linux/arm64 --target test -t shorts-test .
docker run --rm shorts-test
```

Сухой прогон без сети и расходов (fixture-тренды, mock-модели, синтетическое видео; нужен ffmpeg):

```bash
APP_ENV=dev PROVIDER_MODE=mock TTS_PROVIDER=mock TREND_SOURCE=fixture \
TREND_FIXTURE_FILE=examples/trends_fixture.json BUDGET_VIDEO_REQUESTS_PER_DAY=10 \
NIM_VIDEO_RESOLUTION=256_9_16 .venv/bin/shorts run --dry-run
.venv/bin/shorts status
```

## Настройка

### 1. NVIDIA

1. Ключ `nvapi-…` создаётся на build.nvidia.com («Get API Key»). Не вставляйте его в чат и не коммитьте.
2. Перед использованием trial прочтите NVIDIA API Trial Terms в своём аккаунте (см. capabilities §2).
3. Текст: `NIM_TEXT_MODEL` (по умолчанию `nvidia/nemotron-3-super-120b-a12b`), анализ: `NIM_VISION_MODEL`.
4. Видео: укажите `NIM_VIDEO_URL` — URL `…/v1/infer` из кабинета NVIDIA (если появится для вашего ключа)
   или self-hosted Cosmos3 Generator NIM на GPU-хосте. Для async-ответов `202 + NVCF-REQID` —
   `NIM_VIDEO_ASYNC_STATUS_URL=https://…/{request_id}`, только если он задокументирован для вашего endpoint.
5. Бюджет: `BUDGET_VIDEO_REQUESTS_PER_DAY` (> 0 включает генерацию), `BUDGET_VIDEO_REQUESTS_PER_JOB`.

### 2. YouTube

1. Google Cloud: включить **YouTube Data API v3**; создать API key (поиск) и OAuth client типа **Desktop app**.
2. OAuth consent screen: добавить себя в test users. В статусе «Testing» refresh token живёт 7 дней —
   для постоянной работы переведите приложение в «In production» (scopes `youtube.upload`, `youtube.readonly`).
3. Авторизация владельца канала (на своём компьютере, токен пишется в файл 0600 и не печатается):

   ```bash
   mkdir -p ../secrets
   YOUTUBE_CLIENT_ID=… YOUTUBE_CLIENT_SECRET=… .venv/bin/shorts auth init --out ../secrets/youtube-token.json
   ```

4. `YOUTUBE_CHANNEL_ID` — id канала (`UC…`); загрузка отказывает, если токен принадлежит другому каналу.
5. Аудитория: `PUBLISH_MADE_FOR_KIDS=false|true` — обязателен для любой реальной загрузки.
6. Автопубликация (после первой приватной загрузки): `AUTO_PUBLISH=true`,
   `PUBLISH_CONSENT_CHANNEL_ID=<тот же UC…>`, `PUBLISH_PRIVACY=private|unlisted|public`,
   `PUBLISH_TIME_UTC=HH:MM` (по желанию, только для public), `PUBLISH_DAILY_LIMIT=1`.
   Первая реальная загрузка всегда private, даже при автопубликации.

### 3. Хранилище (S3-совместимое)

Любой S3 API (Yandex Object Storage, Cloudflare R2, MinIO/Garage в LAN). Нужны bucket, ключ с правами
на `S3_PREFIX`, `S3_ENDPOINT_URL`, `S3_REGION`. Структура: `jobs/<job>/scenes/*.mp4`, `jobs/<job>/final.mp4`,
`backups/YYYY-MM-DD/shorts.dump`, `reference/<video_id>/manifest.json` (разрешённые материалы).
Рекомендуется lifecycle-правило bucket на `jobs/` (ARTIFACT_RETENTION_DAYS) как вторая линия очистки.

### 4. Кластер (k3s + Argo CD)

```bash
export KUBECONFIG=~/.kube/homelab.yaml
cp shorts/.env.example secrets/shorts.env      # заполнить; secrets/ в .gitignore
GHCR_TOKEN=<PAT read:packages> scripts/create-shorts-secrets.sh secrets/shorts.env
# Application apps/shorts.yaml подхватывается root app (app-of-apps); если root ещё не применён:
kubectl apply -f bootstrap/root-app.yaml
```

Образ публикует workflow `shorts` (arm64, GHCR) и открывает PR с digest. До первого merge digest — нули,
CronJob приостановлен.

## Первый dry-run в кластере

CronJob приостановлен; разовые команды запускает `scripts/shorts-job.sh` (Job из шаблона CronJob:
тот же образ, Secret'ы, лимиты):

```bash
export KUBECONFIG=~/.kube/homelab.yaml
scripts/shorts-job.sh doctor            # без внешних запросов; exit 2 = есть блокеры
scripts/shorts-job.sh discover          # поиск: 1–N вызовов search.list, без генерации
scripts/shorts-job.sh run --dry-run     # весь путь до validated, без загрузки
scripts/shorts-job.sh status
```

Без `NIM_VIDEO_URL` и бюджета генерации dry-run остановится на `scripted` с причиной — это ожидаемо.

## Приватный smoke-test

Локально из `shorts/` с заполненным `../secrets/shorts.env` (значения не печатаются; строки с незаполненными `<…>` удалите — иначе `source` воспримет `<` как перенаправление):

```bash
set -a; . ../secrets/shorts.env; set +a
.venv/bin/shorts doctor --live                                  # /v1/models, 1 текстовый запрос (8 токенов), health генератора, videos.list (1 unit), OAuth + канал
BUDGET_VIDEO_REQUESTS_PER_DAY=1 .venv/bin/shorts smoke-test --i-accept-cost   # ОДНА платная генерация, 25 кадров 256_9_16
DRY_RUN=false AUTO_PUBLISH=false PUBLISH_MADE_FOR_KIDS=false BUDGET_VIDEO_REQUESTS_PER_DAY=6 .venv/bin/shorts run
.venv/bin/shorts status <job>                                   # до published; видео приватное
```

В кластере то же: `DRY_RUN=false`, `BUDGET_VIDEO_REQUESTS_PER_DAY=6`, `PUBLISH_MADE_FOR_KIDS` — в ConfigMap/Secret,
затем `scripts/shorts-job.sh run`. Локальный запуск требует ffmpeg с libx264/aac и шрифт (`FONT_FILE`).

## Команды

| Команда | Назначение |
|---|---|
| `doctor [--live]` | конфигурация, ffmpeg, БД, хранилище, блокеры; `--live` — бесплатные/дешёвые реальные запросы |
| `smoke-test --i-accept-cost` | одна реальная генерация 25 кадров |
| `discover` | поиск кандидатов: область поиска, время, формат, AI-сигналы |
| `run [--dry-run] [--job ID] [--new-upload-session]` | запуск/продолжение пайплайна |
| `status [JOB]`, `review` | состояние, события, внешние операции, бюджет; очередь проверки |
| `approve JOB`, `reject JOB --reason` | решение по `review_required` |
| `retry JOB --stage S [--regenerate-scenes 1,2]` | повтор с этапа без повторной оплаты готовых сцен |
| `reconcile JOB`, `resolve-op OP --note [--artifact F]` | сверка `generation_unknown` |
| `auth init --out FILE` | OAuth владельца канала |
| `purge`, `backup-upload FILE`, `migrate` | обслуживание |

## Структура

```
src/shorts_factory/
  config.py models.py store.py budget.py net.py log.py storage.py ff.py
  trends/ (TrendSource: youtube, fixture, classify)   media.py (ReferenceMediaSource)
  providers/ (nim_chat text+vision, nim_video Cosmos3, tts, mock)
  prompts/ (*_v1.md, версия в заголовке)   analysis.py scripting.py render.py qc.py
  youtube/ (oauth, resumable upload)   pipeline.py factory.py cli.py
examples/   входные/выходные JSON (валидируются тестом)
```
