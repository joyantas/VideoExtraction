# VideoExtraction

Campaign-video evidence extraction and upload-readiness review with a web upload
frontend, authenticated API, and containerized analysis worker. The project name
is intentionally **VideoExtraction**.

**Implemented:** durable local uploads/jobs/reports; real FFprobe metadata and
FFmpeg frame checks; an Azure private Blob + Queue backend; an event-driven
Container Apps worker entry point; opt-in Content Understanding campaign fields,
Read OCR on timestamped frames, and Content Safety on sampled images and recognized
transcript/OCR text.

**Not deployed or live-service verified:** Azure hosting, Entra app registration,
AI resources, Foundry model deployments, the custom analyzer, and Azure RBAC.
AI is disabled by default. No service output is fabricated, and an unconfigured,
failed, incomplete or unverified check does not become a pass.

## Run locally

Requires Python 3.11+ and both `ffmpeg` and `ffprobe` on PATH. On macOS:
`brew install ffmpeg`. The container installs both tools itself.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.sample .env
```

Generate a local-only bearer token and place it in `.env` as
`LOCAL_BEARER_TOKEN` (at least 32 characters):

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Do not commit `.env`. In **each terminal**, load the same settings:

```bash
source .venv/bin/activate
set -a
source .env
set +a
```

Start the API:

```bash
uvicorn videoextration.api:app --host 127.0.0.1 --port "${PORT:-3000}"
```

In a second terminal, start the worker:

```bash
python -m videoextration.worker
# Or process at most one queued job:
python -m videoextration.worker --once
```

Open <http://127.0.0.1:3000>, enter the local bearer token, choose a video, and
optionally specify exact required text (one item per line). Keep the job ID to
resume polling. The token is kept in page memory, not browser persistent storage.

Default bounds: 100 MiB upload, 120-second maximum input duration, six sampled
frames, 45-second media-analysis wall time, and a ten-minute upload capability.
The illustrative placement target is 9:16, at least 720x1280, and 3–60 seconds.
These are sample rules, **not current official social-platform specifications**.
Changing processing bounds is separate from changing/versioning placement rules.
To tailor placement checks, edit `PLACEMENT_PROFILE` in
`src/videoextration/rules.py` and bump `PROFILE_VERSION` and `RULES_VERSION`.
The actual threshold definition is copied into every persisted report.

Local reports persist in `data/jobs.sqlite3`; opaque media and sampled frames
persist under `data/media/`. Both API and worker must use the same `DATA_DIR`.
Local development mode intentionally cannot certify a video ready while the AI,
OCR, safety and human-verification requirements remain unevaluated.

## API

All `/api/jobs` endpoints require `Authorization: Bearer <access-token>`.
The local upload route instead requires its single-use scoped upload capability.

| Method and route | Purpose |
|---|---|
| `POST /api/jobs` | Declare filename, MIME type, size, profile and required text; return scoped upload URL/headers |
| `PUT <upload_url>` | Upload raw video bytes using only returned upload headers |
| `POST /api/jobs/{id}/complete` | Validate upload and enqueue analysis |
| `GET /api/jobs/{id}` | Owner-scoped job status |
| `GET /api/jobs/{id}/report` | Owner-scoped persisted report |
| `GET /healthz` | Anonymous liveness |
| `GET /readyz` | Dependency readiness; not an assertion of complete AI configuration |

The browser uploads directly to private Blob Storage in Azure mode. It must
**never** attach the API bearer token to a Blob upload request. The API returns
create-only SAS permission, not an account key or broad storage access.

Reports separate evidence from conclusions and use `pass`, `fail`,
`needs-review`, `not-evaluated`. Report completion and job completion do not
mean publishing approval. Generated campaign metadata remains explicitly
unverified; sampled OCR cannot establish every-frame text coverage.

New reports include `transcript.text` and `transcript.segments` for recognized
audio, with `start_seconds`, `end_seconds` and segment text. Azure Speech fast
transcription is optional and separately configured; see the setup instructions
below. Recognition remains `needs-review`, not verified wording. Disabled,
unconfigured and failed recognition returns `state: "not-evaluated"`, `text: null`
and an explicit reason; no transcript is invented. Previously saved reports are
not reprocessed automatically.

To enable it **only after approving paid Azure usage**, set `SPEECH_ENDPOINT`
to your approved resource's `https://NAME.cognitiveservices.azure.com` origin and
`ENABLE_PAID_AI=true`. Optionally set `SPEECH_LOCALES=en-US` (or a comma-separated
list); leaving it empty uses service language identification. In local mode,
authenticate with `az login` using an identity authorized for that Speech
resource; in Azure mode the worker uses managed identity. No API key or new model
deployment is needed. Restart the worker after changing its environment.

The worker extracts the first audio stream as mono 16 kHz PCM, using bounded
FFmpeg processing, then calls the GA Speech fast transcription API `2025-10-15`.
Segment times are relative to that extracted audio, not guaranteed video-clock
offsets. The JSON preserves raw service evidence and does not treat recognition
as a safety/legal check. If both Speech and Content Understanding are configured,
both can incur charges; omit `SPEECH_ENDPOINT` when the separate Speech operation
is not wanted. Local mode does not enable the campaign/OCR/safety pipeline.

## Container

```bash
docker build -t videoextration:sample .
mkdir -p data
docker run --rm -p 3000:3000 --env-file .env \
  -v "$PWD/data:/app/data" videoextration:sample
# In another terminal, sharing the same directory and environment:
docker run --rm --env-file .env -v "$PWD/data:/app/data" \
  videoextration:sample python -m videoextration.worker
```

Ensure the bind mount is writable by the image's non-root `app` user. Azure
workers use temporary disk only and persist records/reports to Blob Storage.

## Azure

The target subscription is
`acb0b35a-6cfe-4bfc-b459-b9bb61f3e4ca`. Generation is not deployment authorization.
Use the manual deployment instructions only after separate approval.

- [Architecture, trust boundaries, limitations and official references](docs/architecture.md)
- [Azure identity, custom analyzer/model mapping, service setup and deployment](docs/azure-setup.md)

Set `MODE=azure` to select the Azure API/worker implementations.
`LOCAL_BEARER_TOKEN` is forbidden in this mode. Managed identity settings,
Entra token validation and Storage/AI prerequisites must be configured first;
there is no insecure fallback to local authentication or fake results.

## Development

```bash
python -m pytest -q
node --check src/videoextration/static/app.js
```

Tests exercise authentication/ownership, upload capability expiry and replay,
size/type constraints, durable job leases, report conservatism, bounded media
subprocesses, real FFmpeg/FFprobe extraction, mocked Azure REST contracts,
queue-delivery failure and finalization recovery. Real-media tests explicitly
skip if FFmpeg/FFprobe are missing. Unit tests never invoke paid inference.

This is a learning sample, not a production security/compliance or legal
clearance product. It does not guarantee platform acceptance or resolve music,
licensing, copyright, claim substantiation or other legal rights.
