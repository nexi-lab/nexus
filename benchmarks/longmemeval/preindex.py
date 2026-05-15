#!/usr/bin/env python3
"""Pre-index LongMemEval haystacks into Nexus — parallel version.

Sends 5 questions concurrently to maximize OpenAI embedding throughput.

Usage:
    python benchmarks/longmemeval/preindex.py [granularity] [concurrency]
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    sys.exit("httpx required: pip install httpx")

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_DATA = DATA_DIR / "longmemeval_s_cleaned.json"
PROGRESS_FILE = DATA_DIR / "preindex_progress.json"

NEXUS_URL = os.environ.get("NEXUS_URL", "http://localhost:40970")
NEXUS_API_KEY = os.environ.get(
    "NEXUS_API_KEY", "sk-pgVobcduFYpVrec61i17xr2gQbZogGYZI50-Qmk7lSc"
)

MAX_WORDS = 4000


def build_docs_for_entry(entry: dict, granularity: str) -> list[dict]:
    q_id = entry["question_id"]
    docs = []
    for sess_data in entry["haystack_sessions"]:
        if granularity == "session":
            text = " ".join(t["content"] for t in sess_data)
            words = text.split()
            if len(words) > MAX_WORDS:
                text = " ".join(words[:MAX_WORDS])
            idx = len(docs)
            docs.append({
                "id": f"{q_id}-{idx}",
                "text": text,
                "path": f"/bench/{q_id}/{idx}",
            })
    return docs


def load_progress() -> set[str]:
    if PROGRESS_FILE.exists():
        return set(json.loads(PROGRESS_FILE.read_text()))
    return set()


def save_progress(done: set[str]) -> None:
    PROGRESS_FILE.write_text(json.dumps(sorted(done)))


async def index_one(
    client: httpx.AsyncClient,
    entry: dict,
    granularity: str,
    sem: asyncio.Semaphore,
) -> tuple[str, bool, int]:
    q_id = entry["question_id"]
    docs = build_docs_for_entry(entry, granularity)

    async with sem:
        for attempt in range(5):
            try:
                r = await client.post(
                    f"{NEXUS_URL}/api/v2/search/index",
                    json={"documents": docs},
                )
            except (httpx.ConnectError, httpx.ReadTimeout):
                await asyncio.sleep(10)
                continue

            if r.status_code == 200:
                return q_id, True, len(docs)
            elif r.status_code == 429:
                wait = int(r.headers.get("retry-after", "30"))
                await asyncio.sleep(wait)
            elif r.status_code == 500:
                await asyncio.sleep(5)
            else:
                return q_id, False, 0

    return q_id, False, 0


async def preindex(data_path: str, granularity: str = "session", concurrency: int = 5) -> None:
    print(f"Loading {data_path} ...")
    with open(data_path) as f:
        data = json.load(f)
    data = [e for e in data if "_abs" not in e["question_id"]]

    done = load_progress()
    remaining = [e for e in data if e["question_id"] not in done]
    print(f"{len(data)} total, {len(done)} done, {len(remaining)} remaining")
    print(f"Concurrency: {concurrency}\n")

    if not remaining:
        print("All done!")
        return

    client = httpx.AsyncClient(
        timeout=600.0,
        headers={"Authorization": f"Bearer {NEXUS_API_KEY}"},
    )

    sem = asyncio.Semaphore(concurrency)
    total_docs = 0
    failed = 0
    t_start = time.perf_counter()

    # Process in chunks to save progress periodically
    chunk_size = 20
    for chunk_start in range(0, len(remaining), chunk_size):
        chunk = remaining[chunk_start : chunk_start + chunk_size]
        tasks = [index_one(client, e, granularity, sem) for e in chunk]
        results = await asyncio.gather(*tasks)

        for q_id, success, n_docs in results:
            if success:
                done.add(q_id)
                total_docs += n_docs
            else:
                failed += 1

        save_progress(done)
        elapsed = time.perf_counter() - t_start
        rate = total_docs / elapsed if elapsed > 0 else 0
        print(
            f"[{len(done)}/{len(data)}] "
            f"+{sum(1 for _, s, _ in results if s)} ok, "
            f"+{sum(1 for _, s, _ in results if not s)} fail  "
            f"({total_docs} docs, {rate:.0f} docs/s, {elapsed:.0f}s)"
        )

    elapsed = time.perf_counter() - t_start
    print(f"\nDone: {len(done)}/{len(data)}, {failed} failed, {elapsed:.0f}s total")
    await client.aclose()


if __name__ == "__main__":
    gran = sys.argv[1] if len(sys.argv) > 1 else "session"
    conc = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    asyncio.run(preindex(str(DEFAULT_DATA), gran, conc))
