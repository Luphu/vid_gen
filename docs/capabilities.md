# Возможности внешних сервисов (проверка до реализации)

Дата проверки: **2026-10-11**. Источники перечислены у каждого раздела.
Обозначения:

* **[проверено]** — подтверждено запросом из этой рабочей среды (без API key, только публичные данные);
* **[документировано]** — указано в официальной документации, запросом не проверялось;
* **[не подтверждено]** — документации нет или она противоречива; код не полагается на это без `doctor`;
* **[блокер]** — без этого этап не может работать в production.

Реальные запросы с ключом в этой сессии **не выполнялись**: ключ не читался и не передавался.
Их выполняет `shorts doctor --live` после настройки (см. README).

## 1. NVIDIA: модели и endpoint

### 1.1 Текст: сценарий и метаданные

| | |
|---|---|
| Endpoint | `POST https://integrate.api.nvidia.com/v1/chat/completions` (OpenAI-совместимый) |
| Список моделей | `GET https://integrate.api.nvidia.com/v1/models` — **[проверено]** без ключа, 80 моделей на 2026-10-11 |
| Модель по умолчанию | `nvidia/nemotron-3-super-120b-a12b` — **[проверено]** присутствует в списке |
| Альтернативы из списка | `nvidia/nemotron-3.5-lightning-30b-a3b`, `nvidia/nemotron-nano-3-30b-a3b`, `openai/gpt-oss-20b` |
| Формат результата | `choices[0].message.content` — текст; JSON просим промптом и **всегда валидируем** pydantic-схемой |
| Structured output | `response_format`/guided JSON для конкретной hosted-модели **[не подтверждено]** → выключено по умолчанию (`NIM_TEXT_RESPONSE_FORMAT=none`) |
| Рассуждения | Некоторые модели возвращают `<think>…</think>` или `reasoning_content`. Код удаляет их и **не сохраняет** |

Наличие модели в `/v1/models` не гарантирует доступность для конкретного ключа, квоту или стабильность —
это проверяет `doctor --live` (один короткий запрос, `max_tokens=8`).

### 1.2 Анализ изображений и видео (VLM)

| | |
|---|---|
| Кандидат из задания | `nvidia/cosmos3-nano-reasoner` |
| Статус в hosted-каталоге | **[проверено]** отсутствует в `GET /v1/models` на 2026-10-11. Страница build.nvidia.com существует; рабочий hosted-вызов **[не подтверждено]** |
| Присутствует в каталоге | `nvidia/cosmos-reason2-8b` **[проверено]** — модель по умолчанию в конфигурации (`NIM_VISION_MODEL`) |
| Документированный API (self-hosted NIM) | `POST /v1/chat/completions`, контент `video_url` (HTTP(S) URL или `data:video/mp4;base64,…`), `video_frames` (массив JPEG data URL), `image_url`; `media_io_kwargs.video.fps` **или** `num_frames` (взаимоисключающие, по умолчанию 4 fps) **[документировано]** |
| Ограничения | Лучше всего при ≤16k мультимодальных токенов; максимальный размер/длительность видео не указаны **[документировано]** |

Решение: адаптер `NimVisionClient` отправляет **кадры** (`image_url`, до `ANALYSIS_MAX_FRAMES`, по умолчанию 8,
сторона ≤ 448 px). Это самый консервативный формат, который принимают и OpenAI-совместимые VLM, и Cosmos Reasoner.
Отправка `video_url`/`video_frames` — опция `NIM_VISION_INPUT=video_frames` после проверки `doctor --live`.

Источники: [Cosmos 3 Reasoner API (NIM VLM 1.7.0)](https://docs.nvidia.com/nim/vision-language-models/1.7.0/examples/cosmos-reason3/api.html),
[Cosmos cookbook: NIM](https://github.com/NVIDIA/cosmos/blob/main/cookbooks/cosmos3/nim/README.md),
[build.nvidia.com: cosmos3-nano-reasoner](https://build.nvidia.com/nvidia/cosmos3-nano-reasoner).

### 1.3 Генерация видео — `cosmos3-nano` **[блокер для hosted-режима]**

Документирован **только self-hosted NIM-контейнер** Cosmos3 Generator
([api-reference](https://github.com/NVIDIA/cosmos/blob/main/cookbooks/cosmos3/nim/api-reference.md),
[generation](https://github.com/NVIDIA/cosmos/blob/main/cookbooks/cosmos3/nim/generation.md)):

| Параметр | Значение **[документировано]** |
|---|---|
| Endpoint | `POST {base}/v1/infer`, один синхронный JSON-запрос; неизвестные поля отклоняются; готовность — `GET /v1/health/ready`, метаданные — `GET /v1/metadata` |
| `model_mode` | `text2video`, `image2video`, `text2image`, `video2video`, … (обязателен) |
| `prompt` | ≤ 20 000 символов, обязателен для T2V; `negative_prompt` ≤ 20 000 |
| `resolution` | enum; вертикальные: `256_9_16` = 192×320, `480_9_16` = 480×832, **`720_9_16` = 720×1280** |
| `num_frames` | 25 … max по уровню: 256 → 397, 480 → 297, **720 → 197** |
| `fps` | 1–60, рекомендовано 10–30, в примерах 24 |
| Длительность сцены | `num_frames / fps`: 720p при 24 fps ≤ **≈ 8,2 с** |
| `seed`, `guidance_scale` [1, 7], `num_inference_steps` [1, 100] | строгие типы (`"35"` или `35.0` вместо 35 — ошибка) |
| Ответ | `{"b64_video": "<raw base64 MP4>"}`; видео — **VP9 в MP4**; звук не генерируется («public Generator request model does not expose … sound generation») |
| Таймаут | примеры допускают до 30 мин на HTTP-запрос («ceiling, not expected time») |
| Защита | guardrails включены по умолчанию; на build.nvidia.com упоминается обязательная проверка ввода и водяной знак SynthID |

**Hosted-endpoint не подтверждён.** Страница [build.nvidia.com/nvidia/cosmos3-nano](https://build.nvidia.com/nvidia/cosmos3-nano)
помечена как бесплатный trial-endpoint, но не публикует URL и пример кода. В
[теме форума NVIDIA](https://forums.developer.nvidia.com/t/404-error-missing-api-endpoint-code-for-cosmos3-nano-on-build-nvidia-com/372044)
(июнь–август 2026) пользователи сообщают о 404 на всех попробованных путях и 500 в playground; ответа NVIDIA нет.
Сторонние источники упоминают для других hosted genai-моделей схему `202 + NVCF-REQID` и опрос
`GET https://ai.api.nvidia.com/v1/status/{requestId}` (документировано NVIDIA для некоторых моделей, например
[vila statuspolling](https://docs.api.nvidia.com/nim/reference/nvidia-vila-statuspolling)), но **не для cosmos3-nano**.

Поэтому:

* URL генератора **не зашит в код**: `NIM_VIDEO_URL` обязателен, по умолчанию пуст → этап `generated` выдаёт
  ошибку конфигурации `video generator endpoint not configured` (не mock).
* Адаптер `NimVideoClient` реализует документированную схему `/v1/infer` и **опционально** обработку
  `202 + NVCF-REQID` (`NIM_VIDEO_ASYNC_STATUS_URL`, пустой по умолчанию). Если 202 приходит без настроенного
  status URL, операция получает `generation_unknown`, request ID сохраняется.
* Рабочие варианты разблокировки: (а) NVIDIA публикует hosted URL для вашего ключа — проверить в UI «Get API Key → code»
  и прописать в `NIM_VIDEO_URL`; (б) self-hosted NIM `nvcr.io/nim/nvidia/cosmos3-generator…` на внешнем GPU
  (не на Raspberry Pi), `NIM_VIDEO_URL=https://<gpu-host>/v1/infer`; (в) другой генератор — новый адаптер.

### 1.4 Озвучка (TTS)

Модель видео звук не генерирует. Hosted TTS NVIDIA (Riva/Magpie) использует **gRPC** с function-id; в этом MVP
не реализован и не проверялся. Реализованы адаптеры `none` (ролик без озвучки, явно отмечено в отчёте и описании)
и `mock` (тишина/тон для тестов). Музыка — только из разрешённых файлов с манифестом лицензии.

### 1.5 Полезное, но не используемое

`nvidia/ai-synthetic-video-detector` присутствует в каталоге **[проверено]**. Формат вызова не изучался; его
результат был бы ещё одним вероятностным сигналом, а не доказательством.

## 2. Условия trial, квоты, использование результатов

* build.nvidia.com: «This trial service is governed by the NVIDIA API Trial Terms of Service». Лицензия модели
  Cosmos3 — OpenMDW-1.1; контейнер NIM — NVIDIA Software License Agreement + Product-Specific Terms. **[проверено]** текстом страницы.
* Диалог «Before You Use AI Models» на build.nvidia.com сообщает, что ввод и вывод trial-сервиса записываются
  NVIDIA и не следует отправлять конфиденциальные/персональные данные. Диалог в этой сессии **не принимался**.
* Допустимость **production/коммерческого использования** trial-endpoint и результатов: полный текст API Trial ToS
  получить не удалось → **[не подтверждено]**. Перед автопубликацией прочитайте условия в своём аккаунте NVIDIA.
  По этой причине `AUTO_PUBLISH=false` по умолчанию, а включение требует `PUBLISH_CONSENT_*`.
* Квоты/кредиты trial: в документации для конкретного ключа не указаны; учёт ведётся локально
  (`budget_ledger`), лимиты задаются в конфигурации. 429 обрабатывается с `Retry-After`.

## 3. YouTube Data API v3

| Метод | Квота **[документировано](https://developers.google.com/youtube/v3/determine_quota_cost)** |
|---|---|
| `search.list` | 1 вызов из **отдельного** дневного bucket: 100 вызовов/сутки |
| `videos.insert` | 1 вызов из отдельного bucket: 100 вызовов/сутки |
| `videos.list`, `channels.list`, `playlistItems.list` | 1 unit из общего бюджета 10 000 units/сутки |
| `videos.update` | 50 units |

Сброс — полночь Pacific Time. Код считает вызовы в `budget_ledger` по суткам PT и останавливается до лимита.

`videos.insert` **[документировано](https://developers.google.com/youtube/v3/docs/videos/insert)**:
scopes — `youtube.upload` (используем минимальный) и `youtube.readonly` для `channels.list mine=true` и
статуса обработки. **Без `privacyStatus` видео публикуется как public** → код всегда задаёт его явно.
Поля `status.publishAt`, `status.selfDeclaredMadeForKids`, `status.containsSyntheticMedia` поддерживаются.
Текущая документация: «Videos uploaded from unverified API projects are not restricted to private viewing mode».
Загрузка требует OAuth 2.0 владельца канала; API key и service account для обычного канала **недостаточны**.

### 3.1 Ограничения поиска Shorts

* `search.list videoDuration=short` — это видео **короче 4 минут**, а не Shorts. Shorts могут быть до 3 минут.
* В API нет поля «это Short». Классификация в коде:
  * `confirmed` — только по авторитетному источнику: манифест правообладателя (`is_short: true`) или
    подтверждённые вертикальные медиа ≤ 180 с из разрешённого `ReferenceMediaSource`;
  * `probable` — длительность ≤ 180 с **и** признак `#shorts` в названии/описании/тегах;
  * `unknown` — остальные ≤ 180 с;
  * `not_short` — длительность > 180 с.
* `order=viewCount` сортирует **внутри результата запроса** (ключевые слова, регион, язык, окно `publishedAfter`).
  Это не глобальный максимум просмотров. Отчёт `discover` всегда показывает область поиска и `fetched_at`.
* Производные метрики (viral score, views/hour, engagement) **не рассчитываются**: сортировка — порядок API или
  сырой `statistics.viewCount`.
* Данные API хранятся не дольше `YOUTUBE_DATA_RETENTION_DAYS` (по умолчанию 25 < 30 дней из
  [Developer Policies](https://developers.google.com/youtube/terms/developer-policies)); `shorts purge` удаляет устаревшее.

### 3.2 Определение AI-контента

* Сигналы: ключевые слова в названии/описании/тегах (настраиваемый список), `status.containsSyntheticMedia`, если API
  его вернул (для чужих видео обычно не возвращается), вывод VLM по разрешённым кадрам.
* Результат: `ai_likelihood ∈ [0,1]`, `evidence[]` (поле + фрагмент), `classification_source`.
  Это **эвристика**, а не доказательство; уверенность модели хранится отдельно и помечается как мнение модели.
* Отсутствие метки не означает отсутствия ИИ: без сигналов `ai_likelihood=null`, `classification_source=none`.
* Неоднозначные (между порогами) кандидаты отправляются в `review_required`.

## 4. Источник материалов для анализа

YouTube Data API **не отдаёт** MP4, аудио или субтитры чужих роликов (`captions.download` — только для своих
видео/с разрешения владельца). Скачивание с YouTube в MVP не реализовано.

`ReferenceMediaSource`:

| Реализация | Что даёт |
|---|---|
| `LocalDirMediaSource` | Оригиналы, предоставленные автором/пользователем: `<dir>/<video_id>/manifest.json` + файлы |
| `S3MediaSource` | То же в S3: `reference/<video_id>/manifest.json` |
| `NoMediaSource` | Ничего → режим `metadata_only` |

Манифест обязан содержать `license` и `provided_by`; без них файл не используется.
В `metadata_only` анализ строится только из названия/описания/статистики; поля о кадрах, монтаже, речи и музыке
равны `null`, а в `limitations` перечислено, что не анализировалось.

## 5. Учётные данные

| Секрет | Назначение | Где |
|---|---|---|
| `NVIDIA_API_KEY` | text/vision/video NIM | Secret `shorts-nvidia` |
| `YOUTUBE_API_KEY` | `search.list`, `videos.list` (публичные данные) | Secret `shorts-youtube` |
| `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` | OAuth client (тип Desktop) | Secret `shorts-youtube` |
| `YOUTUBE_REFRESH_TOKEN` | загрузка от имени владельца канала | Secret `shorts-youtube` |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | артефакты | Secret `shorts-s3` |
| `POSTGRES_PASSWORD` / `DATABASE_URL` | состояние | Secret `shorts-db` |
| `UPLOAD_URI_KEY` | шифрование upload session URI (AES-GCM, 32 байта base64) | Secret `shorts-app` |

## 6. Что ещё не проверено

* Реальные ответы `/v1/chat/completions` для выбранных моделей с вашим ключом.
* Наличие hosted cosmos3-nano / cosmos3-nano-reasoner для вашего ключа.
* Квоты NVIDIA trial для вашего аккаунта.
* OAuth-клиент, канал, режим аудита проекта Google Cloud.
* S3-хранилище (endpoint, bucket, lifecycle).
