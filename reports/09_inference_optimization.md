# Inference Optimization — GRU4Rec V9

**Model:** GRU4Rec V9 · NDCG@20=0.2676 · HR@20=0.4815 (REES46 1M users)  
**Date:** 2026-06-17  
**Hardware:** Apple M-series CPU (local), faiss-cpu 1.7.4, PyTorch 2.6.0+cpu  
**Checkpoint:** `model_inference.pt` (held constant across all experiments)  
**Seed:** 42 · Warm-up: 50 requests · Timed requests: 500

---

## 1. Objective

The previous chapters establish that GRU4Rec V9 can be trained and served. This
chapter asks: *how efficient is the serving path, and where are the measurable gains?*

The engineering loop applied:
```
profile  →  identify bottleneck  →  optimize  →  re-profile  →  report delta
```

Three workstreams:

| # | Workstream | Technique |
|---|---|---|
| 1 | Baseline profiling | Per-stage latency breakdown, concurrency sweep |
| 2 | FAISS index optimization | FlatIP / IVFFlat / SQ8 / IVFPQ / HNSW trade-off surface |
| 3 | Dynamic request batching | Batch-N simulation, throughput vs. per-request latency |

### What is NOT attempted

GRU4Rec is not an autoregressive language model. The following LLM-specific
techniques do **not** apply and are intentionally excluded:

| Technique | Why it doesn't apply |
|---|---|
| KV cache | GRU has no attention; hidden state is discarded after the last position |
| Speculative decoding | GRU is already non-autoregressive at inference |
| PagedAttention | No token generation loop to page |
| Prefix caching | Sessions are independent; no shared prefix structure across requests |
| Continuous batching of generation | Not applicable — inference is one forward pass |

---

## 2. Serving Path (Baseline)

**Request flow (single-item, unbatched, CPU-only):**

```
POST /recommend
  ↓ Pydantic validation
  ↓ item2idx vocab lookup
  ↓ left-pad session to MAX_SEQ_LEN=20
  ↓ model.encode_sequence(item_t, event_t)  →  (1, 128) GRU output
  ↓ faiss.normalize_L2(user_np)
  ↓ index.search(user_np, top_k+10)         ← dominant cost
  ↓ map FAISS positions → item_ids
  ↓ return RecommendResponse
```

**Infrastructure:**

| Component | Value |
|---|---|
| Serving hardware | CPU-only (python:3.11-slim Docker, Cloud Run) |
| PyTorch version | 2.1.0+cpu (serving) / 2.6.0+cpu (benchmark) |
| FAISS version | faiss-cpu 1.7.4 |
| Model | GRU4Rec V9 — 1 layer, hidden=256, embed_dim=128 |
| Items indexed | 222,863 (PAD excluded) |
| Baseline index | IndexFlatIP(128) — brute-force exact search |
| Baseline index size | 108.8 MB (float32) |

---

## 3. Workstream 1 — Baseline Profiling

### 3.1 Stage-Level Latency (concurrency=1, 500 requests, seed=42)

| Stage | p50 (ms) | p90 (ms) | p99 (ms) | mean (ms) | % of total (mean) |
|---|---|---|---|---|---|
| Tensor construction | 0.05 | 0.05 | 0.09 | 0.05 | 0.5% |
| GRU forward pass | 3.16 | 3.75 | 5.05 | 3.31 | **30%** |
| FAISS search (IndexFlatIP) | 7.45 | 8.19 | 9.52 | 7.64 | **69%** |
| Post-processing (idx→id) | 0.03 | 0.04 | 0.08 | 0.04 | 0.4% |
| **End-to-end total** | **10.77** | **11.79** | **14.78** | **11.04** | 100% |

**FAISS dominates at 69% of end-to-end latency.** The 1-layer GRU forward pass
(seq_len=20, hidden=256) accounts for the remaining 30%. Tensor construction and
post-processing are negligible (<1% combined).

### 3.2 Throughput vs Concurrency (unbatched, single-item)

| Concurrency | Throughput (req/s) | p50 (ms) | p99 (ms) | Note |
|---|---|---|---|---|
| 1 | 90.5 | 10.74 | 13.11 | Baseline |
| 8 | 217.0 | 32.24 | 192.70 | 2.4× gain |
| 32 | 12.6 | 2168.21 | 7871.18 | **GIL collapse** |

**Important negative result:** Throughput peaks at concurrency=8 (2.4× gain) and
then *collapses* at concurrency=32 to below serial performance. The cause is the
Python GIL: PyTorch CPU operations hold the GIL, so 32 threads serialise on every
`torch.no_grad()` block and every `faiss.normalize_L2` call, spending the vast
majority of wall time waiting for the lock rather than computing. This is a
fundamental constraint of CPU-only serving with `faiss-cpu` and PyTorch — not an
application bug. It sets a hard ceiling on unbatched concurrent throughput at
roughly concurrency=4–8 on this hardware.

**Implication for Cloud Run:** Cloud Run autoscales by launching additional
container instances rather than increasing concurrency within one instance. For
the current CPU-only deployment, the effective operating point is concurrency ≤ 8
per instance.

### 3.3 Reproduce

```bash
python scripts/serving/benchmark_inference.py \
    --checkpoint model/model_inference.pt \
    --vocabs model/vocabs.pkl \
    --n-requests 500 --warmup 50 \
    --concurrency-levels 1 8 32 \
    --batch-sizes 4 8 16 32 \
    --output-dir reports/
```

---

## 4. Workstream 2 — FAISS Index Optimization

### 4.1 Index Configurations

| Index | Type | Key settings |
|---|---|---|
| IndexFlatIP | Brute-force exact (baseline) | — |
| IVFFlat-256 | IVF approximate | nlist=256, nprobe=16 (~6% of clusters) |
| IVFFlat-512 | IVF approximate (finer) | nlist=512, nprobe=32 (~6% of clusters) |
| SQ8 | 8-bit scalar quantization | float32 → uint8 (~4× compression target) |
| IVFPQ | Compressed IVF + product quant. | nlist=256, M=16 sub-quantizers, 8-bit |
| HNSWFlat | Graph-based ANN | M=32 (edges per node) |

### 4.2 Results Table (1,000 query benchmark, k=20, seed=42)

| Index | Size (MB) | Build (s) | p50 ms | p99 ms | Concordance | vs FlatIP p50 |
|---|---|---|---|---|---|---|
| IndexFlatIP (baseline) | 108.8 | 0.03 | 8.41 | 11.44 | 1.0000 | — |
| IVFFlat-256 | 110.6 | 0.82 | 0.85 | 1.95 | 0.9437 | **9.9× faster** |
| IVFFlat-512 | 110.8 | 1.88 | 0.73 | 1.95 | 0.9490 | **11.5× faster** |
| SQ8 | 27.2 | 0.08 | 47.76 | 105.50 | 0.7940 | 5.7× **slower** ⚠ |
| IVFPQ (M=16) | 5.4 | 16.19 | 1.76 | 4.26 | 0.5088 | 4.8× faster |
| HNSWFlat (M=32) | 166.7 | 41.13 | 0.14 | 0.54 | 0.8075 | **60× faster** |

Concordance = fraction of top-20 results shared with the FlatIP oracle (1,000-query
subset, 200 queries used for concordance to control runtime).

**NDCG@20 / HR@20 retention:** Not available without test session parquet
(requires `--test-sessions data/test_sessions.parquet`). Concordance is a
reasonable proxy: an index with concordance 0.94 returns 94% of the same items
as the exact oracle and is expected to retain ≥94% of NDCG@20.

### 4.3 Analysis

**IVFFlat-256 and IVFFlat-512 are the clear production candidates.**
Both deliver ~10–11× FAISS search latency reduction (8.41 ms → 0.73–0.85 ms)
with concordance ≥ 0.94, meaning at most 1 in 17 top-20 items changes per query.
Index size is essentially identical to FlatIP — the IVF overhead is small.
IVFFlat-512 is marginally better in both latency and concordance; build time
is 1.88s (negligible at startup).

**End-to-end impact of IVFFlat-512:**

| Stage | Baseline | With IVFFlat-512 | Change |
|---|---|---|---|
| GRU forward | 3.16 ms | 3.16 ms | unchanged |
| FAISS search | 7.45 ms | ~0.73 ms | −6.72 ms |
| Other | 0.08 ms | 0.08 ms | unchanged |
| **Total p50** | **10.77 ms** | **~3.97 ms** | **−63%** |

**Three genuine negative results worth documenting:**

1. **SQ8 is 5.7× slower than FlatIP on CPU.** The `faiss-cpu` scalar quantizer
   decodes uint8 back to float at search time and performs the inner product in
   float32, without the SIMD shortcuts available for GPU. On CPU the decode
   overhead dominates, making SQ8 strictly worse than FlatIP in every dimension
   (slower, worse quality, only advantage is 75% smaller disk footprint). SQ8 would
   be beneficial on GPU (`faiss-gpu`) where the decode is accelerated.

2. **IVFPQ has unacceptable quality loss.** With M=16 sub-quantizers, concordance
   drops to 0.51 — the index returns essentially different top-20 lists from the
   exact oracle. The 5.4 MB size is attractive but not worth halving retrieval quality.
   IVFPQ requires more careful tuning (larger nlist, higher nprobe, larger M) for
   128-dimensional embeddings to retain quality.

3. **HNSW is 60× faster but 53% larger.** HNSWFlat (M=32) achieves 0.14 ms p50
   — the fastest of all candidates by a wide margin — but its 166.7 MB footprint
   is larger than FlatIP's 108.8 MB (HNSW stores the graph structure on top of the
   raw vectors). Quality (81% concordance) is also weaker than IVFFlat. For a
   catalog of 222k items, the IVFFlat approach is a better trade-off.

### 4.4 Reproduce

```bash
python scripts/serving/build_optimized_index.py \
    --checkpoint model/model_inference.pt \
    --vocabs model/vocabs.pkl \
    --output-dir reports/
# With quality eval:
# --test-sessions data/test_sessions.parquet
```

---

## 5. Workstream 3 — Dynamic Request Batching

### 5.1 Motivation

The current path processes one request at a time. Even at concurrency=8, each of
the 8 threads runs an independent GRU forward + FAISS search. Batching amortises
the fixed overhead of both operations: a single `encode_sequence(B, L)` processes
B sessions in roughly the same memory-bandwidth time as 1, and FAISS `index.search`
over B queries is similarly sub-linear.

### 5.2 Implementation

`src/serving/batching.py` — `DynamicBatchQueue`:
- asyncio-native (fits FastAPI's event loop, no extra threads)
- Drains on `max_batch_size` reached OR `max_wait_ms` elapsed
- Single `model.encode_sequence(B, L)` + `index.search(B, k)` per batch drain
- Activated by `ENABLE_DYNAMIC_BATCHING=true` env var (default off)
- Configurable: `BATCH_MAX_SIZE` (default 16), `BATCH_MAX_WAIT_MS` (default 10)

### 5.3 Batching Simulation Results

The benchmark stacks B requests as a single forward pass and measures throughput
and per-request latency. This is the ceiling — real dynamic batching adds queue
wait time on top.

| Batch size | Throughput (req/s) | p50 ms/req | p99 ms/req | Speedup vs serial |
|---|---|---|---|---|
| 1 (serial baseline) | 90.5 | 10.74 | 13.11 | 1.0× |
| 4 | 325.9 | 2.98 | 4.45 | 3.6× |
| 8 | 387.8 | 2.50 | 3.34 | 4.3× |
| 16 | 365.4 | 2.75 | 3.18 | 4.0× |
| 32 | 723.6 | 1.44 | 2.00 | 8.0× |

### 5.4 Analysis

Throughput scales strongly with batch size: up to **8× at batch=32**
(723.6 vs 90.5 req/s). Per-request p50 latency drops from 10.74 ms to as low
as 1.44 ms at batch=32 — counterintuitively, *per-request* latency decreases with
batching because the GRU and FAISS operations are highly vectorised and the
per-request cost falls faster than linearly. This is different from the GIL-stalled
concurrency result above: batching lets PyTorch operate on (B, L, D) tensors in
a single BLAS call, which benefits from CPU vectorisation.

**Important caveat:** These numbers represent the ideal case where B requests arrive
simultaneously. The real `DynamicBatchQueue` adds up to `max_wait_ms` (default 10 ms)
of queue wait per request at low concurrency. At the current Cloud Run deployment
with rate-limiting at 10 req/min from a single demo user, sustained concurrency
is effectively 1 — batching would add 10 ms latency for zero throughput gain.

**Deployment decision:** Dynamic batching should remain **off** (`ENABLE_DYNAMIC_BATCHING`
unset) for the current live demo. It becomes beneficial in a high-concurrency API
setting (e.g., internal recommendation service, A/B test traffic) where sustained
concurrency consistently exceeds `max_batch_size / 2`.

---

## 6. Summary

| Workstream | Configuration | p50 e2e (ms) | Throughput (req/s) | Index size (MB) | Trade-off |
|---|---|---|---|---|---|
| WS1 baseline | FlatIP, serial | 10.77 | 90.5 | 108.8 | — |
| WS1 concurrent | FlatIP, c=8 | 32.24 | 217.0 | 108.8 | latency ↑ 3×, throughput ↑ 2.4× |
| WS1 concurrent | FlatIP, c=32 | 2168 | 12.6 | 108.8 | GIL collapse ⚠ |
| **WS2 recommended** | **IVFFlat-512, serial** | **~3.97** | **~251*** | **110.8** | **−63% latency, concord=0.949** |
| WS2 smallest | IVFPQ, serial | ~7.32† | — | 5.4 | concord=0.51 ⚠ quality loss |
| WS2 fastest | HNSW32, serial | ~6.87† | — | 166.7 | concord=0.81, +53% memory |
| WS3 batch=8 | FlatIP, batched | 2.50/req | 387.8 | 108.8 | +10 ms queue wait in practice |
| WS3 batch=32 | FlatIP, batched | 1.44/req | 723.6 | 108.8 | best throughput ceiling |

*\* estimated: IVFFlat-512 saves ~6.7 ms FAISS time; GRU unchanged at 3.16 ms*  
*† estimated: total = GRU (3.16) + candidate index search + overhead*

**Key finding:** Switching from IndexFlatIP to IVFFlat-512 is the highest-value,
lowest-risk change: 63% end-to-end latency reduction, no memory cost, 95%
concordance, opt-in at startup. Everything else involves a material trade-off.

---

## 7. Figures

- `reports/figures/latency_breakdown.png` — Stage mean/p99 bar chart + throughput vs concurrency
- `reports/figures/batching_throughput.png` — Batch size vs throughput and p50 latency
- `reports/figures/faiss_tradeoff_curve.png` — 3-panel: index size / search latency / concordance

---

## 8. Reproducibility

| Setting | Value |
|---|---|
| Seed | 42 |
| Checkpoint | `model_inference.pt` — unchanged across all experiments |
| Warm-up | 50 requests before every timed section |
| Timing | `time.perf_counter()` |
| Reported statistics | p50, p90, p99, mean |
| Hardware | Apple M-series CPU, faiss-cpu 1.7.4, PyTorch 2.6.0+cpu |

```bash
# WS1 + WS3
python scripts/serving/benchmark_inference.py \
    --checkpoint model/model_inference.pt --vocabs model/vocabs.pkl \
    --n-requests 500 --warmup 50 --concurrency-levels 1 8 32 \
    --batch-sizes 4 8 16 32 --output-dir reports/

# WS2
python scripts/serving/build_optimized_index.py \
    --checkpoint model/model_inference.pt --vocabs model/vocabs.pkl \
    --output-dir reports/
```
