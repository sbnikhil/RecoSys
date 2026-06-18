"""FAISS index optimization for GRU4Rec V9 — WS2.

Builds six FAISS index variants over the item embedding table and measures:
  - Build time (s)
  - Index size on disk (MB)
  - Search latency p50/p99 at k=20 (ms)
  - Retrieval quality: NDCG@20 and HR@20 vs. the IndexFlatIP baseline
    (requires --test-sessions; skipped if not provided)

The quality measurement computes the fraction of top-20 items that match
between each candidate index and the FlatIP oracle (concordance), plus
HR@20/NDCG@20 against ground-truth targets when test sessions are supplied.

Usage:
    # Latency + size only (no test data needed):
    python scripts/serving/build_optimized_index.py \\
        --checkpoint model/model_inference.pt \\
        --vocabs model/vocabs.pkl

    # Full quality evaluation (provide held-out test sessions parquet):
    python scripts/serving/build_optimized_index.py \\
        --checkpoint model/model_inference.pt \\
        --vocabs model/vocabs.pkl \\
        --test-sessions data/test_sessions.parquet

Outputs:
    reports/inference_index_tradeoffs.json
    reports/figures/faiss_tradeoff_curve.png
    reports/figures/index_size_vs_ndcg.png
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import tempfile
import time
from pathlib import Path

# Disable OpenMP/BLAS multi-threading before any native library is imported —
# faiss + torch both use AVX2 and their parallel thread pools collide during
# load_state_dict on macOS when multi-threaded.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

# faiss must be imported before torch to avoid AVX2 SIMD register corruption on macOS
import faiss
import numpy as np
import torch

torch.set_num_threads(1)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.sequence.models.gru4rec import GRU4RecModel

_SEED        = 42
_MAX_SEQ_LEN = 20
_N_BENCH     = 1000   # number of queries for latency benchmark

np.random.seed(_SEED)
torch.manual_seed(_SEED)


# ── Index factory ─────────────────────────────────────────────────────────────

def _build_index(name: str, embeddings: np.ndarray) -> faiss.Index:
    """Build and return a named FAISS index trained on ``embeddings``."""
    d = embeddings.shape[1]

    if name == "FlatIP":
        idx = faiss.IndexFlatIP(d)
        idx.add(embeddings)

    elif name == "IVFFlat-256":
        quantizer = faiss.IndexFlatIP(d)
        idx       = faiss.IndexIVFFlat(quantizer, d, 256, faiss.METRIC_INNER_PRODUCT)
        idx.train(embeddings)
        idx.add(embeddings)
        idx.nprobe = 16  # probe 16/256 clusters ≈ 6% of catalog

    elif name == "IVFFlat-512":
        quantizer = faiss.IndexFlatIP(d)
        idx       = faiss.IndexIVFFlat(quantizer, d, 512, faiss.METRIC_INNER_PRODUCT)
        idx.train(embeddings)
        idx.add(embeddings)
        idx.nprobe = 32  # probe 32/512 clusters ≈ 6% of catalog

    elif name == "SQ8":
        # Scalar quantization: float32 → uint8, ~4× memory reduction
        idx = faiss.IndexScalarQuantizer(d, faiss.ScalarQuantizer.QT_8bit,
                                         faiss.METRIC_INNER_PRODUCT)
        idx.train(embeddings)
        idx.add(embeddings)

    elif name == "IVFPQ":
        # Product quantization: smallest size, largest quality cost
        # M=16 sub-quantizers, 8-bit codes → each vector = 16 bytes
        quantizer = faiss.IndexFlatIP(d)
        idx       = faiss.IndexIVFPQ(quantizer, d, 256, 16, 8)
        idx.train(embeddings)
        idx.add(embeddings)
        idx.nprobe = 16

    elif name == "HNSW32":
        # Graph-based ANN: very low latency, higher memory than FlatIP
        idx = faiss.IndexHNSWFlat(d, 32, faiss.METRIC_INNER_PRODUCT)
        idx.add(embeddings)  # HNSW does not require separate train

    else:
        raise ValueError(f"Unknown index: {name}")

    return idx


_INDEX_CONFIGS = [
    "FlatIP",        # baseline — brute-force exact search
    "IVFFlat-256",   # IVF approximate, 256 clusters
    "IVFFlat-512",   # IVF approximate, 512 clusters (finer)
    "SQ8",           # 8-bit scalar quantization (~4× memory reduction)
    "IVFPQ",         # product quantization (smallest, largest quality cost)
    "HNSW32",        # graph ANN (lowest latency, highest memory)
]


# ── Index size on disk ────────────────────────────────────────────────────────

def _index_size_mb(idx: faiss.Index) -> float:
    """Serialize to a temp file and measure byte size."""
    with tempfile.NamedTemporaryFile(suffix=".faiss", delete=False) as f:
        tmp = f.name
    faiss.write_index(idx, tmp)
    size = Path(tmp).stat().st_size / 1024 ** 2
    Path(tmp).unlink()
    return size


# ── Latency benchmark ─────────────────────────────────────────────────────────

def _bench_search(
    idx:      faiss.Index,
    queries:  np.ndarray,
    k:        int = 20,
    n_warmup: int = 50,
) -> dict[str, float]:
    """Run ANN search on all queries; return latency stats in ms."""
    # warm up
    for i in range(min(n_warmup, len(queries))):
        idx.search(queries[i : i + 1], k)

    times: list[float] = []
    for i in range(len(queries)):
        t0 = time.perf_counter()
        idx.search(queries[i : i + 1], k)
        times.append((time.perf_counter() - t0) * 1000)

    arr = np.array(times)
    return {
        "p50_ms":  round(float(np.percentile(arr, 50)), 3),
        "p90_ms":  round(float(np.percentile(arr, 90)), 3),
        "p99_ms":  round(float(np.percentile(arr, 99)), 3),
        "mean_ms": round(float(arr.mean()),             3),
    }


# ── Retrieval concordance vs FlatIP oracle ────────────────────────────────────

def _concordance(
    candidate_idx: faiss.Index,
    oracle_idx:    faiss.Index,
    queries:       np.ndarray,
    k:             int = 20,
) -> float:
    """Fraction of top-k results that agree with the FlatIP oracle."""
    hits = 0
    total = 0
    for i in range(len(queries)):
        q = queries[i : i + 1]
        _, oracle_res    = oracle_idx.search(q, k)
        _, candidate_res = candidate_idx.search(q, k)
        oracle_set    = set(oracle_res[0].tolist())
        candidate_set = set(candidate_res[0].tolist())
        hits  += len(oracle_set & candidate_set)
        total += k
    return hits / total if total > 0 else 0.0


# ── Quality evaluation against ground-truth sessions ─────────────────────────

def _evaluate_index_quality(
    idx:           faiss.Index,
    item_idx_array: np.ndarray,
    model:         GRU4RecModel,
    test_sessions: list[dict],
    k:             int = 20,
    batch_size:    int = 256,
) -> dict[str, float]:
    """Compute HR@20 and NDCG@20 for a given FAISS index over test sessions.

    test_sessions: list of dicts with keys:
        prefix_items  (list[int]) — left-padded to _MAX_SEQ_LEN
        prefix_events (list[int]) — left-padded
        target_item   (int)       — ground-truth last item (item_idx)
    """
    hr20s:   list[float] = []
    ndcg20s: list[float] = []

    iidx2pos = {int(iidx): pos for pos, iidx in enumerate(item_idx_array)}

    model.eval()
    with torch.no_grad():
        for b_start in range(0, len(test_sessions), batch_size):
            batch   = test_sessions[b_start : b_start + batch_size]
            b       = len(batch)
            item_t  = torch.tensor([s["prefix_items"]  for s in batch], dtype=torch.long)
            event_t = torch.tensor([s["prefix_events"] for s in batch], dtype=torch.long)

            user_embs = model.encode_sequence(item_t, event_t)
            user_np   = user_embs.cpu().detach().clone().numpy().astype(np.float32)
            faiss.normalize_L2(user_np)

            _, faiss_indices = idx.search(user_np, k)

            for i in range(b):
                target_iidx = int(batch[i]["target_item"])
                recs        = [int(item_idx_array[pos]) for pos in faiss_indices[i] if pos >= 0]
                hit         = int(target_iidx in recs[:k])
                if hit:
                    rank  = recs[:k].index(target_iidx)
                    ndcg  = 1.0 / np.log2(rank + 2)
                else:
                    ndcg  = 0.0
                hr20s.append(float(hit))
                ndcg20s.append(ndcg)

    return {
        "hr_20":   round(float(np.mean(hr20s)),   4),
        "ndcg_20": round(float(np.mean(ndcg20s)), 4),
        "n_sessions": len(test_sessions),
    }


# ── Load test sessions from parquet ──────────────────────────────────────────

def _load_test_sessions(path: Path, vocabs: dict, max_sessions: int = 5000) -> list[dict]:
    """Load test sessions parquet → list of dicts for quality eval.

    Expects columns: item_seq (list[int]), event_seq (list[int]) — same format
    produced by build_session_sequences.py.
    """
    import pandas as pd
    df = pd.read_parquet(path)
    if len(df) > max_sessions:
        df = df.sample(max_sessions, random_state=_SEED)

    sessions = []
    for _, row in df.iterrows():
        items  = list(row["item_seq"])
        events = list(row["event_seq"])
        if len(items) < 2:
            continue
        target = items[-1]
        prefix = items[:-1]
        pe     = events[:-1]

        pad_items  = [0] * _MAX_SEQ_LEN
        pad_events = [0] * _MAX_SEQ_LEN
        start      = max(0, _MAX_SEQ_LEN - len(prefix))
        for j, (it, ev) in enumerate(zip(prefix[-_MAX_SEQ_LEN:], pe[-_MAX_SEQ_LEN:])):
            pad_items [start + j] = it
            pad_events[start + j] = ev

        sessions.append({
            "prefix_items":  pad_items,
            "prefix_events": pad_events,
            "target_item":   int(target),
        })
    return sessions


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="FAISS index optimization benchmark")
    parser.add_argument("--checkpoint",    required=True, help="Path to model_inference.pt")
    parser.add_argument("--vocabs",        required=True, help="Path to vocabs.pkl")
    parser.add_argument("--test-sessions", default=None,
                        help="(optional) parquet with test sessions for quality eval")
    parser.add_argument("--max-sessions",  type=int, default=5000,
                        help="Max test sessions to evaluate (default 5000)")
    parser.add_argument("--output-dir",    default="reports")
    parser.add_argument("--no-plot",       action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Load model ──────────────────────────────────────────────────────────
    print("Loading model ...")
    ckpt    = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
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

    n_items = int(hparams["n_items"])
    embed_d = int(hparams["embed_dim"])
    print(f"  n_items={n_items:,}, embed_dim={embed_d}")

    with torch.no_grad():
        all_embs = model.get_item_embeddings().cpu().detach().clone().numpy().astype(np.float32)

    keep_mask                    = np.ones(n_items, dtype=bool)
    keep_mask[0]                 = False
    item_idx_array               = np.where(keep_mask)[0].astype(np.int64)
    embeddings                   = all_embs[item_idx_array].copy()
    faiss.normalize_L2(embeddings)
    print(f"  Embeddings: {embeddings.shape[0]:,} items × {embeddings.shape[1]} dims  "
          f"(float32, {embeddings.nbytes / 1024**2:.1f} MB)")

    with open(args.vocabs, "rb") as f:
        vocabs = pickle.load(f)

    # ── Generate benchmark queries ──────────────────────────────────────────
    rng     = np.random.default_rng(_SEED)
    queries = rng.choice(item_idx_array, size=_N_BENCH)
    # Map item_idxs to their embedding row index (position in item_idx_array)
    iidx2pos  = {int(iidx): pos for pos, iidx in enumerate(item_idx_array)}
    query_vecs = embeddings[[iidx2pos[int(q)] for q in queries]].copy()
    # query_vecs are already L2-normalised (embeddings were normalized above)

    # ── Load test sessions (optional) ──────────────────────────────────────
    test_sessions: list[dict] = []
    if args.test_sessions:
        print(f"\nLoading test sessions from {args.test_sessions} ...")
        test_sessions = _load_test_sessions(
            Path(args.test_sessions), vocabs, args.max_sessions
        )
        print(f"  Loaded {len(test_sessions):,} sessions")

    # ── Build and evaluate each index ──────────────────────────────────────
    sep = "=" * 72
    print(f"\n{sep}")
    print("FAISS Index Comparison")
    print(sep)

    results: list[dict] = []
    oracle_idx: faiss.Index | None = None

    for name in _INDEX_CONFIGS:
        print(f"\n  [{name}]")

        # Build
        t0    = time.perf_counter()
        idx   = _build_index(name, embeddings)
        build = time.perf_counter() - t0
        size  = _index_size_mb(idx)

        print(f"    Build time : {build:.2f}s")
        print(f"    Disk size  : {size:.1f} MB")

        # Latency
        lat = _bench_search(idx, query_vecs, k=20)
        print(f"    Search p50 : {lat['p50_ms']:.2f} ms  p99={lat['p99_ms']:.2f} ms")

        # Concordance vs FlatIP oracle (skip for FlatIP itself)
        if name == "FlatIP":
            oracle_idx  = idx
            concordance = 1.0
        else:
            assert oracle_idx is not None
            concordance = _concordance(idx, oracle_idx, query_vecs[:200], k=20)
        print(f"    Concordance vs FlatIP @20 : {concordance:.4f}")

        # Quality (if test sessions provided)
        quality: dict = {}
        if test_sessions:
            print(f"    Evaluating NDCG@20/HR@20 on {len(test_sessions):,} sessions ...")
            quality = _evaluate_index_quality(
                idx, item_idx_array, model, test_sessions
            )
            print(f"    NDCG@20 = {quality['ndcg_20']:.4f}  HR@20 = {quality['hr_20']:.4f}")

        results.append({
            "index_name":   name,
            "build_time_s": round(build, 3),
            "size_mb":      round(size, 1),
            "concordance":  round(concordance, 4),
            "latency":      lat,
            **quality,
        })

    # ── Print summary table ─────────────────────────────────────────────────
    print(f"\n{sep}")
    print("Summary")
    print(sep)
    has_quality = bool(test_sessions)
    if has_quality:
        print(f"\n  {'Index':<14}  {'Size MB':>8}  {'Build s':>8}  {'p50 ms':>8}  {'p99 ms':>8}  "
              f"{'Concord':>8}  {'NDCG@20':>8}  {'HR@20':>8}")
        print(f"  {'-'*14}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}")
        for r in results:
            print(f"  {r['index_name']:<14}  {r['size_mb']:>8.1f}  {r['build_time_s']:>8.2f}  "
                  f"{r['latency']['p50_ms']:>8.2f}  {r['latency']['p99_ms']:>8.2f}  "
                  f"{r['concordance']:>8.4f}  {r.get('ndcg_20', '—'):>8}  {r.get('hr_20', '—'):>8}")
    else:
        print(f"\n  {'Index':<14}  {'Size MB':>8}  {'Build s':>8}  {'p50 ms':>8}  {'p99 ms':>8}  {'Concord':>8}")
        print(f"  {'-'*14}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}")
        for r in results:
            print(f"  {r['index_name']:<14}  {r['size_mb']:>8.1f}  {r['build_time_s']:>8.2f}  "
                  f"{r['latency']['p50_ms']:>8.2f}  {r['latency']['p99_ms']:>8.2f}  "
                  f"{r['concordance']:>8.4f}")

    # ── Save JSON ───────────────────────────────────────────────────────────
    output = {
        "run_info": {
            "n_items_indexed": int(item_idx_array.shape[0]),
            "embed_dim":       embed_d,
            "n_bench_queries": _N_BENCH,
            "n_test_sessions": len(test_sessions),
            "seed":            _SEED,
        },
        "index_results": results,
    }
    out_path = output_dir / "inference_index_tradeoffs.json"
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nResults saved → {out_path}")

    # ── Plots ────────────────────────────────────────────────────────────────
    if not args.no_plot:
        _plot_results(results, has_quality, output_dir)

    print("Done.")


def _plot_results(results: list[dict], has_quality: bool, output_dir: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib not available — skipping plots")
        return

    fig_dir = output_dir / "figures"
    fig_dir.mkdir(exist_ok=True)

    names    = [r["index_name"]         for r in results]
    sizes    = [r["size_mb"]            for r in results]
    p50s     = [r["latency"]["p50_ms"]  for r in results]
    p99s     = [r["latency"]["p99_ms"]  for r in results]
    concords = [r["concordance"]        for r in results]

    # ── 3-panel: size / latency / concordance ─────────────────────────────
    n_panels = 4 if has_quality else 3
    fig, axes = plt.subplots(1, n_panels, figsize=(5 * n_panels, 5))
    colors    = ["#D65F5F" if n == "FlatIP" else "#4878CF" for n in names]

    x = np.arange(len(names))

    axes[0].bar(x, sizes, color=colors)
    axes[0].set_xticks(x); axes[0].set_xticklabels(names, rotation=30, ha="right")
    axes[0].set_ylabel("Index size (MB)"); axes[0].set_title("Index size on disk")

    axes[1].bar(x - 0.2, p50s, 0.35, label="p50", color=colors, alpha=0.9)
    axes[1].bar(x + 0.2, p99s, 0.35, label="p99", color=colors, alpha=0.45)
    axes[1].set_xticks(x); axes[1].set_xticklabels(names, rotation=30, ha="right")
    axes[1].set_ylabel("Search latency (ms)"); axes[1].set_title("Search latency @ k=20")
    axes[1].legend()

    axes[2].bar(x, concords, color=colors)
    axes[2].set_xticks(x); axes[2].set_xticklabels(names, rotation=30, ha="right")
    axes[2].set_ylabel("Concordance (fraction)"); axes[2].set_title("Top-20 concordance vs FlatIP")
    axes[2].set_ylim(0, 1.05)

    if has_quality:
        ndcg20s = [r.get("ndcg_20", 0.0) for r in results]
        axes[3].bar(x, ndcg20s, color=colors)
        axes[3].set_xticks(x); axes[3].set_xticklabels(names, rotation=30, ha="right")
        axes[3].set_ylabel("NDCG@20"); axes[3].set_title("Retrieval quality (NDCG@20)")

    plt.tight_layout()
    p = fig_dir / "faiss_tradeoff_curve.png"
    plt.savefig(str(p), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {p}")

    # ── Scatter: index size vs NDCG@20 (only if quality data available) ───
    if has_quality:
        ndcg20s = [r.get("ndcg_20", 0.0) for r in results]
        fig, ax = plt.subplots(figsize=(8, 5))
        for name, size, ndcg, color in zip(names, sizes, ndcg20s, colors):
            ax.scatter(size, ndcg, s=120, color=color, zorder=3)
            ax.annotate(name, (size, ndcg), textcoords="offset points",
                        xytext=(6, 4), fontsize=9)
        ax.set_xlabel("Index size (MB)")
        ax.set_ylabel("NDCG@20")
        ax.set_title("Index Size vs Retrieval Quality")
        ax.grid(True, linestyle="--", alpha=0.4)
        plt.tight_layout()
        p = fig_dir / "index_size_vs_ndcg.png"
        plt.savefig(str(p), dpi=150, bbox_inches="tight")
        plt.close()
        print(f"  Saved: {p}")


if __name__ == "__main__":
    main()
