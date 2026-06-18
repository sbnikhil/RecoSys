"""Restore GRU4Rec V9 1M training run to MLflow (DagsHub or local).

All epoch metrics are sourced from reports/08_vertex_ai_1m_training.md, which
was written while GCS was live and contains the authoritative training log.
No model re-training is required.

Usage — DagsHub (recommended, free permanent URL):
    python scripts/monitoring/restore_mlflow_logs.py \
        --tracking-uri https://dagshub.com/<your-username>/RecoSys.mlflow \
        --dagshub-username <your-dagshub-username> \
        --dagshub-token <your-dagshub-token>

Usage — local SQLite:
    python scripts/monitoring/restore_mlflow_logs.py --local

The script creates (or re-uses) an experiment named "gru4rec_v9_training" and
logs one run named "1M_users_vertex_ai".
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


# ── Training data from reports/08_vertex_ai_1m_training.md ───────────────────

_HPARAMS = {
    "model":           "GRU4Rec V9",
    "embed_dim":       128,
    "gru_hidden":      256,
    "n_layers":        1,
    "dropout":         0.3,
    "batch_size":      256,
    "learning_rate":   3e-4,
    "weight_decay":    1e-5,
    "temperature":     0.07,
    "label_smoothing": 0.1,
    "grad_clip":       1.0,
    "scheduler":       "cosine",
    "lr_min":          1e-5,
    "max_epochs":      30,
    "patience":        5,
    "n_users_train":   1_000_000,
    "hardware":        "A100 40GB (a2-highgpu-1g)",
    "training_time_h": 10.77,
    "best_epoch":      24,
}

# (epoch, train_loss, val_ndcg_20)
_EPOCHS = [
    ( 1, 7.6711, 0.2381),
    ( 2, 7.2016, 0.2492),
    ( 3, 7.1148, 0.2537),
    ( 4, 7.0667, 0.2588),
    ( 5, 7.0366, 0.2602),
    ( 6, 7.0149, 0.2617),
    ( 7, 6.9982, 0.2614),
    ( 8, 6.9847, 0.2628),
    ( 9, 6.9729, 0.2638),
    (10, 6.9631, 0.2646),
    (11, 6.9543, 0.2652),
    (12, 6.9467, 0.2650),
    (13, 6.9394, 0.2652),
    (14, 6.9327, 0.2659),
    (15, 6.9267, 0.2666),
    (16, 6.9210, 0.2663),
    (17, 6.9160, 0.2667),
    (18, 6.9111, 0.2661),
    (19, 6.9062, 0.2665),
    (20, 6.9018, 0.2674),
    (21, 6.8979, 0.2670),
    (22, 6.8942, 0.2673),
    (23, 6.8907, 0.2672),
    (24, 6.8873, 0.2676),  # BEST
    (25, 6.8844, 0.2671),
    (26, 6.8820, 0.2672),
    (27, 6.8796, 0.2673),
    (28, 6.8779, 0.2675),
    (29, 6.8768, 0.2674),  # early-stop triggered
]

_TEST_METRICS = {
    "test_ndcg_10":  0.2420,
    "test_hr_10":    0.3803,
    "test_ndcg_20":  0.2676,
    "test_hr_20":    0.4815,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--tracking-uri",
        help="MLflow tracking URI. Defaults to DagsHub URL if --dagshub-username is provided.",
    )
    parser.add_argument("--dagshub-username", help="DagsHub username")
    parser.add_argument("--dagshub-token",    help="DagsHub access token")
    parser.add_argument(
        "--local",
        action="store_true",
        help="Use a local SQLite backend at mlruns/ (ignores other flags)",
    )
    parser.add_argument("--experiment-name", default="gru4rec_v9_training")
    parser.add_argument("--run-name",        default="1M_users_vertex_ai")
    args = parser.parse_args()

    try:
        import mlflow
    except ImportError:
        raise SystemExit("mlflow not installed. Run: pip install mlflow")

    # ── Configure tracking URI ────────────────────────────────────────────────
    if args.local:
        tracking_uri = f"sqlite:///{Path('mlruns.db').absolute()}"
        print(f"Using local SQLite backend: {tracking_uri}")
    elif args.tracking_uri:
        tracking_uri = args.tracking_uri
    elif args.dagshub_username:
        tracking_uri = f"https://dagshub.com/{args.dagshub_username}/RecoSys.mlflow"
    else:
        raise SystemExit(
            "Specify --local, --tracking-uri, or --dagshub-username. "
            "Run with --help for examples."
        )

    mlflow.set_tracking_uri(tracking_uri)

    if args.dagshub_username and args.dagshub_token:
        os.environ["MLFLOW_TRACKING_USERNAME"] = args.dagshub_username
        os.environ["MLFLOW_TRACKING_PASSWORD"] = args.dagshub_token

    # ── Log run ───────────────────────────────────────────────────────────────
    mlflow.set_experiment(args.experiment_name)

    print(f"Logging run '{args.run_name}' to {tracking_uri} ...")
    with mlflow.start_run(run_name=args.run_name) as run:
        # Hyperparameters
        mlflow.log_params(_HPARAMS)

        # Per-epoch metrics (step = epoch number, 1-indexed)
        for epoch, train_loss, val_ndcg in _EPOCHS:
            mlflow.log_metrics(
                {
                    "train_loss":  train_loss,
                    "val_ndcg_20": val_ndcg,
                },
                step=epoch,
            )

        # Final test metrics (logged at step=29, the last epoch)
        mlflow.log_metrics(_TEST_METRICS, step=29)

        # Tags
        mlflow.set_tags({
            "dataset":     "REES46 eCommerce 1M users",
            "source":      "reports/08_vertex_ai_1m_training.md",
            "artifact":    "model/model_inference.pt",
            "best_epoch":  "24",
        })

        run_id = run.info.run_id

    print(f"Done. Run ID: {run_id}")
    if not args.local and args.dagshub_username:
        print(f"View at: https://dagshub.com/{args.dagshub_username}/RecoSys/experiments")
    elif args.local:
        print("View with: mlflow ui --backend-store-uri sqlite:///mlruns.db")


if __name__ == "__main__":
    main()
