# Stem Lab Backend (Spleeter)

This FastAPI service wraps Deezer’s [Spleeter](https://github.com/deezer/spleeter) models so the browser UI can offload stem extraction instead of doing it locally.

## Prerequisites

- Python 3.9–3.10 (Spleeter currently targets these versions best)
- [FFmpeg](https://ffmpeg.org/download.html) available on your `PATH`
- (Optional) GPU support if you install a GPU-enabled TensorFlow build

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r backend/requirements.txt
```

> **Note**: Installing `spleeter` pulls in TensorFlow, which is a large dependency. On Apple Silicon use `pip install tensorflow-macos` before installing the requirements file.

## SMTP (optional)

To send real verification emails, set these environment variables before starting the server:

- `STEMLAB_SMTP_HOST` / `STEMLAB_SMTP_PORT`
- `STEMLAB_SMTP_USER` / `STEMLAB_SMTP_PASS`
- `STEMLAB_EMAIL_FROM`

If these aren't set, the server logs verification codes to the console instead.

## Run

```bash
uvicorn backend.server:app --host 0.0.0.0 --port 5000
```

By default the browser points at `http://localhost:5000/api/separate`, so running the command above on the same machine will just work. Update the endpoint settings in the interface if you deploy the API elsewhere.

## API contract

- `POST /api/separate`
  - Form fields: `file` (`UploadFile`), `model` (default `spleeter:4stems`).
  - Accepts common audio formats and video containers (MP4/MOV/etc.); video uploads are transcoded to WAV with FFmpeg before separation.
  - Response: JSON with a `stems` object containing base64 `data:audio/wav` URLs keyed by `vocals`, `drums`, `bass`, `other`.
  - Errors surface as standard HTTP 4xx/5xx responses with a `detail` message.
- `POST /api/library`
  - JSON body `{ "title": "...", "stems": { "vocals": "data:audio/wav;base64,...", ... } }`.
  - Persists stems on disk and returns metadata (including a bundle download path).
- `GET /api/library`
  - Lists saved sessions.
- `GET /api/library/{id}`
  - Returns metadata plus inline `data:` URLs for each stem so the client can reopen a session.
- `GET /api/library/{id}/bundle`
  - Streams a ZIP archive of the stems for that session.
- `POST /api/auth/request-code`
  - Request a one-time code for email verification (code is logged to the server console in this prototype).
- `POST /api/auth/verify`
  - Verify the code, register/update the account, and store the hashed password.
- `GET /healthz`
  - Simple readiness endpoint.
