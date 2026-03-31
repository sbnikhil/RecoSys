# Sampling, Splits, and Interaction Tables

This document records the design decisions, measured outputs, and GCS/BigQuery artefacts produced by three pipeline scripts built on top of the cleaned `events_clean` table (279.9 M rows, output of `scripts/preprocessing_pipeline.py`). All scripts authenticate via a service account JSON and submit DDL/DML jobs directly to BigQuery.

---

## 1. User-based sampling — `scripts/create_samples.py`

### Motivation

Working against the full 279.9 M-row table for every experiment is expensive and slow. Two user-based sub-samples allow fast iteration and unit-testing of model code before scaling to the full dataset.

### Sampling logic

Users are drawn by randomly ordering the 7.5 M distinct `user_id` values and taking the top N. **All events** for each selected user are then pulled — no row-level random sampling is performed. This preserves each user's full interaction history and avoids partial sequences.

```sql
WITH sampled_users AS (
  SELECT user_id
  FROM   (SELECT DISTINCT user_id FROM `events_clean`)
  ORDER  BY RAND()
  LIMIT  {N}
)
SELECT e.*
FROM `events_clean` e
INNER JOIN sampled_users USING (user_id)
```

### Output tables

| BigQuery table | Target users | Actual users | Actual events | Unique items |
|---|---|---|---|---|
| `recosys.events_sample_50k` | 50,000 | 50,000 | 1,860,124 | 121,951 |
| `recosys.events_sample_500k` | 500,000 | 500,000 | 18,506,282 | 231,031 |

### Sanity checks

- **Events per user:** 37.2 (50k) and 37.0 (500k) — match the full-table average of ~37, confirming unbiased sampling.
- **Item coverage:** 42.9 % (50k) and 81.2 % (500k) of the 284,523 catalogue items. Sublinear growth reflects the long-tail item popularity distribution.
- **Date range:** both samples span the full training window `2019-10-01 → 2020-02-29`.
- **`event_time` dtype:** `TIMESTAMP` in both Parquet exports (explicit `CAST` applied).

### GCS exports

```
gs://recosys-data-bucket/
└── samples/
    ├── events_sample_50k/
    │   └── *.parquet
    └── events_sample_500k/
        └── *.parquet
```

Exports use BigQuery `EXPORT DATA` with `format='PARQUET'` and `overwrite=true`. Files are sharded automatically by BigQuery — downstream readers should point at the folder, not individual shards.

---

## 2. Temporal train/test splits — `scripts/create_splits.py`

### Split boundary

Decided from EDA: the natural calendar boundary between January and February 2020 is used as the holdout cut. February is the final month in the training window and is reserved as the test period.

| Split | Months included |
|---|---|
| **Train** | 2019-10, 2019-11, 2019-12, 2020-01 (4 months) |
| **Test** | 2020-02 (1 month) |

### Split SQL

```sql
-- Train
WHERE FORMAT_TIMESTAMP('%Y-%m', event_time) IN ('2019-10', '2019-11', '2019-12', '2020-01')

-- Test
WHERE FORMAT_TIMESTAMP('%Y-%m', event_time) = '2020-02'
```

### Output tables and measured sizes

| BigQuery table | Rows | Users |
|---|---|---|
| `recosys.train_50k` | 1,512,837 | 44,559 |
| `recosys.test_50k` | 347,287 | 20,626 |
| `recosys.train_500k` | 15,054,830 | 445,150 |
| `recosys.test_500k` | 3,451,452 | 206,887 |
| `recosys.train_full` | 227,460,074 | 6,736,214 |
| `recosys.test_full` | 52,477,169 | 3,132,215 |

### Train/test user overlap

| Size | Train users | Test users | Overlap users | Overlap % | Result |
|---|---|---|---|---|---|
| 50k | 44,559 | 20,626 | 15,185 | 73.6 % | PASS |
| 500k | 445,150 | 206,887 | 152,037 | 73.5 % | PASS |
| full | 6,736,214 | 3,132,215 | 2,303,272 | 73.5 % | PASS |

The ~73.5 % overlap is consistent across all three sizes, confirming it is a genuine property of the dataset rather than a sampling artefact. The prior estimate of ~56 % assumed a uniform user distribution across months; the actual figure is higher because most users have multi-month activity spans that carry over into February. A 73.5 % overlap is healthy for a recommendation system — the majority of test users have a training history the model can learn from.

### GCS exports

```
gs://recosys-data-bucket/
├── samples/
│   ├── users_sample_50k/
│   │   ├── train/  *.parquet
│   │   └── test/   *.parquet
│   └── users_sample_500k/
│       ├── train/  *.parquet
│       └── test/   *.parquet
└── splits/
    ├── train_full/  *.parquet
    └── test_full/   *.parquet
```

---

## 3. Interaction tables — `scripts/create_interactions.py`

### Purpose

Each interaction table collapses the raw event log into one row per `(user_id, product_id)` pair. This is the direct input format expected by collaborative filtering algorithms (ALS, BPR, etc.).

### Confidence-weighting scheme

Rather than treating all events equally, each event type is assigned a weight that reflects its signal strength for purchase intent:

| Event type | Weight | Rationale |
|---|---|---|
| `purchase` | 4 | Strongest signal — confirmed intent |
| `cart` | 2 | Strong signal — expressed intent |
| `view` | 1 | Weak signal — browsing, may be noise |

```
confidence_score = (n_purchases × 4) + (n_carts × 2) + (n_views × 1)
```

Weights were decided from EDA of the event type distribution (94.1 % views, 4.2 % carts, 1.6 % purchases in `events_clean`).

### Output schema

| Column | Type | Description |
|---|---|---|
| `user_id` | INTEGER | User identifier |
| `product_id` | INTEGER | Item identifier |
| `confidence_score` | INTEGER | Weighted sum of all interactions |
| `n_views` | INTEGER | Count of view events |
| `n_carts` | INTEGER | Count of cart events |
| `n_purchases` | INTEGER | Count of purchase events |
| `first_interaction` | TIMESTAMP | Earliest event for this pair |
| `last_interaction` | TIMESTAMP | Latest event for this pair |

### Output tables

| BigQuery table | Source | Unique pairs | Unique users | Unique items |
|---|---|---|---|---|
| `recosys.interactions_train_50k` | `train_50k` | ~1.4 M | ~44,559 | ~120 k |
| `recosys.interactions_train_500k` | `train_500k` | ~13.2 M | ~445,150 | ~220 k |
| `recosys.interactions_train_full` | `train_full` | ~190 M | ~6.7 M | ~280 k |

### Sparsity

All three matrices are >99.99 % sparse — expected for implicit feedback datasets where most user–item combinations are unobserved.

```
sparsity_pct = (1 − total_pairs / (unique_users × unique_items)) × 100
```

### GCS exports

```
gs://recosys-data-bucket/
├── samples/
│   ├── users_sample_50k/
│   │   └── interactions/  *.parquet
│   └── users_sample_500k/
│       └── interactions/  *.parquet
└── features/
    └── interactions_train_full/  *.parquet
```

The full-scale interactions live under `features/` — the production-scale feature store path — while sample interactions are co-located with their corresponding train/test splits under `samples/`.

---

## 4. Complete BigQuery dataset state

After running all three scripts, `recosys-489001.recosys` contains:

| Table | Description |
|---|---|
| `events_raw` | Raw 5-month load (Oct 2019 – Feb 2020), 288.8 M rows |
| `events_clean` | Cleaned + k-core filtered, 279.9 M rows |
| `events_sample_50k` | 50k-user sample, 1.86 M events |
| `events_sample_500k` | 500k-user sample, 18.5 M events |
| `train_50k` | Train split of 50k sample |
| `test_50k` | Test split of 50k sample |
| `train_500k` | Train split of 500k sample |
| `test_500k` | Test split of 500k sample |
| `train_full` | Train split of full dataset, 227.5 M rows |
| `test_full` | Test split of full dataset, 52.5 M rows |
| `interactions_train_50k` | Interaction matrix for 50k train |
| `interactions_train_500k` | Interaction matrix for 500k train |
| `interactions_train_full` | Interaction matrix for full train |

---

## 5. Complete GCS bucket layout

```
gs://recosys-data-bucket/
├── raw/                            # Original 7-month CSVs (52.69 GiB)
│   ├── 2019-Oct.csv … 2020-Apr.csv
├── samples/
│   ├── events_sample_50k/          # Full 50k-user sample (raw events)
│   ├── events_sample_500k/         # Full 500k-user sample (raw events)
│   ├── users_sample_50k/
│   │   ├── train/                  # train_50k
│   │   ├── test/                   # test_50k
│   │   └── interactions/           # interactions_train_50k
│   └── users_sample_500k/
│       ├── train/                  # train_500k
│       ├── test/                   # test_500k
│       └── interactions/           # interactions_train_500k
├── splits/
│   ├── train_full/                 # train_full (227.5 M rows)
│   └── test_full/                  # test_full  (52.5 M rows)
├── features/
│   └── interactions_train_full/    # interactions_train_full (~190 M pairs)
└── processed/
    └── events_clean/               # Parquet output from Spark pipeline
```

---

## 6. Model Development Briefs

All three tracks are now fully independent. Each person works from the
artifacts produced above. The shared evaluation protocol is:

- **Metrics:** Recall@10, NDCG@10, Precision@10
- **Test set:** Feb 2020 split (`test_50k` for development, `test_full` for final numbers)
- **Strategy:** leave-one-out — for each test user/session, predict
  top-10 items, measure whether ground truth appears in the list
- **Iteration order:** develop and tune on 50k → validate on 500k → final run on full data

---

### Nikhil — ALS, BPR-MF, Item-KNN

**Primary inputs:**
```
BQ  : recosys-489001.recosys.interactions_train_50k   ← start here
      recosys-489001.recosys.interactions_train_500k
      recosys-489001.recosys.interactions_train_full
GCS : gs://recosys-data-bucket/samples/users_sample_50k/interactions/*.parquet
      gs://recosys-data-bucket/samples/users_sample_500k/interactions/*.parquet
      gs://recosys-data-bucket/features/interactions_train_full/*.parquet

Test:
BQ  : recosys-489001.recosys.test_50k
      recosys-489001.recosys.test_full
```

**What the interaction table gives you directly:**
`interactions_train_50k` is already in the exact format the `implicit`
library expects — one row per (user_id, product_id) pair with a
`confidence_score`. No further feature engineering needed before training.

**Step 1 — Build ID mappings (do this once, reuse across all three models)**

Raw `user_id` and `product_id` are large sparse integers. All models
need contiguous 0-indexed integer IDs to build the sparse matrix.

```python
import pandas as pd, pickle

df = pd.read_parquet(
    "gs://recosys-data-bucket/samples/users_sample_50k/interactions/"
)

user2idx = {uid: i for i, uid in enumerate(df.user_id.unique())}
item2idx = {iid: i for i, iid in enumerate(df.product_id.unique())}
idx2item = {i: iid for iid, i in item2idx.items()}

pickle.dump(user2idx, open("artifacts/user2idx_50k.pkl", "wb"))
pickle.dump(item2idx, open("artifacts/item2idx_50k.pkl", "wb"))
pickle.dump(idx2item, open("artifacts/idx2item_50k.pkl", "wb"))
```

**Step 2 — Build sparse user-item matrix**

```python
from scipy.sparse import csr_matrix

rows = df.user_id.map(user2idx).values
cols = df.product_id.map(item2idx).values
data = df.confidence_score.values

user_item_matrix = csr_matrix(
    (data, (rows, cols)),
    shape=(len(user2idx), len(item2idx))
)
# Shape: (44,559 users × 105,266 items) for 50k sample
# Sparsity: ~99.98% — expected for implicit feedback
```

**Step 3 — Item-KNN (run first as sanity check)**

No training loop. Pure cosine similarity on item interaction vectors.
If Item-KNN gives reasonable recommendations, your matrix is correct.

```python
from implicit.nearest_neighbours import CosineRecommender
model = CosineRecommender(K=20)
model.fit(user_item_matrix.T)  # implicit library expects (items × users)
```

**Step 4 — ALS**

```python
from implicit.als import AlternatingLeastSquares
model = AlternatingLeastSquares(
    factors=64,
    regularization=0.01,
    iterations=15,
    use_gpu=False   # set True on Colab GPU
)
model.fit(user_item_matrix.T)
```

Key hyperparameters to tune on 50k sample:
- `factors`: 32 / 64 / 128
- `regularization`: 0.001 / 0.01 / 0.1
- `iterations`: 10 / 15 / 20

**Step 5 — BPR-MF**

```python
from implicit.bpr import BayesianPersonalizedRanking
model = BayesianPersonalizedRanking(
    factors=64,
    learning_rate=0.01,
    regularization=0.01,
    iterations=100
)
model.fit(user_item_matrix.T)
```

Nearly identical API to ALS — your evaluation code works unchanged.

**Evaluation (same code for all three models):**

```python
# For each test user with Feb 2020 purchases:
# 1. Get their row from user_item_matrix (their training history)
# 2. model.recommend(user_idx, user_item_matrix, N=10,
#                    filter_already_liked=True)
# 3. Check if any Feb 2020 purchase appears in the top-10 list
# 4. Aggregate Recall@10, NDCG@10, Precision@10 across all test users
```

**Iteration path:**
```
50k (minutes/run)  → tune hyperparams, lock best config
500k (10–30 min)   → confirm metrics hold at scale
full (Dataproc)    → final reported numbers
```

---

### Manoj — Two-Tower Neural Retrieval + FAISS

**Primary inputs:**
```
BQ  : recosys-489001.recosys.interactions_train_50k  ← positive training pairs
      recosys-489001.recosys.train_50k               ← raw events for user features
      recosys-489001.recosys.events_clean            ← item features (full catalog)
      recosys-489001.recosys.test_50k                ← evaluation
GCS : gs://recosys-data-bucket/samples/users_sample_50k/interactions/*.parquet
      gs://recosys-data-bucket/samples/users_sample_50k/train/*.parquet
```

**Two-Tower overview:**
Two separate neural networks that embed users and items into the same
64-dimensional vector space. At inference, the user tower produces a
query vector; FAISS finds the nearest item vectors.

```
User tower : user_id embedding + user behaviour features → 64-dim vector
Item tower : item_id embedding + item catalogue features → 64-dim vector
Training   : dot(user_vec, pos_item_vec) > dot(user_vec, neg_item_vec)
Inference  : FAISS ANN search over all pre-computed item vectors
```

**Step 1 — Build item feature table (BigQuery → Parquet)**

Compute once from `events_clean` (full catalog, not just sample):

```sql
SELECT
  product_id,
  SPLIT(IFNULL(category_code, 'unknown'), '.')[SAFE_OFFSET(0)]  AS cat_l1,
  SPLIT(IFNULL(category_code, 'unknown'), '.')[SAFE_OFFSET(1)]  AS cat_l2,
  IFNULL(brand, 'unknown')                                       AS brand,
  CASE
    WHEN AVG(price) < 10   THEN 'price_0'
    WHEN AVG(price) < 50   THEN 'price_1'
    WHEN AVG(price) < 100  THEN 'price_2'
    WHEN AVG(price) < 200  THEN 'price_3'
    WHEN AVG(price) < 500  THEN 'price_4'
    WHEN AVG(price) < 1000 THEN 'price_5'
    WHEN AVG(price) < 2000 THEN 'price_6'
    ELSE                        'price_7'
  END                                                            AS price_bucket,
  AVG(price)                                                     AS avg_price
FROM `recosys-489001.recosys.events_clean`
GROUP BY product_id, category_code, brand
```

Export to: `gs://recosys-data-bucket/features/item_features/*.parquet`

**Step 2 — Build user feature table (from train split only — no leakage)**

```sql
-- Use train_50k only, never events_clean, to avoid test leakage
SELECT
  user_id,
  COUNT(*)                                                          AS total_events,
  COUNT(DISTINCT FORMAT_TIMESTAMP('%Y-%m', event_time))             AS months_active,
  ROUND(COUNTIF(event_type = 'purchase') / COUNT(*), 4)            AS purchase_rate,
  ROUND(COUNTIF(event_type = 'cart')     / COUNT(*), 4)            AS cart_rate,
  COUNT(DISTINCT user_session)                                      AS n_sessions,
  CASE
    WHEN AVG(EXTRACT(HOUR FROM event_time)) BETWEEN 6  AND 11 THEN 'morning'
    WHEN AVG(EXTRACT(HOUR FROM event_time)) BETWEEN 12 AND 17 THEN 'afternoon'
    WHEN AVG(EXTRACT(HOUR FROM event_time)) BETWEEN 18 AND 22 THEN 'evening'
    ELSE 'night'
  END                                                               AS peak_hour_bucket,
  MOD(CAST(AVG(EXTRACT(DAYOFWEEK FROM event_time)) AS INT64), 7)   AS preferred_dow
FROM `recosys-489001.recosys.train_50k`
GROUP BY user_id
```

Export to: `gs://recosys-data-bucket/features/user_features_50k/*.parquet`

**Step 3 — PyTorch Two-Tower model**

```python
import torch.nn as nn

class ItemTower(nn.Module):
    def __init__(self, n_items, n_cat_l1, n_cat_l2, n_brands,
                 n_price_buckets, embed_dim=32, out_dim=64):
        super().__init__()
        self.item_emb      = nn.Embedding(n_items + 1,        embed_dim)
        self.cat_l1_emb    = nn.Embedding(n_cat_l1 + 1,       embed_dim)
        self.cat_l2_emb    = nn.Embedding(n_cat_l2 + 1,       embed_dim)
        self.brand_emb     = nn.Embedding(n_brands + 1,       embed_dim)
        self.price_emb     = nn.Embedding(n_price_buckets + 1, embed_dim)
        in_dim = embed_dim * 5 + 1  # +1 for avg_price scalar
        self.mlp = nn.Sequential(
            nn.Linear(in_dim, 256), nn.ReLU(), nn.Linear(256, out_dim)
        )

    def forward(self, item_id, cat_l1, cat_l2, brand, price_bucket, avg_price):
        x = torch.cat([
            self.item_emb(item_id), self.cat_l1_emb(cat_l1),
            self.cat_l2_emb(cat_l2), self.brand_emb(brand),
            self.price_emb(price_bucket), avg_price.unsqueeze(1)
        ], dim=1)
        return nn.functional.normalize(self.mlp(x), dim=1)

class UserTower(nn.Module):
    def __init__(self, n_users, n_hour_buckets=4, embed_dim=32, out_dim=64):
        super().__init__()
        self.user_emb = nn.Embedding(n_users + 1, embed_dim)
        self.hour_emb = nn.Embedding(n_hour_buckets + 1, embed_dim)
        in_dim = embed_dim * 2 + 5  # +5 dense features
        self.mlp = nn.Sequential(
            nn.Linear(in_dim, 256), nn.ReLU(), nn.Linear(256, out_dim)
        )

    def forward(self, user_id, hour_bucket, dense_features):
        # dense_features: [log_total_events, months_active,
        #                  purchase_rate, cart_rate, n_sessions]
        x = torch.cat([
            self.user_emb(user_id), self.hour_emb(hour_bucket),
            dense_features
        ], dim=1)
        return nn.functional.normalize(self.mlp(x), dim=1)
```

Training with in-batch negatives:
```python
# For each batch of (user_vec, positive_item_vec):
scores = user_vecs @ item_vecs.T          # (batch_size, batch_size)
labels = torch.arange(batch_size)         # diagonal = positive pairs
loss   = nn.CrossEntropyLoss()(scores, labels)
```

**Step 4 — Export item embeddings → FAISS index**

```python
import faiss, numpy as np

# Run all items through trained item tower
item_embeddings = item_tower(all_item_features).detach().numpy()
faiss.normalize_L2(item_embeddings)

index = faiss.IndexFlatIP(64)   # exact inner product search
index.add(item_embeddings)
faiss.write_index(index, "artifacts/item_index_50k.faiss")
```

**Step 5 — Retrieval evaluation**

```python
# For each test user:
# 1. Run user features through user tower → 64-dim query vector
# 2. FAISS search → distances, top-10 item indices
# 3. Map indices back to product_ids via idx2item
# 4. Mask out items already seen in training
# 5. Check if Feb 2020 purchases appear in the filtered top-10
# 6. Compute Recall@10, NDCG@10, Precision@10
```

**Iteration path:**
```
50k, CPU (Colab free)    → architecture decisions, loss tuning
500k, GPU (Colab Pro)    → embedding quality check, scale validation
full data, GPU           → final FAISS index, reported metrics
```

---

### Kathy — GRU4Rec, SASRec

**Primary inputs:**
```
BQ  : recosys-489001.recosys.train_50k   ← raw events in temporal order
      recosys-489001.recosys.test_50k    ← for session-level evaluation
GCS : gs://recosys-data-bucket/samples/users_sample_50k/train/*.parquet
      gs://recosys-data-bucket/samples/users_sample_50k/test/*.parquet
```

**Important:** Do NOT use the interaction tables. GRU4Rec and SASRec
need raw events sorted by `event_time` within each session. The
temporal order of items in the sequence is the signal — collapsed
(user, item) pairs destroy this.

**Step 1 — Build session sequence dataset**

```python
import pandas as pd

df = pd.read_parquet(
    "gs://recosys-data-bucket/samples/users_sample_50k/train/"
)
df = df.sort_values(["user_session", "event_time"])

# Group by session → ordered list of product_ids
sequences = (
    df.groupby("user_session")["product_id"]
    .apply(list)
    .reset_index(name="item_sequence")
)
sequences["length"] = sequences.item_sequence.map(len)

# Session config from EDA:
# min_session_length = 2  (drop single-event sessions — 41.88% of sessions)
# max_session_length = 50 (truncate — keep last 50 events)
sequences = sequences[sequences.length >= 2].copy()
sequences["item_sequence"] = sequences.item_sequence.map(lambda x: x[-50:])
```

**Step 2 — Build item ID vocabulary**

```python
# 0 is reserved for the PAD token
all_items = df.product_id.unique()
item2idx  = {iid: i + 1 for i, iid in enumerate(all_items)}
idx2item  = {i: iid for iid, i in item2idx.items()}
n_items   = len(item2idx)

sequences["encoded"] = sequences.item_sequence.map(
    lambda seq: [item2idx.get(i, 0) for i in seq]
)
```

**Step 3 — Build sliding window training pairs**

```python
# For next-item prediction:
# input  = sequence prefix [item_1, ..., item_{t-1}]
# target = item_t  (the next item)
training_pairs = []
for _, row in sequences.iterrows():
    seq = row["encoded"]
    for t in range(1, len(seq)):
        training_pairs.append({
            "input_seq": seq[:t],
            "target":    seq[t]
        })

# Pad input sequences to fixed length for batching
from torch.nn.utils.rnn import pad_sequence
# pad_value=0 (reserved PAD token), left-pad for GRU4Rec
```

**Step 4 — GRU4Rec (implement first)**

```python
import torch, torch.nn as nn

class GRU4Rec(nn.Module):
    def __init__(self, n_items, embed_dim=64, hidden_size=128,
                 n_layers=1, dropout=0.25):
        super().__init__()
        self.embedding = nn.Embedding(n_items + 1, embed_dim, padding_idx=0)
        self.gru = nn.GRU(embed_dim, hidden_size, n_layers,
                          batch_first=True, dropout=dropout if n_layers > 1 else 0)
        self.dropout = nn.Dropout(dropout)
        self.output  = nn.Linear(hidden_size, n_items + 1)

    def forward(self, x):
        # x: (batch, seq_len) — padded, 0 = PAD
        emb    = self.dropout(self.embedding(x))  # (batch, seq_len, embed_dim)
        out, _ = self.gru(emb)                    # (batch, seq_len, hidden_size)
        logits = self.output(out[:, -1, :])        # (batch, n_items+1) — last step
        return logits
```

Loss: cross-entropy with sampled negatives — for each positive target,
sample 100 random items as negatives. Avoids full softmax over all items.

Key hyperparameters to tune:
- `embed_dim`: 32 / 64 / 128
- `hidden_size`: 64 / 128 / 256
- `dropout`: 0.1 / 0.25 / 0.5
- `learning_rate`: 1e-3 / 5e-4

**Step 5 — SASRec (after GRU4Rec is working)**

Self-attention over item sequences. Same input format as GRU4Rec.
Use an existing PyTorch implementation (the original Kang & McAuley
2018 repo or the `recbole` library) rather than building from scratch.

Key hyperparameters: `num_heads=2`, `num_blocks=2`, `hidden_size=64`,
`dropout=0.2`, `max_len=50`.

**Evaluation — next-item prediction (session-level):**

```python
# For each test session in test_50k:
# 1. Sort by event_time, take sequence prefix (all items except last)
# 2. Run prefix through trained model → logits over all items
# 3. Mask items already seen in the session (set logits to -inf)
# 4. Take top-10 items by logit score
# 5. Check if the actual last item appears in the top-10
# 6. Aggregate Recall@10, NDCG@10 across all test sessions

test_df = pd.read_parquet(
    "gs://recosys-data-bucket/samples/users_sample_50k/test/"
)
test_df = test_df.sort_values(["user_session", "event_time"])
test_seqs = (
    test_df.groupby("user_session")["product_id"]
    .apply(list).reset_index(name="item_sequence")
)
# Only evaluate sessions with >= 2 items
test_seqs = test_seqs[test_seqs.item_sequence.map(len) >= 2]
```

**Iteration path:**
```
50k, CPU (Colab free)    → GRU4Rec end-to-end, debug evaluation code
50k, GPU (Colab Pro)     → SASRec, hyperparameter tuning
500k, GPU                → scale validation
full data, GPU           → final reported metrics
```

---

## 7. Shared Evaluation Protocol

All five models are evaluated on the same test split using the same
metrics. Use `test_50k` for development, `test_full` for final numbers.

| Metric | What it measures |
|---|---|
| Recall@10 | Did any ground truth item appear in the top-10 predictions? |
| NDCG@10 | Did ground truth items appear near the top of the list? |
| Precision@10 | What fraction of the top-10 predictions were relevant? |

**Ground truth:**
- Nikhil (ALS, BPR, Item-KNN): purchases per user in the test split
- Manoj (Two-Tower): purchases per user in the test split
- Kathy (GRU4Rec, SASRec): last item in each test session

**Critical rule — filter training items before scoring:**
Never recommend items a user already interacted with during training.
- `implicit` library: use `filter_already_liked=True` — handled automatically
- Two-Tower + FAISS: mask training items from FAISS results before top-10
- GRU4Rec / SASRec: set logits of seen items to `-inf` before top-10

**Minimum test population for reliable metrics:** 1,000 users/sessions.
The 50k test split has 20,626 users — well above this threshold.