window.BENCHMARK_DATA = {
  "lastUpdate": 1771209247316,
  "repoUrl": "https://github.com/nexi-lab/nexus",
  "entries": {
    "Benchmark": [
      {
        "commit": {
          "author": {
            "email": "songym@sudoprivacy.com",
            "name": "elfenlieds7",
            "username": "elfenlieds7"
          },
          "committer": {
            "email": "songym@sudoprivacy.com",
            "name": "elfenlieds7",
            "username": "elfenlieds7"
          },
          "distinct": true,
          "id": "f4a7d492185110703b4709ff271ea0574e0c71b7",
          "message": "fix(#1519): fix benchmark workflow — clean worktree + correct checkout after orphan\n\n- git checkout - fails after orphan branch creation (no previous ref);\n  use git checkout $GITHUB_SHA instead\n- uv.lock gets modified by uv pip install; add git checkout -- . step\n  before benchmark-action to ensure clean working tree for branch switch\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>",
          "timestamp": "2026-02-16T10:30:56+08:00",
          "tree_id": "8239de4e8134ebc632b81b6955b74dafdbf0611c",
          "url": "https://github.com/nexi-lab/nexus/commit/f4a7d492185110703b4709ff271ea0574e0c71b7"
        },
        "date": 1771209246609,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 321.56057092675616,
            "unit": "iter/sec",
            "range": "stddev: 0.009717014524523896",
            "extra": "mean: 3.1098340107990916 msec\nrounds: 463"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 325.95203191567333,
            "unit": "iter/sec",
            "range": "stddev: 0.0006412502341657073",
            "extra": "mean: 3.0679360828734112 msec\nrounds: 362"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15856.544182631655,
            "unit": "iter/sec",
            "range": "stddev: 0.00001810608049519511",
            "extra": "mean: 63.06544405150666 usec\nrounds: 15550"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 14286.832528940313,
            "unit": "iter/sec",
            "range": "stddev: 0.000017198443423895096",
            "extra": "mean: 69.99452103706939 usec\nrounds: 19133"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53859.04605576098,
            "unit": "iter/sec",
            "range": "stddev: 0.000017475443777608843",
            "extra": "mean: 18.566983139001138 usec\nrounds: 44600"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 243.55256921477456,
            "unit": "iter/sec",
            "range": "stddev: 0.00027095880955551265",
            "extra": "mean: 4.105889760161632 msec\nrounds: 246"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 182.86772549215354,
            "unit": "iter/sec",
            "range": "stddev: 0.00038222252573176714",
            "extra": "mean: 5.468433521052942 msec\nrounds: 190"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 68.96400314309683,
            "unit": "iter/sec",
            "range": "stddev: 0.0013867265236250648",
            "extra": "mean: 14.500318346152996 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23734.976980377443,
            "unit": "iter/sec",
            "range": "stddev: 0.000001769916826307088",
            "extra": "mean: 42.13191362379394 usec\nrounds: 24046"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2542.58108114199,
            "unit": "iter/sec",
            "range": "stddev: 0.000030503259737419028",
            "extra": "mean: 393.301125150689 usec\nrounds: 1662"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5101.858646185341,
            "unit": "iter/sec",
            "range": "stddev: 0.000051652855399885635",
            "extra": "mean: 196.00699849802774 usec\nrounds: 4660"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 40.57012394847325,
            "unit": "iter/sec",
            "range": "stddev: 0.001006524188442416",
            "extra": "mean: 24.6486799318155 msec\nrounds: 44"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1430.429356981665,
            "unit": "iter/sec",
            "range": "stddev: 0.00025346554961923345",
            "extra": "mean: 699.0907975421381 usec\nrounds: 1546"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3947.524297928471,
            "unit": "iter/sec",
            "range": "stddev: 0.000005548034127463294",
            "extra": "mean: 253.3233298968588 usec\nrounds: 3977"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18473.295500946937,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025811400429128084",
            "extra": "mean: 54.13219314056554 usec\nrounds: 18427"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3868.690812636419,
            "unit": "iter/sec",
            "range": "stddev: 0.00002519618730032877",
            "extra": "mean: 258.48537617265004 usec\nrounds: 3945"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1028.869975759293,
            "unit": "iter/sec",
            "range": "stddev: 0.000015705705790096837",
            "extra": "mean: 971.9401125122857 usec\nrounds: 1031"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 535.6073303914301,
            "unit": "iter/sec",
            "range": "stddev: 0.0074846428376398495",
            "extra": "mean: 1.8670394209675671 msec\nrounds: 620"
          }
        ]
      }
    ]
  }
}