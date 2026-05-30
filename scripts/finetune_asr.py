#!/usr/bin/env python3
"""Fine-tune NVIDIA's pretrained Arabic FastConformer on SADA.

This is the dedicated pretrained fine-tuning path rebuilt from the working
EMS-finetune project. Use this script for NeMo Arabic ASR fine-tuning on the
SADA manifests filtered to Najdi / Hijazi / Khaleeji dialects.
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from numbers import Number
from pathlib import Path

try:
    import lightning.pytorch as pl
    from lightning.pytorch.callbacks import EarlyStopping, LearningRateMonitor
except ImportError:
    import pytorch_lightning as pl
    from pytorch_lightning.callbacks import EarlyStopping, LearningRateMonitor

import torch
from omegaconf import OmegaConf, open_dict

import nemo.collections.asr as nemo_asr
from nemo.collections.asr.losses.rnnt import RNNTLoss
from nemo.utils import logging
from nemo.utils.exp_manager import exp_manager


DIALECT_ALIASES = {
    "najdi": "najdi",
    "hijazi": "hijazi",
    "hejazi": "hijazi",
    "khaleeji": "khaleeji",
    "khaliji": "khaleeji",
    "gulf": "khaleeji",
}


def parse_args():
    parser = argparse.ArgumentParser(description="Fine-tune Arabic ASR model")
    parser.add_argument(
        "--config",
        type=str,
        default="configs/finetune_ar_fastconformer.yaml",
        help="Path to YAML config file",
    )
    parser.add_argument(
        "--run-name",
        type=str,
        default=None,
        help="Optional suffix to distinguish experiments",
    )
    args, overrides = parser.parse_known_args()
    return args, overrides


def apply_overrides(cfg, overrides: list[str]):
    for override in overrides:
        if "=" not in override:
            continue
        key, value = override.split("=", 1)
        try:
            value = int(value)
        except ValueError:
            try:
                value = float(value)
            except ValueError:
                if value.lower() == "true":
                    value = True
                elif value.lower() == "false":
                    value = False
        OmegaConf.update(cfg, key, value)
    return cfg


def resolve_missing(conf, defaults=None):
    """Recursively replace OmegaConf MISSING (???) values with safe values."""
    if defaults is None:
        defaults = {}
    if not OmegaConf.is_dict(conf):
        return
    for key in list(conf):
        if OmegaConf.is_missing(conf, key):
            OmegaConf.update(conf, key, defaults.get(key, None), force_add=True)
        elif OmegaConf.is_dict(conf[key]):
            resolve_missing(conf[key], defaults)


def count_manifest_lines(path: str) -> int:
    with open(path, "r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def norm_dialect(value: str) -> str:
    return DIALECT_ALIASES.get(value.strip().lower(), value.strip().lower())


def load_dialect_index(data_dir: Path) -> dict[str, str]:
    index: dict[str, str] = {}
    for split in ["train", "validation", "test"]:
        tsv = data_dir / split / "metadata.tsv"
        if not tsv.exists():
            continue
        with open(tsv, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                audio_path = row.get("audio_filepath", "")
                dialect = norm_dialect(row.get("dialect", "unknown"))
                if not audio_path:
                    continue
                index[audio_path] = dialect
                index[Path(audio_path).name] = dialect
    return index


def create_dialect_manifest(
    source_manifest: str,
    dialect: str,
    dialect_index: dict[str, str],
    output_path: Path,
) -> dict:
    stats = {"dialect": dialect, "samples": 0, "duration_h": 0.0, "manifest": str(output_path)}
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(source_manifest, "r", encoding="utf-8") as f_in, open(
        output_path, "w", encoding="utf-8"
    ) as f_out:
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            audio_path = entry["audio_filepath"]
            entry_dialect = dialect_index.get(audio_path) or dialect_index.get(Path(audio_path).name)
            if entry_dialect != dialect:
                continue
            f_out.write(json.dumps(entry, ensure_ascii=False) + "\n")
            stats["samples"] += 1
            stats["duration_h"] += entry.get("duration", 0.0) / 3600

    return stats


def scalarize_metrics(metrics: dict) -> dict:
    out = {}
    for key, value in metrics.items():
        if torch.is_tensor(value):
            if value.numel() == 1:
                out[key] = float(value.detach().cpu().item())
        elif isinstance(value, Number):
            out[key] = float(value)
    return out


def log_metrics_to_tensorboard(trainer, prefix: str, metrics: dict):
    scalar_metrics = scalarize_metrics(metrics)
    if not scalar_metrics:
        return

    loggers = getattr(trainer, "loggers", None)
    if not loggers:
        logger = getattr(trainer, "logger", None)
        loggers = [logger] if logger else []

    for logger in loggers:
        experiment = getattr(logger, "experiment", None)
        if experiment is None or not hasattr(experiment, "add_scalar"):
            continue
        for key, value in scalar_metrics.items():
            experiment.add_scalar(f"{prefix}/{key}", value, trainer.global_step)


def snapshot_metric_modes(asr_model) -> dict:
    return {
        "cur_decoder": getattr(asr_model, "cur_decoder", "rnnt"),
        "rnnt_use_cer": getattr(getattr(asr_model, "wer", None), "use_cer", False),
        "ctc_use_cer": getattr(getattr(asr_model, "ctc_wer", None), "use_cer", False),
        "rnnt_log_prediction": getattr(getattr(asr_model, "wer", None), "log_prediction", False),
        "ctc_log_prediction": getattr(getattr(asr_model, "ctc_wer", None), "log_prediction", False),
    }


def set_metric_modes(asr_model, decoder_type: str, use_cer: bool, log_prediction: bool):
    OmegaConf.set_struct(asr_model.cfg, False)
    asr_model.cfg.use_cer = use_cer
    asr_model.cfg.log_prediction = log_prediction
    if hasattr(asr_model.cfg, "aux_ctc"):
        asr_model.cfg.aux_ctc.use_cer = use_cer
    OmegaConf.set_struct(asr_model.cfg, True)

    if hasattr(asr_model, "wer"):
        asr_model.wer.use_cer = use_cer
        asr_model.wer.log_prediction = log_prediction
    if hasattr(asr_model, "ctc_wer"):
        asr_model.ctc_wer.use_cer = use_cer
        asr_model.ctc_wer.log_prediction = log_prediction

    if getattr(asr_model, "cur_decoder", "rnnt") != decoder_type:
        asr_model.change_decoding_strategy(decoder_type=decoder_type, verbose=False)


def restore_metric_modes(asr_model, snapshot: dict):
    set_metric_modes(
        asr_model,
        decoder_type=snapshot["cur_decoder"],
        use_cer=snapshot["rnnt_use_cer"] if snapshot["cur_decoder"] == "rnnt" else snapshot["ctc_use_cer"],
        log_prediction=snapshot["rnnt_log_prediction"]
        if snapshot["cur_decoder"] == "rnnt"
        else snapshot["ctc_log_prediction"],
    )
    if hasattr(asr_model, "wer"):
        asr_model.wer.use_cer = snapshot["rnnt_use_cer"]
        asr_model.wer.log_prediction = snapshot["rnnt_log_prediction"]
    if hasattr(asr_model, "ctc_wer"):
        asr_model.ctc_wer.use_cer = snapshot["ctc_use_cer"]
        asr_model.ctc_wer.log_prediction = snapshot["ctc_log_prediction"]


def evaluate_split(
    trainer,
    asr_model,
    ds_cfg,
    manifest_path: str,
    stage: str,
    decoder_type: str = "rnnt",
    error_rate: str = "wer",
    log_prediction: bool = False,
) -> dict:
    snapshot = snapshot_metric_modes(asr_model)
    set_metric_modes(
        asr_model,
        decoder_type=decoder_type,
        use_cer=(error_rate == "cer"),
        log_prediction=log_prediction,
    )

    eval_cfg = OmegaConf.create(OmegaConf.to_container(ds_cfg, resolve=True))
    eval_cfg.manifest_filepath = manifest_path
    try:
        if stage == "validation":
            asr_model.setup_validation_data(eval_cfg)
            results = trainer.validate(asr_model, ckpt_path=None, verbose=False)
        else:
            asr_model.setup_test_data(eval_cfg)
            results = trainer.test(asr_model, ckpt_path=None, verbose=False)
        return scalarize_metrics(results[0]) if results else {}
    finally:
        restore_metric_modes(asr_model, snapshot)


def run_eval_matrix(
    trainer,
    asr_model,
    ds_cfg,
    manifest_path: str,
    stage: str,
    decoder_types: list[str],
    error_rates: list[str],
    log_prediction: bool,
    tb_prefix: str,
) -> dict:
    out = {}
    for decoder_type in decoder_types:
        out[decoder_type] = {}
        for error_rate in error_rates:
            metrics = evaluate_split(
                trainer,
                asr_model,
                ds_cfg,
                manifest_path,
                stage=stage,
                decoder_type=decoder_type,
                error_rate=error_rate,
                log_prediction=log_prediction,
            )
            out[decoder_type][error_rate] = metrics
            log_metrics_to_tensorboard(trainer, f"{tb_prefix}/{decoder_type}/{error_rate}", metrics)
    return out


def main():
    args, overrides = parse_args()

    if not os.path.isfile(args.config):
        logging.error(f"Config not found: {args.config}")
        sys.exit(1)

    cfg = OmegaConf.load(args.config)
    cfg = apply_overrides(cfg, overrides)
    if args.run_name:
        cfg.name = f"{cfg.name}-{args.run_name}"

    # GPU diagnostics
    if torch.cuda.is_available():
        gpu = torch.cuda.get_device_name(0)
        vram = torch.cuda.get_device_properties(0).total_memory / 1e9
        logging.info(f"GPU: {gpu} ({vram:.1f} GB VRAM)")
    else:
        logging.warning("No GPU detected!")

    pretrained_name = cfg.get("pretrained_model", "nvidia/stt_ar_fastconformer_hybrid_large_pcd_v1.0")
    logging.info(f"Loading pretrained model: {pretrained_name}")
    logging.info("This will download the model from NVIDIA NGC / HF cache on first run.")

    asr_model = nemo_asr.models.ASRModel.from_pretrained(model_name=pretrained_name, map_location="cpu")
    logging.info(f"Model loaded: {type(asr_model).__name__}")
    total_params = sum(p.numel() for p in asr_model.parameters())
    logging.info(f"Pretrained model parameters: {total_params:,}")

    train_manifest = str(cfg.model.train_ds.manifest_filepath)
    val_manifest = str(cfg.model.validation_ds.manifest_filepath)
    test_manifest = str(cfg.model.test_ds.manifest_filepath) if "test_ds" in cfg.model else val_manifest

    for mf, name in [(train_manifest, "train"), (val_manifest, "validation"), (test_manifest, "test")]:
        if not os.path.isfile(mf):
            logging.error(f"{name} manifest not found: {mf}")
            sys.exit(1)

    logging.info(f"Training samples: {count_manifest_lines(train_manifest):,}")
    logging.info(f"Validation samples: {count_manifest_lines(val_manifest):,}")
    logging.info(f"Test samples: {count_manifest_lines(test_manifest):,}")

    dialect_eval_cfg = cfg.get("dialect_eval", {})
    dialect_eval_enabled = dialect_eval_cfg.get("enabled", True)
    dialects = [norm_dialect(d) for d in dialect_eval_cfg.get("dialects", ["najdi", "hijazi", "khaleeji"])]
    dialect_index = {}
    if dialect_eval_enabled:
        data_dir = Path(str(cfg.get("data_dir", "data")))
        dialect_index = load_dialect_index(data_dir)
        if not dialect_index:
            dialect_eval_enabled = False
            logging.warning(f"Dialect evaluation disabled: no metadata index found under {data_dir}")
        else:
            logging.info(f"Dialect evaluation enabled for: {', '.join(dialects)}")

    metric_logging_cfg = cfg.get("metric_logging", {})
    metric_decoder_types = [str(x).lower() for x in metric_logging_cfg.get("decoder_types", ["rnnt", "ctc"])]
    metric_error_rates = [str(x).lower() for x in metric_logging_cfg.get("error_rates", ["wer", "cer"])]
    metric_log_prediction = bool(metric_logging_cfg.get("log_predictions", True))
    training_logging_cfg = cfg.get("training_logging", {})

    # Rebuild pretrained model config with our dataset paths/settings.
    OmegaConf.set_struct(asr_model.cfg, False)
    resolve_missing(asr_model.cfg)

    asr_model.cfg.train_ds.manifest_filepath = train_manifest
    asr_model.cfg.train_ds.tarred_audio_filepaths = None
    asr_model.cfg.train_ds.is_tarred = False
    asr_model.cfg.train_ds.batch_size = cfg.model.train_ds.batch_size
    asr_model.cfg.train_ds.num_workers = cfg.model.train_ds.get("num_workers", 4)
    asr_model.cfg.train_ds.pin_memory = cfg.model.train_ds.get("pin_memory", True)
    asr_model.cfg.train_ds.shuffle = True
    asr_model.cfg.train_ds.max_duration = cfg.model.train_ds.get("max_duration", 20.0)
    asr_model.cfg.train_ds.min_duration = cfg.model.train_ds.get("min_duration", 0.3)
    asr_model.cfg.train_ds.sample_rate = cfg.model.train_ds.get("sample_rate", 16000)
    asr_model.cfg.train_ds.trim_silence = cfg.model.train_ds.get("trim_silence", False)

    asr_model.cfg.validation_ds.manifest_filepath = val_manifest
    asr_model.cfg.validation_ds.batch_size = cfg.model.validation_ds.batch_size
    asr_model.cfg.validation_ds.num_workers = cfg.model.validation_ds.get("num_workers", 4)
    asr_model.cfg.validation_ds.pin_memory = cfg.model.validation_ds.get("pin_memory", True)
    asr_model.cfg.validation_ds.shuffle = False
    asr_model.cfg.validation_ds.max_duration = cfg.model.validation_ds.get("max_duration", 20.0)
    asr_model.cfg.validation_ds.min_duration = cfg.model.validation_ds.get("min_duration", 0.3)
    asr_model.cfg.validation_ds.sample_rate = cfg.model.validation_ds.get("sample_rate", 16000)

    asr_model.cfg.test_ds.manifest_filepath = test_manifest
    asr_model.cfg.test_ds.batch_size = cfg.model.test_ds.get("batch_size", 8)
    asr_model.cfg.test_ds.num_workers = cfg.model.test_ds.get("num_workers", 4)
    asr_model.cfg.test_ds.pin_memory = cfg.model.test_ds.get("pin_memory", True)
    asr_model.cfg.test_ds.shuffle = False
    asr_model.cfg.test_ds.max_duration = cfg.model.test_ds.get("max_duration", 20.0)
    asr_model.cfg.test_ds.min_duration = cfg.model.test_ds.get("min_duration", 0.3)
    asr_model.cfg.test_ds.sample_rate = cfg.model.test_ds.get("sample_rate", 16000)

    OmegaConf.set_struct(asr_model.cfg, True)

    asr_model.setup_training_data(asr_model.cfg.train_ds)
    asr_model.setup_validation_data(asr_model.cfg.validation_ds)

    if "optim" in cfg.model:
        optim_cfg = OmegaConf.to_container(cfg.model.optim, resolve=True)
        asr_model.setup_optimization(OmegaConf.create(optim_cfg))
        logging.info(f"Optimizer: {optim_cfg['name']}, LR: {optim_cfg['lr']}")

    if "loss" in cfg.model:
        loss_cfg = OmegaConf.to_container(cfg.model.loss, resolve=True)
        loss_name = loss_cfg.get("loss_name", "default")
        loss_kwargs = loss_cfg.get(f"{loss_name}_kwargs", loss_cfg.get("loss_kwargs"))
        asr_model.loss = RNNTLoss(
            num_classes=asr_model.joint.num_classes_with_blank - 1,
            loss_name=loss_name,
            loss_kwargs=loss_kwargs,
        )
        if hasattr(asr_model.joint, "set_loss"):
            asr_model.joint.set_loss(asr_model.loss)
        logging.info(f"RNNT loss backend: {loss_name}")

    # Keep fit-time metrics lightweight. Full report metrics are computed after training.
    fit_log_predictions = bool(training_logging_cfg.get("log_predictions", False))
    fit_use_cer = bool(training_logging_cfg.get("use_cer_during_fit", False))
    OmegaConf.set_struct(asr_model.cfg, False)
    with open_dict(asr_model.cfg):
        asr_model.cfg.log_prediction = fit_log_predictions
        asr_model.cfg.use_cer = fit_use_cer
        if hasattr(asr_model.cfg, "aux_ctc"):
            asr_model.cfg.aux_ctc.use_cer = fit_use_cer
            if hasattr(asr_model.cfg.aux_ctc, "decoding"):
                asr_model.cfg.aux_ctc.decoding.strategy = training_logging_cfg.get(
                    "ctc_decoding_strategy", "greedy_batch"
                )
    OmegaConf.set_struct(asr_model.cfg, True)

    if hasattr(asr_model, "wer"):
        asr_model.wer.log_prediction = fit_log_predictions
        asr_model.wer.use_cer = fit_use_cer
    if hasattr(asr_model, "ctc_wer"):
        asr_model.ctc_wer.log_prediction = fit_log_predictions
        asr_model.ctc_wer.use_cer = fit_use_cer
    if hasattr(asr_model, "change_decoding_strategy") and hasattr(asr_model.cfg, "aux_ctc"):
        asr_model.change_decoding_strategy(
            decoder_type="ctc", decoding_cfg=asr_model.cfg.aux_ctc.decoding, verbose=False
        )
        asr_model.change_decoding_strategy(decoder_type="rnnt", verbose=False)
    logging.info(
        "Fit-time metrics: "
        f"log_prediction={fit_log_predictions}, use_cer={fit_use_cer}, "
        f"ctc_strategy={training_logging_cfg.get('ctc_decoding_strategy', 'greedy_batch')}"
    )

    if "spec_augment" in cfg.model:
        OmegaConf.set_struct(asr_model.cfg, False)
        asr_model.cfg.spec_augment = cfg.model.spec_augment
        OmegaConf.set_struct(asr_model.cfg, True)
        logging.info("Updated SpecAugment config")

    trainer_cfg = OmegaConf.to_container(cfg.trainer, resolve=True)
    trainer_cfg.pop("callbacks", None)
    trainer_cfg["callbacks"] = [
        EarlyStopping(
            monitor=cfg.get("early_stopping", {}).get("monitor", "val_wer"),
            patience=cfg.get("early_stopping", {}).get("patience", 6),
            mode=cfg.get("early_stopping", {}).get("mode", "min"),
            min_delta=cfg.get("early_stopping", {}).get("min_delta", 0.001),
            verbose=True,
            check_on_train_epoch_end=False,
        ),
        LearningRateMonitor(logging_interval="step"),
    ]
    trainer = pl.Trainer(**trainer_cfg)

    exp_cfg = OmegaConf.to_container(cfg.get("exp_manager", {}), resolve=True)
    if exp_cfg:
        exp_manager(trainer, cfg=OmegaConf.create(exp_cfg))

    asr_model.set_trainer(trainer)

    logging.info("Starting fine-tuning...")
    logging.info(f"  Epochs       : {cfg.trainer.max_epochs}")
    logging.info(f"  Batch size   : {cfg.model.train_ds.batch_size}")
    logging.info(f"  Grad accum   : {cfg.trainer.accumulate_grad_batches}")
    logging.info(f"  Effective BS : {cfg.model.train_ds.batch_size * cfg.trainer.accumulate_grad_batches}")
    logging.info(f"  Precision    : {cfg.trainer.precision}")
    logging.info(f"  Train mf     : {train_manifest}")
    logging.info(f"  Val mf       : {val_manifest}")
    logging.info(f"  Test mf      : {test_manifest}")

    trainer.fit(asr_model)

    exp_dir = Path(cfg.get("exp_manager", {}).get("exp_dir", "experiments"))
    final_nemo = exp_dir / f"{cfg.name}_final.nemo"
    asr_model.save_to(str(final_nemo))
    logging.info(f"Fine-tuned model saved to: {final_nemo}")

    validation_metrics = {}
    test_metrics = {}
    dialect_artifacts = {"validation": {}, "test": {}}

    logging.info("Running overall validation evaluation...")
    validation_metrics["overall"] = run_eval_matrix(
        trainer,
        asr_model,
        cfg.model.validation_ds,
        val_manifest,
        stage="validation",
        decoder_types=metric_decoder_types,
        error_rates=metric_error_rates,
        log_prediction=metric_log_prediction,
        tb_prefix="rich_eval/overall_validation",
    )

    if os.path.isfile(test_manifest):
        logging.info("Running overall test evaluation...")
        test_metrics["overall"] = run_eval_matrix(
            trainer,
            asr_model,
            cfg.model.test_ds,
            test_manifest,
            stage="test",
            decoder_types=metric_decoder_types,
            error_rates=metric_error_rates,
            log_prediction=metric_log_prediction,
            tb_prefix="rich_eval/overall_test",
        )

    if dialect_eval_enabled:
        dialect_manifest_dir = exp_dir / cfg.name / "dialect_manifests"
        validation_metrics["dialects"] = {}
        test_metrics["dialects"] = {}

        for dialect in dialects:
            val_output = dialect_manifest_dir / "validation" / f"{dialect}_manifest.json"
            val_stats = create_dialect_manifest(val_manifest, dialect, dialect_index, val_output)
            dialect_artifacts["validation"][dialect] = val_stats
            if val_stats["samples"] > 0:
                logging.info(
                    f"Running validation evaluation for {dialect}: "
                    f"{val_stats['samples']} samples ({val_stats['duration_h']:.2f}h)"
                )
                metrics = run_eval_matrix(
                    trainer,
                    asr_model,
                    cfg.model.validation_ds,
                    str(val_output),
                    stage="validation",
                    decoder_types=metric_decoder_types,
                    error_rates=metric_error_rates,
                    log_prediction=metric_log_prediction,
                    tb_prefix=f"rich_eval/validation_{dialect}",
                )
                validation_metrics["dialects"][dialect] = metrics

            test_output = dialect_manifest_dir / "test" / f"{dialect}_manifest.json"
            test_stats = create_dialect_manifest(test_manifest, dialect, dialect_index, test_output)
            dialect_artifacts["test"][dialect] = test_stats
            if test_stats["samples"] > 0:
                logging.info(
                    f"Running test evaluation for {dialect}: "
                    f"{test_stats['samples']} samples ({test_stats['duration_h']:.2f}h)"
                )
                metrics = run_eval_matrix(
                    trainer,
                    asr_model,
                    cfg.model.test_ds,
                    str(test_output),
                    stage="test",
                    decoder_types=metric_decoder_types,
                    error_rates=metric_error_rates,
                    log_prediction=metric_log_prediction,
                    tb_prefix=f"rich_eval/test_{dialect}",
                )
                test_metrics["dialects"][dialect] = metrics

    summary = {
        "name": cfg.name,
        "config": args.config,
        "pretrained_model": pretrained_name,
        "timestamp_utc": datetime.utcnow().isoformat() + "Z",
        "final_nemo": str(final_nemo),
        "train_manifest": train_manifest,
        "validation_manifest": val_manifest,
        "test_manifest": test_manifest,
        "trainer_callback_metrics": scalarize_metrics(dict(trainer.callback_metrics)),
        "trainer_logged_metrics": scalarize_metrics(dict(trainer.logged_metrics)),
        "trainer_progress_bar_metrics": scalarize_metrics(dict(trainer.progress_bar_metrics)),
        "validation_metrics": validation_metrics,
        "test_metrics": test_metrics,
        "dialect_artifacts": dialect_artifacts,
        "metric_logging": {
            "decoder_types": metric_decoder_types,
            "error_rates": metric_error_rates,
            "log_predictions": metric_log_prediction,
        },
    }
    summary_dir = exp_dir / cfg.name / "summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / "run_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
