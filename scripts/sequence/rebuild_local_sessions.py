"""Rebuild session parquets from raw REES46 CSVs — no GCS or BigQuery required.

Uses model/vocabs.pkl (which you already have locally) to determine the exact
1M-user cohort.  Only events from users in the vocab are kept, so the resulting
sessions are identical to what was originally produced by the Dataproc pipeline.

Inputs:
    --events-dir    Folder with the 5 raw REES46 CSV (or .csv.gz) files:
                        2019-Oct.csv[.gz]
                        2019-Nov.csv[.gz]
                        2019-Dec.csv[.gz]
                        2020-Jan.csv[.gz]
                        2020-Feb.csv[.gz]
    --vocabs-path   Path to vocabs.pkl  (default: model/vocabs.pkl)
    --out-dir       Output directory    (default: artifacts/1M/sequences)

Outputs (written to --out-dir):
    train_sessions.parquet   — sessions with events before 2020-01-25
    val_sessions.parquet     — sessions in [2020-01-25, 2020-02-01)
    test_sessions.parquet    — sessions in Feb 2020
    metadata.json

Typical Colab run (~60 min on CPU, no GPU needed):
    !python scripts/sequence/rebuild_local_sessions.py \\
        --events-dir /content/drive/MyDrive/rees46 \\
        --out-dir    /content/drive/MyDrive/recosys_sessions
"""

from __future__ import annotations

import argparse
import json
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd

# ── Constants (must match build_session_sequences.py) ────────────────────────

VAL_START  = pd.Timestamp("2020-01-25", tz="UTC")
TEST_START = pd.Timestamp("2020-02-01", tz="UTC")
MAX_SEQ_LEN = 20
MIN_SEQ_LEN = 2

EVENT_TYPE_MAP: dict[str, int] = {
    "view":             1,
    "cart":             2,
    "purchase":         3,
    "remove_from_cart": 0,  # treated as padding (same as original pipeline)
}

# Raw CSV column names from REES46 Kaggle download
_RAW_COLS = ["event_time", "event_type", "product_id", "user_id", "user_session"]

# Which months go into the training/val window vs. the test window
_TRAIN_MONTHS = ["2019-Oct", "2019-Nov", "2019-Dec", "2020-Jan"]
_TEST_MONTHS  = ["2020-Feb"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _step(msg: str) -> float:
    print(f"\n  ▶ {msg} ...")
    return time.time()


def _done(t0: float, label: str = "") -> None:
    elapsed = int(time.time() - t0)
    suffix  = f" — {label}" if label else ""
    print(f"     done in {elapsed // 60}m {elapsed % 60}s{suffix}")


def _find_csv(events_dir: Path, month: str) -> Path | None:
    for ext in [".csv", ".csv.gz"]:
        p = events_dir / f"{month}{ext}"
        if p.exists():
            return p
    return None


def _load_month(path: Path, user2idx: dict, item2idx: dict) -> pd.DataFrame:
    """Read one CSV file, filter to in-vocab users, map IDs → indices."""
    t0 = _step(f"Reading {path.name}")
    df = pd.read_csv(path, usecols=_RAW_COLS, low_memory=False)
    _done(t0, label=f"{len(df):,} rows")

    t0 = _step(f"Filtering to in-vocab users ({path.name})")
    # Coerce product_id to int (REES46 has them as int)
    df["product_id"] = pd.to_numeric(df["product_id"], errors="coerce")
    df["user_id"]    = pd.to_numeric(df["user_id"],    errors="coerce")
    df = df.dropna(subset=["product_id", "user_id", "user_session"])
    df["product_id"] = df["product_id"].astype(np.int64)
    df["user_id"]    = df["user_id"].astype(np.int64)

    # Keep only users in the 1M vocab
    df = df[df["user_id"].isin(user2idx)].copy()
    _done(t0, label=f"{len(df):,} in-vocab rows")

    t0 = _step(f"Mapping IDs and event types ({path.name})")
    df["user_idx"]   = df["user_id"].map(user2idx).astype(np.int64)
    df["item_idx"]   = df["product_id"].map(item2idx)
    df["event_idx"]  = df["event_type"].str.lower().map(EVENT_TYPE_MAP)
    df["event_time"] = pd.to_datetime(df["event_time"], utc=True)

    n_pre = len(df)
    df = df.dropna(subset=["item_idx", "event_idx"])
    if len(df) < n_pre:
        print(f"     dropped {n_pre - len(df):,} OOV items or unknown event types")

    df["item_idx"]  = df["item_idx"].astype(np.int64)
    df["event_idx"] = df["event_idx"].astype(np.int64)
    _done(t0, label=f"{len(df):,} mapped rows")

    return df[["event_time", "user_idx", "item_idx", "event_idx", "user_session"]]


def _remove_consecutive_repeats(
    items: np.ndarray, events: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    if items.shape[0] <= 1:
        return items, events
    keep = np.ones(items.shape[0], dtype=bool)
    keep[1:] = items[1:] != items[:-1]
    return items[keep], events[keep]


def _build_sessions(df: pd.DataFrame, label: str) -> pd.DataFrame:
    """Group events by (user_idx, user_session) → per-session sequences."""
    t0 = _step(f"Sorting ({label})")
    df = df.sort_values(
        ["user_idx", "user_session", "event_time"], kind="mergesort"
    ).reset_index(drop=True)
    _done(t0)

    t0 = _step(f"Aggregating sessions ({label})")
    grouped = (
        df.groupby(["user_idx", "user_session"], sort=False, as_index=False)
          .agg(item_seq=("item_idx", list), event_seq=("event_idx", list))
    )
    _done(t0, label=f"{len(grouped):,} raw sessions")

    t0 = _step(f"Applying dedup + truncation ({label})")
    new_items: list[list[int]]  = []
    new_events: list[list[int]] = []
    for items, events in zip(
        grouped["item_seq"].to_numpy(), grouped["event_seq"].to_numpy()
    ):
        arr_i = np.asarray(items,  dtype=np.int64)
        arr_e = np.asarray(events, dtype=np.int64)
        arr_i, arr_e = _remove_consecutive_repeats(arr_i, arr_e)
        if arr_i.shape[0] > MAX_SEQ_LEN:
            arr_i = arr_i[-MAX_SEQ_LEN:]
            arr_e = arr_e[-MAX_SEQ_LEN:]
        new_items.append(arr_i.tolist())
        new_events.append(arr_e.tolist())
    grouped["item_seq"]  = new_items
    grouped["event_seq"] = new_events
    grouped["seq_len"]   = grouped["item_seq"].apply(len).astype(np.int64)

    n_before = len(grouped)
    grouped  = grouped[grouped["seq_len"] >= MIN_SEQ_LEN].reset_index(drop=True)
    n_after  = len(grouped)
    grouped.insert(0, "session_idx", np.arange(n_after, dtype=np.int64))
    grouped["user_idx"] = grouped["user_idx"].astype(np.int64)
    _done(
        t0,
        label=(f"{n_after:,} sessions (dropped {n_before - n_after:,} "
               f"with < {MIN_SEQ_LEN} events post-dedup)"),
    )

    lens = grouped["seq_len"].to_numpy()
    print(
        f"     length stats: min={lens.min()}  median={int(np.median(lens))}"
        f"  p90={int(np.percentile(lens, 90))}  max={int(lens.max())}"
    )
    return grouped[["session_idx", "user_idx", "item_seq", "event_seq", "seq_len"]]


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild session parquets from raw REES46 CSVs"
    )
    parser.add_argument(
        "--events-dir",
        required=True,
        help="Folder containing 2019-Oct.csv[.gz] … 2020-Feb.csv[.gz]",
    )
    parser.add_argument(
        "--vocabs-path",
        default="model/vocabs.pkl",
        help="Path to vocabs.pkl  (default: model/vocabs.pkl)",
    )
    parser.add_argument(
        "--out-dir",
        default="artifacts/1M/sequences",
        help="Output directory for parquets  (default: artifacts/1M/sequences)",
    )
    args = parser.parse_args()

    events_dir = Path(args.events_dir)
    vocabs_path = Path(args.vocabs_path)
    out_dir    = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    t_total = time.time()
    print("=" * 60)
    print("RecoSys — Rebuild session parquets from local REES46 CSVs")
    print("=" * 60)
    print(f"  Events dir  : {events_dir}")
    print(f"  Vocabs path : {vocabs_path}")
    print(f"  Output dir  : {out_dir}")
    print(f"  VAL start   : {VAL_START.date()}")
    print(f"  TEST start  : {TEST_START.date()}")

    # ── Load vocabs ──────────────────────────────────────────────────────────
    t0 = _step(f"Loading vocabs from {vocabs_path}")
    with open(vocabs_path, "rb") as f:
        vocabs: dict = pickle.load(f)
    user2idx: dict = vocabs["user2idx"]
    item2idx: dict = vocabs["item2idx"]
    _done(t0, label=f"{len(user2idx):,} users  /  {len(item2idx):,} items")

    # ── Load train-window months (Oct–Jan) one by one to save RAM ────────────
    train_frames: list[pd.DataFrame] = []
    for month in _TRAIN_MONTHS:
        path = _find_csv(events_dir, month)
        if path is None:
            print(f"  WARNING: {month} not found in {events_dir}, skipping")
            continue
        train_frames.append(_load_month(path, user2idx, item2idx))

    if not train_frames:
        raise SystemExit("No training-window CSV files found. Check --events-dir.")

    t0 = _step("Concatenating train-window months")
    all_train = pd.concat(train_frames, ignore_index=True)
    del train_frames
    _done(t0, label=f"{len(all_train):,} total events")

    # ── Split train / val by date ─────────────────────────────────────────────
    train_mask = all_train["event_time"] < VAL_START
    val_mask   = ~train_mask

    print(f"\n  Event split:")
    print(f"     train (before {VAL_START.date()}): {train_mask.sum():,} events")
    print(f"     val   (on/after {VAL_START.date()}): {val_mask.sum():,} events")

    # ── Build and write train_sessions ────────────────────────────────────────
    train_sessions = _build_sessions(all_train.loc[train_mask].copy(), "train")
    out_train = out_dir / "train_sessions.parquet"
    t0 = _step(f"Writing {out_train}")
    train_sessions.to_parquet(out_train, index=False)
    _done(t0, label=f"{out_train.stat().st_size / 1e6:.1f} MB")

    # ── Build and write val_sessions ──────────────────────────────────────────
    val_sessions = _build_sessions(all_train.loc[val_mask].copy(), "val")
    out_val = out_dir / "val_sessions.parquet"
    t0 = _step(f"Writing {out_val}")
    val_sessions.to_parquet(out_val, index=False)
    _done(t0, label=f"{out_val.stat().st_size / 1e6:.1f} MB")

    del all_train

    # ── Load test-window month (Feb) ──────────────────────────────────────────
    test_frames: list[pd.DataFrame] = []
    for month in _TEST_MONTHS:
        path = _find_csv(events_dir, month)
        if path is None:
            print(f"  WARNING: {month} not found in {events_dir}, skipping")
            continue
        test_frames.append(_load_month(path, user2idx, item2idx))

    if not test_frames:
        print("WARNING: No test CSV found — test_sessions.parquet will not be written.")
    else:
        t0 = _step("Concatenating test-window months")
        all_test = pd.concat(test_frames, ignore_index=True)
        del test_frames
        _done(t0, label=f"{len(all_test):,} total events")

        test_sessions = _build_sessions(all_test, "test")
        out_test = out_dir / "test_sessions.parquet"
        t0 = _step(f"Writing {out_test}")
        test_sessions.to_parquet(out_test, index=False)
        _done(t0, label=f"{out_test.stat().st_size / 1e6:.1f} MB")

        # ── metadata.json ─────────────────────────────────────────────────────
        metadata = {
            "schema_version":   "v9_session",
            "source":           "rebuild_local_sessions.py from raw REES46 CSVs",
            "max_seq_len":      MAX_SEQ_LEN,
            "min_seq_len":      MIN_SEQ_LEN,
            "n_items":          len(item2idx),
            "n_users":          len(user2idx),
            "event_type_map":   EVENT_TYPE_MAP,
            "val_start":        VAL_START.isoformat(),
            "test_start":       TEST_START.isoformat(),
            "n_train_sessions": int(len(train_sessions)),
            "n_val_sessions":   int(len(val_sessions)),
            "n_test_sessions":  int(len(test_sessions)),
        }
        meta_path = out_dir / "metadata.json"
        meta_path.write_text(json.dumps(metadata, indent=2))
        print(f"\n  Wrote {meta_path}")

    elapsed = int(time.time() - t_total)
    print(f"\n{'=' * 60}")
    print(f"Total time: {elapsed // 60}m {elapsed % 60}s")
    print(f"Output in : {out_dir}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
