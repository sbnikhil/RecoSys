# RecoSys

An end-to-end ML portfolio project — session-based recommendation engine on the REES46
eCommerce clickstream dataset (Oct 2019 – Jan 2020, ~280 M events). Covers the full
MLOps stack: BigQuery → Spark → GRU4Rec → Vertex AI → Hugging Face Spaces → MLflow → drift monitoring + weekly re-training.

**[Live Demo →](https://recosys.vercel.app/)**

---

## Results

| Metric | Value | T4Rec Target | Baseline (popularity) |
|---|---|---|---|
| NDCG@20 | **0.2676** | ≥ 0.22 ✓ | 0.0353 |
| HR@20 | **0.4815** | ≥ 0.44 ✓ | 0.0806 |
| NDCG@10 | **0.2420** | — | 0.0296 |
| HR@10 | **0.3803** | — | 0.0579 |
| vs. T4Rec XLNet+RTD (best published) | **+5.1% NDCG@20** | — | — |
| vs. Popularity baseline | **+7.6× NDCG@20** | — | — |

Model: GRU4Rec V9 with event-type features, trained on 1M-user REES46 sample on Vertex AI (A100 40GB, 10h 46m).

Scaling from 500k → 1M users produced a consistent **+2.7% NDCG@20** gain, confirming data-scale benefits.
SASRec was attempted 5 times — all failed on short-session data (best NDCG@20 = 0.0044). Documented as a negative result.

---

## Live Demo

**[https://recosys.vercel.app/](https://recosys.vercel.app/)**

A full e-commerce front-end backed by the live GRU4Rec V9 model served on
**Hugging Face Spaces** (Docker, FastAPI + FAISS, CPU-only). Model artifacts
(`model_inference.pt`, `vocabs.pkl`) are stored in the
[manojarulmurugan/recosys-models](https://huggingface.co/datasets/manojarulmurugan/recosys-models)
HF Hub dataset repo and downloaded at container startup. The Vercel frontend proxies
`/api/*` requests to the HF Space backend via the `BACKEND_URL` environment variable.

### Shop tab
- Browse a 2,004-product catalog with category filtering and search
- Click any product → product detail page opens, a `view` event is silently logged to your session
- **Add to Cart** logs a `cart` event; **Buy** logs a `purchase` event
- Cart page with per-item or bulk purchase
- **Session panel** (right sidebar) tracks your activity in real time — use the **Quick / Browsing / Shopper** quick-fill buttons to auto-load demo events and immediately get recommendations without manually clicking through the catalog
- **Get Recommendations** → POSTs your session to `/api/recommend` → ranked product cards powered by GRU4Rec V9 + FAISS ANN search over 209,092 items

### Model Performance tab
- **KPI cards** — NDCG@20, HR@20, vs-T4Rec lift, catalog size
- **Training progression chart** — Val NDCG@20 + Train loss over 29 epochs; best checkpoint (epoch 24) marked
- **Model comparison table + bar chart** — GRU4Rec V9 vs T4Rec XLNet+RTD vs Popularity baseline vs SASRec, with type badges distinguishing my models from published baselines and negative results
- **Distribution drift monitor** — live from `/api/drift`; Jensen-Shannon divergence between Jan 2020 (train) and Mar 2020 (test) distributions, with plain-English explanations, donut charts for Bestseller Stability and Model Coverage
- **Precision at K chart** — NDCG@10, HR@10, NDCG@20, HR@20
- **Data scaling impact chart** — 500k vs 1M side-by-side
- **Architecture + training run cards** — full hyperparameter table, Vertex AI job metadata

### API endpoints (Vercel → HF Spaces proxy)

| Route | Method | Description |
|---|---|---|
| `/api/recommend` | POST | Session-based recommendations. Body: `{"session":[{"item_id":"...","event_type":"view"}],"top_k":20}` |
| `/api/health` | GET | Model health + items indexed in FAISS |
| `/api/drift` | GET | Distribution drift report (JSD, overlap, coverage) |

Serving: **Hugging Face Spaces** (Docker, port 7860, `Dockerfile.spaces`)  
Artifacts: `manojarulmurugan/recosys-models` on HF Hub (downloaded at startup via `HF_DATASET_REPO` env var)

---

## Project status

| Phase | Description | Status |
|---|---|---|
| 1 | Data ingestion — raw CSVs → GCS → BigQuery | ✅ Complete |
| 2 | Exploratory data analysis (BigQuery + Spark) | ✅ Complete |
| 3 | Spark preprocessing pipeline (Dataproc) | ✅ Complete |
| 4 | Sampling, temporal splits, interaction tables | ✅ Complete |
| 4 | Two-Tower V1–V6 + GRU4Rec V7 + SASRec V8 (all below pop baseline) | ✅ Complete (negative results documented) |
| 4 | GRU4Rec V9 session-based — 500k — NDCG@20=0.2606 | ✅ Complete |
| 5 | 1M-user sample creation (890,736 users, 222,864 items) | ✅ Complete |
| 6–7 | Vertex AI training on 1M sample — NDCG@20=0.2676 | ✅ Complete |
| 8–9 | FastAPI + FAISS serving (originally Cloud Run; migrated to HF Spaces) | ✅ Complete |
| 10–11 | MLflow experiment tracking (DagsHub) | ✅ Complete |
| 12–13 | Distribution drift monitoring (JSD, COVID-period shift) | ✅ Complete |
| 14 | End-to-end live demo (Vercel → HF Spaces) | ✅ Complete |
| 15 | Inference optimization — profiling, FAISS index comparison, dynamic batching | ✅ Complete |
| 16 | Weekly re-training pipeline — rolling eval, experience replay, COVID stress-test | ✅ Complete |

---

## Infrastructure

| Resource | Value |
|---|---|
| GCP project | `recosys-489001` *(training/preprocessing only)* |
| BigQuery dataset | `recosys-489001.recosys` |
| Vertex AI job | `3348631089810767872` · A100 40GB |
| Dataproc cluster | `eda-reco` — `us-central1`, `n4-standard-2` × 3 nodes |
| Model artifacts | HF Hub dataset · `manojarulmurugan/recosys-models` |
| Serving | Hugging Face Spaces · Docker · FastAPI + FAISS · port 7860 |
| Demo hosting | Vercel · `manojarulmurugan/RecoSys` fork |
| MLflow tracking | DagsHub · `manojarulmurugan/RecoSys` |
| Service account | `~/secrets/recosys-service-account.json` |

---

## Dataset

**Source:** [REES46 eCommerce Behaviour Data](https://www.kaggle.com/datasets/mkechinov/ecommerce-behavior-data-from-multi-category-store) (Kaggle)

| Property | Value |
|---|---|
| Raw rows loaded | 288,779,227 |
| Months in scope | Oct 2019 – Jan 2020 (train) + Feb 2020 (test) |
| Months held out | Mar – Apr 2020 (reserved for drift evaluation) |
| Event types | `view` 94.1 %, `cart` 4.2 %, `purchase` 1.6 % |
| Feedback type | Implicit only (no explicit ratings) |
| Schema | `event_time, event_type, product_id, category_id, category_code, brand, price, user_id, user_session` |

---

## Model — GRU4Rec V9

**Architecture:** Single-layer GRU encoder → cosine-similarity scoring head → full softmax loss (temperature = 0.07, label smoothing = 0.1).

**Key design choices:**
- Session-based framing: each `user_session` is an independent sequence. Avoids collapsing multi-session users into one long history, which matches T4Rec paper §4.1 evaluation conventions.
- Event-type embedding concatenated with item embedding at input — view/cart/purchase carry different intent signals.
- Cosine similarity head with temperature scaling: gradients are ~14× stronger than raw dot-product, critical for sparse implicit feedback.
- Trained with full softmax over all 222,864 items per step — memory-intensive but avoids sampled-softmax bias.

**Hyperparameters:**

| Parameter | Value |
|---|---|
| `embed_dim` | 128 |
| `gru_hidden` | 256 |
| `n_layers` | 1 |
| `dropout` | 0.3 |
| `batch_size` | 256 |
| `learning_rate` | 3e-4 |
| `temperature` | 0.07 |
| `label_smoothing` | 0.1 |
| `scheduler` | cosine (lr_min=1e-5) |
| `max_seq_len` | 20 |
| `patience` | 5 |

**Training data (1M run):**

| Metric | Value |
|---|---|
| Users | 890,736 |
| Items (incl PAD) | 222,864 |
| Train sessions | 2,884,945 |
| Val sessions | 151,177 |
| Best epoch | 24 / 29 |
| FAISS index items | 209,092 |

---

## Negative result — SASRec V10

SASRec was attempted 5 times on the same dataset with systematic hyperparameter and loss-function variation. All attempts failed to beat the popularity baseline (NDCG@20 = 0.034). Best result: NDCG@20 = 0.0044.

**Root cause:** Full self-attention memorises exact training sequence → item co-occurrences. On short sessions (max_len = 20) it has too much capacity and collapses to negative embedding collapse — positive items get high scores, all others are pushed uniformly negative with no transfer to the validation set.

**Literature backing:** Ludewig & Jannach (RecSys 2019), Hidasi & Czapp (RecSys 2023) — GRU4Rec outperforms SASRec on short-session eCommerce tasks. SASRec wins on long user-history benchmarks (ML-1M, Amazon reviews).

---

## Serving — Hugging Face Spaces

*Dockerfile: [`Dockerfile.spaces`](Dockerfile.spaces)*  
*Model loader: [`src/serving/model_loader.py`](src/serving/model_loader.py)*  
*App: [`src/serving/app.py`](src/serving/app.py)*

The GRU4Rec V9 model is served as a FastAPI application inside a Hugging Face Spaces
Docker container. The serving path is CPU-only and single-item unbatched.

### Request flow

```
POST /recommend
  ↓  Pydantic validation
  ↓  item2idx vocab lookup
  ↓  left-pad session to MAX_SEQ_LEN=20
  ↓  model.encode_sequence(item_t, event_t)   →  (1, 128) GRU output
  ↓  faiss.normalize_L2(user_np)
  ↓  index.search(user_np, top_k+10)          ←  dominant cost (IndexFlatIP, 222,863 items)
  ↓  map FAISS positions → item_ids
  ↓  return RecommendResponse
```

### Artifact resolution at startup

```
1. /app/model/           — baked into Docker image (instant, preferred)
2. HF_DATASET_REPO       — downloads from manojarulmurugan/recosys-models at startup
3. GCS                   — legacy fallback (no longer used)
```

In the HF Spaces deployment, `HF_DATASET_REPO=manojarulmurugan/recosys-models` is set
as a Space secret. The container downloads `model_inference.pt` (115 MB) and `vocabs.pkl`
(22 MB) from the HF Hub dataset repo, then builds the FAISS IndexFlatIP over 222,863 items.

### Serving specs

| Component | Value |
|---|---|
| Runtime | `python:3.11-slim` Docker, HF Spaces |
| Port | 7860 (HF Spaces default) |
| PyTorch | 2.1.0+cpu |
| FAISS | faiss-cpu 1.7.4 |
| Index | IndexFlatIP(128) — brute-force, exact |
| Items indexed | 222,863 (PAD excluded) |
| Rate limiting | slowapi (prevents runaway demo usage) |

### Dynamic batching (opt-in)

`src/serving/batching.py` implements a `DynamicBatchQueue` (asyncio-native) that
accumulates concurrent requests into a single GRU forward + FAISS search. Activated by
`ENABLE_DYNAMIC_BATCHING=true` env var — default off. At current demo traffic (single
user, low concurrency) the queue wait time would exceed any batching gain, so it remains
disabled. See Inference Optimization § Workstream 3 for benchmark numbers.

---

## Inference Optimization

*Detailed results and methodology: [`reports/09_inference_optimization.md`](reports/09_inference_optimization.md)*  
*Colab notebook: [`notebooks/10_inference_optimization_colab.ipynb`](notebooks/10_inference_optimization_colab.ipynb)*

The engineering loop applied after deployment:
```
profile  →  identify bottleneck  →  optimize  →  re-profile  →  report delta
```

### Workstream 1 — Baseline Profiling

The serving path is single-item and unbatched. Every `/recommend` request runs:
tensor construction → GRU forward → FAISS search → post-processing.

| Stage | p50 (ms) | p99 (ms) | % of total |
|---|---|---|---|
| Tensor construction | 0.05 | 0.09 | 0.5% |
| GRU forward (encode_sequence) | 3.16 | 5.05 | 30% |
| FAISS search (IndexFlatIP) | 7.45 | 9.52 | **69%** |
| Post-processing (idx→id) | 0.03 | 0.08 | 0.4% |
| **End-to-end total** | **10.77** | **14.78** | 100% |

**Finding:** FAISS IndexFlatIP dominates at 69% of end-to-end latency. The 1-layer GRU forward at seq_len=20 accounts for 30%. Tensor construction and post-processing are negligible (<1% combined).

Throughput: concurrency=1 → **90.5 req/s** · concurrency=8 → **217 req/s** · concurrency=32 → **12.6 req/s** (GIL collapse — PyTorch CPU ops hold the GIL; throughput peaks at c≈8).

### Workstream 2 — FAISS Index Optimization

Six index types benchmarked on latency × memory × retrieval quality:

| Index | Size (MB) | p50 search (ms) | Concordance vs FlatIP | vs FlatIP latency |
|---|---|---|---|---|
| IndexFlatIP (baseline) | 108.8 | 8.41 | 1.000 | — |
| IVFFlat-256 (nprobe=16) | 110.6 | 0.85 | 0.944 | **9.9× faster** |
| **IVFFlat-512 (nprobe=32)** | **110.8** | **0.73** | **0.949** | **11.5× faster ✓** |
| SQ8 (8-bit scalar quant.) | 27.2 | 47.76 | 0.794 | 5.7× **slower** ⚠ |
| IVFPQ (M=16, 8-bit) | 5.4 | 1.76 | 0.509 | 4.8× faster, quality ⚠ |
| HNSWFlat (M=32) | 166.7 | 0.14 | 0.808 | 60× faster, +53% RAM |

Concordance = fraction of top-20 results shared with the FlatIP oracle.
Quality is always paired with latency/memory — never reported in isolation.

**Recommended: IVFFlat-512** — 11.5× FAISS speedup, −63% end-to-end latency, same index size, 94.9% concordance. Notable negative result: SQ8 is 5.7× *slower* on CPU (faiss-cpu decodes uint8→float32 at search time without SIMD acceleration).

### Workstream 3 — Dynamic Request Batching

Current path processes one request at a time. `DynamicBatchQueue`
(`src/serving/batching.py`) batches concurrent requests into a single
GRU forward + FAISS search:

| Batch size | Throughput (req/s) | p50 ms/req | Speedup |
|---|---|---|---|
| 1 (serial) | 90.5 | 10.74 | 1.0× |
| 4 | 325.9 | 2.98 | 3.6× |
| 8 | 387.8 | 2.50 | 4.3× |
| 16 | 365.4 | 2.75 | 4.0× |
| 32 | 723.6 | 1.44 | **8.0×** |

**Opt-in:** `ENABLE_DYNAMIC_BATCHING=true` env var. Default off — existing API
contract and live demo are unchanged.

**Honest assessment:** At low concurrency (the public demo case), dynamic batching
adds queue wait time with no benefit. Throughput gain only materialises when
sustained concurrency exceeds ~`max_batch_size / 2`.

### What was explicitly NOT attempted

| Technique | Reason |
|---|---|
| KV cache | GRU has no attention; hidden state is discarded after the last position |
| Speculative decoding | GRU is already non-autoregressive at inference |
| PagedAttention | No token generation loop to page |
| Prefix caching | Sessions are independent; no shared prefix structure |
| Continuous batching of generation | Not applicable — inference is one forward pass |

Knowing which techniques transfer to a recommender is part of the value of this chapter.

---

## Weekly Re-Training Pipeline

*Detailed results and methodology: [`reports/12_weekly_finetuning.md`](reports/12_weekly_finetuning.md)*  
*Colab notebook: [`notebooks/12_weekly_finetuning_colab.ipynb`](notebooks/12_weekly_finetuning_colab.ipynb)*  
*Script: [`scripts/retrain/run_weekly_pipeline.py`](scripts/retrain/run_weekly_pipeline.py)*

A recommender model trained on historical data begins to decay the moment it is deployed. This chapter builds an automated weekly fine-tuning pipeline to keep GRU4Rec V9 adapted to a continuously shifting user distribution. The evaluation period — February and March 2020 — includes the WHO pandemic declaration on March 11, making it an unusually strong natural stress-test for distribution shift.

### Pipeline design (per week)

```
1. DRIFT DETECTION    — Compute JSD vs. monthly baseline; skip fine-tune if JSD < 0.10
2. EXPERIENCE REPLAY  — Mix 30% historical sessions into weekly fine-tune data
3. FINE-TUNING        — 5 epochs, LR 1e-4 → 1e-5 (CosineAnnealingLR), from current best checkpoint
4. ROLLING EVAL       — Evaluate on NEXT week's data (not a frozen val set)
5. CHECKPOINT PROMO   — Promote only if rolling NDCG@20 improved ≥ 0.0005
```

### Drift (JSD) by week

All 8 weeks exceeded the 0.10 threshold. The March JSD series captures the COVID-19 behavioural shift with high fidelity — monotonic acceleration across 4 consecutive weeks beginning the week of the WHO declaration.

| Week | Period | JSD vs. baseline | Baseline month |
|---|---|---|---|
| 1 | Feb 01–08 | 0.122 | Jan 2020 |
| 2 | Feb 08–15 | 0.146 | Jan 2020 |
| 3 | Feb 15–22 | 0.176 | Jan 2020 |
| 4 | Feb 22–Mar 01 | 0.195 | Jan 2020 |
| 5 | Mar 01–08 | 0.125 | Feb 2020 |
| 6 | Mar 08–15 *(WHO declaration)* | 0.155 | Feb 2020 |
| 7 | Mar 15–22 | 0.200 | Feb 2020 |
| 8 | Mar 22–29 | **0.241** | Feb 2020 |

### Rolling evaluation results

Baseline NDCG@20 (original model on Feb 08–15 rolling window): **0.2592**

| Week | Eval window | NDCG@20 | HR@20 | Δ vs. best | Decision |
|---|---|---|---|---|---|
| 1 | Feb 08–15 | 0.2667 | 0.491 | **+0.0076** | **promoted** |
| 2 | Feb 15–22 | 0.2572 | 0.479 | −0.0096 | skipped |
| 3 | Feb 22–Mar 01 | 0.2596 | 0.482 | −0.0071 | skipped |
| 4 | Mar 01–08 | 0.2610 | 0.480 | −0.0058 | skipped |
| 5 | Mar 08–15 | **0.2714** | **0.500** | **+0.0046** | **promoted** |
| 6 | Mar 15–22 | 0.2572 | 0.485 | −0.0142 | skipped |
| 7 | Mar 22–29 | 0.2527 | 0.479 | −0.0187 | skipped |
| 8 | test_sessions.parquet | 0.2370 | 0.435 | −0.0344† | skipped |

†Week 8 eval switches to `test_sessions.parquet` (January distribution) — not comparable to rolling windows.

**Final best checkpoint:** week 5 — NDCG@20=0.2714, HR@20=0.500 (+4.7% relative over baseline)

### Key findings

**Month-boundary fine-tuning works; within-month fine-tuning doesn't.** Both promotions (weeks 1 and 5) are the first week of a new month. All six within-month weeks were skipped. The probability of 6 consecutive negatives by chance is 1/2⁶ ≈ 1.6% — a real pattern. Weekly granularity is likely too fine; a monthly trigger achieves the same promotions with 75% less compute.

**Experience replay prevents forgetting but not overfitting.** Training converges cleanly every week (0.34–0.44 loss drop per week) with no catastrophic forgetting. Within-month failures are caused by overfitting to weekly noise, not loss of historical signal.

**Rolling eval is the correct methodology.** Under a frozen January val set, week 5's successful March adaptation would register as catastrophic regression. Rolling eval correctly measures whether the model predicts the immediate future — the production-relevant question.

**High-drift periods (COVID weeks 6–8) are hard to adapt to.** When the distribution is shifting week-over-week, a model trained on week N cannot reliably predict week N+1 because the ground has moved again. This is a fundamental limitation, not a pipeline flaw.

---

## Repository layout

```
RecoSys/
├── demo/                                    # Vercel-hosted frontend
│   ├── index.html                           # Full e-commerce UI (Shop + Model Performance tabs)
│   ├── catalog.json                         # 2,004-item product catalog (from BigQuery)
│   ├── metrics.json                         # Pre-baked model metrics for the dashboard
│   ├── vercel.json                          # SPA rewrite rule
│   └── api/
│       ├── recommend.js                     # POST /api/recommend → HF Spaces proxy
│       ├── health.js                        # GET /api/health → HF Spaces proxy
│       └── drift.js                         # GET /api/drift → HF Spaces proxy
├── src/
│   ├── data/
│   │   └── feature_builder.py               # Feature engineering helpers (Two-Tower era)
│   ├── sequence/
│   │   ├── models/
│   │   │   ├── gru4rec.py                   # GRU4Rec V9 model definition
│   │   │   └── sasrec.py                    # SASRec V10 model (negative result)
│   │   ├── data/
│   │   │   ├── session_dataset.py           # SessionTrainDataset, SessionEvalDataset
│   │   │   ├── sequence_dataset.py          # Sequence-level dataset (non-session framing)
│   │   │   └── negative_sampler.py          # In-batch negative sampling utilities
│   │   ├── training/
│   │   │   └── train_sequence.py            # train_epoch_session, get_param_groups
│   │   └── evaluation/
│   │       └── evaluate_sequence.py         # NDCG@K / HR@K + FAISS-based evaluation
│   ├── serving/
│   │   ├── app.py                           # FastAPI app (HF Spaces / Cloud Run)
│   │   ├── model_loader.py                  # Artifact loading (baked image → HF Hub → GCS)
│   │   └── batching.py                      # Dynamic batch queue (ENABLE_DYNAMIC_BATCHING)
│   └── two_tower/                           # Two-Tower V1–V6 (all below pop baseline)
│       ├── models/two_tower.py
│       ├── data/dataset.py
│       ├── training/train.py
│       └── evaluation/evaluate.py
├── scripts/
│   ├── preprocessing_pipeline.py            # PySpark cleaning pipeline (Dataproc)
│   ├── create_samples.py                    # BigQuery → 50k/500k user samples
│   ├── create_splits.py                     # Temporal train/test splits
│   ├── create_interactions.py               # Interaction table builder
│   ├── build_catalog.py                     # BigQuery → demo/catalog.json
│   ├── data/
│   │   └── build_1m_sample.py               # 1M-user sample construction from BigQuery
│   ├── sequence/
│   │   ├── build_session_sequences.py       # Events → session parquets (1M)
│   │   ├── build_sequences_500k.py          # Events → session parquets (500k)
│   │   ├── rebuild_local_sessions.py        # Local session rebuild from raw CSVs
│   │   ├── validate_session_sequences.py    # Sanity checks on session parquets
│   │   ├── train_gru4rec_session.py         # GRU4Rec session training entry point
│   │   ├── train_gru4rec.py                 # GRU4Rec sequence training (earlier framing)
│   │   ├── train_sasrec_session.py          # SASRec session training
│   │   └── train_sasrec.py                  # SASRec sequence training
│   ├── retrain/
│   │   └── run_weekly_pipeline.py           # Weekly fine-tuning pipeline (Chapter 12)
│   ├── serving/
│   │   ├── benchmark_inference.py           # WS1+WS3: per-stage profiling + batching sim
│   │   ├── build_optimized_index.py         # WS2: FAISS index comparison + quality eval
│   │   ├── log_experiments_mlflow.py        # Push run metrics to MLflow / DagsHub
│   │   ├── build_and_deploy.ps1             # Docker build + deploy script (Windows)
│   │   ├── test_endpoint.ps1                # Smoke-test the live endpoint
│   │   └── demo_e2e.ps1                     # End-to-end demo script
│   ├── monitoring/
│   │   ├── compute_drift.py                 # JSD, overlap, coverage → drift_report.json
│   │   ├── plot_drift.py                    # Drift visualisation plots
│   │   └── restore_mlflow_logs.py           # Restore MLflow run logs from backup
│   ├── cloud/
│   │   ├── setup_gcp.sh                     # GCP project + API setup
│   │   ├── submit_vertex_job.sh             # Launch Vertex AI training job
│   │   ├── deploy_mlflow.ps1                # Deploy MLflow server to GCE
│   │   └── make_public.ps1                  # Make GCS artifacts publicly readable
│   ├── deploy/
│   │   └── upload_to_hf_hub.py             # Upload model artifacts to HF Hub dataset
│   ├── diagnostics/
│   │   └── diagnostic_cold_warm_inversion.py  # Cold/warm embedding inversion analysis
│   └── two_tower/                           # Two-Tower V1–V6 scripts (negative results)
│       ├── train_two_tower.py … train_two_tower_v6.py
│       ├── build_user_features.py / build_item_features.py
│       ├── evaluate_two_tower.py
│       └── diagnose_*.py / investigate_negatives.py
├── notebooks/
│   ├── 01_setup_and_integration.ipynb       # GCP + BigQuery setup
│   ├── 02_BigQuery_Setup.ipynb              # BigQuery schema + raw data verification
│   ├── 03_EDA_BigQuery.ipynb                # Exploratory analysis (BigQuery)
│   ├── 04_EDA_DataProc.ipynb                # Exploratory analysis (Dataproc / Spark)
│   ├── 05_cleaned_sample_BigQuery_validation.ipynb
│   ├── 06_feature_verification_for_TT.ipynb # Two-Tower feature verification
│   ├── 07_two_tower_500k_colab.ipynb        # Two-Tower 500k training (Colab)
│   ├── 08_GRU4Rec_Rebuild_Colab.ipynb       # GRU4Rec V9 rebuild + 500k training
│   ├── 09_SASRec_V10_Colab.ipynb            # SASRec V10 (negative result, 5 attempts)
│   ├── 10_inference_optimization_colab.ipynb  # Inference profiling + FAISS comparison
│   ├── 11_rebuild_session_parquets_colab.ipynb  # Rebuild session parquets for 1M
│   └── 12_weekly_finetuning_colab.ipynb     # Weekly re-training pipeline (Chapter 12)
├── reports/
│   ├── 01_eda_report_v1.md                  # First-pass EDA findings
│   ├── 02_eda_report_v2.md                  # Refined EDA with Spark
│   ├── 03_dataproc_preprocessing_run.md     # Dataproc cleaning pipeline run log
│   ├── 04_sampling_splits_interactions.md   # Sampling + split methodology
│   ├── 05_model_experiments_50k.md          # Two-Tower V1–V6 experiment results
│   ├── 06_failure_analysis_pretransition.md # Pre-GRU4Rec failure analysis
│   ├── 07_session_model_results.md          # GRU4Rec V9 500k results + SASRec failure
│   ├── 08_vertex_ai_1m_training.md          # 1M Vertex AI training log (epoch-by-epoch)
│   ├── 09_inference_optimization.md         # Inference optimization methodology + results
│   ├── 12_weekly_finetuning.md              # Weekly re-training pipeline results
│   ├── drift_report.json                    # Latest distribution drift monitor output
│   ├── inference_benchmark_baseline.json    # WS1 raw benchmark output
│   ├── inference_index_tradeoffs.json       # WS2 FAISS index comparison output
│   └── figures/
│       ├── 500k_training_curves.png         # Val NDCG@20 + train loss over epochs
│       ├── item_popularity_drift.png        # Popularity distribution drift plot
│       ├── popularity_rank_scatter.png      # Rank-frequency scatter
│       ├── latency_breakdown.png            # WS1: stage latency + throughput vs concurrency
│       ├── faiss_tradeoff_curve.png         # WS2: index comparison 4-panel
│       ├── index_size_vs_ndcg.png           # WS2: size vs quality scatter
│       └── batching_throughput.png          # WS3: batch size vs throughput/latency
├── artifacts/
│   ├── 500k/
│   │   ├── user_features_500k/              # Parquet user feature table (500k)
│   │   └── item_features/                   # Parquet item feature table
│   └── 1M/
│       └── sequences/                       # train/val/test session parquets (1M)
├── configs/
│   └── vertex_gru4rec_1m.yaml              # Vertex AI job config for 1M GRU4Rec run
├── docs/
│   ├── sequence_model_results.md            # Internal sequence model results reference
│   ├── prior_work_comparative_analysis.md   # Comparison against prior work
│   └── day5_14_continuation_brief.md        # Project continuation notes
├── model/
│   ├── model_inference.pt                   # Production checkpoint (epoch 24, NDCG@20=0.2676)
│   └── vocabs.pkl                           # item2idx / idx2item / user2idx vocabs
├── Dockerfile.serving                        # Serving image (legacy Cloud Run build)
├── Dockerfile.spaces                         # Hugging Face Spaces serving image (active)
├── Dockerfile.mlflow                         # MLflow tracking server image
├── Dockerfile.gru4rec                        # Vertex AI training image
├── requirements.txt
└── README.md
```

---

## Phase 3 — Preprocessing pipeline

**Script:** `scripts/preprocessing_pipeline.py` (PySpark, runs on Dataproc)

Five cleaning steps applied to `events_raw` in order:

| Step | Operation | Rows removed |
|---|---|---|
| 1 | Fill nulls: `category_code` and `brand` → `"unknown"`, drop null `user_session` | ~2 |
| 2 | Exact deduplication on `(event_time, event_type, product_id, user_id, user_session)` | ~8.8 M |
| 3 | Near-duplicate removal — same user/product/type within 1 second | ~96 k |
| 4 | Price floor — drop events with `price < 1.0` | ~6 k |
| 5 | Bot removal — drop users with avg events/day > 300 | ~2 users |

Followed by **3-core filtering** (iterative): retain only users and items with ≥ 3 interactions each, converges in ~3 rounds.

**Output:** `recosys.events_clean` — **279,937,243 rows**, 7,565,157 users, 284,523 items.

---

## Phase 4 — Sampling, splits, and interaction tables

### User-based samples

| Table | Users | Events | Items |
|---|---|---|---|
| `recosys.events_sample_50k` | 50,000 | 1,860,124 | 121,951 |
| `recosys.events_sample_500k` | 500,000 | 18,506,282 | 231,031 |

### Temporal train/test splits

| Table | Rows | Users |
|---|---|---|
| `recosys.train_50k` | 1,512,837 | 44,559 |
| `recosys.test_50k` | 347,287 | 20,626 |
| `recosys.train_500k` | 15,054,830 | 445,150 |
| `recosys.test_500k` | 3,451,452 | 206,887 |
| `recosys.train_full` | 227,460,074 | 6,736,214 |
| `recosys.test_full` | 52,477,169 | 3,132,215 |

---

## GCS bucket layout

```
gs://recosys-data-bucket/
├── raw/                                    # Original CSVs (52.69 GiB, 7 files)
├── processed/events_clean/                 # Parquet output from Spark pipeline
├── samples/
│   ├── events_sample_50k/
│   └── events_sample_500k/
├── data/1M/                                # 1M-user session sequences
└── models/
    ├── gru4rec_session_v9/                 # 500k checkpoint
    └── gru4rec_session_v9_1M/
        ├── best_checkpoint.pt              # Epoch 24 (NDCG@20=0.2676)
        ├── training_log.json
        └── hparams.json
```

---

## Requirements

```
google-cloud-bigquery>=3.0.0
google-cloud-storage>=3.0.0
pandas>=1.0.0
torch>=2.0.0
faiss-cpu>=1.7.4
fastapi>=0.100.0
uvicorn>=0.23.0
db-dtypes>=1.0.0
polars>=0.20.0
matplotlib>=3.0.0
seaborn>=0.12.0
```

Install with:

```bash
pip install -r requirements.txt
```
