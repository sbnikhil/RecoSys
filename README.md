# RecoSys

An end-to-end ML portfolio project — session-based recommendation engine on the REES46
eCommerce clickstream dataset (Oct 2019 – Jan 2020, ~280 M events). Covers the full
MLOps stack: BigQuery → Spark → GRU4Rec → Vertex AI → Cloud Run → MLflow → drift monitoring.

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

A full e-commerce front-end backed by the live GRU4Rec V9 model on Cloud Run.

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

### API endpoints (Vercel → Cloud Run proxy)

| Route | Method | Description |
|---|---|---|
| `/api/recommend` | POST | Session-based recommendations. Body: `{"session":[{"item_id":"...","event_type":"view"}],"top_k":20}` |
| `/api/health` | GET | Model health + items indexed in FAISS |
| `/api/drift` | GET | Live distribution drift report (JSD, overlap, coverage) |

Cloud Run service: `https://recosys-recommender-o34zzoh3da-uc.a.run.app`

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
| 8–9 | Cloud Run serving (FastAPI + FAISS) | ✅ Complete |
| 10–11 | MLflow experiment tracking | ✅ Complete |
| 12–13 | Distribution drift monitoring (COVID-period shift) | ✅ Complete |
| 14 | End-to-end live demo (Vercel) | ✅ Complete |

---

## Infrastructure

| Resource | Value |
|---|---|
| GCP project | `recosys-489001` |
| BigQuery dataset | `recosys-489001.recosys` |
| GCS bucket | `gs://recosys-data-bucket` |
| Cloud Run service | `recosys-recommender` · `us-central1` |
| Vertex AI job | `3348631089810767872` · A100 40GB |
| Dataproc cluster | `eda-reco` — `us-central1`, `n4-standard-2` × 3 nodes |
| Demo hosting | Vercel · `manojarulmurugan/RecoSys` fork |
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

## Repository layout

```
RecoSys/
├── demo/                            # Vercel-hosted frontend
│   ├── index.html                   # Full e-commerce UI (Shop + Model Performance tabs)
│   ├── catalog.json                 # 2,004-item product catalog (from BigQuery)
│   ├── metrics.json                 # Pre-baked model metrics for the dashboard
│   ├── vercel.json                  # SPA rewrite rule
│   └── api/
│       ├── recommend.js             # POST /api/recommend → Cloud Run proxy
│       ├── health.js                # GET /api/health → Cloud Run proxy
│       └── drift.js                 # GET /api/drift → Cloud Run proxy
├── src/
│   ├── data/
│   │   └── feature_builder.py
│   ├── sequence/
│   │   ├── models/gru4rec.py        # GRU4Rec model definition
│   │   ├── data/session_dataset.py  # SessionTrainDataset, SessionEvalDataset
│   │   └── evaluation/evaluate_sequence.py
│   ├── serving/
│   │   └── app.py                   # FastAPI app (Cloud Run)
│   └── two_tower/
│       └── ...
├── scripts/
│   ├── preprocessing_pipeline.py    # PySpark cleaning (Dataproc)
│   ├── create_samples.py
│   ├── create_splits.py
│   ├── create_interactions.py
│   ├── build_catalog.py             # BigQuery → demo/catalog.json
│   ├── sequence/
│   │   ├── build_session_sequences.py
│   │   └── train_gru4rec_session.py
│   ├── monitoring/
│   │   ├── compute_drift.py         # JSD, overlap, coverage → drift_report.json
│   │   └── plot_drift.py
│   └── serving/
│       └── log_experiments_mlflow.py
├── notebooks/
│   ├── 01_setup_and_integration.ipynb
│   ├── 02_sampling_and_splits.ipynb
│   ├── 03_EDA_BigQuery.ipynb
│   ├── 04_EDA_DataProc.ipynb
│   └── 05_cleaned_sample_BigQuery_validation.ipynb
├── reports/
│   ├── 07_session_model_results.md  # GRU4Rec V9 500k results + SASRec failure analysis
│   ├── 08_vertex_ai_1m_training.md  # 1M Vertex AI training log (epoch-by-epoch)
│   ├── drift_report.json            # Latest drift monitor output
│   └── figures/
│       ├── 500k_training_curves.png
│       └── item_popularity_drift.png
├── artifacts/
│   └── diagnostics/
│       └── cold_warm_summary.json
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
