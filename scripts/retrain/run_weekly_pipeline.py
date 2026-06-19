"""Iterative weekly fine-tuning pipeline for GRU4Rec V9.

Splits Feb 2020 raw events into 4 weekly increments, then for each week:
  1. Computes item-popularity drift (JSD) vs. the Jan 2020 baseline
  2. If drift > threshold OR week 1: fine-tunes from the current checkpoint
     for FINETUNE_EPOCHS epochs at a lower LR
  3. Evaluates the fine-tuned model on val_sessions.parquet
  4. Promotes the new checkpoint if NDCG@20 improved ≥ IMPROVE_THRESHOLD
  5. Logs metrics to DagsHub MLflow (optional)

Design choice — fine-tune, not retrain:
  Full retraining is ~10 h on A100 / ~35 h on T4.  Fine-tuning 5 epochs
  from the existing checkpoint takes ~2 h on T4 per weekly increment (4 runs =
  ~8 h total, easily split across Colab sessions via --weeks flag).
  The checkpoint dir is on Google Drive, so it persists between sessions.

Colab usage (4 separate sessions, one per week):
    # Mount Drive first in Colab, then:
    !python scripts/retrain/run_weekly_pipeline.py \\
        --feb-csv      /content/drive/MyDrive/rees46/2020-Feb.csv.gz \\
        --jan-csv      /content/drive/MyDrive/rees46/2020-Jan.csv.gz \\
        --base-ckpt    model/model_inference.pt \\
        --vocabs-path  model/vocabs.pkl \\
        --val-sessions artifacts/1M/sequences/val_sessions.parquet \\
        --ckpt-dir     /content/drive/MyDrive/recosys_weekly \\
        --weeks        1     # Change to 2, 3, 4 in subsequent sessions

    # Log to DagsHub (optional):
        --dagshub-username <u> --dagshub-token <t>
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import sys
import time
from pathlib import Path

# ensure repo root is on sys.path regardless of where the script is invoked from
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd
import torch
import torch.optim as optim
from torch.utils.data import DataLoader

from src.sequence.data.session_dataset import SessionEvalDataset, SessionTrainDataset
from src.sequence.evaluation.evaluate_sequence import evaluate_sessions
from src.sequence.models.gru4rec import GRU4RecModel
from src.sequence.training.train_sequence import get_param_groups, train_epoch_session

# ── Pipeline constants ────────────────────────────────────────────────────────

FINETUNE_EPOCHS   = 5       # epochs per weekly increment
FINETUNE_LR       = 1e-4   # 3× lower than original 3e-4
FINETUNE_LR_MIN   = 1e-5
BATCH_SIZE        = 128     # lower than training (T4 has less VRAM during fine-tune)
DRIFT_THRESHOLD   = 0.10   # JSD above this triggers fine-tuning (paper threshold)
IMPROVE_THRESHOLD = 0.0005  # promote only if NDCG@20 improves by ≥ 0.05%
MAX_SEQ_LEN       = 20
MIN_SEQ_LEN       = 2

# Week date ranges (inclusive start, exclusive end)
_WEEK_RANGES = {
    1: ("2020-02-01", "2020-02-08"),
    2: ("2020-02-08", "2020-02-15"),
    3: ("2020-02-15", "2020-02-22"),
    4: ("2020-02-22", "2020-03-01"),
}

EVENT_TYPE_MAP = {"view": 1, "cart": 2, "purchase": 3, "remove_from_cart": 0}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _step(msg: str) -> float:
    print(f"\n  ▶ {msg} ...")
    return time.time()


def _done(t0: float, label: str = "") -> None:
    elapsed = int(time.time() - t0)
    print(f"     done in {elapsed // 60}m {elapsed % 60}s" + (f" — {label}" if label else ""))


def _load_raw_csv(path: Path, user2idx: dict, item2idx: dict) -> pd.DataFrame:
    """Load a raw REES46 CSV and filter to in-vocab users."""
    t0 = _step(f"Loading {path.name}")
    df = pd.read_csv(
        path,
        usecols=["event_time", "event_type", "product_id", "user_id", "user_session"],
        low_memory=False,
    )
    df["product_id"] = pd.to_numeric(df["product_id"], errors="coerce")
    df["user_id"]    = pd.to_numeric(df["user_id"],    errors="coerce")
    df = df.dropna(subset=["product_id", "user_id", "user_session"])
    df["product_id"] = df["product_id"].astype(np.int64)
    df["user_id"]    = df["user_id"].astype(np.int64)
    df = df[df["user_id"].isin(user2idx)].copy()
    df["user_idx"]   = df["user_id"].map(user2idx).astype(np.int64)
    df["item_idx"]   = df["product_id"].map(item2idx)
    df["event_idx"]  = df["event_type"].str.lower().map(EVENT_TYPE_MAP)
    df["event_time"] = pd.to_datetime(df["event_time"], utc=True)
    df = df.dropna(subset=["item_idx", "event_idx"])
    df["item_idx"]   = df["item_idx"].astype(np.int64)
    df["event_idx"]  = df["event_idx"].astype(np.int64)
    _done(t0, label=f"{len(df):,} in-vocab events")
    return df[["event_time", "user_idx", "item_idx", "event_idx", "user_session"]]


def _remove_consecutive_repeats(
    items: np.ndarray, events: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    if items.shape[0] <= 1:
        return items, events
    keep = np.ones(items.shape[0], dtype=bool)
    keep[1:] = items[1:] != items[:-1]
    return items[keep], events[keep]


def _build_sessions(df: pd.DataFrame) -> pd.DataFrame:
    """Build session parquet DataFrame from event-level DataFrame."""
    df = df.sort_values(
        ["user_idx", "user_session", "event_time"], kind="mergesort"
    ).reset_index(drop=True)

    grouped = (
        df.groupby(["user_idx", "user_session"], sort=False, as_index=False)
          .agg(item_seq=("item_idx", list), event_seq=("event_idx", list))
    )
    new_items, new_events = [], []
    for items, events in zip(
        grouped["item_seq"].to_numpy(), grouped["event_seq"].to_numpy()
    ):
        ai, ae = np.asarray(items, np.int64), np.asarray(events, np.int64)
        ai, ae = _remove_consecutive_repeats(ai, ae)
        if ai.shape[0] > MAX_SEQ_LEN:
            ai, ae = ai[-MAX_SEQ_LEN:], ae[-MAX_SEQ_LEN:]
        new_items.append(ai.tolist())
        new_events.append(ae.tolist())
    grouped["item_seq"]  = new_items
    grouped["event_seq"] = new_events
    grouped["seq_len"]   = grouped["item_seq"].apply(len).astype(np.int64)
    grouped = grouped[grouped["seq_len"] >= MIN_SEQ_LEN].reset_index(drop=True)
    grouped.insert(0, "session_idx", np.arange(len(grouped), dtype=np.int64))
    grouped["user_idx"] = grouped["user_idx"].astype(np.int64)
    return grouped[["session_idx", "user_idx", "item_seq", "event_seq", "seq_len"]]


def _compute_jsd(ref_events: pd.DataFrame, new_events: pd.DataFrame, n_items: int) -> float:
    """Jensen-Shannon divergence between item-popularity distributions."""
    def _freq(df: pd.DataFrame) -> np.ndarray:
        counts = np.zeros(n_items, dtype=np.float64)
        for idx, cnt in df["item_idx"].value_counts().items():
            if 0 <= idx < n_items:
                counts[int(idx)] = cnt
        total = counts.sum()
        return counts / total if total > 0 else counts

    p = _freq(ref_events)
    q = _freq(new_events)
    m = 0.5 * (p + q)

    def _kl(a: np.ndarray, b: np.ndarray) -> float:
        mask = (a > 0) & (b > 0)
        return float(np.sum(a[mask] * np.log(a[mask] / b[mask])))

    jsd = 0.5 * _kl(p, m) + 0.5 * _kl(q, m)
    return float(np.clip(jsd / math.log(2), 0.0, 1.0))  # normalize to [0, 1]


def _load_checkpoint(ckpt_path: Path, device: torch.device) -> tuple[GRU4RecModel, dict]:
    """Load a checkpoint and return the model + hparams."""
    ckpt = torch.load(str(ckpt_path), map_location=device, weights_only=False)
    hp   = ckpt["hparams"]
    model = GRU4RecModel(
        n_items       = int(hp["n_items"]),
        n_event_types = 4,
        embed_dim     = int(hp["embed_dim"]),
        gru_hidden    = int(hp["gru_hidden"]),
        n_layers      = int(hp["n_layers"]),
        dropout       = float(hp.get("dropout", 0.3)),
    ).to(device)
    model.load_state_dict(ckpt["model_state"])
    return model, hp


def _evaluate(
    model: GRU4RecModel,
    val_df: pd.DataFrame,
    train_sessions_df: pd.DataFrame,
    n_items: int,
    device: torch.device,
    label: str,
) -> dict:
    """Evaluate model on val_sessions. Returns metrics dict."""
    eval_ds = SessionEvalDataset(val_df, max_seq_len=MAX_SEQ_LEN)
    return evaluate_sessions(
        model            = model,
        prefix_item_arr  = eval_ds.prefix_item_arr,
        prefix_event_arr = eval_ds.prefix_event_arr,
        target_items     = eval_ds.target_items,
        train_sessions_df= train_sessions_df,
        n_items          = n_items,
        device           = device,
        batch_size       = 512,
        n_faiss_candidates = 50,
        label            = label,
        normalize        = True,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Iterative weekly fine-tuning pipeline for GRU4Rec V9"
    )
    parser.add_argument("--feb-csv",       required=True, help="Path to 2020-Feb.csv[.gz]")
    parser.add_argument("--jan-csv",       required=True, help="Path to 2020-Jan.csv[.gz] (drift baseline)")
    parser.add_argument("--base-ckpt",     default="model/model_inference.pt")
    parser.add_argument("--vocabs-path",   default="model/vocabs.pkl")
    parser.add_argument("--val-sessions",  required=True, help="Path to val_sessions.parquet")
    parser.add_argument("--ckpt-dir",      required=True, help="Directory to save weekly checkpoints")
    parser.add_argument(
        "--weeks",
        type=int,
        nargs="+",
        default=[1, 2, 3, 4],
        help="Which weeks to process (default: 1 2 3 4). Run one per Colab session.",
    )
    parser.add_argument("--finetune-epochs",    type=int,   default=FINETUNE_EPOCHS)
    parser.add_argument("--lr",                 type=float, default=FINETUNE_LR)
    parser.add_argument("--batch-size",         type=int,   default=BATCH_SIZE)
    parser.add_argument("--drift-threshold",    type=float, default=DRIFT_THRESHOLD)
    parser.add_argument("--improve-threshold",  type=float, default=IMPROVE_THRESHOLD)
    parser.add_argument("--dagshub-username",   default=None)
    parser.add_argument("--dagshub-token",      default=None)
    parser.add_argument("--mlflow-uri",         default=None)
    args = parser.parse_args()

    ckpt_dir = Path(args.ckpt_dir)
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"\n{'=' * 60}")
    print(f"RecoSys — Weekly Fine-Tuning Pipeline")
    print(f"{'=' * 60}")
    print(f"  Device      : {device}")
    print(f"  Weeks       : {args.weeks}")
    print(f"  Epochs/week : {args.finetune_epochs}")
    print(f"  LR          : {args.lr}")

    # ── MLflow setup ─────────────────────────────────────────────────────────
    mlflow_enabled = False
    try:
        import mlflow
        import os
        if args.dagshub_username and args.dagshub_token:
            tracking_uri = args.mlflow_uri or f"https://dagshub.com/{args.dagshub_username}/RecoSys.mlflow"
            os.environ["MLFLOW_TRACKING_USERNAME"] = args.dagshub_username
            os.environ["MLFLOW_TRACKING_PASSWORD"] = args.dagshub_token
        elif args.mlflow_uri:
            tracking_uri = args.mlflow_uri
        else:
            tracking_uri = None
        if tracking_uri:
            mlflow.set_tracking_uri(tracking_uri)
            mlflow.set_experiment("gru4rec_v9_weekly_finetune")
            mlflow_enabled = True
    except ImportError:
        print("  MLflow not installed — skipping experiment logging")

    # ── Load vocabs ───────────────────────────────────────────────────────────
    t0 = _step("Loading vocabs")
    with open(args.vocabs_path, "rb") as f:
        vocabs: dict = pickle.load(f)
    user2idx: dict = vocabs["user2idx"]
    item2idx: dict = vocabs["item2idx"]
    n_items = len(item2idx) + 1  # +1 for PAD
    _done(t0, label=f"{len(user2idx):,} users / {n_items:,} items")

    # ── Load validation sessions ──────────────────────────────────────────────
    t0 = _step("Loading val sessions")
    val_df = pd.read_parquet(args.val_sessions)
    _done(t0, label=f"{len(val_df):,} sessions")

    # ── Load Jan events as drift baseline ─────────────────────────────────────
    t0 = _step("Loading Jan 2020 events for drift baseline")
    jan_events = _load_raw_csv(Path(args.jan_csv), user2idx, item2idx)

    # ── Load all Feb events once (split by week inside loop) ──────────────────
    t0 = _step("Loading Feb 2020 events")
    feb_events = _load_raw_csv(Path(args.feb_csv), user2idx, item2idx)

    # ── Load initial "current best" checkpoint ────────────────────────────────
    current_ckpt = ckpt_dir / "current_best.pt"
    if not current_ckpt.exists():
        import shutil
        shutil.copy2(args.base_ckpt, current_ckpt)
        print(f"  Copied base checkpoint → {current_ckpt}")

    # Load baseline NDCG from state file
    state_path = ckpt_dir / "pipeline_state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
    else:
        # Evaluate baseline model to set the starting NDCG
        print("\n  Evaluating baseline model ...")
        model, hp = _load_checkpoint(current_ckpt, device)
        base_model_metrics = _evaluate(model, val_df, val_df, n_items, device, "baseline")
        state = {
            "current_ndcg_20": float(base_model_metrics["ndcg_20"]),
            "history": [],
        }
        state_path.write_text(json.dumps(state, indent=2))
        print(f"  Baseline NDCG@20: {state['current_ndcg_20']:.4f}")

    # ── Weekly loop ───────────────────────────────────────────────────────────
    for week in sorted(args.weeks):
        if week not in _WEEK_RANGES:
            print(f"  WARNING: week {week} not in 1-4, skipping")
            continue

        start_str, end_str = _WEEK_RANGES[week]
        start_ts = pd.Timestamp(start_str, tz="UTC")
        end_ts   = pd.Timestamp(end_str,   tz="UTC")

        print(f"\n{'─' * 60}")
        print(f"  WEEK {week}: {start_str} → {end_str}")
        print(f"{'─' * 60}")

        # Filter this week's events
        week_mask   = (feb_events["event_time"] >= start_ts) & (feb_events["event_time"] < end_ts)
        week_events = feb_events.loc[week_mask].copy()
        print(f"  {len(week_events):,} events in week {week}")

        if len(week_events) < 100:
            print(f"  Too few events for week {week}, skipping")
            continue

        # ── 1. Drift score ────────────────────────────────────────────────────
        jsd = _compute_jsd(jan_events, week_events, n_items)
        print(f"  Drift (JSD vs Jan 2020): {jsd:.4f} (threshold={args.drift_threshold})")

        should_finetune = (jsd >= args.drift_threshold) or (week == 1)
        if not should_finetune:
            print(f"  Drift below threshold — skipping fine-tuning for week {week}")
            state["history"].append({
                "week": week, "jsd": jsd, "action": "skipped", "reason": "below_threshold"
            })
            state_path.write_text(json.dumps(state, indent=2))
            continue

        # ── 2. Build session sequences for this week ──────────────────────────
        t0 = _step(f"Building sessions for week {week}")
        week_sessions = _build_sessions(week_events)
        _done(t0, label=f"{len(week_sessions):,} sessions")

        if len(week_sessions) < 50:
            print(f"  Too few sessions for week {week}, skipping fine-tuning")
            continue

        train_ds = SessionTrainDataset(week_sessions, max_seq_len=MAX_SEQ_LEN)
        train_loader = DataLoader(
            train_ds,
            batch_size  = args.batch_size,
            shuffle     = True,
            num_workers = 2,
            pin_memory  = device.type == "cuda",
            drop_last   = False,
        )
        print(f"  Train loader: {len(train_loader):,} batches × {args.batch_size}")

        # ── 3. Fine-tune ───────────────────────────────────────────────────────
        model, hp = _load_checkpoint(current_ckpt, device)
        optimizer = optim.AdamW(
            get_param_groups(model, lr=args.lr, weight_decay=1e-5)
        )
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer, T_max=args.finetune_epochs, eta_min=FINETUNE_LR_MIN
        )

        epoch_losses = []
        for epoch in range(1, args.finetune_epochs + 1):
            t0 = _step(f"Fine-tuning epoch {epoch}/{args.finetune_epochs}")
            loss = train_epoch_session(
                model        = model,
                dataloader   = train_loader,
                optimizer    = optimizer,
                device       = device,
                temperature  = float(hp.get("temperature", 0.07)),
                label_smoothing = float(hp.get("label_smoothing", 0.1)),
                grad_clip    = 1.0,
                log_every    = 50,
                step_scheduler = None,
            )
            scheduler.step()
            epoch_losses.append(loss)
            _done(t0, label=f"loss={loss:.4f}  lr={scheduler.get_last_lr()[0]:.2e}")

        # ── 4. Evaluate ────────────────────────────────────────────────────────
        t0 = _step("Evaluating fine-tuned model")
        metrics = _evaluate(model, val_df, week_sessions, n_items, device, f"week{week}")
        _done(t0)

        new_ndcg    = float(metrics["ndcg_20"])
        prev_ndcg   = float(state["current_ndcg_20"])
        improvement = new_ndcg - prev_ndcg
        print(f"\n  NDCG@20: {new_ndcg:.4f}  (prev best: {prev_ndcg:.4f}, Δ={improvement:+.4f})")
        print(f"  HR@20  : {metrics.get('hr_20', 0.0):.4f}")

        # ── 5. Promote if improved ─────────────────────────────────────────────
        week_ckpt = ckpt_dir / f"week{week}_checkpoint.pt"
        torch.save(
            {
                "hparams":     hp,
                "model_state": model.state_dict(),
                "epoch_losses": epoch_losses,
                "val_ndcg_20": new_ndcg,
                "week":        week,
                "jsd":         jsd,
            },
            str(week_ckpt),
        )
        print(f"  Saved → {week_ckpt}")

        action = "skipped"
        if improvement >= args.improve_threshold:
            import shutil
            shutil.copy2(week_ckpt, current_ckpt)
            state["current_ndcg_20"] = new_ndcg
            action = "promoted"
            print(f"  ✓ Promoted: NDCG@20 improved by {improvement:+.4f}")
        else:
            print(f"  ✗ Not promoted: improvement ({improvement:+.4f}) < threshold ({args.improve_threshold})")

        history_entry = {
            "week":          week,
            "jsd":           jsd,
            "action":        action,
            "val_ndcg_20":   new_ndcg,
            "improvement":   improvement,
            "epoch_losses":  epoch_losses,
            "hr_20":         metrics.get("hr_20"),
        }
        state["history"].append(history_entry)
        state_path.write_text(json.dumps(state, indent=2))

        # ── MLflow logging ─────────────────────────────────────────────────────
        if mlflow_enabled:
            with mlflow.start_run(run_name=f"week{week}_finetune"):
                mlflow.log_param("week", week)
                mlflow.log_param("finetune_epochs", args.finetune_epochs)
                mlflow.log_param("lr", args.lr)
                mlflow.log_metric("jsd", jsd)
                mlflow.log_metric("val_ndcg_20", new_ndcg)
                mlflow.log_metric("val_hr_20", metrics.get("hr_20", 0.0))
                mlflow.log_metric("improvement", improvement)
                for ep, loss in enumerate(epoch_losses, 1):
                    mlflow.log_metric("finetune_loss", loss, step=ep)
                mlflow.set_tag("action", action)

    # ── Final summary ─────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print("Pipeline complete. Summary:")
    print(f"{'=' * 60}")
    for entry in state["history"]:
        w = entry["week"]
        jsd = entry["jsd"]
        act = entry["action"]
        ndcg = entry.get("val_ndcg_20", "—")
        imp  = entry.get("improvement", "—")
        print(f"  Week {w}: JSD={jsd:.3f}  action={act:<10}  NDCG@20={ndcg:.4f if isinstance(ndcg, float) else ndcg}  Δ={imp:+.4f if isinstance(imp, float) else imp}")
    print(f"\n  Current best NDCG@20 : {state['current_ndcg_20']:.4f}")
    print(f"  Best checkpoint      : {current_ckpt}")
    print(f"  State saved to       : {state_path}")


if __name__ == "__main__":
    main()
