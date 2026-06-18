"""Upload model artifacts to a Hugging Face Hub dataset repo.

Creates the dataset repo if it does not exist, then uploads:
    model/model_inference.pt  →  recosys-models/model_inference.pt
    model/vocabs.pkl          →  recosys-models/vocabs.pkl

Usage:
    python scripts/deploy/upload_to_hf_hub.py \
        --token hf_xxx \
        --hf-username yourname

    # Custom dataset repo name:
    python scripts/deploy/upload_to_hf_hub.py \
        --token hf_xxx \
        --hf-username yourname \
        --repo-name my-recosys-models
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload RecoSys model files to HF Hub")
    parser.add_argument("--token",        required=True,  help="HF access token (write permission)")
    parser.add_argument("--hf-username",  required=True,  help="Your HF username")
    parser.add_argument("--repo-name",    default="recosys-models", help="Dataset repo name on HF Hub")
    parser.add_argument("--model-dir",    default="model", help="Local directory containing model_inference.pt and vocabs.pkl")
    args = parser.parse_args()

    try:
        from huggingface_hub import HfApi, create_repo
    except ImportError:
        raise SystemExit("huggingface_hub not installed. Run: pip install huggingface_hub")

    model_dir = Path(args.model_dir)
    ckpt_path   = model_dir / "model_inference.pt"
    vocabs_path = model_dir / "vocabs.pkl"

    if not ckpt_path.exists():
        raise FileNotFoundError(f"Checkpoint not found: {ckpt_path}")
    if not vocabs_path.exists():
        raise FileNotFoundError(f"Vocabs not found: {vocabs_path}")

    repo_id = f"{args.hf_username}/{args.repo_name}"
    api = HfApi(token=args.token)

    print(f"Creating / verifying dataset repo: {repo_id}")
    create_repo(
        repo_id   = repo_id,
        repo_type = "dataset",
        private   = False,
        exist_ok  = True,
        token     = args.token,
    )

    for local_path, remote_name in [
        (ckpt_path,   "model_inference.pt"),
        (vocabs_path, "vocabs.pkl"),
    ]:
        size_mb = local_path.stat().st_size / 1024 / 1024
        print(f"Uploading {local_path}  ({size_mb:.1f} MB)  →  {repo_id}/{remote_name}")
        api.upload_file(
            path_or_fileobj = str(local_path),
            path_in_repo    = remote_name,
            repo_id         = repo_id,
            repo_type       = "dataset",
            token           = args.token,
        )
        print(f"  ✓ {remote_name}")

    print()
    print(f"Done. Model files are at: https://huggingface.co/datasets/{repo_id}")
    print()
    print("Next step: set HF_DATASET_REPO in your Space's environment variables:")
    print(f"  HF_DATASET_REPO={repo_id}")


if __name__ == "__main__":
    main()
