"""
create_samples.py

Creates user-based samples from events_clean and exports them to GCS as Parquet.

Usage:
    python scripts/create_samples.py

Auth:
    Reads GOOGLE_APPLICATION_CREDENTIALS env var, falling back to
    ~/secrets/recosys-service-account.json.
"""

import os
import time
from google.oauth2 import service_account
from google.cloud import bigquery

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_ID   = "recosys-489001"
DATASET_ID   = "recosys"
BUCKET       = "recosys-data-bucket"
SOURCE_TABLE = f"{PROJECT_ID}.{DATASET_ID}.events_clean"

SAMPLES = [
    {"table": "events_sample_50k",  "target_users": 50_000},   # ✅
    {"table": "events_sample_500k", "target_users": 500_000},  # ✅
]
ALL_COLUMNS = [
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
        os.path.expanduser(r"C:\Users\Patron\Documents\GitHub\RecoSys\secrets\recosys-service-account.json"),
    )
    credentials = service_account.Credentials.from_service_account_file(
        key_path,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    client = bigquery.Client(project=PROJECT_ID, credentials=credentials)
    print(f"  Authenticated as : {credentials.service_account_email}")
    print(f"  Project          : {client.project}")
    return client


# ── Create sample table ───────────────────────────────────────────────────────
def create_sample_table(bq: bigquery.Client, table_name: str, target_users: int) -> None:
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{table_name}"

    # Randomly pick target_users distinct user_ids, then pull ALL their events.
    # ORDER BY RAND() on the 7.5 M distinct user pool is fast and unbiased.
    sql = f"""
    CREATE OR REPLACE TABLE `{table_ref}` AS
    WITH sampled_users AS (
      SELECT user_id
      FROM   (SELECT DISTINCT user_id FROM `{SOURCE_TABLE}`)
      ORDER  BY RAND()
      LIMIT  {target_users}
    )
    SELECT e.*
    FROM `{SOURCE_TABLE}` e
    INNER JOIN sampled_users USING (user_id)
    """

    t0 = _step(f"Creating `{table_ref}` ({target_users:,} users sampled)")
    _run_job(bq, sql)
    _done(t0)


# ── Export to GCS ─────────────────────────────────────────────────────────────
def export_to_gcs(bq: bigquery.Client, table_name: str) -> None:
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{table_name}"
    gcs_uri   = f"gs://{BUCKET}/samples/{table_name}/*.parquet"
    cols      = ",\n      ".join(ALL_COLUMNS)

    # CAST(event_time AS TIMESTAMP) is mandatory: without it BigQuery writes
    # the column as STRING in Parquet, breaking downstream Polars/Spark reads.
    sql = f"""
    EXPORT DATA
      OPTIONS (
        uri      = '{gcs_uri}',
        format   = 'PARQUET',
        overwrite = true
      )
    AS
    SELECT
      {cols}
    FROM `{table_ref}`
    """

    t0 = _step(f"Exporting to {gcs_uri}")
    _run_job(bq, sql)
    _done(t0, label=gcs_uri)


# ── Validate ──────────────────────────────────────────────────────────────────
def validate_table(bq: bigquery.Client, table_name: str) -> None:
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{table_name}"

    # Schema check — verify event_time stored as TIMESTAMP, not STRING
    bq_table = bq.get_table(table_ref)
    et_field  = next(f for f in bq_table.schema if f.name == "event_time")
    dtype_ok  = et_field.field_type == "TIMESTAMP"

    sql = f"""
    SELECT
      COUNT(DISTINCT user_id)    AS unique_users,
      COUNT(*)                   AS total_events,
      COUNT(DISTINCT product_id) AS unique_items,
      MIN(event_time)            AS min_event_time,
      MAX(event_time)            AS max_event_time
    FROM `{table_ref}`
    """

    t0  = _step(f"Validating `{table_ref}`")
    job = bq.query(sql)
    row = job.result().to_dataframe().iloc[0]
    _done(t0)

    W = 52
    print()
    print("  " + "─" * W)
    print(f"  Validation — {table_name}")
    print("  " + "─" * W)
    print(f"  {'Users':<24} {int(row.unique_users):>14,}")
    print(f"  {'Events':<24} {int(row.total_events):>14,}")
    print(f"  {'Unique items':<24} {int(row.unique_items):>14,}")
    print(f"  {'Min event_time':<24} {str(row.min_event_time)[:19]:>14}")
    print(f"  {'Max event_time':<24} {str(row.max_event_time)[:19]:>14}")
    dtype_label = "TIMESTAMP ✅" if dtype_ok else f"{et_field.field_type} ❌  (expected TIMESTAMP)"
    print(f"  {'event_time dtype':<24} {dtype_label:>14}")
    print("  " + "─" * W)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    t_total = time.time()

    _section("RecoSys — Create Samples")
    print(f"  Source  : {SOURCE_TABLE}")
    print(f"  Bucket  : gs://{BUCKET}/samples/")
    print(f"  Samples : {[s['table'] for s in SAMPLES]}")

    _section("Authentication")
    bq = _build_client()

    for cfg in SAMPLES:
        table_name   = cfg["table"]
        target_users = cfg["target_users"]

        _section(f"Sample: {table_name}")

        create_sample_table(bq, table_name, target_users)
        validate_table(bq, table_name)
        export_to_gcs(bq, table_name)

    _section("Done")
    print(f"  Total time : {_elapsed(t_total)}")
    print()


if __name__ == "__main__":
    main()

