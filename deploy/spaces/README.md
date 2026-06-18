---
title: RecoSys API
emoji: 🛍️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# RecoSys — GRU4Rec V9 Recommendation API

Session-based next-item recommendations trained on the REES46 eCommerce dataset (1M users).

**Model:** GRU4Rec V9 — 1-layer GRU, embed_dim=128, hidden=256  
**Performance:** NDCG@20 = 0.2676, HR@20 = 0.4815 on held-out Feb 2020 sessions

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Model status + metadata |
| POST | `/recommend` | Session → top-K item IDs |
| GET | `/drift` | Latest popularity drift report |
| GET | `/recommend/example` | Sample request payload |

## Usage

```bash
curl -X POST https://<your-space-url>/recommend \
  -H "Content-Type: application/json" \
  -d '{
    "session": [
      {"item_id": "4209538", "event_type": "view"},
      {"item_id": "3622698", "event_type": "cart"}
    ],
    "top_k": 10
  }'
```

## Setup

Set the following Space secret / env var before building:

```
HF_DATASET_REPO=<your-hf-username>/recosys-models
```
