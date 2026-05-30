#!/usr/bin/env python3
"""Train a FastConformer-CTC-BPE ASR model on SADA.

Usage:
    python scripts/train_asr.py --config configs/fastconformer_ctc_bpe_medium.yaml

Override any config value via CLI:
    python scripts/train_asr.py --config configs/fastconformer_ctc_bpe_medium.yaml \
        trainer.max_epochs=50 model.train_ds.batch_size=8
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    import lightning.pytorch as pl
    from lightning.pytorch.callbacks import EarlyStopping, LearningRateMonitor
except ImportError:
    import pytorch_lightning as pl
    from pytorch_lightning.callbacks import EarlyStopping, LearningRateMonitor

import torch
from omegaconf import DictConfig, ListConfig, OmegaConf, open_dict

import nemo.collections.asr as nemo_asr
from nemo.utils import logging
from nemo.utils.exp_manager import exp_manager


def _replace_missing_placeholders(value):
    """Recursively replace OmegaConf missing placeholders ('???') with
    serialization-safe plain values so Lightning hparams export won't fail."""
    if isinstance(value, dict):
        return {k: _replace_missing_placeholders(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_missing_placeholders(v) for v in value]
    if value == "???":
        return None
    return value


def _clear_missing_nodes_in_cfg(cfg_node):
    """Recursively replace OmegaConf MISSING nodes with None in-place."""
    if isinstance(cfg_node, DictConfig):
        with open_dict(cfg_node):
            for key in list(cfg_node.keys()):
                if OmegaConf.is_missing(cfg_node, key):
                    cfg_node[key] = None
                else:
                    _clear_missing_nodes_in_cfg(cfg_node[key])
    elif isinstance(cfg_node, ListConfig):
        for item in cfg_node:
            _clear_missing_nodes_in_cfg(item)


def parse_args():
    p = argparse.ArgumentParser(description="Train NeMo ASR model")
    p.add_argument("--config", type=str,
                   default="configs/fastconformer_ctc_bpe_medium.yaml")
    p.add_argument("--resume-from", type=str, default=None,
                   help="Path to a .nemo or .ckpt file to resume from")
    p.add_argument("--run-name", type=str, default=None,
                   help="Optional suffix to distinguish experiments")
    args, overrides = p.parse_known_args()
    return args, overrides


def apply_overrides(cfg, overrides: list[str]):
    for item in overrides:
        if "=" not in item:
            continue
        key, val = item.split("=", 1)
        try:
            val = int(val)
        except ValueError:
            try:
                val = float(val)
            except ValueError:
                if val.lower() == "true":
                    val = True
                elif val.lower() == "false":
                    val = False
        OmegaConf.update(cfg, key, val)
    return cfg


def main():
    args, overrides = parse_args()

    if not os.path.isfile(args.config):
        logging.error(f"Config not found: {args.config}")
        sys.exit(1)

    cfg = OmegaConf.load(args.config)
    cfg = apply_overrides(cfg, overrides)
    if args.run_name:
        cfg.name = f"{cfg.name}-{args.run_name}"

    logging.info(f"Config:\n{OmegaConf.to_yaml(cfg, resolve=True)}")

    # ── Pre-flight checks ────────────────────────────────────────────
    pretrained_name = cfg.get("pretrained_model_name")
    use_pretrained = bool(pretrained_name)
    if use_pretrained:
        logging.info(f"Using pretrained NeMo model: {pretrained_name}")
    else:
        tok_model = Path(cfg.model.tokenizer.dir) / "tokenizer.model"
        if not tok_model.exists():
            logging.error(f"Tokenizer not found: {tok_model}. "
                          "Run scripts/build_tokenizer.py first.")
            sys.exit(1)

    for ds in ["train_ds", "validation_ds"]:
        mf = cfg.model[ds].manifest_filepath
        if not os.path.isfile(mf):
            logging.error(f"Manifest not found: {mf}. "
                          "Run scripts/prepare_manifests.py first.")
            sys.exit(1)

    if torch.cuda.is_available():
        gpu = torch.cuda.get_device_name(0)
        vram = torch.cuda.get_device_properties(0).total_memory / 1e9
        logging.info(f"GPU: {gpu} ({vram:.1f} GB)")
    else:
        logging.warning("No GPU — training will be very slow.")

    # ── Experiment layout ────────────────────────────────────────────
    exp_root = Path(cfg.get("exp_manager", {}).get("exp_dir", "experiments"))
    run_dir = exp_root / cfg.name
    for sub in ("logs", "checkpoints", "configs", "summary"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    (run_dir / "configs" / "resolved_config.yaml").write_text(
        OmegaConf.to_yaml(cfg, resolve=True), encoding="utf-8")

    # ── Trainer ──────────────────────────────────────────────────────
    # NeMo exp_manager owns logger + checkpointing — disable Lightning's
    # built-in versions to avoid conflicts.  Deterministic off because
    # GPU CTC loss backward has no deterministic implementation.
    cfg.trainer.deterministic = False
    cfg.trainer.logger = False
    cfg.trainer.enable_checkpointing = False
    torch.use_deterministic_algorithms(False)

    trainer_cfg = OmegaConf.to_container(cfg.trainer, resolve=True)
    trainer_cfg["deterministic"] = False
    trainer_cfg["logger"] = False
    trainer_cfg["enable_checkpointing"] = False

    callbacks = [LearningRateMonitor(logging_interval="step")]
    es = cfg.get("early_stopping")
    if es and es.get("enabled", True):
        callbacks.append(EarlyStopping(
            monitor=es.get("monitor", "val_wer"),
            patience=es.get("patience", 10),
            mode=es.get("mode", "min"),
            min_delta=es.get("min_delta", 0.001),
            verbose=True,
        ))
    trainer_cfg["callbacks"] = callbacks
    trainer = pl.Trainer(**trainer_cfg)

    # ── Experiment manager ───────────────────────────────────────────
    exp_cfg = cfg.get("exp_manager")
    if exp_cfg:
        exp_manager(trainer, cfg=OmegaConf.create(
            OmegaConf.to_container(exp_cfg, resolve=True)))

    # Resolve dataset sub-configs once so OmegaConf interpolations work even
    # when these nodes are passed around outside the full cfg tree.
    train_ds_cfg = OmegaConf.create(
        OmegaConf.to_container(cfg.model.train_ds, resolve=True))
    val_ds_cfg = OmegaConf.create(
        OmegaConf.to_container(cfg.model.validation_ds, resolve=True))
    test_ds_cfg = None
    if "test_ds" in cfg.model:
        test_ds_cfg = OmegaConf.create(
            OmegaConf.to_container(cfg.model.test_ds, resolve=True))

    # ── Model ────────────────────────────────────────────────────────
    if args.resume_from and args.resume_from.endswith(".nemo"):
        logging.info(f"Restoring from {args.resume_from}")
        model = nemo_asr.models.ASRModel.restore_from(args.resume_from)
        model.setup_training_data(train_ds_cfg)
        model.setup_validation_data(val_ds_cfg)
    elif use_pretrained:
        logging.info(f"Loading pretrained checkpoint: {pretrained_name}")
        model = nemo_asr.models.ASRModel.from_pretrained(model_name=pretrained_name)
        model.set_trainer(trainer)
        # Pretrained NeMo checkpoints may keep mandatory placeholder values
        # (??? fields such as validation_ds.manifest_filepath, tokenizer.dir,
        # etc.). Lightning tries to serialize model.cfg before fit(), which
        # crashes on those placeholders. Sanitize the config first so all
        # missing mandatory nodes become ordinary values, then inject our
        # fine-tuning datasets / optimizer config.
        _clear_missing_nodes_in_cfg(model.cfg)
        safe_cfg_dict = OmegaConf.to_container(
            model.cfg, resolve=False, throw_on_missing=False)
        safe_cfg_dict = _replace_missing_placeholders(safe_cfg_dict)
        safe_cfg = OmegaConf.create(safe_cfg_dict)
        with open_dict(safe_cfg):
            safe_cfg.train_ds = train_ds_cfg
            safe_cfg.validation_ds = val_ds_cfg
            if test_ds_cfg is not None:
                safe_cfg.test_ds = test_ds_cfg
            if "optim" in cfg.model:
                safe_cfg.optim = OmegaConf.create(
                    OmegaConf.to_container(cfg.model.optim, resolve=True))
        model._cfg = safe_cfg
        model.setup_training_data(train_ds_cfg)
        model.setup_validation_data(val_ds_cfg)
        if "optim" in cfg.model:
            logging.info("Configuring fine-tuning optimizer from config")
            model.setup_optimization(cfg.model.optim)
    else:
        model = nemo_asr.models.EncDecCTCModelBPE(cfg=cfg.model, trainer=trainer)

    total = sum(p.numel() for p in model.parameters())
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    logging.info(f"Parameters: {total:,} total, {trainable:,} trainable")

    # ── Train ────────────────────────────────────────────────────────
    logging.info("Starting training...")
    trainer.fit(model)

    # ── Save ─────────────────────────────────────────────────────────
    final_nemo = run_dir / f"{cfg.name}_final.nemo"
    model.save_to(str(final_nemo))
    logging.info(f"Final model: {final_nemo}")

    ckpt_cb = trainer.checkpoint_callback
    best_path = ckpt_cb.best_model_path if ckpt_cb else ""
    best_score = (float(ckpt_cb.best_model_score)
                  if ckpt_cb and ckpt_cb.best_model_score is not None else None)

    if best_path and Path(best_path).exists():
        dest = run_dir / "checkpoints" / Path(best_path).name
        if Path(best_path).resolve() != dest.resolve():
            shutil.copy2(best_path, dest)
        logging.info(f"Best checkpoint: {best_path} (val_wer={best_score})")

    # ── Test ─────────────────────────────────────────────────────────
    test_metrics = {}
    test_mf = cfg.model.get("test_ds", {}).get("manifest_filepath")
    if test_mf and os.path.isfile(test_mf):
        logging.info("Evaluating on test set...")
        model.setup_test_data(test_ds_cfg)
        results = trainer.test(model)
        if results:
            test_metrics = results[0]

    # ── Summary ──────────────────────────────────────────────────────
    cb_metrics = {}
    for k, v in trainer.callback_metrics.items():
        try:
            cb_metrics[str(k)] = float(v)
        except Exception:
            cb_metrics[str(k)] = str(v)

    summary = {
        "name": cfg.name,
        "config": args.config,
        "timestamp_utc": datetime.utcnow().isoformat() + "Z",
        "best_checkpoint": best_path,
        "best_score": best_score,
        "final_nemo": str(final_nemo),
        "final_metrics": cb_metrics,
        "test_metrics": test_metrics,
    }
    summary_path = run_dir / "summary" / "run_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    logging.info(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
