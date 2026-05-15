# SANDBOX hybrid retrieval — scratch evals

Throwaway drivers used during the SANDBOX hybrid work to validate
`_hybrid_search_sandbox` against published retrieval benchmarks. Not
wired into CI; rerun by hand when tuning fusion params or model picks.

## `run_herb_qa.py`

Uses the in-tree HERB corpus and QA set
(`nexus.cli.commands.demo_data.HERB_CORPUS` /  `HERB_QA_SET`) to
mirror the retrieval gate in `scripts/test_build_perf_e2e.py` section 6
(`hits >= 7/8` at top-5). Self-contained — no external clones needed.

```
.venv/bin/python tools/scratch/sandbox_eval/run_herb_qa.py
```

Last known result: **8/8 hits, p50=11ms, p95=14ms** (bge-small + BM25S
+ RRF, default fusion).

## `run_tier5.py`

Tier-5 fuzzy queries (30 hand-authored, vague recall). Requires two
external inputs the script does NOT vendor:

* Corpus: clone `garrytan/gbrain-evals` to `/tmp/gbrain-evals-clone/`
  for the 240-page `world-v1` corpus.
* Queries: dump `TIER5_FUZZY_QUERIES` to `/tmp/tier5_queries.json` via
  bun (script in the gbrain-evals tsconfig project).

Tunable via `EVAL_*` env vars — see the module docstring for the full
list. Used to find the SANDBOX hybrid ceiling on hard fuzzy retrieval
(scoreable R@5 ≈ 0.826 with weighted fusion at low alpha).
