"""
create_splits.py

Creates temporal train/test split tables for the 50k-user sample,
500k-user sample, and full events_clean dataset, then exports all six
tables to GCS as Parquet.

Split boundary (decided from EDA):
    Train : 2019-10, 2019-11, 2019-12, 2020-01
    Test  : 2020-02

Usage:
    python scripts/create_splits.py

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

TRAIN_MONTHS = ("2019-10", "2019-11", "2019-12", "2020-01")
TEST_MONTH   = "2020-02"

# Each entry drives table creation, export, and validation for one data size.
SPLITS = [
    {
        "size":        "50k",
        "source":      f"{PROJECT_ID}.{DATASET_ID}.events_sample_50k",
        "train_table": "train_50k",
        "test_table":  "test_50k",
        "train_gcs":   f"gs://{BUCKET}/samples/users_sample_50k/train/*.parquet",
        "test_gcs":    f"gs://{BUCKET}/samples/users_sample_50k/test/*.parquet",
    },
    {
        "size":        "500k",
        "source":      f"{PROJECT_ID}.{DATASET_ID}.events_sample_500k",
        "train_table": "train_500k",
        "test_table":  "test_500k",
        "train_gcs":   f"gs://{BUCKET}/samples/users_sample_500k/train/*.parquet",
        "test_gcs":    f"gs://{BUCKET}/samples/users_sample_500k/test/*.parquet",
    },
    {
        "size":        "full",
        "source":      f"{PROJECT_ID}.{DATASET_ID}.events_clean",
        "train_table": "train_full",
        "test_table":  "test_full",
        "train_gcs":   f"gs://{BUCKET}/splits/train_full/*.parquet",
        "test_gcs":    f"gs://{BUCKET}/splits/test_full/*.parquet",
    },
]

# Column list used in every EXPORT DATA SELECT.
# CAST(event_time AS TIMESTAMP) prevents BigQuery from writing it as STRING.
EXPORT_COLUMNS = [
    "CAST(event_time AS TIMESTAMP) AS event_time",
    "event_type",
    "product_id",
    "category_id",
    "category_code",
    "brand",
    "price",
    "user_id",
    "user_session",
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


# ── Create split tables ───────────────────────────────────────────────────────
def create_split_tables(bq: bigquery.Client, cfg: dict) -> None:
    source      = cfg["source"]
    train_ref   = f"{PROJECT_ID}.{DATASET_ID}.{cfg['train_table']}"
    test_ref    = f"{PROJECT_ID}.{DATASET_ID}.{cfg['test_table']}"
    month_list  = ", ".join(f"'{m}'" for m in TRAIN_MONTHS)

    train_sql = f"""
    CREATE OR REPLACE TABLE `{train_ref}` AS
    SELECT *
    FROM   `{source}`
    WHERE  FORMAT_TIMESTAMP('%Y-%m', event_time) IN ({month_list})
    """

    test_sql = f"""
    CREATE OR REPLACE TABLE `{test_ref}` AS
    SELECT *
    FROM   `{source}`
    WHERE  FORMAT_TIMESTAMP('%Y-%m', event_time) = '{TEST_MONTH}'
    """

    t0 = _step(f"Creating train table  `{train_ref}`")
    _run_job(bq, train_sql)
    _done(t0)

    t0 = _step(f"Creating test table   `{test_ref}`")
    _run_job(bq, test_sql)
    _done(t0)


# ── Export to GCS ─────────────────────────────────────────────────────────────
def export_to_gcs(bq: bigquery.Client, table_name: str, gcs_uri: str) -> None:
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{table_name}"
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

    t0 = _step(f"Exporting `{table_name}` → {gcs_uri}")
    _run_job(bq, sql)
    _done(t0)


# ── Validate one size ─────────────────────────────────────────────────────────
def validate_size(bq: bigquery.Client, cfg: dict) -> dict:
    train_ref = f"{PROJECT_ID}.{DATASET_ID}.{cfg['train_table']}"
    test_ref  = f"{PROJECT_ID}.{DATASET_ID}.{cfg['test_table']}"

    # Single query: join both tables' user sets to compute overlap atomically.
    sql = f"""
    WITH
      train_users AS (SELECT DISTINCT user_id FROM `{train_ref}`),
      test_users  AS (SELECT DISTINCT user_id FROM `{test_ref}`),
      overlap     AS (
        SELECT user_id FROM test_users
        INNER JOIN train_users USING (user_id)
      )
    SELECT
      (SELECT COUNT(*) FROM `{train_ref}`)  AS train_rows,
      (SELECT COUNT(*) FROM train_users)    AS train_users,
      (SELECT COUNT(*) FROM `{test_ref}`)   AS test_rows,
      (SELECT COUNT(*) FROM test_users)     AS test_users,
      (SELECT COUNT(*) FROM overlap)        AS overlap_users
    """

    t0  = _step(f"Validating splits for size={cfg['size']}")
    row = bq.query(sql).result().to_dataframe().iloc[0]
    _done(t0)

    overlap_pct = row.overlap_users / row.test_users * 100 if row.test_users else 0.0
    return {
        "size":          cfg["size"],
        "train_rows":    int(row.train_rows),
        "train_users":   int(row.train_users),
        "test_rows":     int(row.test_rows),
        "test_users":    int(row.test_users),
        "overlap_users": int(row.overlap_users),
        "overlap_pct":   overlap_pct,
    }


# ── Summary table ─────────────────────────────────────────────────────────────
def print_summary(results: list[dict]) -> None:
    _section("Validation Summary")

    # Column widths
    C = [6, 14, 13, 12, 11, 15, 13, 10]
    headers = [
        "size", "train_rows", "train_users",
        "test_rows", "test_users", "overlap_users", "overlap_pct", "result",
    ]

    sep = "  +" + "+".join("-" * (w + 2) for w in C) + "+"

    def _row(*cells):
        parts = []
        for cell, w in zip(cells, C):
            parts.append(f" {str(cell):<{w}} ")
        print("  |" + "|".join(parts) + "|")

    print(sep)
    _row(*headers)
    print(sep)

    for r in results:
        pct_str = f"{r['overlap_pct']:.1f}%"
        status  = "PASS ✅" if 65.0 <= r["overlap_pct"] <= 85.0 else "FAIL ❌"
        _row(
            r["size"],
            f"{r['train_rows']:,}",
            f"{r['train_users']:,}",
            f"{r['test_rows']:,}",
            f"{r['test_users']:,}",
            f"{r['overlap_users']:,}",
            pct_str,
            status,
        )

    print(sep)
    print()
    print("  overlap_pct = users in BOTH train and test / total test users")
    print("  PASS range  = 65 % – 85 %  (measured ~73.5 % across all sizes)")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    t_total = time.time()

    _section("RecoSys — Create Temporal Splits")
    print(f"  Train months : {', '.join(TRAIN_MONTHS)}")
    print(f"  Test month   : {TEST_MONTH}")
    print(f"  Sizes        : {[s['size'] for s in SPLITS]}")
    print(f"  Tables       : 6 total (train + test × 3 sizes)")

    _section("Authentication")
    bq = _build_client()

    # ── Phase 1: create all 6 tables ─────────────────────────────────────────
    for cfg in SPLITS:
        _section(f"Split — {cfg['size']}  ({cfg['source'].split('.')[-1]})")
        create_split_tables(bq, cfg)

    # ── Phase 2: export all 6 tables ─────────────────────────────────────────
    _section("Exporting to GCS")
    for cfg in SPLITS:
        export_to_gcs(bq, cfg["train_table"], cfg["train_gcs"])
        export_to_gcs(bq, cfg["test_table"],  cfg["test_gcs"])

    # ── Phase 3: validate ─────────────────────────────────────────────────────
    _section("Running Validation Queries")
    results = [validate_size(bq, cfg) for cfg in SPLITS]

    print_summary(results)

    _section("Done")
    print(f"  Total time : {_elapsed(t_total)}")
    print()


if __name__ == "__main__":
    main()
