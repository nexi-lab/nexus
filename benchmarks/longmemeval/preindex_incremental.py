#!/usr/bin/env python3
"""Incremental pre-indexing with per-question save and retry.

Each question is indexed as a separate API call. Progress saved after each.
Resume from where it left off. Monitors and reports every 10 questions.
"""

import json
import os
import sys
import time
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "longmemeval_s_cleaned.json"
PROGRESS_FILE = DATA_DIR / "preindex_progress.json"

URL = os.environ.get("NEXUS_URL", "http://localhost:46930")
KEY = os.environ.get("NEXUS_API_KEY", "sk-WCsGdtHC1ackWlSUEqtOEcglSHEUfBkLQS6P2wCyfhk")
MAX_WORDS = 4000


def load_data():
    with open(DATA_FILE) as f:
        data = json.load(f)
    return [e for e in data if "_abs" not in e["question_id"]]


def load_progress():
    if PROGRESS_FILE.exists():
        return set(json.loads(PROGRESS_FILE.read_text()))
    return set()


def save_progress(done):
    PROGRESS_FILE.write_text(json.dumps(sorted(done)))


def build_docs(entry):
    q_id = entry["question_id"]
    docs = []
    for sd in entry["haystack_sessions"]:
        text = " ".join(t["content"] for t in sd)
        w = text.split()
        if len(w) > MAX_WORDS:
            text = " ".join(w[:MAX_WORDS])
        docs.append({
            "id": f"{q_id}-{len(docs)}",
            "text": text,
            "path": f"/bench/{q_id}/{len(docs)}",
        })
    return docs


def main():
    data = load_data()
    done = load_progress()
    remaining = [e for e in data if e["question_id"] not in done]
    print(f"{len(done)}/{len(data)} done, {len(remaining)} remaining")

    if not remaining:
        print("All done!")
        return

    client = httpx.Client(
        timeout=600.0,
        headers={"Authorization": f"Bearer {KEY}"},
    )

    t_start = time.time()
    new_done = 0
    new_failed = 0

    for i, entry in enumerate(remaining):
        q_id = entry["question_id"]
        docs = build_docs(entry)

        ok = False
        for attempt in range(3):
            try:
                r = client.post(f"{URL}/api/v2/search/index", json={"documents": docs})
                if r.status_code == 200:
                    done.add(q_id)
                    new_done += 1
                    ok = True
                    break
                else:
                    print(f"  {q_id} attempt {attempt+1}: HTTP {r.status_code}")
                    if r.status_code == 500:
                        time.sleep(5)
            except Exception as e:
                print(f"  {q_id} attempt {attempt+1}: {e}")
                time.sleep(10)

        if not ok:
            new_failed += 1
            print(f"  FAILED: {q_id} after 3 attempts")

        # Save progress after every question
        save_progress(done)

        # Report every 10 questions
        if (new_done + new_failed) % 10 == 0:
            elapsed = time.time() - t_start
            rate = new_done / elapsed if elapsed > 0 else 0
            eta = (len(remaining) - i - 1) / rate / 60 if rate > 0 else 0
            print(
                f"[{len(done)}/{len(data)}] "
                f"+{new_done} ok +{new_failed} fail "
                f"{elapsed:.0f}s {rate:.2f}q/s "
                f"ETA {eta:.0f}min"
            )

    elapsed = time.time() - t_start
    print(f"\nDONE: {len(done)}/{len(data)}, {new_failed} failed, {elapsed:.0f}s")
    client.close()


if __name__ == "__main__":
    main()
