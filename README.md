# SAREI | سارع

<p align="center">
  <img src="frontend/public/logo.png" alt="SAREI logo" width="500" />
</p>

## Intelligent Unified Medical Emergency Platform

**Made By:** Abdullah Alotaibi, Abdulmalik Alotaibi, Mohammed Aljabri  
**Course:** Graduation Project 498  
**Supervisor:** Dr. Ismail Keshta  
**Date:** May 2026  
**Project:** SAREI | سارع AI-Assisted Voice-Based Triage System for Emergency Medical Services

A human-in-the-loop emergency support platform that listens to a live
Arabic emergency call, transcribes it in real time, extracts the key
case details, and suggests an explainable triage level for the
dispatcher to approve, edit, or override. The system assists dispatchers;
it does not diagnose patients and does not replace human judgment.

## Main Features

- Live Arabic call transcription (streaming ASR via NVIDIA NeMo).
- Deterministic, explainable triage engine (rule-based, no black box).
- Optional LLM enrichment (local Ollama / Qwen) for highlights and
  structured medical-entity extraction.
- Automatic extraction of patient location, patient count, and patient
  demographics (name, age, gender) from the transcript.
- Four role-based portals: Dispatcher, Medic (ambulance), Hospital, Admin.
- Per-call audio recording, saved as one combined WAV on call end.
- JWT authentication, role-based access control, and an audit log of
  sensitive admin actions.

## Technology Stack

| Layer | Technologies |
|---|---|
| Backend | Python, FastAPI, SQLAlchemy, Pydantic, SQLite |
| Auth | JWT (python-jose), password hashing (passlib) |
| Realtime | WebSockets (FastAPI) |
| Frontend | React 18, TypeScript, Vite, Tailwind CSS, React Router, Framer Motion |
| Speech (ASR) | NVIDIA NeMo (FastConformer, Arabic / SADA) |
| LLM enrichment | Ollama running a Qwen model (optional) |

## High-Level Architecture

```text
Caller Audio
  -> Audio Preprocessing (16 kHz mono PCM)
  -> Streaming ASR (NeMo)
  -> Rolling Transcript
  -> Fast Deterministic Extraction (location, count, demographics, symptoms)
  -> Deterministic Triage Engine
  -> Optional LLM Enrichment (Qwen via Ollama)
  -> Dispatcher Portal (approve / edit / override)
  -> Confirmed Case Routing
  -> Medic (Ambulance) and Hospital Portals
```

## Repository Layout

```text
SAREI/
|-- backend/            # FastAPI application
|-- frontend/           # React + Vite UI
|-- requirements.txt    # Python: API + ASR
|-- package.json        # npm install / npm run dev
|-- models/             # ASR model (.nemo)
|-- configs/            # Triage rules + ASR training configs
|-- scripts/            # Helpers and training scripts
|-- data/               # Recordings (runtime) and datasets (training)
`-- tokenizer/          # SentencePiece tokenizer
```

## Backend Overview

The FastAPI app entrypoint is `backend.main:app`. All routes are
served under `/api/v1` with these prefixes (see
`backend/api/v1/router.py`): `auth`, `cases`, `dispatcher`,
`inference`, `realtime`, `triage`, `admin`, `ambulance`, `hospital`.

Database entities (`backend/db/models.py`):

- `User` - account with a role (`dispatcher`, `medic`, `hospital`, `admin`).
- `Case` - an emergency case with transcript, extraction, triage, and routing fields.
- `TranscriptSegment` - individual transcribed segments for a case.
- `CallRecording` - the saved audio recording metadata for a case.
- `AuditLog` - record of sensitive administrative actions.

## Frontend Overview

The route map lives in `frontend/src/app/App.tsx`. Each portal is guarded
by `ProtectedRoute`, which checks the logged-in user's role (admin can
open any portal):

- `/login`, `/register` - authentication pages
- `/dispatcher` - primary call-handling workspace (live transcript,
  case summary, triage, manual case entry, recordings)
- `/medic` - ambulance crew view with case status flow
- `/hospital` - receiving-hospital view of incoming cases
- `/admin` - user management (CRUD) and audit log

## Operational Flow

1. A dispatcher starts (or manually creates) a case.
2. Browser audio is streamed to the backend and transcribed.
3. The fast deterministic layer extracts location, patient count, and
   demographics; the triage engine produces an explainable level.
4. Optional LLM enrichment adds medical highlights/entities.
5. The dispatcher approves, edits, or overrides the details.
6. The confirmed case is routed to the medic and hospital portals.
7. The call's audio is saved as one combined recording.
8. Sensitive admin actions are written to the audit log.

## ASR Model Performance

The Arabic ASR model was fine-tuned using NVIDIA NeMo FastConformer on
SADA Arabic dialect data. The best final decoder was RNNT.

Final RNNT headline accuracy:

- Validation word accuracy: 72.96% (`100 - WER`)
- Validation character accuracy: 88.53% (`100 - CER`)
- Test word accuracy: 68.99% (`100 - WER`)
- Test character accuracy: 86.60% (`100 - CER`)

| Split | Decoder | WER | CER |
|---|---:|---:|---:|
| Validation | RNNT | 27.04% | 11.47% |
| Test | RNNT | 31.01% | 13.40% |
| Validation | CTC | 29.69% | 11.53% |
| Test | CTC | 34.11% | 13.63% |

### Test Performance by Dialect

| Dialect | RNNT WER | RNNT CER | CTC WER | CTC CER |
|---|---:|---:|---:|---:|
| Najdi | 30.06% | 13.02% | 33.38% | 13.34% |
| Hijazi | 28.86% | 12.46% | 31.02% | 12.61% |
| Khaleeji | 33.94% | 14.69% | 37.45% | 14.85% |

## Setup Guide

Follow these steps in order on a Windows PC. Do not skip the install checks.

The **project root** is the folder that contains these files and folders:

```text
requirements.txt
package.json
backend/
frontend/
models/
```

### 1. Install Python 3.11

Open PowerShell.

Install Python 3.11:

```powershell
winget install Python.Python.3.11
```

Close PowerShell.

Open PowerShell again.

Check that Python 3.11 installed correctly:

```powershell
py -3.11 --version
```

You should see something like:

```text
Python 3.11.x
```

If this command fails, Python 3.11 is not installed correctly.

Backup option: download Python 3.11 from `https://www.python.org/downloads/release/python-3119/`.
During manual installation, check `Add python.exe to PATH`.

### 2. Install Node.js

In PowerShell, install Node.js LTS:

```powershell
winget install OpenJS.NodeJS.LTS
```

Close PowerShell.

Open PowerShell again.

Check Node and npm:

```powershell
node -v
npm -v
```

Node should be version 18 or newer.

Backup option: download Node.js LTS from `https://nodejs.org/`.

### 3. Install ffmpeg

In PowerShell, run:

```powershell
winget install Gyan.FFmpeg
```

After installing, close PowerShell and open it again.

Check ffmpeg:

```powershell
ffmpeg -version
```

### 4. Download and extract the project

Download the SAREI ZIP file from Google Drive:

```text
https://drive.google.com/file/d/1lhTRaONNKb4tfhXp7x7WRIaUANSHAKg9/view?usp=sharing
```

Right-click the ZIP file.

Click **Extract All**.

Move the extracted folder somewhere easy to find, for example:

```text
C:\Users\your-name\Desktop\SAREI
```

Open PowerShell and go to the extracted folder:

```powershell
cd C:\path\to\SAREI
```

For example:

```powershell
cd C:\Users\your-name\Desktop\SAREI
```

If the folder name has spaces, put quotes around it:

```powershell
cd "C:\Users\your-name\Desktop\SAREI AI Project"
```

### 5. Check that you are in the correct folder

Run:

```powershell
dir
```

You should see:

```text
requirements.txt
package.json
backend
frontend
models
```

If you do not see these, you are in the wrong folder.

### 6. Check the ASR model

Run this from the project root:

```powershell
dir models\FastConformer-Arabic-SADA-Finetune-baseline-v1_final.nemo
```

The model file must be inside `models/` and must be large, around 438 MB.

If the size is tiny, for example `134` bytes, that is not the real model file.
Download the complete project ZIP again or download the real `.nemo` model file
and place it here:

```text
models\FastConformer-Arabic-SADA-Finetune-baseline-v1_final.nemo
```

The app needs this file for live Arabic transcription.

### 7. Create the Python environment

Run these lines one by one:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If `py -3.11 -m venv .venv` fails, stop and install Python 3.11 first.

If PowerShell blocks activation, run this once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Then activate again:

```powershell
.venv\Scripts\Activate.ps1
```

If you use CMD instead of PowerShell, activate with:

```cmd
.venv\Scripts\activate.bat
```

### 8. Create the environment file

In PowerShell, run:

```powershell
Copy-Item .env.example .env
```

In CMD, run:

```cmd
copy .env.example .env
```

The default `.env` values are enough for local development.

### 9. Install frontend packages

Run this from the project root:

```powershell
npm install
```

### 10. Start the backend

Open a terminal in the project root.

Activate the Python environment:

```powershell
.venv\Scripts\Activate.ps1
```

Start the backend:

```powershell
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8011 --reload
```

Keep this terminal open.

Wait until the backend finishes loading the NeMo ASR model.

### 11. Start the frontend

Open a second terminal.

Go to the same project root:

```powershell
cd C:\path\to\SAREI
```

Start the frontend:

```powershell
npm run dev
```

Keep this terminal open too.

### 12. Open the app

Open this URL in your browser:

```text
http://localhost:5173
```

The backend API runs here:

```text
http://localhost:8011/api/v1
```

To test the main flow:

1. Log in or register.
2. Open the Dispatcher portal.
3. Start a case.
4. Allow microphone access.
5. Speak Arabic.
6. Review the transcript, extracted details, and suggested triage.

### 13. Run it again later

Next time, you do not need to install everything again.

Open terminal 1:

```powershell
cd C:\path\to\SAREI
.venv\Scripts\Activate.ps1
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8011 --reload
```

Open terminal 2:

```powershell
cd C:\path\to\SAREI
npm run dev
```

Then open:

```text
http://localhost:5173
```

### Troubleshooting

| Problem | What to do |
|---|---|
| `No runtime installed that matches 3.11` | Install Python 3.11, then close and reopen PowerShell |
| `.venv\Scripts\Activate.ps1` does not exist | The venv was not created. Run `py -3.11 -m venv .venv` first |
| `Activate.ps1` is blocked | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then activate again |
| Model file is missing or only 134 bytes | Download the real `.nemo` model file and put it in `models/` |
| `ASR model preload failed` | Check that `.env` has `NEMO_MODEL_PATH=models/FastConformer-Arabic-SADA-Finetune-baseline-v1_final.nemo` |
| No transcript appears | Make sure the backend is running and the model loaded successfully |
| Audio or WebM error | Install ffmpeg, reopen the terminal, then run `ffmpeg -version` |
| Frontend opens but API calls fail | Make sure the backend is running on port `8011` |
| `npm run dev` fails | Run `npm install` from the project root |
| Python package import error | Activate `.venv`, then run `python -m pip install -r requirements.txt` again |

---

### Other files

| File | Purpose |
|---|---|
| `requirements.txt` | Run the app (API + ASR) |
| `requirements-train.txt` | Optional — train/fine-tune ASR (`scripts/`) |
| `.env` | Settings (copy from `.env.example`) |

## Environment Variables

The `.env` file lives at the project root (`copy .env.example .env`). Keys:

| Variable | Purpose |
|---|---|
| `APP_NAME` | Display name of the application. |
| `ENVIRONMENT` | `development` or `production`. |
| `DEBUG` | Enable debug behavior (`true`/`false`). |
| `SECRET_KEY` | Secret used to sign JWT access tokens. Set a strong random value. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | JWT lifetime in minutes. |
| `ALLOW_REGISTRATION` | Whether the public `/register` endpoint is enabled. |
| `OLLAMA_BASE_URL` | Base URL of the local Ollama server (LLM enrichment). |
| `QWEN_MODEL_NAME` | Qwen model name served by Ollama. |
| `NEMO_MODEL_PATH` | Path to the trained `.nemo` ASR model. |
| `ASR_DECODER_TYPE` | `rnnt` or `ctc`. |
| `ASR_DEVICE` | `auto`, `cpu`, or `cuda`. |

The SQLite database is created at the project root as `ems_triage.db`.
Saved call recordings are written to `data/recordings/`.

## Notes for the Instructor

- Default roles: `dispatcher`, `medic`, `hospital`, `admin`. The admin
  portal can create accounts for each role.
- Opening the database: `ems_triage.db` (project root) is a standard
  SQLite file. You can inspect it with the `sqlite3` CLI or any SQLite
  GUI (e.g. DB Browser for SQLite).
- **ASR:** Required for live transcription — see [Setup Guide](#setup-guide).
- **LLM enrichment** (Ollama/Qwen) is optional; deterministic triage works
  without it.
- The `scripts/` folder is documented in `scripts/README.md`. Operational
  helpers live directly under `scripts/`; one-off debug/diagnostic
  helpers live under `scripts/dev/`. ML training scripts (NeMo) also live
  under `scripts/`.

## ASR Training (optional, advanced)

Install training extras from the project root:

```bash
pip install -r requirements-train.txt
```

The Arabic ASR model is trained with NVIDIA NeMo on the SADA dataset using
the scripts under `scripts/` (`download_sada.py`, `prepare_manifests.py`,
`filter_dialect.py`, `build_tokenizer.py`, `train_asr.py`,
`evaluate_asr.py`, `transcribe.py`). Training runs and exported models are
stored under `experiments/`. View training curves with:

```bash
tensorboard --logdir experiments --port 6006
```

This track is not required to run or review the web application.
