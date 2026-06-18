"""Baseline inference benchmarking for GRU4Rec V9 serving path.

Measures per-stage latency (tensor build → GRU forward → FAISS search →
post-process) and end-to-end throughput at multiple concurrency levels.
Also simulates dynamic batching so the throughput gain can be quantified
without running a live server.

Usage (local checkpoint):
    python scripts/serving/benchmark_inference.py \\
        --checkpoint model/model_inference.pt \\
        --vocabs model/vocabs.pkl

Colab / GCS (set env vars first):
    GCS_CHECKPOINT_DIR=gs://recosys-data-bucket/models/gru4rec_session_v9_1M
    GCS_VOCABS_PATH=gs://recosys-data-bucket/data/1M/vocabs.pkl
    python scripts/serving/benchmark_inference.py --use-gcs

Outputs:
    reports/inference_benchmark_baseline.json
    reports/figures/latency_breakdown.png
    reports/figures/batching_throughput.png
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

import faiss
import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.sequence.models.gru4rec import GRU4RecModel

_MAX_SEQ_LEN = 20
_SEED        = 42

np.random.seed(_SEED)
torch.manual_seed(_SEED)


# ── Artifact loading (bypasses GCS resolution for local benchmarks) ────────────

def _load_artifacts_local(ckpt_path: Path, vocabs_path: Path):
    """Load model + build IndexFlatIP directly from local files."""
    print("Loading checkpoint ...")
    ckpt    = torch.load(str(ckpt_path), map_location="cpu", weights_only=False)
    hparams = ckpt["hparams"]

    model = GRU4RecModel(
        n_items       = int(hparams["n_items"]),
        n_event_types = 4,
        embed_dim     = int(hparams["embed_dim"]),
        gru_hidden    = int(hparams["gru_hidden"]),
        n_layers      = int(hparams["n_layers"]),
        dropout       = float(hparams.get("dropout", 0.3)),
    )
    model.load_state_dict(ckpt["model_state"])
    model.eval()
    print(f"  n_items={hparams['n_items']:,}, embed_dim={hparams['embed_dim']}, "
          f"gru_hidden={hparams['gru_hidden']}, n_layers={hparams['n_layers']}")

    n_items = int(hparams["n_items"])
    with torch.no_grad():
        all_embs = model.get_item_embeddings().cpu().numpy().astype(np.float32)

    keep_mask             = np.ones(n_items, dtype=bool)
    keep_mask[0]          = False
    item_idx_array        = np.where(keep_mask)[0].astype(np.int64)
    embeddings            = all_embs[item_idx_array].copy()
    faiss.normalize_L2(embeddings)

    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)
    size_mb = index.ntotal * embeddings.shape[1] * 4 / 1024 ** 2
    print(f"  IndexFlatIP: {index.ntotal:,} items × {embeddings.shape[1]} dims  ({size_mb:.1f} MB)")

    with open(vocabs_path, "rb") as f:
        vocabs = pickle.load(f)
    print(f"  Vocabs: {len(vocabs['item2idx']):,} items")

    return model, index, item_idx_array, vocabs, hparams


def _load_artifacts_gcs() -> tuple:
    """Fallback: download from GCS (same env vars as app.py)."""
    from src.serving.model_loader import load_artifacts
    gcs_ckpt  = os.environ["GCS_CHECKPOINT_DIR"]
    gcs_vocab = os.environ["GCS_VOCABS_PATH"]
    art       = load_artifacts(gcs_ckpt, gcs_vocab)
    return art.model, art.index, art.item_idx_array, art.vocabs, art.hparams


# ── Synthetic session generator ────────────────────────────────────────────────

def _generate_sessions(
    item_idx_array: np.ndarray,
    n: int,
    min_len: int = 2,
    max_len: int = 10,
) -> list[tuple[list[int], list[int]]]:
    rng      = np.random.default_rng(_SEED)
    sessions = []
    for _ in range(n):
        seq_len   = int(rng.integers(min_len, max_len + 1))
        items     = rng.choice(item_idx_array, size=seq_len, replace=True).tolist()
        events    = rng.choice([1, 2, 3], size=seq_len, p=[0.7, 0.2, 0.1]).tolist()
        item_seq  = [0] * _MAX_SEQ_LEN
        event_seq = [0] * _MAX_SEQ_LEN
        start     = max(0, _MAX_SEQ_LEN - seq_len)
        for i, (it, ev) in enumerate(zip(items[-_MAX_SEQ_LEN:], events[-_MAX_SEQ_LEN:])):
            item_seq [start + i] = it
            event_seq[start + i] = ev
        sessions.append((item_seq, event_seq))
    return sessions


# ── Single-request timed inference ────────────────────────────────────────────

def _timed_request(
    model:          GRU4RecModel,
    index:          faiss.Index,
    item_idx_array: np.ndarray,
    vocabs:         dict,
    item_seq:       list[int],
    event_seq:      list[int],
    top_k:          int = 20,
) -> dict[str, float]:
    """Run one inference request; return per-stage timings in ms."""
    # Stage 1: tensor construction
    t0 = time.perf_counter()
    item_t  = torch.tensor([item_seq],  dtype=torch.long)
    event_t = torch.tensor([event_seq], dtype=torch.long)
    t_tensor = (time.perf_counter() - t0) * 1000

    # Stage 2: GRU forward pass
    t0 = time.perf_counter()
    with torch.no_grad():
        user_emb = model.encode_sequence(item_t, event_t)
    t_gru = (time.perf_counter() - t0) * 1000

    # Stage 3: FAISS search (includes normalize_L2)
    t0 = time.perf_counter()
    user_np = user_emb.cpu().numpy().astype(np.float32)
    faiss.normalize_L2(user_np)
    n_candidates = min(top_k + 10, index.ntotal)
    _, faiss_indices = index.search(user_np, n_candidates)
    t_faiss = (time.perf_counter() - t0) * 1000

    # Stage 4: post-processing (FAISS pos → item_id)
    t0 = time.perf_counter()
    idx2item   = vocabs["idx2item"]
    rec_ids: list[str] = []
    for pos in faiss_indices[0]:
        if pos < 0:
            continue
        pid = idx2item.get(int(item_idx_array[pos]))
        if pid is not None:
            rec_ids.append(str(pid))
        if len(rec_ids) >= top_k:
            break
    t_post = (time.perf_counter() - t0) * 1000

    total = t_tensor + t_gru + t_faiss + t_post
    return {
        "tensor_ms":   t_tensor,
        "gru_ms":      t_gru,
        "faiss_ms":    t_faiss,
        "postproc_ms": t_post,
        "total_ms":    total,
    }


# ── Serial stage-level benchmark ──────────────────────────────────────────────

def _benchmark_serial(
    model:          GRU4RecModel,
    index:          faiss.Index,
    item_idx_array: np.ndarray,
    vocabs:         dict,
    sessions:       list[tuple[list[int], list[int]]],
    n_warmup:       int,
) -> list[dict[str, float]]:
    for item_seq, event_seq in sessions[:n_warmup]:
        _timed_request(model, index, item_idx_array, vocabs, item_seq, event_seq)
    return [
        _timed_request(model, index, item_idx_array, vocabs, item_seq, event_seq)
        for item_seq, event_seq in sessions[n_warmup:]
    ]


# ── Concurrent (unbatched) throughput benchmark ───────────────────────────────

def _benchmark_concurrent(
    model:          GRU4RecModel,
    index:          faiss.Index,
    item_idx_array: np.ndarray,
    vocabs:         dict,
    sessions:       list[tuple[list[int], list[int]]],
    concurrency:    int,
    n_warmup:       int,
) -> tuple[list[float], float]:
    """Returns (per-request latencies ms, throughput req/s)."""
    for item_seq, event_seq in sessions[:n_warmup]:
        _timed_request(model, index, item_idx_array, vocabs, item_seq, event_seq)

    test_sessions = sessions[n_warmup:]
    latencies: list[float] = []

    wall_t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [
            ex.submit(_timed_request, model, index, item_idx_array, vocabs, isq, esq)
            for isq, esq in test_sessions
        ]
        for f in futs:
            latencies.append(f.result()["total_ms"])
    wall_elapsed = time.perf_counter() - wall_t0

    return latencies, len(test_sessions) / wall_elapsed


# ── Batched throughput benchmark (simulates dynamic batching) ────────────────

def _benchmark_batched(
    model:          GRU4RecModel,
    index:          faiss.Index,
    item_idx_array: np.ndarray,
    sessions:       list[tuple[list[int], list[int]]],
    max_batch_size: int,
    n_warmup:       int,
) -> tuple[list[float], float]:
    """Simulate dynamic batching: group requests, run as a single forward+search.

    Measures per-request latency (batch_time / B) and overall throughput.
    This is a lower bound on what the asyncio dynamic batcher achieves at high
    concurrency (real-world batching adds queue wait time on top).
    """
    for item_seq, event_seq in sessions[:n_warmup]:
        with torch.no_grad():
            t  = torch.tensor([item_seq],  dtype=torch.long)
            e  = torch.tensor([event_seq], dtype=torch.long)
            model.encode_sequence(t, e)

    test_sessions = sessions[n_warmup:]
    per_req_latencies: list[float] = []

    wall_t0 = time.perf_counter()
    for b_start in range(0, len(test_sessions), max_batch_size):
        batch = test_sessions[b_start : b_start + max_batch_size]
        b     = len(batch)

        t0 = time.perf_counter()

        item_batch  = torch.tensor([s[0] for s in batch], dtype=torch.long)  # (B, L)
        event_batch = torch.tensor([s[1] for s in batch], dtype=torch.long)

        with torch.no_grad():
            user_embs = model.encode_sequence(item_batch, event_batch)         # (B, D)

        user_np = user_embs.cpu().numpy().astype(np.float32)
        faiss.normalize_L2(user_np)
        index.search(user_np, 30)                                              # (B, 30)

        batch_ms     = (time.perf_counter() - t0) * 1000
        per_req_ms   = batch_ms / b
        per_req_latencies.extend([per_req_ms] * b)

    wall_elapsed = time.perf_counter() - wall_t0
    return per_req_latencies, len(test_sessions) / wall_elapsed


# ── Stats helper ──────────────────────────────────────────────────────────────

def _stats(values: list[float]) -> dict[str, float]:
    arr = np.array(values, dtype=np.float64)
    return {
        "p50_ms":  round(float(np.percentile(arr, 50)), 3),
        "p90_ms":  round(float(np.percentile(arr, 90)), 3),
        "p99_ms":  round(float(np.percentile(arr, 99)), 3),
        "mean_ms": round(float(arr.mean()),             3),
        "min_ms":  round(float(arr.min()),              3),
        "max_ms":  round(float(arr.max()),              3),
        "n":       len(values),
    }


# ── Plotting ──────────────────────────────────────────────────────────────────

def _plot_results(
    stage_stats:         dict[str, dict],
    concurrency_results: list[dict],
    batching_results:    list[dict],
    output_dir:          Path,
) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib not available — skipping plots")
        return

    fig_dir = output_dir / "figures"
    fig_dir.mkdir(exist_ok=True)

    # ── Plot 1: Latency breakdown (stacked bar + p50/p99 table) ───────────
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    stages       = ["tensor_ms", "gru_ms", "faiss_ms", "postproc_ms"]
    stage_labels = ["Tensor\nbuild", "GRU\nforward", "FAISS\nsearch", "Post-\nprocess"]
    means        = [stage_stats[s]["mean_ms"] for s in stages]
    p99s         = [stage_stats[s]["p99_ms"]  for s in stages]
    colors       = ["#4878CF", "#6ACC65", "#D65F5F", "#B47CC7"]

    x    = np.arange(len(stages))
    w    = 0.35
    bars = axes[0].bar(x - w/2, means, w, label="mean",  color=colors, alpha=0.85)
    axes[0].bar(x + w/2, p99s,  w, label="p99",   color=colors, alpha=0.45)
    axes[0].set_xticks(x)
    axes[0].set_xticklabels(stage_labels)
    axes[0].set_ylabel("Latency (ms)")
    axes[0].set_title("Latency by Stage — mean (solid) vs p99 (light)")
    axes[0].legend()
    for bar, v in zip(bars, means):
        axes[0].text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.1,
                     f"{v:.1f}", ha="center", va="bottom", fontsize=8)

    # ── Plot 2: Throughput vs concurrency (batched vs unbatched) ──────────
    conc     = [r["concurrency"]    for r in concurrency_results]
    tput_ub  = [r["throughput_rps"] for r in concurrency_results]

    axes[1].plot(conc, tput_ub, "o-", color="#D65F5F", linewidth=2,
                 markersize=8, label="Unbatched (current)")

    if batching_results:
        bsizes = [r["max_batch_size"] for r in batching_results]
        tputs  = [r["throughput_rps"] for r in batching_results]
        axes[1].plot(bsizes, tputs, "s--", color="#4878CF", linewidth=2,
                     markersize=8, label="Batched (simulated)")

    axes[1].set_xlabel("Concurrency / Batch size")
    axes[1].set_ylabel("Throughput (req/s)")
    axes[1].set_title("Throughput: Unbatched vs Batched")
    axes[1].legend()

    plt.tight_layout()
    p = fig_dir / "latency_breakdown.png"
    plt.savefig(str(p), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {p}")

    # ── Plot 3: Batching throughput vs batch size ──────────────────────────
    if batching_results:
        fig, ax = plt.subplots(figsize=(8, 5))
        bsizes  = [r["max_batch_size"] for r in batching_results]
        tputs   = [r["throughput_rps"] for r in batching_results]
        p50s    = [r["p50_ms"]         for r in batching_results]

        ax2 = ax.twinx()
        ax.plot(bsizes, tputs, "o-", color="#4878CF", linewidth=2, markersize=8, label="Throughput")
        ax2.plot(bsizes, p50s, "s--", color="#D65F5F", linewidth=2, markersize=8, label="p50 latency")

        ax.set_xlabel("Max batch size")
        ax.set_ylabel("Throughput (req/s)", color="#4878CF")
        ax2.set_ylabel("p50 per-request latency (ms)", color="#D65F5F")
        ax.set_title("Dynamic Batching: Throughput vs Latency Trade-off")

        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(lines1 + lines2, labels1 + labels2, loc="center right")

        plt.tight_layout()
        p = fig_dir / "batching_throughput.png"
        plt.savefig(str(p), dpi=150, bbox_inches="tight")
        plt.close()
        print(f"  Saved: {p}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GRU4Rec V9 inference benchmark")
    src_g = parser.add_mutually_exclusive_group(required=True)
    src_g.add_argument("--checkpoint", help="Path to model_inference.pt")
    src_g.add_argument("--use-gcs",    action="store_true",
                       help="Download from GCS (requires env vars GCS_CHECKPOINT_DIR + GCS_VOCABS_PATH)")
    parser.add_argument("--vocabs",             default=None,
                        help="Path to vocabs.pkl (required when --checkpoint is used)")
    parser.add_argument("--n-requests",         type=int,   default=500)
    parser.add_argument("--warmup",             type=int,   default=50)
    parser.add_argument("--concurrency-levels", nargs="+",  type=int,   default=[1, 8, 32])
    parser.add_argument("--batch-sizes",        nargs="+",  type=int,   default=[4, 8, 16, 32],
                        help="Batch sizes to test in the dynamic-batching simulation")
    parser.add_argument("--output-dir",         default="reports")
    parser.add_argument("--no-plot",            action="store_true")
    args = parser.parse_args()

    if args.checkpoint and not args.vocabs:
        parser.error("--vocabs is required when --checkpoint is used")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Load ────────────────────────────────────────────────────────────────
    if args.use_gcs:
        model, index, item_idx_array, vocabs, hparams = _load_artifacts_gcs()
    else:
        model, index, item_idx_array, vocabs, hparams = _load_artifacts_local(
            Path(args.checkpoint), Path(args.vocabs)
        )

    n_total  = args.n_requests + args.warmup
    sessions = _generate_sessions(item_idx_array, n_total)
    print(f"\nGenerated {n_total} synthetic sessions "
          f"({args.warmup} warm-up + {args.n_requests} timed)\n")

    # ═══════════════════════════════════════════════════════════════════════
    # WS1a — Stage-level profiling (serial, concurrency=1)
    # ═══════════════════════════════════════════════════════════════════════
    sep = "=" * 64
    print(sep)
    print("WORKSTREAM 1A — Stage-level latency profiling (serial)")
    print(sep)

    serial_timings = _benchmark_serial(model, index, item_idx_array, vocabs,
                                       sessions, args.warmup)
    stage_names = ["tensor_ms", "gru_ms", "faiss_ms", "postproc_ms", "total_ms"]
    stage_labels_print = {
        "tensor_ms":   "Tensor construction     ",
        "gru_ms":      "GRU forward pass        ",
        "faiss_ms":    "FAISS search (FlatIP)   ",
        "postproc_ms": "Post-processing (idx→id)",
        "total_ms":    "End-to-end total        ",
    }
    stage_stats: dict[str, dict] = {}
    print(f"\n  {'Stage':<28}  {'p50 ms':>8}  {'p90 ms':>8}  {'p99 ms':>8}  {'mean ms':>8}")
    print(f"  {'-'*28}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}")
    for stage in stage_names:
        vals   = [t[stage] for t in serial_timings]
        st     = _stats(vals)
        stage_stats[stage] = st
        label  = stage_labels_print[stage]
        if stage == "total_ms":
            print(f"  {'-'*28}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}")
        print(f"  {label:<28}  {st['p50_ms']:>8.2f}  {st['p90_ms']:>8.2f}  {st['p99_ms']:>8.2f}  {st['mean_ms']:>8.2f}")

    # ═══════════════════════════════════════════════════════════════════════
    # WS1b — Throughput at concurrency levels (unbatched)
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{sep}")
    print("WORKSTREAM 1B — Throughput vs concurrency (unbatched single-item)")
    print(sep)
    print(f"\n  {'Concurrency':>12}  {'req/s':>10}  {'p50 ms':>10}  {'p99 ms':>10}")
    print(f"  {'-'*12}  {'-'*10}  {'-'*10}  {'-'*10}")

    concurrency_results: list[dict] = []
    for c in args.concurrency_levels:
        lats, tput = _benchmark_concurrent(model, index, item_idx_array, vocabs,
                                           sessions, c, args.warmup)
        st = _stats(lats)
        print(f"  {c:>12}  {tput:>10.1f}  {st['p50_ms']:>10.2f}  {st['p99_ms']:>10.2f}")
        concurrency_results.append({"concurrency": c, "throughput_rps": round(tput, 2), **st})

    # ═══════════════════════════════════════════════════════════════════════
    # WS3  — Dynamic batching simulation
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{sep}")
    print("WORKSTREAM 3 — Dynamic batching simulation")
    print(sep)
    print("  (batch B requests together: single GRU forward + single FAISS search)")
    print(f"\n  {'Batch size':>10}  {'req/s':>10}  {'p50 ms/req':>12}  {'p99 ms/req':>12}  {'vs serial req/s':>16}")
    print(f"  {'-'*10}  {'-'*10}  {'-'*12}  {'-'*12}  {'-'*16}")

    serial_rps = concurrency_results[0]["throughput_rps"] if concurrency_results else None
    batching_results: list[dict] = []
    for bs in args.batch_sizes:
        lats, tput = _benchmark_batched(model, index, item_idx_array,
                                        sessions, bs, args.warmup)
        st       = _stats(lats)
        speedup  = f"{tput / serial_rps:.1f}×" if serial_rps else "—"
        print(f"  {bs:>10}  {tput:>10.1f}  {st['p50_ms']:>12.2f}  {st['p99_ms']:>12.2f}  {speedup:>16}")
        batching_results.append({
            "max_batch_size": bs,
            "throughput_rps": round(tput, 2),
            **st,
        })

    # ═══════════════════════════════════════════════════════════════════════
    # Save JSON
    # ═══════════════════════════════════════════════════════════════════════
    output = {
        "run_info": {
            "n_items_indexed":     int(index.ntotal),
            "embed_dim":           int(hparams["embed_dim"]),
            "gru_hidden":          int(hparams["gru_hidden"]),
            "n_layers":            int(hparams["n_layers"]),
            "index_type":          "IndexFlatIP",
            "index_size_mb":       round(index.ntotal * int(hparams["embed_dim"]) * 4 / 1024 ** 2, 1),
            "n_requests":          args.n_requests,
            "n_warmup":            args.warmup,
            "hardware":            "cpu",
            "seed":                _SEED,
        },
        "stage_latency_ms":     stage_stats,
        "concurrency_results":  concurrency_results,
        "batching_simulation":  batching_results,
    }

    out_path = output_dir / "inference_benchmark_baseline.json"
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nResults saved → {out_path}")

    # ═══════════════════════════════════════════════════════════════════════
    # Plots
    # ═══════════════════════════════════════════════════════════════════════
    if not args.no_plot:
        print("\nGenerating plots ...")
        _plot_results(stage_stats, concurrency_results, batching_results, output_dir)

    print("\nDone.")


if __name__ == "__main__":
    main()
