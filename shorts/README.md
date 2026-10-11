# shorts — оригинальные YouTube Shorts по темам трендов

MVP-пайплайн: найти недавние популярные кандидаты в AI Shorts (YouTube Data API) → проанализировать
доступные материалы → написать **новый** сценарий на похожую тему → сгенерировать сцены через NVIDIA
(Cosmos3 Generator) → собрать вертикальное видео FFmpeg → проверить → загрузить на свой канал.

Перед настройкой прочитайте [docs/capabilities.md](../docs/capabilities.md): production-генерация идёт через
**Replicate**; NVIDIA trial — только для оценки (условия trial запрещают production).
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

### 1. Replicate (текст, видео, озвучка — production)

NVIDIA trial запрещает использовать сгенерированный контент в production (docs/capabilities.md §2), поэтому всё,
что попадает в публикацию, генерируется через Replicate:

1. Аккаунт на replicate.com → Billing: привязать оплату (модели платные, $0.10 за клип 720p).
2. Account → API tokens → создать токен `r8_…`. В чат не вставлять: только в `secrets/shorts.env`
   (`REPLICATE_API_TOKEN=…`).
3. Модели по умолчанию: `qwen/qwen3-235b-a22b-instruct-2507` (сценарий), `wan-video/wan-2.2-t2v-fast`
   (видео 9:16, 720p), `minimax/speech-02-turbo` (озвучка, голос `Russian_ReliableMan`).

NVIDIA (`NVIDIA_API_KEY`) остаётся для `doctor --live` и экспериментов (`TEXT_PROVIDER=nvidia`).

### 2. YouTube

**2.1. API key для поиска** — уже есть, но в его проекте Google Cloud выключен YouTube Data API v3.
Откройте console.cloud.google.com → выберите проект ключа → «APIs & Services» → «Library» → «YouTube Data API v3»
→ **Enable**. Рекомендуется ограничить ключ: «Credentials» → ключ → «API restrictions» → только YouTube Data API v3.

**2.2. Доступ на загрузку (OAuth)** — API key не умеет загружать видео; нужен вход владельца канала:

1. Тот же проект → «APIs & Services» → **OAuth consent screen**: тип *External*, название любое, ваш email;
   в «Test users» добавьте Google-аккаунт, которому принадлежит канал @whale_kiddo.
2. «Credentials» → **Create credentials → OAuth client ID** → тип **Desktop app** → скачайте/скопируйте
   `client_id` и `client_secret` в `secrets/shorts.env` (`YOUTUBE_CLIENT_ID=…`, `YOUTUBE_CLIENT_SECRET=…`).
3. На своём компьютере, из каталога `shorts/`:

   ```bash
   set -a; . ../secrets/shorts.env; set +a
   .venv/bin/shorts auth init --out ../secrets/youtube-token.json
   ```

   Откроется браузер: войдите аккаунтом канала и разрешите доступ (загрузка видео + чтение канала).
   Токен сохранится в файл с правами 0600, на экран не выводится. Пароль никуда, кроме страницы Google, не вводится.
4. В режиме «Testing» токен живёт 7 дней. Для постоянной работы: OAuth consent screen → **Publish app**
   (для личного использования проверка Google не обязательна, при входе будет предупреждение «unverified app»).

Канал: `YOUTUBE_CHANNEL_ID=UCGakgXaB9bPT374KLj1G0Xw` (@whale_kiddo) — уже в ConfigMap. Загрузка отказывает,
если токен принадлежит другому каналу. Контент: общая аудитория 13+, `PUBLISH_MADE_FOR_KIDS=false`.
Автопубликация (после первой приватной загрузки): `AUTO_PUBLISH=true`, `PUBLISH_PRIVACY=public`.

### 3. Хранилище

S3 не нужен: артефакты лежат на PVC `shorts-artifacts` (20Gi, local-path, rpi-node-3), бэкапы БД — на PVC
`shorts-backups` на rpi2. Готовые ролики удаляются через `ARTIFACT_RETENTION_DAYS` (14) — они уже на YouTube.

### 4. Кластер (k3s + Argo CD)

```bash
export KUBECONFIG=~/.kube/homelab.yaml
cp shorts/.env.example secrets/shorts.env      # заполнить; secrets/ в .gitignore
GHCR_TOKEN=<PAT read:packages> scripts/create-shorts-secrets.sh secrets/shorts.env   # YOUTUBE_TOKEN_FILE подхватится сам
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

Без `REPLICATE_API_TOKEN` dry-run остановится на `discovered` с причиной `configuration: text provider…`.
Dry-run тратит деньги на Replicate (сценарий, видео, озвучка) — не загружает только на YouTube.

## Приватный smoke-test

Локально из `shorts/` с заполненным `../secrets/shorts.env` (значения не печатаются; строки с незаполненными `<…>` удалите — иначе `source` воспримет `<` как перенаправление):

```bash
set -a; . ../secrets/shorts.env; set +a
.venv/bin/shorts doctor --live                                  # /v1/models, 1 текстовый запрос (8 токенов), health генератора, videos.list (1 unit), OAuth + канал
BUDGET_VIDEO_REQUESTS_PER_DAY=1 .venv/bin/shorts smoke-test --i-accept-cost --resolution 480p   # ОДНА платная генерация (~$0.05)
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
