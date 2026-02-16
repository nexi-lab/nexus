window.BENCHMARK_DATA = {
  "lastUpdate": 1771214228818,
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
          "id": "6528279ed87ef8523e3f12b24518f76bc5dfacc7",
          "message": "feat(#954): Memory-mapped trigram index for sub-20ms grep (#1605)\n\n* feat(#954): Memory-mapped trigram index for sub-20ms grep\n\nImplement a trigram-based inverted index in Rust for O(1) index lookup\n+ O(k) candidate verification, replacing sequential file scanning.\n\nCore library (nexus_core::trigram):\n- Binary index format with CRC32 integrity per section (header, file\n  table, trigram table, posting lists)\n- Roaring bitmap posting lists for efficient intersection/union\n- Trigram extraction from both content and regex patterns via\n  regex_syntax HIR tree walking\n- Case-insensitive support via dual-indexed trigrams (original +\n  lowercased)\n- Builder, writer (WASM-safe, no I/O), and comprehensive error types\n- 90 Rust tests including proptest roundtrip verification\n\nPyO3 reader (nexus_pyo3::trigram):\n- Memory-mapped index reader with binary search in trigram table\n- Thread-safe index cache with parking_lot::RwLock\n- Parallel candidate verification via rayon (>10 candidates)\n- Functions: build_trigram_index, trigram_grep, trigram_index_stats,\n  invalidate_trigram_cache\n\nPython integration:\n- trigram_fast.py wrapper following grep_fast.py pattern\n- TRIGRAM_INDEX strategy in SearchStrategy enum\n- Strategy selection in search_service when file_count > 500\n- Zone management: build/status/invalidate per zone\n- Fallback to RUST_BULK on trigram failure\n\nAlso fixes DRY violation: de-duplicated GrepMatch and\nis_literal_pattern between nexus_core and nexus_pyo3.\n\nTest suite: 90 Rust + 50 Python tests (unit, regression with superset\ninvariant, integration, benchmarks).\n\n* feat(#954): CAS-compatible trigram search + E2E validation\n\nAdd build_trigram_index_from_entries() and trigram_search_candidates()\nPyO3 functions to support NexusFS CAS backends where virtual paths\ndon't correspond to real filesystem paths. Update Python integration\nto read content through NexusFS for both indexing and verification.\n\n- Rust: build_trigram_index_from_entries accepts (path, content) pairs\n- Rust: trigram_search_candidates returns candidate paths without I/O\n- Python: build_trigram_index_for_zone reads via NexusFS CAS backend\n- Python: _try_grep_with_trigram uses candidates + Python verification\n- E2E: 7 tests validating full HTTP → NexusFS → Trigram pipeline\n- All 101 tests passing (44 Rust + 42 unit + 8 integration + 7 E2E)\n\n* chore(#954): Fix ruff lint/format and mypy issues\n\n- Fix import sorting (I001) and formatting for ruff compliance\n- Fix SIM102 nested if → combined if in _select_grep_strategy\n- Remove unused import (MagicMock) and f-string without placeholder\n- Add type: ignore[no-redef] on all try/except import lines for mypy\n- Fix integration test to mock _read() for CAS-compatible verification\n\n* fix(#954): CI fixes — cargo fmt, file size exception\n\n- Apply cargo fmt to trigram Rust files (writer.rs, builder.rs, trigram.rs)\n- Add search_service.py to file size check exceptions (2,290 lines)\n\n* chore(#954): Fix ruff format across cherry-picked files\n\nApply ruff format + isort fixes to files carried over from parent\nbranch cherry-pick that had pre-existing formatting issues.\n\n* fix(#954): Resolve Rust clippy warnings for CI\n\n- Use or_default() instead of or_insert_with(RoaringBitmap::new)\n- Remove identical if/else branches in query.rs\n\n* fix(#954): Fix test failures — read_set_cache import path + StaticPool for rebac test\n\n- Update test_read_set_cache.py import from nexus.storage to nexus.core\n  (module was moved in refactor #1519)\n- Add StaticPool to test_rebac_manager_snapshot engine fixture to ensure\n  single-connection SQLite in-memory database sharing\n\n* fix(#954): mark flaky test_bulk_check as xfail on Ubuntu CI\n\nThe Rust bulk checker has a race condition with in-memory SQLite\nthat causes intermittent failures on Ubuntu CI. Same failure\nobserved on main (run 22047883167). Passes on macOS CI and locally.",
          "timestamp": "2026-02-15T19:26:50-08:00",
          "tree_id": "b51e610c47a4540dd8e74804b1de2a4e8868ea18",
          "url": "https://github.com/nexi-lab/nexus/commit/6528279ed87ef8523e3f12b24518f76bc5dfacc7"
        },
        "date": 1771212670398,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 367.38854005930153,
            "unit": "iter/sec",
            "range": "stddev: 0.00522559846177571",
            "extra": "mean: 2.7219139710742914 msec\nrounds: 484"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 329.77285712105237,
            "unit": "iter/sec",
            "range": "stddev: 0.0009410878355485842",
            "extra": "mean: 3.032390260162989 msec\nrounds: 369"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16109.567636833039,
            "unit": "iter/sec",
            "range": "stddev: 0.00001938114377661116",
            "extra": "mean: 62.07491240879689 usec\nrounds: 17536"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15127.07490061728,
            "unit": "iter/sec",
            "range": "stddev: 0.00001754101950905762",
            "extra": "mean: 66.10663373916353 usec\nrounds: 19295"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53054.753801447674,
            "unit": "iter/sec",
            "range": "stddev: 0.00002740938368735441",
            "extra": "mean: 18.84845236945975 usec\nrounds: 45538"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 244.6865448156539,
            "unit": "iter/sec",
            "range": "stddev: 0.00041177211324609216",
            "extra": "mean: 4.086861419999195 msec\nrounds: 250"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 187.26059970462626,
            "unit": "iter/sec",
            "range": "stddev: 0.0003897739410078975",
            "extra": "mean: 5.3401516473691775 msec\nrounds: 190"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 61.306525935001,
            "unit": "iter/sec",
            "range": "stddev: 0.021855074969339618",
            "extra": "mean: 16.31147720000037 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23706.952409277295,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017179528073238714",
            "extra": "mean: 42.181718794384885 usec\nrounds: 23954"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2662.4628032242617,
            "unit": "iter/sec",
            "range": "stddev: 0.0000304718445465916",
            "extra": "mean: 375.59210171462036 usec\nrounds: 1691"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5100.654294418441,
            "unit": "iter/sec",
            "range": "stddev: 0.000023337041298070565",
            "extra": "mean: 196.05327910465974 usec\nrounds: 4020"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.01281466629862,
            "unit": "iter/sec",
            "range": "stddev: 0.004312766470919749",
            "extra": "mean: 24.382623044443918 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1449.3525448935568,
            "unit": "iter/sec",
            "range": "stddev: 0.0003095239769354842",
            "extra": "mean: 689.9632553330507 usec\nrounds: 1547"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3923.0662362390453,
            "unit": "iter/sec",
            "range": "stddev: 0.00003667372618173692",
            "extra": "mean: 254.9026551635991 usec\nrounds: 3970"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18226.25775100136,
            "unit": "iter/sec",
            "range": "stddev: 0.000004176202905327804",
            "extra": "mean: 54.86589807197583 usec\nrounds: 18258"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3956.1497131391548,
            "unit": "iter/sec",
            "range": "stddev: 0.000009912201451055008",
            "extra": "mean: 252.77102043909068 usec\nrounds: 3963"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1031.186294891336,
            "unit": "iter/sec",
            "range": "stddev: 0.000019864530951135324",
            "extra": "mean: 969.7568760893757 usec\nrounds: 1033"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 647.7568824453617,
            "unit": "iter/sec",
            "range": "stddev: 0.000046133462586859816",
            "extra": "mean: 1.5437890775083352 msec\nrounds: 658"
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
          "id": "40061de3e4889fe3e7076f8a4befdafefaa558c5",
          "message": "Merge pull request #1623 from nexi-lab/fix/encapsulate-cross-zone-sql\n\nrefactor(#1519): encapsulate cross-zone SQL into ReBACManager public method",
          "timestamp": "2026-02-16T11:35:41+08:00",
          "tree_id": "39c8ca6098cdaeb58a7c406a22f442bcc5b2082b",
          "url": "https://github.com/nexi-lab/nexus/commit/40061de3e4889fe3e7076f8a4befdafefaa558c5"
        },
        "date": 1771213114296,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 322.35380167147457,
            "unit": "iter/sec",
            "range": "stddev: 0.010597035494914971",
            "extra": "mean: 3.102181500000256 msec\nrounds: 410"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 334.37245536205273,
            "unit": "iter/sec",
            "range": "stddev: 0.0008753330773312915",
            "extra": "mean: 2.9906769650544844 msec\nrounds: 372"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14794.001987300304,
            "unit": "iter/sec",
            "range": "stddev: 0.0000198352014449867",
            "extra": "mean: 67.59496185402945 usec\nrounds: 17302"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15207.90763791953,
            "unit": "iter/sec",
            "range": "stddev: 0.0000200507970208948",
            "extra": "mean: 65.75526520864653 usec\nrounds: 18608"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 49838.039909388324,
            "unit": "iter/sec",
            "range": "stddev: 0.00002648239341620846",
            "extra": "mean: 20.06499456676311 usec\nrounds: 32209"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 237.46815683810243,
            "unit": "iter/sec",
            "range": "stddev: 0.00029378431753963164",
            "extra": "mean: 4.211090924000246 msec\nrounds: 250"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 177.14994600263097,
            "unit": "iter/sec",
            "range": "stddev: 0.0006694916691651005",
            "extra": "mean: 5.644935392670954 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 67.41926933572338,
            "unit": "iter/sec",
            "range": "stddev: 0.001478063464158274",
            "extra": "mean: 14.832554696200644 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23719.216603973695,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018433121507339752",
            "extra": "mean: 42.15990842768684 usec\nrounds: 23850"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2533.378011191571,
            "unit": "iter/sec",
            "range": "stddev: 0.000030082017456804693",
            "extra": "mean: 394.7298806504013 usec\nrounds: 1659"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5063.862351465474,
            "unit": "iter/sec",
            "range": "stddev: 0.00002520743633511089",
            "extra": "mean: 197.477721666467 usec\nrounds: 4897"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.58016699019744,
            "unit": "iter/sec",
            "range": "stddev: 0.0009427543424634081",
            "extra": "mean: 23.48511221739018 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1425.1784191521335,
            "unit": "iter/sec",
            "range": "stddev: 0.0002460361540617059",
            "extra": "mean: 701.6665328085164 usec\nrounds: 1524"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3936.515164184712,
            "unit": "iter/sec",
            "range": "stddev: 0.000006486130466555136",
            "extra": "mean: 254.0317916461295 usec\nrounds: 4022"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18473.407562699515,
            "unit": "iter/sec",
            "range": "stddev: 0.000002734221379313104",
            "extra": "mean: 54.13186476863883 usec\nrounds: 18265"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3886.6986563804253,
            "unit": "iter/sec",
            "range": "stddev: 0.000008392196426798326",
            "extra": "mean: 257.2877622911143 usec\nrounds: 3946"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1007.6498303901845,
            "unit": "iter/sec",
            "range": "stddev: 0.00008865448284823084",
            "extra": "mean: 992.4082452460472 usec\nrounds: 999"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 530.2281838816339,
            "unit": "iter/sec",
            "range": "stddev: 0.007509005830670141",
            "extra": "mean: 1.8859804710479822 msec\nrounds: 639"
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
          "id": "79e30a9b5911951bc58d71261339a5f6b4fa2355",
          "message": "feat(#1601): ConnectorProtocol + BackendFactory + OAuthMixin (#1615)\n\n* feat(#1601): ConnectorProtocol boundary + BackendFactory + OAuthMixin\n\n- Define tiered ConnectorProtocol (ContentStore → DirectoryOps → Connector)\n  as the Storage Brick boundary in core/protocols/connector.py\n- Centralize backend creation via BackendFactory, replacing 3 duplicated\n  if/elif chains (~300 LOC removed) in mount_core_service, mount_service,\n  and cli/utils\n- Replace 11 isinstance checks with capability flags (is_passthrough,\n  has_root_path) across nexus_fs_events, events_service, fastapi_server\n- Extract OAuthConnectorMixin from 4 OAuth connectors (~136 LOC deduped)\n- Add @register_connector to SlackConnectorBackend\n- Add 25 contract tests + 7 factory tests (58 total, all passing)\n\n* chore(#1601): fix ruff format for 4 files\n\n* fix(#1601): add cast for mypy type narrowing after isinstance removal\n\nAfter replacing isinstance(backend, PassthroughBackend) with capability\nflag checks, mypy lost type narrowing. Add TYPE_CHECKING import of\nPassthroughBackend and cast() calls after assert is_passthrough guards.\n\n* fix(#1601): filter extra kwargs by constructor signature in BackendFactory\n\nBackendFactory.create() now inspects the constructor signature and only\npasses extra kwargs (like session_factory) that the target backend\nactually accepts. Also converts KeyError to RuntimeError for backward\ncompatibility with existing test expectations.\n\n* fix(#1601): use StaticPool for in-memory SQLite in rebac snapshot tests\n\nIn-memory SQLite creates a new database per connection. Without\nStaticPool, the EnhancedReBACManager's internal engine.connect() gets a\nfresh empty DB without tables, causing \"no such table: rebac_tuples\"\nunder parallel test execution (pytest-xdist).",
          "timestamp": "2026-02-15T19:37:41-08:00",
          "tree_id": "b55ed7272514e14a8272c09afdc65d68185c6740",
          "url": "https://github.com/nexi-lab/nexus/commit/79e30a9b5911951bc58d71261339a5f6b4fa2355"
        },
        "date": 1771213289086,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 365.50664976145737,
            "unit": "iter/sec",
            "range": "stddev: 0.0036242656151076313",
            "extra": "mean: 2.7359283357844117 msec\nrounds: 408"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 332.0708614128493,
            "unit": "iter/sec",
            "range": "stddev: 0.00054828785623437",
            "extra": "mean: 3.0114054444444114 msec\nrounds: 360"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 18353.458310401555,
            "unit": "iter/sec",
            "range": "stddev: 0.000011758231256147905",
            "extra": "mean: 54.48564423595659 usec\nrounds: 16941"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 17722.112175461003,
            "unit": "iter/sec",
            "range": "stddev: 0.000020973989280435546",
            "extra": "mean: 56.426682671868775 usec\nrounds: 18369"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 52878.39079422665,
            "unit": "iter/sec",
            "range": "stddev: 0.0000179445309132783",
            "extra": "mean: 18.91131679652365 usec\nrounds: 47349"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 246.5449695414971,
            "unit": "iter/sec",
            "range": "stddev: 0.0002454641624780583",
            "extra": "mean: 4.056055176707573 msec\nrounds: 249"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 183.4523204137746,
            "unit": "iter/sec",
            "range": "stddev: 0.00042263428229732616",
            "extra": "mean: 5.451007639175735 msec\nrounds: 194"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 69.64770314049625,
            "unit": "iter/sec",
            "range": "stddev: 0.0017751427004850618",
            "extra": "mean: 14.357975279999664 msec\nrounds: 75"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23691.22467668922,
            "unit": "iter/sec",
            "range": "stddev: 0.000002155764841668841",
            "extra": "mean: 42.20972168585027 usec\nrounds: 24034"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2534.7320405963123,
            "unit": "iter/sec",
            "range": "stddev: 0.000036827538553798994",
            "extra": "mean: 394.5190197559279 usec\nrounds: 1721"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5056.872454234793,
            "unit": "iter/sec",
            "range": "stddev: 0.000030093677988627915",
            "extra": "mean: 197.75068662500414 usec\nrounds: 2572"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 40.88494952194512,
            "unit": "iter/sec",
            "range": "stddev: 0.0013859689439827837",
            "extra": "mean: 24.458878186048562 msec\nrounds: 43"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1438.7087565756888,
            "unit": "iter/sec",
            "range": "stddev: 0.0002846363088930669",
            "extra": "mean: 695.0677094508886 usec\nrounds: 1566"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3962.941510240412,
            "unit": "iter/sec",
            "range": "stddev: 0.0000053773962475694755",
            "extra": "mean: 252.3378145793868 usec\nrounds: 3937"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18493.462570655414,
            "unit": "iter/sec",
            "range": "stddev: 0.000003757155868069691",
            "extra": "mean: 54.073162133885866 usec\nrounds: 18238"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3928.2671464058076,
            "unit": "iter/sec",
            "range": "stddev: 0.000009589380655423261",
            "extra": "mean: 254.5651715451573 usec\nrounds: 3929"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1019.3516420335891,
            "unit": "iter/sec",
            "range": "stddev: 0.000014156180998587103",
            "extra": "mean: 981.0157346732842 usec\nrounds: 995"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 533.9620849626572,
            "unit": "iter/sec",
            "range": "stddev: 0.007199181892807832",
            "extra": "mean: 1.8727921479105307 msec\nrounds: 622"
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
          "id": "a7e401a4566c7add00fee24a5aa163b628bf7149",
          "message": "Merge pull request #1627 from nexi-lab/fix/move-memory-router-to-services\n\nrefactor(#1519): move memory_router + relationship_extractor from core/ to services/memory/",
          "timestamp": "2026-02-16T11:39:11+08:00",
          "tree_id": "4dcee467a5cc796fdb134e39005a4ec6c2179501",
          "url": "https://github.com/nexi-lab/nexus/commit/a7e401a4566c7add00fee24a5aa163b628bf7149"
        },
        "date": 1771213523279,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 290.7497618363698,
            "unit": "iter/sec",
            "range": "stddev: 0.0024680764076940196",
            "extra": "mean: 3.4393837287570577 msec\nrounds: 306"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 281.8000213484874,
            "unit": "iter/sec",
            "range": "stddev: 0.0009804591089418975",
            "extra": "mean: 3.5486157709099393 msec\nrounds: 275"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16203.03872995904,
            "unit": "iter/sec",
            "range": "stddev: 0.000021092083049945387",
            "extra": "mean: 61.716818472514255 usec\nrounds: 16901"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 17437.732785239717,
            "unit": "iter/sec",
            "range": "stddev: 0.00001954850314554606",
            "extra": "mean: 57.34690468742912 usec\nrounds: 17920"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53041.51710690504,
            "unit": "iter/sec",
            "range": "stddev: 0.000015023655975209398",
            "extra": "mean: 18.8531560661152 usec\nrounds: 44699"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 239.05208670384656,
            "unit": "iter/sec",
            "range": "stddev: 0.00046166317228600877",
            "extra": "mean: 4.183188750989092 msec\nrounds: 253"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 183.6267510342079,
            "unit": "iter/sec",
            "range": "stddev: 0.00031793746798026957",
            "extra": "mean: 5.445829621054013 msec\nrounds: 190"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 71.3921892889892,
            "unit": "iter/sec",
            "range": "stddev: 0.0012292655021227403",
            "extra": "mean: 14.007134533332342 msec\nrounds: 75"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23743.590328132133,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017439897439543767",
            "extra": "mean: 42.11662963267899 usec\nrounds: 24041"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2561.4321053918843,
            "unit": "iter/sec",
            "range": "stddev: 0.00002749542085085131",
            "extra": "mean: 390.4066002354592 usec\nrounds: 1696"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5016.00777488975,
            "unit": "iter/sec",
            "range": "stddev: 0.000029077266946545573",
            "extra": "mean: 199.3617324530522 usec\nrounds: 2878"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 35.88261048143931,
            "unit": "iter/sec",
            "range": "stddev: 0.0026129417272850787",
            "extra": "mean: 27.868652435899595 msec\nrounds: 39"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1418.265060812327,
            "unit": "iter/sec",
            "range": "stddev: 0.0003592097409457573",
            "extra": "mean: 705.0868188399417 usec\nrounds: 1518"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3904.7395313611587,
            "unit": "iter/sec",
            "range": "stddev: 0.000005726681510670899",
            "extra": "mean: 256.0990283650005 usec\nrounds: 3878"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18286.689284678927,
            "unit": "iter/sec",
            "range": "stddev: 0.000004333093434995567",
            "extra": "mean: 54.68458420397762 usec\nrounds: 18182"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3922.86937770653,
            "unit": "iter/sec",
            "range": "stddev: 0.000014500778858236203",
            "extra": "mean: 254.91544676020817 usec\nrounds: 3982"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1027.5443185729173,
            "unit": "iter/sec",
            "range": "stddev: 0.000017849924730156708",
            "extra": "mean: 973.1940335077988 usec\nrounds: 955"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 553.6283692282727,
            "unit": "iter/sec",
            "range": "stddev: 0.006562345778976135",
            "extra": "mean: 1.806265819423135 msec\nrounds: 659"
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
          "id": "ef5c69284f85ade8f3befee783f7791e20059de7",
          "message": "Merge pull request #1629 from nexi-lab/fix/extract-cache-invalidation-from-kernel\n\nrefactor(#1519): extract cache invalidation from kernel into observer pattern",
          "timestamp": "2026-02-16T11:39:14+08:00",
          "tree_id": "8f3266280c3ce67574828863ed9e61e510a8c1e9",
          "url": "https://github.com/nexi-lab/nexus/commit/ef5c69284f85ade8f3befee783f7791e20059de7"
        },
        "date": 1771214049492,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 348.7163368341459,
            "unit": "iter/sec",
            "range": "stddev: 0.006949743452386622",
            "extra": "mean: 2.8676603140495054 msec\nrounds: 484"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 334.16245131751884,
            "unit": "iter/sec",
            "range": "stddev: 0.0010779331016211118",
            "extra": "mean: 2.992556452878684 msec\nrounds: 382"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 18109.11294228358,
            "unit": "iter/sec",
            "range": "stddev: 0.00001209652358203515",
            "extra": "mean: 55.22081634739083 usec\nrounds: 17642"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 17014.157604915577,
            "unit": "iter/sec",
            "range": "stddev: 0.000016303154377554166",
            "extra": "mean: 58.77458192294452 usec\nrounds: 17016"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54555.69679086187,
            "unit": "iter/sec",
            "range": "stddev: 0.000019938753589892113",
            "extra": "mean: 18.32989144714766 usec\nrounds: 47037"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 237.39381716680782,
            "unit": "iter/sec",
            "range": "stddev: 0.0005728042877645362",
            "extra": "mean: 4.212409623530073 msec\nrounds: 255"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 187.37507919674172,
            "unit": "iter/sec",
            "range": "stddev: 0.00030757947737350644",
            "extra": "mean: 5.336889005127578 msec\nrounds: 195"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 59.78340400987359,
            "unit": "iter/sec",
            "range": "stddev: 0.02381363761942089",
            "extra": "mean: 16.727050199999383 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23718.761122947115,
            "unit": "iter/sec",
            "range": "stddev: 0.000001763104955482096",
            "extra": "mean: 42.16071804157313 usec\nrounds: 24000"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2667.652073047506,
            "unit": "iter/sec",
            "range": "stddev: 0.000027382398468680005",
            "extra": "mean: 374.86147841521455 usec\nrounds: 1691"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4999.0068946633755,
            "unit": "iter/sec",
            "range": "stddev: 0.000021325975461767753",
            "extra": "mean: 200.0397321050981 usec\nrounds: 4722"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.72439549717076,
            "unit": "iter/sec",
            "range": "stddev: 0.0007985080093690715",
            "extra": "mean: 23.966794199997643 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1391.9223399270847,
            "unit": "iter/sec",
            "range": "stddev: 0.00039381513458325084",
            "extra": "mean: 718.4308860596236 usec\nrounds: 1571"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3988.372980763174,
            "unit": "iter/sec",
            "range": "stddev: 0.000006093973195321962",
            "extra": "mean: 250.72880716603652 usec\nrounds: 4019"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18267.0744456995,
            "unit": "iter/sec",
            "range": "stddev: 0.0000030437281806430157",
            "extra": "mean: 54.743303476021225 usec\nrounds: 17204"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3959.9751358856056,
            "unit": "iter/sec",
            "range": "stddev: 0.000009701670382300039",
            "extra": "mean: 252.52683809499746 usec\nrounds: 3990"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1019.9018145149653,
            "unit": "iter/sec",
            "range": "stddev: 0.000021594882842592936",
            "extra": "mean: 980.4865387709599 usec\nrounds: 993"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 545.3556200698483,
            "unit": "iter/sec",
            "range": "stddev: 0.007417171103259338",
            "extra": "mean: 1.833665892857071 msec\nrounds: 644"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "songym@sudoprivacy.com",
            "name": "elfenlieds7",
            "username": "elfenlieds7"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "5218b9d46626d8000754e63f35a3b15120221cee",
          "message": "fix(#1588): fix PermissionEnforcer import missed by PR #1630\n\n* fix(#1588): fix PermissionEnforcer import in memory_permission_enforcer.py\n\nThe PR #1630 auto-merged before the CI fix commit was picked up.\nThis corrects the import path: core.permissions → services.permissions.enforcer.\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* chore: trigger CI\n\n---------\n\nCo-authored-by: Claude Opus 4.6 <noreply@anthropic.com>",
          "timestamp": "2026-02-16T11:50:22+08:00",
          "tree_id": "a8369f73a59e07cff6099e10df7495a62e9e63ac",
          "url": "https://github.com/nexi-lab/nexus/commit/5218b9d46626d8000754e63f35a3b15120221cee"
        },
        "date": 1771214227777,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 336.57170198632053,
            "unit": "iter/sec",
            "range": "stddev: 0.0025332510335291476",
            "extra": "mean: 2.971135107611167 msec\nrounds: 381"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 309.7382100661441,
            "unit": "iter/sec",
            "range": "stddev: 0.0006360605859459497",
            "extra": "mean: 3.228532894880653 msec\nrounds: 371"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 18292.206397552058,
            "unit": "iter/sec",
            "range": "stddev: 0.000012989466961113979",
            "extra": "mean: 54.66809078503642 usec\nrounds: 16864"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 17573.59997773219,
            "unit": "iter/sec",
            "range": "stddev: 0.000012782617900158112",
            "extra": "mean: 56.903537195971076 usec\nrounds: 17139"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 51909.426889854374,
            "unit": "iter/sec",
            "range": "stddev: 0.000016028472761386225",
            "extra": "mean: 19.26432364822445 usec\nrounds: 46773"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 247.42856209424195,
            "unit": "iter/sec",
            "range": "stddev: 0.00021964333091358183",
            "extra": "mean: 4.041570591268742 msec\nrounds: 252"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 183.6460685662643,
            "unit": "iter/sec",
            "range": "stddev: 0.00048015749163479146",
            "extra": "mean: 5.445256780104573 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 60.14168682847585,
            "unit": "iter/sec",
            "range": "stddev: 0.02269365973249944",
            "extra": "mean: 16.627401935898487 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23740.232111297326,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017519706176761412",
            "extra": "mean: 42.12258731556914 usec\nrounds: 23982"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2643.7901990144446,
            "unit": "iter/sec",
            "range": "stddev: 0.000024928690440009252",
            "extra": "mean: 378.2448396899199 usec\nrounds: 1678"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4986.065633033748,
            "unit": "iter/sec",
            "range": "stddev: 0.000032051966817315146",
            "extra": "mean: 200.5589323523515 usec\nrounds: 4213"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 37.73625284609641,
            "unit": "iter/sec",
            "range": "stddev: 0.0049943321010983135",
            "extra": "mean: 26.499716441862986 msec\nrounds: 43"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1455.3499150504733,
            "unit": "iter/sec",
            "range": "stddev: 0.0002441631289429234",
            "extra": "mean: 687.1199768925117 usec\nrounds: 1255"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3931.6112504235957,
            "unit": "iter/sec",
            "range": "stddev: 0.000008016067433485678",
            "extra": "mean: 254.34864647217066 usec\nrounds: 4011"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18463.2650547077,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026734837249546897",
            "extra": "mean: 54.16160126808251 usec\nrounds: 17350"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3912.2394144197892,
            "unit": "iter/sec",
            "range": "stddev: 0.000015540586495185368",
            "extra": "mean: 255.60807866568322 usec\nrounds: 4017"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1013.2923471850914,
            "unit": "iter/sec",
            "range": "stddev: 0.000019892000578318862",
            "extra": "mean: 986.8820215389791 usec\nrounds: 975"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 662.4839041378802,
            "unit": "iter/sec",
            "range": "stddev: 0.000023429199499528045",
            "extra": "mean: 1.5094706358207215 msec\nrounds: 670"
          }
        ]
      }
    ]
  }
}