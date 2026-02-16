window.BENCHMARK_DATA = {
  "lastUpdate": 1771212595415,
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
      },
      {
        "commit": {
          "author": {
            "email": "taofeng.nju@gmail.com",
            "name": "oliverfeng",
            "username": "windoliver"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "b8b1435c260c2437ab1f9ab42fecff035a636908",
          "message": "feat(#1244): namespace dcache event-driven invalidation — L1/L2/L3 cache coherence (#1609)\n\nWire rebac_write/rebac_delete → CacheCoordinator → NamespaceManager.invalidate()\nso grant/revoke events immediately invalidate all three cache layers (dcache L1,\nmount table L2, persistent view L3). Previously, invalidate() only cleared L1+L2,\nleaving stale L3 views that caused namespace visibility to return False despite\nvalid grants.\n\nKey changes:\n- CacheCoordinator: add namespace invalidator registry + notify_namespace_invalidators()\n- EnhancedReBACManager: fire namespace invalidation in rebac_write() and rebac_delete()\n- NamespaceManager.invalidate(): clear L3 persistent store via delete_views()\n- NamespaceManager.invalidate_all(): clear L3 via delete_all_views()\n- PersistentViewStore protocol: add delete_all_views() method\n- FastAPI lifespan: wire namespace invalidation callback on startup\n- E2E test: 19 assertions covering full HTTP stack with permissions enabled\n  (grant→immediate read, revoke→immediate deny, per-subject isolation,\n  5 rapid grant/revoke cycles, p99 < 5ms performance)",
          "timestamp": "2026-02-15T18:41:15-08:00",
          "tree_id": "2de46b5fc2c59710297e863474c0e46ab45d74e8",
          "url": "https://github.com/nexi-lab/nexus/commit/b8b1435c260c2437ab1f9ab42fecff035a636908"
        },
        "date": 1771210048455,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 302.00227697080186,
            "unit": "iter/sec",
            "range": "stddev: 0.009314412758109404",
            "extra": "mean: 3.3112333126438047 msec\nrounds: 435"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 304.2351904698694,
            "unit": "iter/sec",
            "range": "stddev: 0.001206664493201249",
            "extra": "mean: 3.2869307408376125 msec\nrounds: 382"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 17630.93125457365,
            "unit": "iter/sec",
            "range": "stddev: 0.000014331852460841893",
            "extra": "mean: 56.71850145411856 usec\nrounds: 17192"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 14674.554421774197,
            "unit": "iter/sec",
            "range": "stddev: 0.00001636988923113254",
            "extra": "mean: 68.14516960843416 usec\nrounds: 18413"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54206.72488886595,
            "unit": "iter/sec",
            "range": "stddev: 0.000011787719289111785",
            "extra": "mean: 18.447895571078853 usec\nrounds: 47506"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 245.72004092600054,
            "unit": "iter/sec",
            "range": "stddev: 0.0002596336006913352",
            "extra": "mean: 4.069672120481021 msec\nrounds: 249"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.62657498910306,
            "unit": "iter/sec",
            "range": "stddev: 0.00036455325019310245",
            "extra": "mean: 5.416338357893611 msec\nrounds: 190"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 73.44699731824173,
            "unit": "iter/sec",
            "range": "stddev: 0.0011613620440303189",
            "extra": "mean: 13.615260480521158 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23716.212800814174,
            "unit": "iter/sec",
            "range": "stddev: 0.000004075509632607693",
            "extra": "mean: 42.16524823751245 usec\nrounds: 23828"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2625.7924189667,
            "unit": "iter/sec",
            "range": "stddev: 0.00003005504392529979",
            "extra": "mean: 380.8374160793408 usec\nrounds: 1704"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5160.479782263851,
            "unit": "iter/sec",
            "range": "stddev: 0.000030635575942056946",
            "extra": "mean: 193.78043170267202 usec\nrounds: 3148"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 40.63611846990357,
            "unit": "iter/sec",
            "range": "stddev: 0.0016990149118826284",
            "extra": "mean: 24.608649586958766 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1448.7704096407167,
            "unit": "iter/sec",
            "range": "stddev: 0.000282414536534654",
            "extra": "mean: 690.2404917615565 usec\nrounds: 1578"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3936.935416765903,
            "unit": "iter/sec",
            "range": "stddev: 0.000005031677520948746",
            "extra": "mean: 254.00467473796553 usec\nrounds: 4006"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18425.20168065038,
            "unit": "iter/sec",
            "range": "stddev: 0.000002707887655227985",
            "extra": "mean: 54.27349004543985 usec\nrounds: 18484"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3912.510782187538,
            "unit": "iter/sec",
            "range": "stddev: 0.000009245784230653206",
            "extra": "mean: 255.59034994937096 usec\nrounds: 3952"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1027.418316845525,
            "unit": "iter/sec",
            "range": "stddev: 0.000038498996065105725",
            "extra": "mean: 973.3133852142065 usec\nrounds: 1028"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 535.7540029055265,
            "unit": "iter/sec",
            "range": "stddev: 0.006391314627538083",
            "extra": "mean: 1.8665282845797748 msec\nrounds: 629"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "taofeng.nju@gmail.com",
            "name": "oliverfeng",
            "username": "windoliver"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "7c7fd66064f99f082865f7a407b358cefb70977b",
          "message": "refactor(#1603): decompose remote/client.py into domain clients (#1613)\n\nExtract 9 domain client modules from the monolithic remote client\n(2,406 LOC) following the OpenAI/Stripe pattern:\n\n- skills.py (22 methods), sandbox.py (10), oauth.py (6), mcp.py (8+1),\n  share_links.py (6), memory.py (21) — sync + async variants\n- admin.py (5), ace.py (11), llm.py (4) — async-only\n\nKey changes:\n- @cached_property facade on RemoteNexusFS + AsyncRemoteNexusFS\n- __getattr__ backwards-compat delegation via _DOMAIN_METHOD_MAP\n- Close async parity gap (46+ missing async methods now covered)\n- RemoteMemory/AsyncRemoteMemory/AsyncAdminAPI/AsyncACE wrappers\n  use lambda indirection for test mock compatibility\n- Parametrized unit tests for all domain clients + parity tests\n- client.py: 2,406 → ~830 LOC, async_client.py: 1,083 → ~920 LOC\n\n492 unit tests pass, 27 E2E tests pass (permissions enabled),\n4 RPC parity tests pass, all performance benchmarks met.",
          "timestamp": "2026-02-15T19:04:39-08:00",
          "tree_id": "5c7f4115a1c278db3e9b1183c69ca22424298ec8",
          "url": "https://github.com/nexi-lab/nexus/commit/7c7fd66064f99f082865f7a407b358cefb70977b"
        },
        "date": 1771211271372,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 291.2481262912494,
            "unit": "iter/sec",
            "range": "stddev: 0.002067778073004459",
            "extra": "mean: 3.4334984836949496 msec\nrounds: 368"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 295.06654795770623,
            "unit": "iter/sec",
            "range": "stddev: 0.0012754816115164219",
            "extra": "mean: 3.389065981628444 msec\nrounds: 381"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 17642.527849448474,
            "unit": "iter/sec",
            "range": "stddev: 0.000013436094670953361",
            "extra": "mean: 56.68121986449131 usec\nrounds: 17579"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 17209.129442201687,
            "unit": "iter/sec",
            "range": "stddev: 0.000014048586358842746",
            "extra": "mean: 58.1086918637334 usec\nrounds: 17846"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53200.65501447824,
            "unit": "iter/sec",
            "range": "stddev: 0.000017574640592777745",
            "extra": "mean: 18.796761049800157 usec\nrounds: 48010"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 250.76555689471206,
            "unit": "iter/sec",
            "range": "stddev: 0.00019788987523559407",
            "extra": "mean: 3.9877884841252977 msec\nrounds: 252"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 186.90830708344242,
            "unit": "iter/sec",
            "range": "stddev: 0.00033550419926433645",
            "extra": "mean: 5.350216989304626 msec\nrounds: 187"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 73.22548461232029,
            "unit": "iter/sec",
            "range": "stddev: 0.0011671778354050977",
            "extra": "mean: 13.656447687500162 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23734.792009232104,
            "unit": "iter/sec",
            "range": "stddev: 0.000001669327246983142",
            "extra": "mean: 42.132241968289875 usec\nrounds: 23999"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2619.9933538587884,
            "unit": "iter/sec",
            "range": "stddev: 0.000029599873656518592",
            "extra": "mean: 381.68035751967705 usec\nrounds: 1709"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5162.23929121718,
            "unit": "iter/sec",
            "range": "stddev: 0.00002881729490838316",
            "extra": "mean: 193.71438315565078 usec\nrounds: 4322"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 37.20175299051132,
            "unit": "iter/sec",
            "range": "stddev: 0.001933757456246596",
            "extra": "mean: 26.88045373171152 msec\nrounds: 41"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1458.794377397593,
            "unit": "iter/sec",
            "range": "stddev: 0.00022610542895455077",
            "extra": "mean: 685.4975694271208 usec\nrounds: 1642"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3965.1683034760194,
            "unit": "iter/sec",
            "range": "stddev: 0.00000818069432921871",
            "extra": "mean: 252.19610454450608 usec\nrounds: 4027"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18464.091416780015,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025566303653974047",
            "extra": "mean: 54.15917726074559 usec\nrounds: 18470"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3925.815523779803,
            "unit": "iter/sec",
            "range": "stddev: 0.000008148300727423767",
            "extra": "mean: 254.72414430650397 usec\nrounds: 3943"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1057.902769790711,
            "unit": "iter/sec",
            "range": "stddev: 0.000026591474100599017",
            "extra": "mean: 945.2664541164154 usec\nrounds: 1057"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 563.4228429176991,
            "unit": "iter/sec",
            "range": "stddev: 0.006387114341326988",
            "extra": "mean: 1.7748659156619835 msec\nrounds: 664"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "elfenliedsp@gmail.com",
            "name": "elfenlieds7",
            "username": "elfenlieds7"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "04e4940a9583b8d3b481dc706ea676b7f49a91a1",
          "message": "Merge pull request #1630 from nexi-lab/fix/audit-violations\n\nfix(#1588): fix 5 architecture violations from audit",
          "timestamp": "2026-02-16T11:26:24+08:00",
          "tree_id": "31c399b0b155d170a6d27de6435a85d66d033f01",
          "url": "https://github.com/nexi-lab/nexus/commit/04e4940a9583b8d3b481dc706ea676b7f49a91a1"
        },
        "date": 1771212594728,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 294.33272965998583,
            "unit": "iter/sec",
            "range": "stddev: 0.01046324512162072",
            "extra": "mean: 3.397515462025591 msec\nrounds: 474"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 312.76961998285884,
            "unit": "iter/sec",
            "range": "stddev: 0.001403030172026315",
            "extra": "mean: 3.1972414713897224 msec\nrounds: 367"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 18502.179778215326,
            "unit": "iter/sec",
            "range": "stddev: 0.000012014206596151523",
            "extra": "mean: 54.04768583955774 usec\nrounds: 15734"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16333.674132806824,
            "unit": "iter/sec",
            "range": "stddev: 0.00002482509500390342",
            "extra": "mean: 61.2232123568243 usec\nrounds: 17626"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 45234.47413380611,
            "unit": "iter/sec",
            "range": "stddev: 0.0007811671810101025",
            "extra": "mean: 22.107032725569972 usec\nrounds: 46233"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 250.91085394348613,
            "unit": "iter/sec",
            "range": "stddev: 0.00019487945256796912",
            "extra": "mean: 3.98547924206274 msec\nrounds: 252"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 180.61568619138274,
            "unit": "iter/sec",
            "range": "stddev: 0.0006293913014663505",
            "extra": "mean: 5.536617671957833 msec\nrounds: 189"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 72.28254650486268,
            "unit": "iter/sec",
            "range": "stddev: 0.0013478844612380932",
            "extra": "mean: 13.834598369230486 msec\nrounds: 65"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23751.707623529765,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017592836633360448",
            "extra": "mean: 42.10223600973196 usec\nrounds: 23999"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2560.7790032987414,
            "unit": "iter/sec",
            "range": "stddev: 0.000029341329546516848",
            "extra": "mean: 390.50616968970036 usec\nrounds: 1709"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4995.226900122503,
            "unit": "iter/sec",
            "range": "stddev: 0.00003053678554306832",
            "extra": "mean: 200.19110642911454 usec\nrounds: 2753"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 39.36166541662723,
            "unit": "iter/sec",
            "range": "stddev: 0.0012286932192443175",
            "extra": "mean: 25.405429099998855 msec\nrounds: 40"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1467.5961913695655,
            "unit": "iter/sec",
            "range": "stddev: 0.00022583825282481936",
            "extra": "mean: 681.3863417475872 usec\nrounds: 1545"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3963.6102958813676,
            "unit": "iter/sec",
            "range": "stddev: 0.00000860531378642786",
            "extra": "mean: 252.29523725859514 usec\nrounds: 4042"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18107.362245057026,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026906025898494886",
            "extra": "mean: 55.22615533209325 usec\nrounds: 17704"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3978.4896472268633,
            "unit": "iter/sec",
            "range": "stddev: 0.000012120268118895003",
            "extra": "mean: 251.35166575010004 usec\nrounds: 4000"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 971.1831206157278,
            "unit": "iter/sec",
            "range": "stddev: 0.00005622747913622076",
            "extra": "mean: 1.0296719318659515 msec\nrounds: 954"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 561.4555533780831,
            "unit": "iter/sec",
            "range": "stddev: 0.00673973151935508",
            "extra": "mean: 1.7810848854968968 msec\nrounds: 655"
          }
        ]
      }
    ]
  }
}