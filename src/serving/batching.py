"""Dynamic request batching for GRU4Rec V9 serving — WS3.

Activated by ENABLE_DYNAMIC_BATCHING=true in app.py.  When active, concurrent
/recommend requests are batched into a single GRU forward pass + FAISS search
instead of running one at a time.

Design:
  - Fully asyncio-native: fits FastAPI's event loop without extra threads.
  - Each request adds its padded tensors to a shared queue as a Future and
    waits for the result.
  - A background asyncio.Task drains the queue when either:
      (a) max_batch_size requests have accumulated, or
      (b) max_wait_ms milliseconds have elapsed since the first request
          in the current batch arrived (whichever happens first).
  - On drain: tensors are stacked → single model.encode_sequence call →
    single index.search call → results split and Futures resolved.

Trade-off:
  - Throughput   ↑  at high concurrency (amortises GRU + FAISS overhead)
  - p50 latency  ↑  at low concurrency  (requests wait up to max_wait_ms)
  Quantified in benchmark_inference.py --batch-sizes.

Usage in app.py:
    from src.serving.batching import DynamicBatchQueue
    _batch_queue: DynamicBatchQueue | None = None

    # On startup (inside lifespan):
    if os.environ.get("ENABLE_DYNAMIC_BATCHING") == "true":
        _batch_queue = DynamicBatchQueue(
            model=_artifacts.model,
            index=_artifacts.index,
            item_idx_array=_artifacts.item_idx_array,
            vocabs=_artifacts.vocabs,
            max_batch_size=16,
            max_wait_ms=10.0,
        )
        asyncio.get_event_loop().create_task(_batch_queue.run())

    # In the /recommend handler:
    if _batch_queue is not None:
        rec_ids = await _batch_queue.enqueue(item_seq, event_seq, top_k)
    else:
        ...  # existing single-item path
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import faiss
import numpy as np
import torch

from src.sequence.models.gru4rec import GRU4RecModel


@dataclass
class _PendingRequest:
    item_seq:  list[int]
    event_seq: list[int]
    top_k:     int
    future:    asyncio.Future
    arrived:   float   # time.monotonic() at enqueue


class DynamicBatchQueue:
    """Accumulates concurrent /recommend requests and processes them as batches.

    Args:
        model:          GRU4Rec model (eval mode, on CPU or GPU).
        index:          Pre-built FAISS index.
        item_idx_array: (n_indexed,) int64 — FAISS row → catalog item_idx.
        vocabs:         Serving vocabs dict with 'idx2item' key.
        max_batch_size: Drain immediately when this many requests are pending.
        max_wait_ms:    Drain after this many milliseconds even if batch is not full.
    """

    def __init__(
        self,
        model:           GRU4RecModel,
        index:           faiss.Index,
        item_idx_array:  np.ndarray,
        vocabs:          dict,
        max_batch_size:  int   = 16,
        max_wait_ms:     float = 10.0,
    ) -> None:
        self._model          = model
        self._index          = index
        self._item_idx_array = item_idx_array
        self._idx2item       = vocabs["idx2item"]
        self._max_batch      = max_batch_size
        self._max_wait_s     = max_wait_ms / 1000.0
        self._queue:         list[_PendingRequest] = []
        self._lock           = asyncio.Lock()
        self._has_work       = asyncio.Event()

    async def enqueue(
        self,
        item_seq:  list[int],
        event_seq: list[int],
        top_k:     int = 20,
    ) -> list[str]:
        """Add a request and wait for its result.  Returns list of item_id strings."""
        loop    = asyncio.get_event_loop()
        future  = loop.create_future()
        pending = _PendingRequest(item_seq, event_seq, top_k, future, time.monotonic())

        async with self._lock:
            self._queue.append(pending)
            self._has_work.set()

        return await future

    async def run(self) -> None:
        """Background task: drain the queue in a loop.  Run via asyncio.create_task."""
        while True:
            # Wait until at least one request is pending.
            await self._has_work.wait()

            # Give other coroutines a chance to add more requests up to max_wait_ms.
            async with self._lock:
                if not self._queue:
                    self._has_work.clear()
                    continue
                oldest_arrived = self._queue[0].arrived

            time_to_wait = self._max_wait_s - (time.monotonic() - oldest_arrived)
            if time_to_wait > 0 and len(self._queue) < self._max_batch:
                await asyncio.sleep(time_to_wait)

            # Grab the current batch.
            async with self._lock:
                if not self._queue:
                    self._has_work.clear()
                    continue
                batch              = self._queue[: self._max_batch]
                self._queue        = self._queue[self._max_batch :]
                if not self._queue:
                    self._has_work.clear()

            # Run inference in the default executor so it doesn't block the
            # event loop (PyTorch CPU ops are GIL-holding and non-async).
            loop = asyncio.get_event_loop()
            try:
                results = await loop.run_in_executor(None, self._process_batch, batch)
            except Exception as exc:
                for pending in batch:
                    if not pending.future.done():
                        pending.future.set_exception(exc)
                continue

            for pending, rec_ids in zip(batch, results):
                if not pending.future.done():
                    pending.future.set_result(rec_ids)

    def _process_batch(self, batch: list[_PendingRequest]) -> list[list[str]]:
        """Synchronous batch inference: stacks tensors, single forward + search."""
        b = len(batch)

        item_batch  = torch.tensor(
            [p.item_seq  for p in batch], dtype=torch.long
        )   # (B, L)
        event_batch = torch.tensor(
            [p.event_seq for p in batch], dtype=torch.long
        )   # (B, L)

        with torch.no_grad():
            user_embs = self._model.encode_sequence(item_batch, event_batch)  # (B, D)

        user_np = user_embs.cpu().numpy().astype(np.float32)
        faiss.normalize_L2(user_np)

        max_k        = max(p.top_k for p in batch)
        n_candidates = min(max_k + 10, self._index.ntotal)
        _, faiss_indices = self._index.search(user_np, n_candidates)           # (B, n_cand)

        output: list[list[str]] = []
        for i, pending in enumerate(batch):
            rec_ids: list[str] = []
            for pos in faiss_indices[i]:
                if pos < 0:
                    continue
                iidx = int(self._item_idx_array[pos])
                pid  = self._idx2item.get(iidx)
                if pid is not None:
                    rec_ids.append(str(pid))
                if len(rec_ids) >= pending.top_k:
                    break
            output.append(rec_ids)

        return output
