import base64
import json
import os
import random
import shutil
from datetime import datetime, timedelta
from pathlib import Path
import hashlib
from tempfile import TemporaryDirectory
from typing import Dict
from uuid import uuid4

import ffmpeg
from dotenv import load_dotenv
import smtplib
import ssl
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

try:
  from spleeter.separator import Separator
except ImportError as exc:  # pragma: no cover
  raise RuntimeError(
      "Spleeter is not installed. Make sure `pip install spleeter` has completed successfully."
  ) from exc


LIBRARY_ROOT = Path("library")
LIBRARY_ROOT.mkdir(exist_ok=True)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mpg", ".mpeg"}

ACCOUNTS_PATH = Path("accounts.json")
CODE_TTL = timedelta(minutes=10)
PENDING_CODES: Dict[str, Dict[str, str]] = {}

SMTP_HOST = os.getenv("STEMLAB_SMTP_HOST")
SMTP_PORT = int(os.getenv("STEMLAB_SMTP_PORT", "587"))
SMTP_USER = os.getenv("STEMLAB_SMTP_USER")
SMTP_PASS = os.getenv("STEMLAB_SMTP_PASS")
SMTP_FROM = os.getenv("STEMLAB_EMAIL_FROM", SMTP_USER or "stem-lab@example.com")
SMTP_USE_TLS = os.getenv("STEMLAB_SMTP_USE_TLS", "1") != "0"


def load_accounts() -> Dict[str, Dict[str, str]]:
  if ACCOUNTS_PATH.exists():
    try:
      return json.loads(ACCOUNTS_PATH.read_text())
    except Exception:
      return {}
  return {}


def save_accounts(accounts: Dict[str, Dict[str, str]]) -> None:
  ACCOUNTS_PATH.write_text(json.dumps(accounts, indent=2))


def normalize_email(email: str) -> str:
  return email.strip().lower()


load_dotenv()

accounts = load_accounts()


def record_account(email: str, name: str, password_hash: str) -> Dict[str, str]:
  email_key = normalize_email(email)
  now = datetime.utcnow().isoformat() + "Z"
  account = accounts.get(email_key, {})
  created_at = account.get("createdAt") or now
  account.update({
      "email": email,
      "name": name,
      "passwordHash": password_hash,
      "createdAt": created_at,
      "updatedAt": now,
  })
  accounts[email_key] = account
  save_accounts(accounts)
  return account


def store_verification_code(email: str, code: str) -> None:
  PENDING_CODES[normalize_email(email)] = {
      "code": code,
      "expires": (datetime.utcnow() + CODE_TTL).isoformat() + "Z",
  }


def pop_verification_code(email: str, code: str) -> Dict[str, str]:
  entry = PENDING_CODES.get(normalize_email(email))
  if not entry:
    raise HTTPException(status_code=400, detail="No verification code requested for this email.")
  expires_at = datetime.fromisoformat(entry["expires"].replace("Z", ""))
  if datetime.utcnow() > expires_at:
    raise HTTPException(status_code=400, detail="Verification code has expired.")
  if entry["code"] != code:
    raise HTTPException(status_code=400, detail="Invalid verification code.")
  PENDING_CODES.pop(normalize_email(email), None)
  return entry


def generate_code() -> str:
  return f"{random.randint(0, 999999):06d}"


def hash_password(password: str) -> str:
  return hashlib.sha256(password.encode("utf-8")).hexdigest()


def send_verification_email(email: str, code: str) -> None:
  if SMTP_HOST and SMTP_FROM:
    try:
      context = ssl.create_default_context()
      with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        if SMTP_USE_TLS:
          server.starttls(context=context)
        if SMTP_USER and SMTP_PASS:
          server.login(SMTP_USER, SMTP_PASS)
        message = f"From: {SMTP_FROM}
To: {email}
Subject: Stem Lab verification code

Your verification code is: {code}
"
        server.sendmail(SMTP_FROM, [email], message)
      print(f"[Stem Lab] Verification email sent to {email}")
      return
    except Exception as exc:
      print(f"[Stem Lab] Could not send email via SMTP: {exc}. Falling back to console.")
  print(f"[Stem Lab] Verification code for {email}: {code}")


app = FastAPI(title="Stem Lab Splitter", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEFAULT_MODEL = "spleeter:4stems"
EXPECTED_STEMS = ("vocals", "drums", "bass", "other")
_separator_cache: Dict[str, Separator] = {}


def get_separator(model: str) -> Separator:
  if model not in _separator_cache:
    try:
      _separator_cache[model] = Separator(model)
    except Exception as exc:
      raise HTTPException(status_code=500, detail=f"Could not initialize Spleeter model '{model}': {exc}")
  return _separator_cache[model]


def encode_audio_file(path: Path) -> str:
  data = path.read_bytes()
  encoded = base64.b64encode(data).decode("ascii")
  return f"data:audio/wav;base64,{encoded}"


async def stream_copy_to_path(upload: UploadFile, destination: Path) -> None:
  with destination.open("wb") as buffer:
    while True:
      chunk = await upload.read(1024 * 1024)
      if not chunk:
        break
      buffer.write(chunk)


@app.post("/api/separate")
async def separate(
    file: UploadFile = File(...),
    model: str = Form(DEFAULT_MODEL),
):
  if not file.filename:
    raise HTTPException(status_code=400, detail="Upload must include a filename.")

  with TemporaryDirectory(prefix="stemstudio_") as tmpdir:
    tmp_dir_path = Path(tmpdir)
    input_path = tmp_dir_path / file.filename
    await stream_copy_to_path(file, input_path)
    await file.close()

    process_source = input_path
    if input_path.suffix.lower() in VIDEO_EXTENSIONS:
      audio_path = tmp_dir_path / f"{input_path.stem}_audio.wav"
      try:
        (
            ffmpeg
            .input(str(input_path))
            .output(str(audio_path), ac=2, ar=44100, format="wav")
            .overwrite_output()
            .run(quiet=True)
        )
        process_source = audio_path
      except ffmpeg.Error as exc:
        raise HTTPException(status_code=500, detail=f"Could not extract audio from video: {exc}") from exc

    separator = get_separator(model)
    try:
      separator.separate_to_file(
          str(process_source),
          str(tmp_dir_path),
          codec="wav",
          filename_format="{filename}/{instrument}.{codec}",
      )
    except Exception as exc:
      raise HTTPException(status_code=500, detail=f"Stem separation failed: {exc}") from exc

    stems_dir = tmp_dir_path / Path(file.filename).stem
    if not stems_dir.exists():
      raise HTTPException(status_code=500, detail="Stem service did not produce any files.")

    stems: Dict[str, str] = {}
    for stem_name in EXPECTED_STEMS:
      stem_path = stems_dir / f"{stem_name}.wav"
      if stem_path.exists():
        stems[stem_name] = encode_audio_file(stem_path)

    if not stems:
      raise HTTPException(status_code=500, detail="No stems were generated by Spleeter.")

    return JSONResponse({"model": model, "stems": stems})


def decode_data_url(value: str) -> bytes:
  value = value.strip()
  if value.startswith("data:"):
    header, _, base64_part = value.partition(",")
    if ";base64" not in header:
      raise ValueError("Unsupported data URL format")
    data = base64.b64decode(base64_part)
    return data
  return base64.b64decode(value)


@app.post("/api/auth/request-code")
async def request_code(payload: Dict[str, str]):
  email = (payload.get("email") or "").strip()
  if not email:
    raise HTTPException(status_code=400, detail="Email is required.")
  code = generate_code()
  store_verification_code(email, code)
  send_verification_email(email, code)
  return {"ok": True}


@app.post("/api/auth/verify")
async def verify_code(payload: Dict[str, str]):
  email = (payload.get("email") or "").strip()
  code = (payload.get("code") or "").strip()
  password = payload.get("password") or ""
  password_hash = payload.get("passwordHash")
  name = (payload.get("name") or "").strip()
  if not email or not code or not (password or password_hash):
    raise HTTPException(status_code=400, detail="Email, code, and password are required.")
  entry = pop_verification_code(email, code)
  if not password_hash:
    password_hash = hash_password(password)
  account = record_account(email, name, password_hash)
  print(f"[Stem Lab] Account verified for {email}")
  return JSONResponse({
      "email": account["email"],
      "name": account.get("name", ""),
      "createdAt": account.get("createdAt"),
      "updatedAt": account.get("updatedAt"),
  })


@app.get("/api/library/{item_id}")
async def get_library_item(item_id: str):
  item_dir = LIBRARY_ROOT / item_id
  meta_path = item_dir / "meta.json"
  if not meta_path.exists():
    raise HTTPException(status_code=404, detail="Session not found.")
  metadata = json.loads(meta_path.read_text())
  stems: Dict[str, str] = {}
  for stem_path in item_dir.glob("*.wav"):
    stems[stem_path.stem] = encode_audio_file(stem_path)
  metadata["stems"] = stems
  return JSONResponse(metadata)


@app.post("/api/library")
async def save_library(request: Request) -> JSONResponse:
  try:
    payload = await request.json()
  except Exception as exc:
    raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}") from exc

  stems = payload.get("stems")
  if not isinstance(stems, dict) or not stems:
    raise HTTPException(status_code=400, detail="Payload must include stems")

  title = payload.get("title") or "Session"
  item_id = uuid4().hex
  item_dir = LIBRARY_ROOT / item_id
  item_dir.mkdir(parents=True, exist_ok=True)

  for name, value in stems.items():
    if not isinstance(value, str):
      continue
    try:
      data = decode_data_url(value)
    except Exception as exc:
      raise HTTPException(status_code=400, detail=f"Could not decode stem '{name}': {exc}") from exc
    stem_path = item_dir / f"{name}.wav"
    stem_path.write_bytes(data)

  metadata = {
      "id": item_id,
      "title": title,
      "stems": list(stems.keys()),
      "createdAt": datetime.utcnow().isoformat() + "Z",
      "bundle": f"/api/library/{item_id}/bundle",
  }
  (item_dir / "meta.json").write_text(json.dumps(metadata))
  shutil.make_archive(str(item_dir / "bundle"), "zip", root_dir=item_dir)
  return JSONResponse(metadata)


@app.get("/api/library")
async def list_library() -> JSONResponse:
  items = []
  for meta_path in LIBRARY_ROOT.glob("*/meta.json"):
    try:
      data = json.loads(meta_path.read_text())
      items.append(data)
    except Exception:
      continue
  items.sort(key=lambda item: item.get("createdAt", ""))
  return JSONResponse({"items": items})


@app.get("/api/library/{item_id}/bundle")
async def download_bundle(item_id: str):
  item_dir = LIBRARY_ROOT / item_id
  bundle_path = item_dir / "bundle.zip"
  if not bundle_path.exists():
    raise HTTPException(status_code=404, detail="Bundle not found")
  return FileResponse(bundle_path, media_type="application/zip", filename=f"{item_id}.zip")


@app.get("/healthz")
async def healthcheck() -> Dict[str, str]:
  return {"status": "ok"}


if __name__ == "__main__":  # pragma: no cover
  import uvicorn

  uvicorn.run("backend.server:app", host="0.0.0.0", port=5000, reload=False)
