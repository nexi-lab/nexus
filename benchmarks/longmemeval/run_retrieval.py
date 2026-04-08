#!/usr/bin/env python3
"""LongMemEval retrieval benchmark using Nexus search API.

Indexes each question's haystack via the running Nexus instance (Docker),
queries with the question, and computes recall@k / NDCG@k metrics.

Usage:
    python benchmarks/longmemeval/run_retrieval.py [n_queries] [granularity] [search_type]

Examples:
    python benchmarks/longmemeval/run_retrieval.py 10 session hybrid
    python benchmarks/longmemeval/run_retrieval.py 500 turn keyword

Environment:
    NEXUS_URL   -- base URL of running Nexus (default: http://localhost:40970)
    NEXUS_API_KEY -- API key for auth
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

try:
    import httpx
except ImportError:
    sys.exit("httpx required: pip install httpx")

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_DATA = DATA_DIR / "longmemeval_s_cleaned.json"

NEXUS_URL = os.environ.get("NEXUS_URL", "http://localhost:40970")
NEXUS_API_KEY = os.environ.get(
    "NEXUS_API_KEY", "sk-pgVobcduFYpVrec61i17xr2gQbZogGYZI50-Qmk7lSc"
)


# ── LongMemEval eval utils (ported from their repo) ──────────────────


def _dcg(relevances: list[float], k: int) -> float:
    rel = np.asarray(relevances, dtype=float)[:k]
    if rel.size == 0:
        return 0.0
    return float(rel[0] + np.sum(rel[1:] / np.log2(np.arange(2, rel.size + 1))))


def _ndcg(rankings: list[int], correct_docs: list[str], corpus_ids: list[str], k: int) -> float:
    relevances = [1.0 if corpus_ids[idx] in correct_docs else 0.0 for idx in rankings[:k]]
    all_rel = [1.0 if cid in correct_docs else 0.0 for cid in corpus_ids]
    ideal = sorted(all_rel, reverse=True)
    ideal_dcg = _dcg(ideal, k)
    actual_dcg = _dcg(relevances, k)
    return actual_dcg / ideal_dcg if ideal_dcg > 0 else 0.0


def evaluate_retrieval(
    rankings: list[int], correct_docs: list[str], corpus_ids: list[str], k: int = 10
) -> tuple[float, float, float]:
    recalled = {corpus_ids[idx] for idx in rankings[:k]}
    recall_any = float(any(d in recalled for d in correct_docs))
    recall_all = float(all(d in recalled for d in correct_docs))
    ndcg_score = _ndcg(rankings, correct_docs, corpus_ids, k)
    return recall_any, recall_all, ndcg_score


# ── Corpus building (ported from their repo, with assistant turns) ────


def build_corpus(
    haystack_sessions: list[list[dict]],
    haystack_session_ids: list[str],
    haystack_dates: list[str],
    granularity: str,
) -> tuple[list[str], list[str], list[str]]:
    """Build flat corpus from haystack sessions.

    Includes BOTH user and assistant turns so that single-session-assistant
    questions can be retrieved.

    Returns (corpus_texts, corpus_ids, corpus_timestamps).
    """
    corpus, corpus_ids, corpus_ts = [], [], []

    for sess_data, sess_id, ts in zip(haystack_sessions, haystack_session_ids, haystack_dates):
        if granularity == "session":
            # Include all turns (user + assistant) for full session coverage
            # Truncate to ~4000 words (~5300 tokens) to stay within OpenAI embedding limit
            text = " ".join(t["content"] for t in sess_data)
            words = text.split()
            if len(words) > 4000:
                text = " ".join(words[:4000])
            corpus.append(text)
            cid = sess_id
            # Mark as gold if any turn (user or assistant) has_answer
            if "answer" in sess_id and not any(
                t.get("has_answer", False) for t in sess_data
            ):
                cid = sess_id.replace("answer", "noans")
            corpus_ids.append(cid)
            corpus_ts.append(ts)

        elif granularity == "turn":
            for i_turn, turn in enumerate(sess_data):
                corpus.append(turn["content"])
                if "answer" not in sess_id:
                    corpus_ids.append(f"{sess_id}_{i_turn + 1}")
                elif turn.get("has_answer", False):
                    corpus_ids.append(f"{sess_id}_{i_turn + 1}")
                else:
                    corpus_ids.append(
                        f"{sess_id.replace('answer', 'noans')}_{i_turn + 1}"
                    )
                corpus_ts.append(ts)

    return corpus, corpus_ids, corpus_ts


# ── Nexus API client ─────────────────────────────────────────────────


class NexusSearchClient:
    """Thin async client for Nexus search API."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = httpx.AsyncClient(timeout=120.0, headers=self._headers)

    async def health(self) -> dict:
        r = await self._client.get(f"{self._base}/api/v2/search/health")
        r.raise_for_status()
        return r.json()

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        search_type: str = "hybrid",
        path_prefix: str | None = None,
    ) -> list[dict]:
        params: dict = {"q": query, "type": search_type, "limit": limit}
        if path_prefix:
            params["path"] = path_prefix
        r = await self._client.get(
            f"{self._base}/api/v2/search/query",
            params=params,
        )
        if r.status_code != 200:
            return []
        return r.json().get("results", [])

    async def close(self) -> None:
        await self._client.aclose()


# ── Main benchmark ────────────────────────────────────────────────────


async def run_benchmark(
    data_path: str,
    n_queries: int = 10,
    granularity: str = "session",
    search_type: str = "hybrid",
) -> None:
    print(f"Loading {data_path} ...")
    with open(data_path) as f:
        data = json.load(f)

    # Drop abstention questions (no retrieval target)
    data = [e for e in data if "_abs" not in e["question_id"]]

    data = data[:n_queries]

    print(f"Running {len(data)} questions  granularity={granularity}  search_type={search_type}")

    client = NexusSearchClient(NEXUS_URL, NEXUS_API_KEY)
    health = await client.health()
    print(f"Nexus: {health.get('status')}  backend={health.get('backend')}\n")

    all_metrics: list[dict] = []
    results_log: list[dict] = []

    for i, entry in enumerate(data):
        q_id = entry["question_id"]
        q_type = entry["question_type"]
        question = entry["question"]
        answer = entry["answer"]

        # Build corpus (for metric computation — no re-indexing needed)
        corpus, corpus_ids, corpus_ts = build_corpus(
            entry["haystack_sessions"],
            entry["haystack_session_ids"],
            entry["haystack_dates"],
            granularity,
        )
        correct_docs = [cid for cid in corpus_ids if "answer" in cid]

        q_prefix = f"/bench/{q_id}"

        # Search only — assumes preindex.py has already indexed all haystacks
        sanitized_q = question.replace("'", "")
        t0 = time.perf_counter()
        search_results = await client.search(
            sanitized_q,
            limit=min(len(corpus), 100),
            search_type=search_type,
            path_prefix=q_prefix,
        )
        search_ms = (time.perf_counter() - t0) * 1000

        # Map search results back to corpus indices via path
        # Only consider results from this question's prefix
        rankings: list[int] = []
        seen: set[int] = set()
        for r in search_results:
            path = r.get("path", "")
            if not path.startswith(q_prefix + "/"):
                continue
            try:
                idx = int(path.split("/")[-1])
            except (ValueError, IndexError):
                continue
            if idx < len(corpus) and idx not in seen:
                rankings.append(idx)
                seen.add(idx)

        # Fill in any missing indices (docs not returned by search)
        for idx in range(len(corpus)):
            if idx not in seen:
                rankings.append(idx)

        # Compute metrics
        metrics: dict[str, float] = {}
        for k in [1, 3, 5, 10, 30, 50]:
            if k > len(corpus):
                break
            r_any, r_all, ndcg_v = evaluate_retrieval(rankings, correct_docs, corpus_ids, k)
            metrics[f"recall_any@{k}"] = r_any
            metrics[f"recall_all@{k}"] = r_all
            metrics[f"ndcg_any@{k}"] = ndcg_v

        all_metrics.append({"question_type": q_type, **metrics})

        # Log ranked items in LongMemEval format
        results_log.append({
            "question_id": q_id,
            "question_type": q_type,
            "question": question,
            "answer": answer,
            "retrieval_results": {
                "ranked_items": [
                    {
                        "corpus_id": corpus_ids[idx],
                        "text": corpus[idx][:200],
                        "timestamp": corpus_ts[idx],
                    }
                    for idx in rankings
                ],
                "metrics": {granularity: metrics},
            },
        })

        r5 = metrics.get("recall_any@5", "-")
        n5 = metrics.get("ndcg_any@5", "-")
        status = "HIT" if r5 == 1.0 else "MISS"
        print(
            f"[{i + 1}/{len(data)}] {status}  {q_id}  ({q_type})\n"
            f"  Q: {question}\n"
            f"  A: {answer}\n"
            f"  corpus={len(corpus)}  gold={len(correct_docs)}  "
            f"search={search_ms:.0f}ms\n"
            f"  recall@5={r5}  ndcg@5={n5}\n"
        )

    # ── Aggregate ─────────────────────────────────────────────────────
    print("=" * 64)
    print("AGGREGATE RESULTS")
    print("=" * 64)

    # Overall
    for metric_name in [
        "recall_any@5", "recall_all@5", "ndcg_any@5",
        "recall_any@10", "recall_all@10", "ndcg_any@10",
    ]:
        vals = [m[metric_name] for m in all_metrics if metric_name in m]
        if vals:
            print(f"  {metric_name:20s} = {np.mean(vals):.4f}  (n={len(vals)})")

    # Per question type
    q_types = sorted(set(m["question_type"] for m in all_metrics))
    print(f"\n{'type':30s} {'recall@5':>10s} {'ndcg@5':>10s} {'recall@10':>10s} {'n':>5s}")
    print("-" * 64)
    for qt in q_types:
        subset = [m for m in all_metrics if m["question_type"] == qt]
        r5 = np.mean([m.get("recall_any@5", 0) for m in subset])
        n5 = np.mean([m.get("ndcg_any@5", 0) for m in subset])
        r10 = np.mean([m.get("recall_any@10", 0) for m in subset])
        print(f"  {qt:28s} {r5:10.4f} {n5:10.4f} {r10:10.4f} {len(subset):5d}")

    # Save results
    out_file = DATA_DIR / f"retrieval_results_{granularity}_{search_type}_{len(data)}q.jsonl"
    with open(out_file, "w") as f:
        for entry in results_log:
            f.write(json.dumps(entry) + "\n")
    print(f"\nResults saved to {out_file}")

    await client.close()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    gran = sys.argv[2] if len(sys.argv) > 2 else "session"
    stype = sys.argv[3] if len(sys.argv) > 3 else "hybrid"
    asyncio.run(run_benchmark(str(DEFAULT_DATA), n, gran, stype))
