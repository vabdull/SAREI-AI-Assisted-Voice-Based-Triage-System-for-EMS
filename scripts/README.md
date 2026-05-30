# `scripts/` directory

Helper scripts for this project. They fall into three groups:

- **Operational** scripts (directly in `scripts/`) are needed to run or
  test the app.
- **ML/training** scripts (directly in `scripts/`) were used to build the
  Arabic ASR model.
- **Developer/debug helpers** live under `scripts/dev/`. They are safe to
  ignore for grading and are not imported by the application.

## Operational (used to run the app)
| Script | Purpose |
|---|---|
| `_run_backend.sh` | Launch the FastAPI backend with uvicorn on the canonical dev port (8011). |
| `_restart_backend.sh` | Stop and restart the backend process. |
| `_wait_backend.sh` | Block until the backend health endpoint responds. |

## ML / training pipeline (model building, not runtime)
| Script | Purpose |
|---|---|
| `download_sada.py` | Download the SADA Arabic speech dataset. |
| `prepare_manifests.py` | Build NeMo training manifests from the dataset. |
| `filter_dialect.py` | Filter manifests by dialect. |
| `build_tokenizer.py` | Build the SentencePiece tokenizer. |
| `finetune_asr.py` / `train_asr.py` | Fine-tune / train the FastConformer ASR model. |
| `train_pipeline.sh` | Orchestrates the full training pipeline. |
| `evaluate_asr.py` | Evaluate ASR word error rate. |
| `transcribe.py` | Transcribe an audio file with the trained model. |
| `smoke_triage_engine.py` | Manual smoke test of the deterministic triage engine. |

## Developer / debug helpers (under `scripts/dev/`, safe to ignore)
These were used during development to inspect or reset state. They are
NOT part of the application and are not imported anywhere:

`dev/_backfill_patient_count.py`, `dev/_bench_matcher.py`,
`dev/_check_db_status.py`, `dev/_check_deps.sh`, `dev/_check_imports.sh`,
`dev/_check_logging.sh`, `dev/_check_routes.py`, `dev/_clear_cases.py`,
`dev/_count_cases.py`, `dev/_diag_portals.py`, `dev/_import_check.sh`,
`dev/_inspect_case.py`, `dev/_list_users.py`,
`dev/_migrate_add_medic_completed_at.py`,
`dev/_migrate_add_patient_count.py`, `dev/_probe_portals_admin.py`,
`dev/_smoke_portals.py`, `dev/_smoke_utc_serialize.sh`.

A few additional live-pipeline debug helpers remain directly in
`scripts/`: `debug_live_call.py`, `probe_live_ws.py`,
`verify_live_chunk.py`.

> Note: nothing here is deleted because some scripts (especially the
> migration helpers) document how the database schema evolved.
