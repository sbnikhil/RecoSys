"""
create_interactions.py

Builds confidence-weighted interaction tables from the train splits.
Each (user_id, product_id) pair is collapsed into one row using:
    confidence_score = (n_purchases * 4) + (n_carts * 2) + (n_views * 1)

Source tables  : train_50k, train_500k, train_full
Output tables  : interactions_train_50k, interactions_train_500k,
                 interactions_train_full
GCS exports    : Parquet under samples/ and features/

Usage:
    python scripts/create_interactions.py

Auth:
    Reads GOOGLE_APPLICATION_CREDENTIALS env var, falling back to
    C:\\Users\\Patron\\Documents\\GitHub\\RecoSys\\secrets\\recosys-service-account.json
"""

import os
import time
from google.oauth2 import service_account
from google.cloud import bigquery

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_ID = "recosys-489001"
DATASET_ID = "recosys"
BUCKET     = "recosys-data-bucket"

# Confidence weights (decided from EDA)
W_PURCHASE = 4
W_CART     = 2
W_VIEW     = 1

INTERACTIONS = [
    {
        "size":        "50k",
        "source":      f"{PROJECT_ID}.{DATASET_ID}.train_50k",
        "out_table":   "interactions_train_50k",
        "gcs_uri":     f"gs://{BUCKET}/samples/users_sample_50k/interactions/*.parquet",
    },
    {
        "size":        "500k",
        "source":      f"{PROJECT_ID}.{DATASET_ID}.train_500k",
        "out_table":   "interactions_train_500k",
        "gcs_uri":     f"gs://{BUCKET}/samples/users_sample_500k/interactions/*.parquet",
    },
    {
        "size":        "full",
        "source":      f"{PROJECT_ID}.{DATASET_ID}.train_full",
        "out_table":   "interactions_train_full",
        "gcs_uri":     f"gs://{BUCKET}/features/interactions_train_full/*.parquet",
    },
]

# CAST on both timestamp columns prevents BigQuery from writing them as STRING.
EXPORT_COLUMNS = [
    "user_id",
    "product_id",
    "confidence_score",
    "n_views",
    "n_carts",
    "n_purchases",
    "CAST(first_interaction AS TIMESTAMP) AS first_interaction",
    "CAST(last_interaction  AS TIMESTAMP) AS last_interaction",
]


# ── Helpers ───────────────────────────────────────────────────────────────────
def _elapsed(start: float) -> str:
    s = int(time.time() - start)
    return f"{s // 60}m {s % 60}s"


def _section(title: str) -> None:
    print()
    print("=" * 64)
    print(f"  {title}")
    print("=" * 64)


def _step(msg: str) -> float:
    print(f"\n  ▶  {msg}")
    return time.time()


def _done(t0: float, label: str = "") -> None:
    suffix = f" — {label}" if label else ""
    print(f"     done in {_elapsed(t0)}{suffix}")


def _run_job(bq: bigquery.Client, sql: str) -> bigquery.QueryJob:
    """Submit a BigQuery job, wait for completion, raise on error."""
    job = bq.query(sql)
    job.result()
    if job.errors:
        raise RuntimeError(f"BigQuery job failed: {job.errors}")
    return job


# ── Auth ──────────────────────────────────────────────────────────────────────
def _build_client() -> bigquery.Client:
    key_path = os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS",
        os.path.expanduser(
            r"C:\Users\Patron\Documents\GitHub\RecoSys\secrets\recosys-service-account.json"
        ),
    )
    credentials = service_account.Credentials.from_service_account_file(
        key_path,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    client = bigquery.Client(project=PROJECT_ID, credentials=credentials)
    print(f"  Authenticated as : {credentials.service_account_email}")
    print(f"  Project          : {client.project}")
    return client


# ── Create interaction table ──────────────────────────────────────────────────
def create_interaction_table(bq: bigquery.Client, cfg: dict) -> None:
    source    = cfg["source"]
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{cfg['out_table']}"

    sql = f"""
    CREATE OR REPLACE TABLE `{table_ref}` AS
    SELECT
      user_id,
      product_id,
      COUNTIF(event_type = 'purchase') * {W_PURCHASE}
        + COUNTIF(event_type = 'cart') * {W_CART}
        + COUNTIF(event_type = 'view') * {W_VIEW}   AS confidence_score,
      COUNTIF(event_type = 'view')                   AS n_views,
      COUNTIF(event_type = 'cart')                   AS n_carts,
      COUNTIF(event_type = 'purchase')               AS n_purchases,
      MIN(event_time)                                AS first_interaction,
      MAX(event_time)                                AS last_interaction
    FROM `{source}`
    GROUP BY user_id, product_id
    """

    t0 = _step(f"Creating `{table_ref}`  (source: {source.split('.')[-1]})")
    _run_job(bq, sql)
    _done(t0)


# ── Export to GCS ─────────────────────────────────────────────────────────────
def export_to_gcs(bq: bigquery.Client, cfg: dict) -> None:
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{cfg['out_table']}"
    gcs_uri   = cfg["gcs_uri"]
    cols      = ",\n      ".join(EXPORT_COLUMNS)

    sql = f"""
    EXPORT DATA
      OPTIONS (
        uri       = '{gcs_uri}',
        format    = 'PARQUET',
        overwrite = true
      )
    AS
    SELECT
      {cols}
    FROM `{table_ref}`
    """

    t0 = _step(f"Exporting `{cfg['out_table']}` → {gcs_uri}")
    _run_job(bq, sql)
    _done(t0)


# ── Validate one interaction table ────────────────────────────────────────────
def validate_interactions(bq: bigquery.Client, cfg: dict) -> dict:
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{cfg['out_table']}"

    sql = f"""
    SELECT
      COUNT(*)                                           AS total_pairs,
      COUNT(DISTINCT user_id)                            AS unique_users,
      COUNT(DISTINCT product_id)                         AS unique_items,
      AVG(confidence_score)                              AS avg_score,
      MAX(confidence_score)                              AS max_score,
      COUNTIF(confidence_score = 1)                      AS score_eq_1,
      COUNTIF(confidence_score >= 4)                     AS score_ge_4,
      COUNTIF(confidence_score = 2)                      AS score_eq_2
    FROM `{table_ref}`
    """

    t0  = _step(f"Validating `{table_ref}`")
    row = bq.query(sql).result().to_dataframe().iloc[0]
    _done(t0)

    total_pairs  = int(row.total_pairs)
    unique_users = int(row.unique_users)
    unique_items = int(row.unique_items)
    sparsity_pct = (1.0 - total_pairs / (unique_users * unique_items)) * 100

    return {
        "size":         cfg["size"],
        "total_pairs":  total_pairs,
        "unique_users": unique_users,
        "unique_items": unique_items,
        "avg_score":    float(row.avg_score),
        "max_score":    int(row.max_score),
        "sparsity_pct": sparsity_pct,
        "pct_score_1":  row.score_eq_1 / total_pairs * 100,
        "pct_score_ge4": row.score_ge_4 / total_pairs * 100,
        "pct_score_2":  row.score_eq_2 / total_pairs * 100,
    }


# ── Summary ───────────────────────────────────────────────────────────────────
def print_summary(results: list[dict]) -> None:
    _section("Validation Summary")

    # ── Main summary table ────────────────────────────────────────────────────
    C = [6, 14, 13, 13, 11, 11, 13, 10]
    headers = [
        "size", "total_pairs", "unique_users", "unique_items",
        "avg_score", "max_score", "sparsity_pct", "result",
    ]
    sep = "  +" + "+".join("-" * (w + 2) for w in C) + "+"

    def _row(*cells):
        parts = [f" {str(c):<{w}} " for c, w in zip(cells, C)]
        print("  |" + "|".join(parts) + "|")

    print(sep)
    _row(*headers)
    print(sep)

    all_pass = True
    for r in results:
        status = "PASS ✅" if r["sparsity_pct"] > 99.0 else "FAIL ❌"
        if r["sparsity_pct"] <= 99.0:
            all_pass = False
        _row(
            r["size"],
            f"{r['total_pairs']:,}",
            f"{r['unique_users']:,}",
            f"{r['unique_items']:,}",
            f"{r['avg_score']:.2f}",
            f"{r['max_score']:,}",
            f"{r['sparsity_pct']:.4f}%",
            status,
        )

    print(sep)
    print()
    print("  sparsity_pct = (1 - total_pairs / (unique_users × unique_items)) × 100")
    print("  PASS if sparsity_pct > 99.0 %  (expected for implicit feedback data)")

    # ── Score distribution ────────────────────────────────────────────────────
    print()
    print("  Score distribution:")
    print()

    D = [6, 22, 22, 22]
    dist_sep = "  +" + "+".join("-" * (w + 2) for w in D) + "+"
    dist_headers = ["size", "score=1 (view only)", "score=2 (cart only)", "score≥4 (purchase)"]

    def _drow(*cells):
        parts = [f" {str(c):<{w}} " for c, w in zip(cells, D)]
        print("  |" + "|".join(parts) + "|")

    print(dist_sep)
    _drow(*dist_headers)
    print(dist_sep)

    for r in results:
        _drow(
            r["size"],
            f"{r['pct_score_1']:.1f}%",
            f"{r['pct_score_2']:.1f}%",
            f"{r['pct_score_ge4']:.1f}%",
        )

    print(dist_sep)

    # ── Final verdict ─────────────────────────────────────────────────────────
    print()
    verdict = "✅  All sparsity checks PASSED" if all_pass else "❌  One or more sparsity checks FAILED"
    print(f"  {verdict}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    t_total = time.time()

    _section("RecoSys — Create Interaction Tables")
    print(f"  Confidence weights : purchase={W_PURCHASE}, cart={W_CART}, view={W_VIEW}")
    print(f"  Sources            : {[c['source'].split('.')[-1] for c in INTERACTIONS]}")
    print(f"  Output tables      : {[c['out_table'] for c in INTERACTIONS]}")

    _section("Authentication")
    bq = _build_client()

    # ── Phase 1: create all 3 interaction tables ──────────────────────────────
    _section("Building Interaction Tables")
    for cfg in INTERACTIONS:
        create_interaction_table(bq, cfg)

    # ── Phase 2: export all 3 tables to GCS ──────────────────────────────────
    _section("Exporting to GCS")
    for cfg in INTERACTIONS:
        export_to_gcs(bq, cfg)

    # ── Phase 3: validate ─────────────────────────────────────────────────────
    _section("Running Validation Queries")
    results = [validate_interactions(bq, cfg) for cfg in INTERACTIONS]

    print_summary(results)

    _section("Done")
    print(f"  Total time : {_elapsed(t_total)}")
    print()


if __name__ == "__main__":
    main()
