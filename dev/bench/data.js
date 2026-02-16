window.BENCHMARK_DATA = {
  "lastUpdate": 1771252121527,
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
          "distinct": false,
          "id": "1cb4f5b122518fda029b650e1d5a575ba778f035",
          "message": "fix(#1291): Fix circular imports via Protocol — extract zero-dep leaf module (#1614)\n\n* fix(#1291): extract zero-dependency leaf module to break circular import hub\n\n- Create `core/types.py` with Permission, OperationContext, ContextIdentity,\n  and extract_context_identity() — zero runtime nexus.* imports\n- Re-export from permissions.py and subsystem.py for backward compatibility\n  (72+ downstream files unchanged)\n- Move protocol file imports (search.py, context_manifest.py) to TYPE_CHECKING\n- Remove 6 redundant deferred OperationContext imports in nexus_fs.py\n- Consolidate 7 EntityRegistry deferred imports into _ensure_entity_registry()\n- Add AST-based import cycle detection test (CI guardrail)\n- Add re-export identity tests, protocol import cleanliness tests,\n  startup-time benchmarks, and factory smoke tests (46 new tests)\n\n* style(#1291): apply ruff format to test files\n\n* merge: resolve conflicts with origin/main, add PermissionEnforcer re-export\n\nConflicts resolved:\n- nexus_fs.py: use services.memory.memory_api path, keep EntityRegistry TYPE_CHECKING\n- permissions.py: drop stale TYPE_CHECKING imports, remove unused uuid\n\nFix: add lazy __getattr__ re-export for PermissionEnforcer (moved to\nservices/permissions/enforcer.py) to maintain backward compatibility.\nUses __getattr__ to avoid circular import since enforcer.py imports\nfrom this module.",
          "timestamp": "2026-02-15T20:25:16-08:00",
          "tree_id": "a045fea8945f1e80696382fa4751223ca5e30a6a",
          "url": "https://github.com/nexi-lab/nexus/commit/1cb4f5b122518fda029b650e1d5a575ba778f035"
        },
        "date": 1771216680324,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 309.77542738289793,
            "unit": "iter/sec",
            "range": "stddev: 0.008854167132473556",
            "extra": "mean: 3.228145009590931 msec\nrounds: 417"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 316.4906355820111,
            "unit": "iter/sec",
            "range": "stddev: 0.0010326029850446393",
            "extra": "mean: 3.1596511478484914 msec\nrounds: 372"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 13393.883047512549,
            "unit": "iter/sec",
            "range": "stddev: 0.00001651721803267741",
            "extra": "mean: 74.66094757231103 usec\nrounds: 16518"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 17101.836407720533,
            "unit": "iter/sec",
            "range": "stddev: 0.000009431262621856472",
            "extra": "mean: 58.47325258874276 usec\nrounds: 15163"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53045.79689699658,
            "unit": "iter/sec",
            "range": "stddev: 0.000014154613291585645",
            "extra": "mean: 18.85163497386575 usec\nrounds: 45268"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 230.2783336661459,
            "unit": "iter/sec",
            "range": "stddev: 0.0007769983877190351",
            "extra": "mean: 4.342570940476689 msec\nrounds: 252"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 185.5979798815373,
            "unit": "iter/sec",
            "range": "stddev: 0.00033974354454854687",
            "extra": "mean: 5.387989678757688 msec\nrounds: 193"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 61.441498048754006,
            "unit": "iter/sec",
            "range": "stddev: 0.01988314916409248",
            "extra": "mean: 16.275644828947645 msec\nrounds: 76"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23754.664929477964,
            "unit": "iter/sec",
            "range": "stddev: 0.000004168931009597231",
            "extra": "mean: 42.09699454691387 usec\nrounds: 23839"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2653.9149925876213,
            "unit": "iter/sec",
            "range": "stddev: 0.000029375139086476385",
            "extra": "mean: 376.80182025158973 usec\nrounds: 1669"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4998.903109987136,
            "unit": "iter/sec",
            "range": "stddev: 0.00005793160613459497",
            "extra": "mean: 200.04388522796822 usec\nrounds: 3581"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.10990559946031,
            "unit": "iter/sec",
            "range": "stddev: 0.0009749148226896443",
            "extra": "mean: 24.3250376136385 msec\nrounds: 44"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1477.939656382007,
            "unit": "iter/sec",
            "range": "stddev: 0.00016831903522977624",
            "extra": "mean: 676.6176113360391 usec\nrounds: 1482"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3957.9597657089753,
            "unit": "iter/sec",
            "range": "stddev: 0.000005749024531207383",
            "extra": "mean: 252.6554232975821 usec\nrounds: 4009"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18271.315747927358,
            "unit": "iter/sec",
            "range": "stddev: 0.000003820378560196829",
            "extra": "mean: 54.730595967804724 usec\nrounds: 17360"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3927.4231210872917,
            "unit": "iter/sec",
            "range": "stddev: 0.000009030897736959002",
            "extra": "mean: 254.61987903232435 usec\nrounds: 3968"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1032.058862481676,
            "unit": "iter/sec",
            "range": "stddev: 0.000011979046727781178",
            "extra": "mean: 968.9369825238576 usec\nrounds: 1030"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 651.9425737289235,
            "unit": "iter/sec",
            "range": "stddev: 0.000045593933219432946",
            "extra": "mean: 1.5338774307686156 msec\nrounds: 650"
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
          "id": "d16ffa752f3e489a728e31d17ef8c660dfb42017",
          "message": "Merge pull request #1637 from nexi-lab/fix/move-nexus-filesystem-protocol-to-core\n\nfix(#1519): replace skills.protocols import with core.filesystem in scoped_filesystem",
          "timestamp": "2026-02-16T13:00:18+08:00",
          "tree_id": "512a940a29a1c9feefa6b3497e79e4e06a40caa4",
          "url": "https://github.com/nexi-lab/nexus/commit/d16ffa752f3e489a728e31d17ef8c660dfb42017"
        },
        "date": 1771219456074,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 396.17028414194885,
            "unit": "iter/sec",
            "range": "stddev: 0.004537153501698506",
            "extra": "mean: 2.5241671069950753 msec\nrounds: 486"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 330.6187846286291,
            "unit": "iter/sec",
            "range": "stddev: 0.0010930699183072479",
            "extra": "mean: 3.024631528796103 msec\nrounds: 382"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15843.724833642606,
            "unit": "iter/sec",
            "range": "stddev: 0.00001738720874673831",
            "extra": "mean: 63.11647106345835 usec\nrounds: 15223"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16156.75673703426,
            "unit": "iter/sec",
            "range": "stddev: 0.000016866996512229645",
            "extra": "mean: 61.8936099785309 usec\nrounds: 17758"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54114.88185883534,
            "unit": "iter/sec",
            "range": "stddev: 0.000015366238001936472",
            "extra": "mean: 18.47920508463108 usec\nrounds: 38272"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 243.06155993914444,
            "unit": "iter/sec",
            "range": "stddev: 0.00035532425185452526",
            "extra": "mean: 4.114184078512336 msec\nrounds: 242"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 183.44117451008083,
            "unit": "iter/sec",
            "range": "stddev: 0.00041918435675979317",
            "extra": "mean: 5.451338842932703 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 58.155833732351326,
            "unit": "iter/sec",
            "range": "stddev: 0.022496316428142708",
            "extra": "mean: 17.1951795000011 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23705.575360579478,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017444319877690967",
            "extra": "mean: 42.184169115883265 usec\nrounds: 23954"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2566.4286012343923,
            "unit": "iter/sec",
            "range": "stddev: 0.000030108864877672813",
            "extra": "mean: 389.64653040377715 usec\nrounds: 1661"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4987.520150418705,
            "unit": "iter/sec",
            "range": "stddev: 0.000023916567534818308",
            "extra": "mean: 200.50044307410957 usec\nrounds: 4866"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 43.060062478854945,
            "unit": "iter/sec",
            "range": "stddev: 0.0008214425340229314",
            "extra": "mean: 23.22337550000211 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1411.0649145588293,
            "unit": "iter/sec",
            "range": "stddev: 0.0003476355045898495",
            "extra": "mean: 708.6846180373288 usec\nrounds: 1508"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3970.3299357635156,
            "unit": "iter/sec",
            "range": "stddev: 0.000005615242420299157",
            "extra": "mean: 251.86823669043383 usec\nrounds: 4001"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18147.164949492097,
            "unit": "iter/sec",
            "range": "stddev: 0.000004705290406947631",
            "extra": "mean: 55.10502620013866 usec\nrounds: 17290"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3938.2047170094647,
            "unit": "iter/sec",
            "range": "stddev: 0.000012929962523655124",
            "extra": "mean: 253.9228079436574 usec\nrounds: 3978"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1008.4481850979217,
            "unit": "iter/sec",
            "range": "stddev: 0.000042211791540415955",
            "extra": "mean: 991.6225888223485 usec\nrounds: 1002"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 561.337630700803,
            "unit": "iter/sec",
            "range": "stddev: 0.00639496708775524",
            "extra": "mean: 1.781459045871463 msec\nrounds: 654"
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
          "id": "39d50ebd55a4da92314859ca3abc86cf48b6aef8",
          "message": "Merge pull request #1639 from nexi-lab/fix/move-workspace-manager-to-services\n\nrefactor(#1519): move workspace_manager.py from core/ to services/",
          "timestamp": "2026-02-16T13:00:43+08:00",
          "tree_id": "b7f6c47b3ba8a764d38f6dbd6a88ae46739cf03a",
          "url": "https://github.com/nexi-lab/nexus/commit/39d50ebd55a4da92314859ca3abc86cf48b6aef8"
        },
        "date": 1771220532852,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 403.8768083929145,
            "unit": "iter/sec",
            "range": "stddev: 0.005492571774190906",
            "extra": "mean: 2.476002531512388 msec\nrounds: 476"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 346.00644581790283,
            "unit": "iter/sec",
            "range": "stddev: 0.0007413967028643488",
            "extra": "mean: 2.890119568831046 msec\nrounds: 385"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16434.47797150651,
            "unit": "iter/sec",
            "range": "stddev: 0.000014461740018682887",
            "extra": "mean: 60.84768872694119 usec\nrounds: 16606"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15533.822587269413,
            "unit": "iter/sec",
            "range": "stddev: 0.000015487043370058876",
            "extra": "mean: 64.37565476121377 usec\nrounds: 18115"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53893.635109993724,
            "unit": "iter/sec",
            "range": "stddev: 0.000013833909711467328",
            "extra": "mean: 18.555066808150148 usec\nrounds: 46686"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 247.49001094640735,
            "unit": "iter/sec",
            "range": "stddev: 0.0002447219364289205",
            "extra": "mean: 4.0405671169352555 msec\nrounds: 248"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 173.35710292191354,
            "unit": "iter/sec",
            "range": "stddev: 0.0008269641878504181",
            "extra": "mean: 5.768439730158834 msec\nrounds: 189"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 62.20488229406149,
            "unit": "iter/sec",
            "range": "stddev: 0.02206493410754427",
            "extra": "mean: 16.075908564101038 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23758.800253811176,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017848344713666243",
            "extra": "mean: 42.08966737870482 usec\nrounds: 23874"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2492.960473357725,
            "unit": "iter/sec",
            "range": "stddev: 0.00002735638213266773",
            "extra": "mean: 401.1295047342318 usec\nrounds: 1690"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5033.573455640828,
            "unit": "iter/sec",
            "range": "stddev: 0.000057848227997562154",
            "extra": "mean: 198.66601904445423 usec\nrounds: 3203"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 43.872701502494806,
            "unit": "iter/sec",
            "range": "stddev: 0.0007106327072307931",
            "extra": "mean: 22.793216869563764 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1478.644078141206,
            "unit": "iter/sec",
            "range": "stddev: 0.00023222507104914795",
            "extra": "mean: 676.2952726643276 usec\nrounds: 1445"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3962.5099640946155,
            "unit": "iter/sec",
            "range": "stddev: 0.000019011536158370283",
            "extra": "mean: 252.3652960020979 usec\nrounds: 4027"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18363.986135071336,
            "unit": "iter/sec",
            "range": "stddev: 0.000002576304119847087",
            "extra": "mean: 54.45440835365319 usec\nrounds: 17573"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3940.0211354291264,
            "unit": "iter/sec",
            "range": "stddev: 0.000008179775904374055",
            "extra": "mean: 253.80574510321384 usec\nrounds: 3982"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 996.8095285895511,
            "unit": "iter/sec",
            "range": "stddev: 0.000014653970573971584",
            "extra": "mean: 1.0032006830983682 msec\nrounds: 994"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 666.1362886201173,
            "unit": "iter/sec",
            "range": "stddev: 0.00005224414189393203",
            "extra": "mean: 1.501194300751085 msec\nrounds: 665"
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
          "id": "ea9779b726b6c989f535bbed8b15f9880b5f8867",
          "message": "refactor(#1589): Extract HeartbeatBuffer from AgentRegistry (#1636)\n\n* chore: fix pre-existing mypy errors in async_local.py and nexus_fs_events.py\n\nAdd type: ignore comments for async override pattern and mixin attr access\nthat were hidden by ruff format CI failure on main.\n\n* chore: remove unused type: ignore comments after main merge\n\nMain fixed the async override type issues in Backend, making the\ntype: ignore[override] comments unnecessary.\n\n* refactor(#1589): extract HeartbeatBuffer from AgentRegistry (SRP)\n\nExtract heartbeat buffering (~160 LOC) into a standalone HeartbeatBuffer\nclass composed via DI. AgentRegistry delegates heartbeat(), flush_heartbeats(),\nand detect_stale() to the new class while keeping the public API 100%\nbackward-compatible.\n\n- HeartbeatBuffer accepts flush_callback (no SQLAlchemy dependency)\n- Separate locks by owner (buffer lock vs cache/known-agents lock)\n- _restore_buffer() extracted as named method (fixes 5-level nesting)\n- stats() method for observability (buffer_size, total_flushed, etc.)\n- Fixed f-string logging to %-style in touched code\n- 30 dedicated HeartbeatBuffer tests (all mock-based, no DB)\n- 114 total tests pass (unit + integration + async)\n\n* fix: sort imports in agent_registry.py after path relocation\n\nruff isort fix — nexus.core before nexus.services",
          "timestamp": "2026-02-15T21:00:56-08:00",
          "tree_id": "ef9ac0108f4f8a95061e21f3d6dd414771237085",
          "url": "https://github.com/nexi-lab/nexus/commit/ea9779b726b6c989f535bbed8b15f9880b5f8867"
        },
        "date": 1771220674352,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 325.8421642047914,
            "unit": "iter/sec",
            "range": "stddev: 0.0032210587106713796",
            "extra": "mean: 3.068970531915266 msec\nrounds: 423"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 324.08381310467547,
            "unit": "iter/sec",
            "range": "stddev: 0.0010352611585146528",
            "extra": "mean: 3.0856215570291723 msec\nrounds: 377"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 17181.023671071078,
            "unit": "iter/sec",
            "range": "stddev: 0.000013253766372887844",
            "extra": "mean: 58.20374962196063 usec\nrounds: 15209"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16519.93194643873,
            "unit": "iter/sec",
            "range": "stddev: 0.000014531006384936264",
            "extra": "mean: 60.53293701464516 usec\nrounds: 17512"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54335.234185116824,
            "unit": "iter/sec",
            "range": "stddev: 0.00001382944434226787",
            "extra": "mean: 18.404264102240937 usec\nrounds: 46039"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 235.1125508088327,
            "unit": "iter/sec",
            "range": "stddev: 0.0006073008503227276",
            "extra": "mean: 4.253282083665063 msec\nrounds: 251"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 187.04832433573955,
            "unit": "iter/sec",
            "range": "stddev: 0.00034310607906658176",
            "extra": "mean: 5.34621202061701 msec\nrounds: 194"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 60.5505664118489,
            "unit": "iter/sec",
            "range": "stddev: 0.02234763499522386",
            "extra": "mean: 16.51512214102615 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23730.791184381622,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016974445252615999",
            "extra": "mean: 42.13934513309224 usec\nrounds: 24040"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2585.386821763156,
            "unit": "iter/sec",
            "range": "stddev: 0.000029609462893256658",
            "extra": "mean: 386.7893158510145 usec\nrounds: 1697"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5054.3500244180705,
            "unit": "iter/sec",
            "range": "stddev: 0.00005287128274556731",
            "extra": "mean: 197.8493763132549 usec\nrounds: 4568"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 40.93662573951199,
            "unit": "iter/sec",
            "range": "stddev: 0.002141195763777351",
            "extra": "mean: 24.42800259999937 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1458.7235478227935,
            "unit": "iter/sec",
            "range": "stddev: 0.00019428054333040916",
            "extra": "mean: 685.5308543503958 usec\nrounds: 1586"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3940.4676459070906,
            "unit": "iter/sec",
            "range": "stddev: 0.000005864907505776397",
            "extra": "mean: 253.77698533794236 usec\nrounds: 4024"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18443.680061763225,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026852256890717285",
            "extra": "mean: 54.21911444197972 usec\nrounds: 18481"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3928.0590718629287,
            "unit": "iter/sec",
            "range": "stddev: 0.000006423849585513628",
            "extra": "mean: 254.57865620277906 usec\nrounds: 3950"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1007.1844881395294,
            "unit": "iter/sec",
            "range": "stddev: 0.00003059230581286651",
            "extra": "mean: 992.8667605348046 usec\nrounds: 973"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 649.8242506425364,
            "unit": "iter/sec",
            "range": "stddev: 0.000018200236520296446",
            "extra": "mean: 1.5388776257753 msec\nrounds: 644"
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
          "id": "5285f5ee776811562af3140c7737778fc81827f8",
          "message": "Merge pull request #1640 from nexi-lab/fix/move-mount-manager-to-services\n\nrefactor(#1519): move mount_manager.py from core/ to services/",
          "timestamp": "2026-02-16T13:01:01+08:00",
          "tree_id": "4980e42ed6af47fec0807db3e68fbf47c7f2a7a2",
          "url": "https://github.com/nexi-lab/nexus/commit/5285f5ee776811562af3140c7737778fc81827f8"
        },
        "date": 1771220743328,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 334.1178650463574,
            "unit": "iter/sec",
            "range": "stddev: 0.008687433673849883",
            "extra": "mean: 2.9929557937922726 msec\nrounds: 451"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 327.7701342376814,
            "unit": "iter/sec",
            "range": "stddev: 0.0010025192990077928",
            "extra": "mean: 3.050918602836625 msec\nrounds: 282"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 17022.420535731955,
            "unit": "iter/sec",
            "range": "stddev: 0.000014099012476121273",
            "extra": "mean: 58.74605188497657 usec\nrounds: 16286"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15906.68604969646,
            "unit": "iter/sec",
            "range": "stddev: 0.00001916599023052618",
            "extra": "mean: 62.866645942200044 usec\nrounds: 17435"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 48849.142682800106,
            "unit": "iter/sec",
            "range": "stddev: 0.00002238257091538338",
            "extra": "mean: 20.471188337806844 usec\nrounds: 45206"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 240.31282677997683,
            "unit": "iter/sec",
            "range": "stddev: 0.00028530468777859715",
            "extra": "mean: 4.16124271600188 msec\nrounds: 250"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.63303911541124,
            "unit": "iter/sec",
            "range": "stddev: 0.00033497466103210534",
            "extra": "mean: 5.416148728261553 msec\nrounds: 184"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 58.31342978226716,
            "unit": "iter/sec",
            "range": "stddev: 0.023195541565392993",
            "extra": "mean: 17.148708346153487 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23726.078166663763,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017552213165867564",
            "extra": "mean: 42.14771581613712 usec\nrounds: 23988"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2532.129031928593,
            "unit": "iter/sec",
            "range": "stddev: 0.00004276198587627337",
            "extra": "mean: 394.92458219569926 usec\nrounds: 1685"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4872.157010742192,
            "unit": "iter/sec",
            "range": "stddev: 0.00005457275106405548",
            "extra": "mean: 205.24790104161005 usec\nrounds: 2688"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 39.79695265765868,
            "unit": "iter/sec",
            "range": "stddev: 0.0024336903405291466",
            "extra": "mean: 25.127552066666997 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1411.2218315625469,
            "unit": "iter/sec",
            "range": "stddev: 0.0003466398194555073",
            "extra": "mean: 708.6058177634413 usec\nrounds: 1520"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3968.2894253742716,
            "unit": "iter/sec",
            "range": "stddev: 0.000006547152274446734",
            "extra": "mean: 251.9977483511512 usec\nrounds: 3942"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 17982.408183056683,
            "unit": "iter/sec",
            "range": "stddev: 0.0000031818291483081527",
            "extra": "mean: 55.60990440324985 usec\nrounds: 17260"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3941.996599263912,
            "unit": "iter/sec",
            "range": "stddev: 0.00000967134520235887",
            "extra": "mean: 253.67855471684825 usec\nrounds: 3975"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1008.8293748704898,
            "unit": "iter/sec",
            "range": "stddev: 0.00005355067906716482",
            "extra": "mean: 991.2479006951762 usec\nrounds: 1007"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 639.3151430856216,
            "unit": "iter/sec",
            "range": "stddev: 0.000026685848501954512",
            "extra": "mean: 1.564173805071395 msec\nrounds: 631"
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
          "id": "b84582bd2cf2b378bb466cb548612273cbadad5d",
          "message": "fix(#1588): fix PermissionEnforcer import in 3 remaining test files (#1643)\n\nPR #1630 moved PermissionEnforcer from nexus.core.permissions to\nnexus.services.permissions.enforcer. Commit 5218b9d fixed the\nproduction import but missed 3 test files that still imported from\nthe old location, causing collection errors on both macOS and Ubuntu CI.",
          "timestamp": "2026-02-15T21:26:16-08:00",
          "tree_id": "9aa2bd8a8f68c70caec0b3c6830a16978edf0c10",
          "url": "https://github.com/nexi-lab/nexus/commit/b84582bd2cf2b378bb466cb548612273cbadad5d"
        },
        "date": 1771222135942,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 323.0307513282698,
            "unit": "iter/sec",
            "range": "stddev: 0.008234031555341779",
            "extra": "mean: 3.0956805068498934 msec\nrounds: 438"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 314.5856271269786,
            "unit": "iter/sec",
            "range": "stddev: 0.0011973364314735834",
            "extra": "mean: 3.178784768817052 msec\nrounds: 372"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16893.881850804268,
            "unit": "iter/sec",
            "range": "stddev: 0.000015178287476612295",
            "extra": "mean: 59.19302673188714 usec\nrounds: 16310"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15526.321421014958,
            "unit": "iter/sec",
            "range": "stddev: 0.000016667531678024396",
            "extra": "mean: 64.40675630007857 usec\nrounds: 17698"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 48576.927510413276,
            "unit": "iter/sec",
            "range": "stddev: 0.000023982295643464054",
            "extra": "mean: 20.585904692832482 usec\nrounds: 45600"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 240.74779384852016,
            "unit": "iter/sec",
            "range": "stddev: 0.00029260258336806944",
            "extra": "mean: 4.153724460001513 msec\nrounds: 250"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 181.7285198554959,
            "unit": "iter/sec",
            "range": "stddev: 0.0005142360646940814",
            "extra": "mean: 5.502713612564306 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 66.81009637553245,
            "unit": "iter/sec",
            "range": "stddev: 0.0014629352232409254",
            "extra": "mean: 14.967797597224022 msec\nrounds: 72"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23725.651492961122,
            "unit": "iter/sec",
            "range": "stddev: 0.000001773565764567472",
            "extra": "mean: 42.14847378571155 usec\nrounds: 23365"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2527.136332431626,
            "unit": "iter/sec",
            "range": "stddev: 0.000034624834252866176",
            "extra": "mean: 395.70480910216423 usec\nrounds: 1692"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5046.889813939144,
            "unit": "iter/sec",
            "range": "stddev: 0.00003094672638933132",
            "extra": "mean: 198.14183326096648 usec\nrounds: 2297"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 39.86278488879795,
            "unit": "iter/sec",
            "range": "stddev: 0.0009491616074528833",
            "extra": "mean: 25.086054644441447 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1431.6155228287546,
            "unit": "iter/sec",
            "range": "stddev: 0.000254483856506634",
            "extra": "mean: 698.5115654684172 usec\nrounds: 1558"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3939.2177434307896,
            "unit": "iter/sec",
            "range": "stddev: 0.00002161113562327037",
            "extra": "mean: 253.85750804652608 usec\nrounds: 4039"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18330.710360372224,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027416920518349365",
            "extra": "mean: 54.553259548621995 usec\nrounds: 18301"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3882.439386002398,
            "unit": "iter/sec",
            "range": "stddev: 0.000016821616159527548",
            "extra": "mean: 257.5700224980621 usec\nrounds: 3867"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1004.5782195131386,
            "unit": "iter/sec",
            "range": "stddev: 0.000041091503381092475",
            "extra": "mean: 995.4426450581842 usec\nrounds: 941"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 548.6860062933886,
            "unit": "iter/sec",
            "range": "stddev: 0.007209943374755706",
            "extra": "mean: 1.8225360015201275 msec\nrounds: 657"
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
          "id": "a3da7442cddab623626d38d1e6f279d07c1a1ac5",
          "message": "Merge pull request #1652 from nexi-lab/fix/proxy-transport-server-import\n\nrefactor(#342): extract RPC codec to core.rpc_codec",
          "timestamp": "2026-02-16T13:37:23+08:00",
          "tree_id": "3a3e44b60f0ba02c9c898623717a72d64c789d00",
          "url": "https://github.com/nexi-lab/nexus/commit/a3da7442cddab623626d38d1e6f279d07c1a1ac5"
        },
        "date": 1771222982474,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 288.54439550551166,
            "unit": "iter/sec",
            "range": "stddev: 0.007960108393893687",
            "extra": "mean: 3.4656711950618995 msec\nrounds: 405"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 309.64653554743967,
            "unit": "iter/sec",
            "range": "stddev: 0.0007883062380601672",
            "extra": "mean: 3.2294887402245593 msec\nrounds: 358"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 13092.139739541883,
            "unit": "iter/sec",
            "range": "stddev: 0.00001650242822986337",
            "extra": "mean: 76.38170840628315 usec\nrounds: 16595"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15475.46262843428,
            "unit": "iter/sec",
            "range": "stddev: 0.000017836456306909453",
            "extra": "mean: 64.61842363036189 usec\nrounds: 18070"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54629.76973569182,
            "unit": "iter/sec",
            "range": "stddev: 0.000015464495616326147",
            "extra": "mean: 18.305037799686346 usec\nrounds: 46773"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 241.14514276077557,
            "unit": "iter/sec",
            "range": "stddev: 0.00027382223954199264",
            "extra": "mean: 4.146880126016202 msec\nrounds: 246"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 181.35834737762562,
            "unit": "iter/sec",
            "range": "stddev: 0.0004343216837923509",
            "extra": "mean: 5.513945260638006 msec\nrounds: 188"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 66.85717958617788,
            "unit": "iter/sec",
            "range": "stddev: 0.0014407747514696932",
            "extra": "mean: 14.95725674025802 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23722.5218857848,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018357965046515992",
            "extra": "mean: 42.154034247059876 usec\nrounds: 23856"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2672.817273262358,
            "unit": "iter/sec",
            "range": "stddev: 0.000033499182077877636",
            "extra": "mean: 374.1370612961622 usec\nrounds: 1713"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5011.5363899244285,
            "unit": "iter/sec",
            "range": "stddev: 0.00002607187672051843",
            "extra": "mean: 199.53960665844423 usec\nrounds: 4866"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 37.751929676072976,
            "unit": "iter/sec",
            "range": "stddev: 0.0017631349509122033",
            "extra": "mean: 26.488712195122467 msec\nrounds: 41"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1445.9658182585943,
            "unit": "iter/sec",
            "range": "stddev: 0.00022770763842541036",
            "extra": "mean: 691.5792803486324 usec\nrounds: 1491"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3971.751290135884,
            "unit": "iter/sec",
            "range": "stddev: 0.000006287106044699887",
            "extra": "mean: 251.778101635812 usec\nrounds: 4034"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18328.80774381403,
            "unit": "iter/sec",
            "range": "stddev: 0.0000032571320735442584",
            "extra": "mean: 54.55892243386642 usec\nrounds: 18423"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3953.08381929103,
            "unit": "iter/sec",
            "range": "stddev: 0.000027704697620656932",
            "extra": "mean: 252.96706209972194 usec\nrounds: 4058"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 959.9774143495251,
            "unit": "iter/sec",
            "range": "stddev: 0.000025824822759097497",
            "extra": "mean: 1.0416911742424626 msec\nrounds: 924"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 537.0564853902256,
            "unit": "iter/sec",
            "range": "stddev: 0.007892929087345076",
            "extra": "mean: 1.8620015346679957 msec\nrounds: 649"
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
          "id": "17adeb17b2799eed9262d1292b0f6805a9ca586d",
          "message": "Merge pull request #1653 from nexi-lab/fix/witness-multizone\n\nfeat(#158): multi-zone witness — WitnessZoneRegistry + static bootstrap",
          "timestamp": "2026-02-16T13:37:49+08:00",
          "tree_id": "74db542df70fe5a37481c612373a74bdb97a4d65",
          "url": "https://github.com/nexi-lab/nexus/commit/17adeb17b2799eed9262d1292b0f6805a9ca586d"
        },
        "date": 1771223102096,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 393.8129284267041,
            "unit": "iter/sec",
            "range": "stddev: 0.0042885912128831066",
            "extra": "mean: 2.5392767169809116 msec\nrounds: 424"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 335.8652796921512,
            "unit": "iter/sec",
            "range": "stddev: 0.0009722711298896484",
            "extra": "mean: 2.977384268229762 msec\nrounds: 384"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 17654.137636833537,
            "unit": "iter/sec",
            "range": "stddev: 0.000011557239211779492",
            "extra": "mean: 56.643944925047094 usec\nrounds: 16414"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16794.552131220138,
            "unit": "iter/sec",
            "range": "stddev: 0.000013765494250230932",
            "extra": "mean: 59.54311804129958 usec\nrounds: 17460"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53595.50386808381,
            "unit": "iter/sec",
            "range": "stddev: 0.000019819947793368265",
            "extra": "mean: 18.658281531624915 usec\nrounds: 45288"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 250.00996224431768,
            "unit": "iter/sec",
            "range": "stddev: 0.00023869519481237723",
            "extra": "mean: 3.9998406104424276 msec\nrounds: 249"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.44940858423757,
            "unit": "iter/sec",
            "range": "stddev: 0.0005169594797112522",
            "extra": "mean: 5.421540831578771 msec\nrounds: 190"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 58.387602813603976,
            "unit": "iter/sec",
            "range": "stddev: 0.023166469914379664",
            "extra": "mean: 17.126923384616262 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23731.24955085449,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018533476517260456",
            "extra": "mean: 42.13853121627947 usec\nrounds: 23994"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2587.7851390823,
            "unit": "iter/sec",
            "range": "stddev: 0.00003535265529500185",
            "extra": "mean: 386.43084578290285 usec\nrounds: 1660"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5137.406953575528,
            "unit": "iter/sec",
            "range": "stddev: 0.000015634824695167143",
            "extra": "mean: 194.65072730981936 usec\nrounds: 4892"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 44.14360988024107,
            "unit": "iter/sec",
            "range": "stddev: 0.0005062642376549463",
            "extra": "mean: 22.653335391304406 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1454.8983488330312,
            "unit": "iter/sec",
            "range": "stddev: 0.0002912924649952558",
            "extra": "mean: 687.333242767164 usec\nrounds: 1590"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3982.3349602123217,
            "unit": "iter/sec",
            "range": "stddev: 0.000008020542301221832",
            "extra": "mean: 251.10896245319455 usec\nrounds: 3995"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18459.420193436163,
            "unit": "iter/sec",
            "range": "stddev: 0.0000029279674051806505",
            "extra": "mean: 54.17288243731415 usec\nrounds: 17429"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3956.86324125888,
            "unit": "iter/sec",
            "range": "stddev: 0.000010536986798782958",
            "extra": "mean: 252.72543907326173 usec\nrounds: 4013"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1029.9493047774965,
            "unit": "iter/sec",
            "range": "stddev: 0.00003804763489315165",
            "extra": "mean: 970.9215738691464 usec\nrounds: 995"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 646.6244734457708,
            "unit": "iter/sec",
            "range": "stddev: 0.00002523841230839811",
            "extra": "mean: 1.5464926569684885 msec\nrounds: 653"
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
          "id": "55ca4dbd195e033e7570968768344028f1cced2e",
          "message": "Merge pull request #1654 from nexi-lab/fix/cache-settings-backward-compat\n\nrefactor(#387): remove backward-compat aliases from CacheSettings",
          "timestamp": "2026-02-16T13:40:18+08:00",
          "tree_id": "df7aec3ef0843670643d1b840ea872822a355bbe",
          "url": "https://github.com/nexi-lab/nexus/commit/55ca4dbd195e033e7570968768344028f1cced2e"
        },
        "date": 1771223672971,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 426.9822832675057,
            "unit": "iter/sec",
            "range": "stddev: 0.000601545208404466",
            "extra": "mean: 2.3420175477714067 msec\nrounds: 471"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 339.3012914096436,
            "unit": "iter/sec",
            "range": "stddev: 0.0006706946093035859",
            "extra": "mean: 2.947233109091485 msec\nrounds: 385"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15637.268588703653,
            "unit": "iter/sec",
            "range": "stddev: 0.000015932166706004445",
            "extra": "mean: 63.9497872871736 usec\nrounds: 16708"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 17108.449539744746,
            "unit": "iter/sec",
            "range": "stddev: 0.000010968537399506157",
            "extra": "mean: 58.4506502285256 usec\nrounds: 17926"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53544.71611610119,
            "unit": "iter/sec",
            "range": "stddev: 0.00001330229934649284",
            "extra": "mean: 18.675979116813256 usec\nrounds: 43097"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 243.62305539032857,
            "unit": "iter/sec",
            "range": "stddev: 0.000350416110214176",
            "extra": "mean: 4.104701824701351 msec\nrounds: 251"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 179.31417483408708,
            "unit": "iter/sec",
            "range": "stddev: 0.0007613971842720004",
            "extra": "mean: 5.576803958333265 msec\nrounds: 192"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 72.92403564631647,
            "unit": "iter/sec",
            "range": "stddev: 0.0013470381951198674",
            "extra": "mean: 13.712899884614544 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23687.59056003275,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017554687545023326",
            "extra": "mean: 42.21619744167925 usec\nrounds: 23845"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2564.0271537915896,
            "unit": "iter/sec",
            "range": "stddev: 0.000029579756627917095",
            "extra": "mean: 390.0114702456394 usec\nrounds: 1714"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4988.024849947334,
            "unit": "iter/sec",
            "range": "stddev: 0.00003091019222487533",
            "extra": "mean: 200.4801559901127 usec\nrounds: 2763"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.80865015880106,
            "unit": "iter/sec",
            "range": "stddev: 0.0010545219509217776",
            "extra": "mean: 23.918495244446248 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1454.5124608035799,
            "unit": "iter/sec",
            "range": "stddev: 0.00019584598977919352",
            "extra": "mean: 687.5155950520536 usec\nrounds: 1536"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3933.856514418662,
            "unit": "iter/sec",
            "range": "stddev: 0.000006566175740379631",
            "extra": "mean: 254.2034759871709 usec\nrounds: 3977"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 17431.04262686853,
            "unit": "iter/sec",
            "range": "stddev: 0.000003303575624029266",
            "extra": "mean: 57.368914838093595 usec\nrounds: 18001"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3906.3705388959165,
            "unit": "iter/sec",
            "range": "stddev: 0.000018234765009432194",
            "extra": "mean: 255.9921006066764 usec\nrounds: 3956"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1011.6687158022002,
            "unit": "iter/sec",
            "range": "stddev: 0.000020695830686394397",
            "extra": "mean: 988.4658726518518 usec\nrounds: 958"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 558.6990501864291,
            "unit": "iter/sec",
            "range": "stddev: 0.006514940307870275",
            "extra": "mean: 1.7898723823967766 msec\nrounds: 659"
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
          "id": "fbf7f2c0f2f76ac59658bcba8e63aff37f46f725",
          "message": "Merge pull request #1642 from nexi-lab/fix/move-memory-paging-to-services\n\nrefactor(#1519): move memory_paging/ from core/ to services/memory/",
          "timestamp": "2026-02-16T14:25:31+08:00",
          "tree_id": "34a235a0d325c9701f15a7691aefbe10e00c4cc9",
          "url": "https://github.com/nexi-lab/nexus/commit/fbf7f2c0f2f76ac59658bcba8e63aff37f46f725"
        },
        "date": 1771224683698,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 348.3643335239683,
            "unit": "iter/sec",
            "range": "stddev: 0.007639336164706713",
            "extra": "mean: 2.8705579296371844 msec\nrounds: 469"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 325.7060469601195,
            "unit": "iter/sec",
            "range": "stddev: 0.0006898266678688026",
            "extra": "mean: 3.070253098869985 msec\nrounds: 354"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15183.635691978763,
            "unit": "iter/sec",
            "range": "stddev: 0.00001645059884769145",
            "extra": "mean: 65.86037891624875 usec\nrounds: 16278"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16579.145934519856,
            "unit": "iter/sec",
            "range": "stddev: 0.000012914910603313279",
            "extra": "mean: 60.31673790372246 usec\nrounds: 17402"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 52897.689271617855,
            "unit": "iter/sec",
            "range": "stddev: 0.00001927296131659944",
            "extra": "mean: 18.90441744752257 usec\nrounds: 39593"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 240.44130577795917,
            "unit": "iter/sec",
            "range": "stddev: 0.0002934084585366357",
            "extra": "mean: 4.159019170040077 msec\nrounds: 247"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 181.25066776561857,
            "unit": "iter/sec",
            "range": "stddev: 0.00044424389399157666",
            "extra": "mean: 5.517221052631564 msec\nrounds: 190"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 57.5362855237298,
            "unit": "iter/sec",
            "range": "stddev: 0.02460317575907522",
            "extra": "mean: 17.38033644155857 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23671.095216539732,
            "unit": "iter/sec",
            "range": "stddev: 0.0000022403059409252996",
            "extra": "mean: 42.24561604996075 usec\nrounds: 23240"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2525.1398704043663,
            "unit": "iter/sec",
            "range": "stddev: 0.000028487614336528704",
            "extra": "mean: 396.0176668708113 usec\nrounds: 1630"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4927.756228479465,
            "unit": "iter/sec",
            "range": "stddev: 0.00004302748258285908",
            "extra": "mean: 202.9321162886674 usec\nrounds: 4807"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 40.51993220744097,
            "unit": "iter/sec",
            "range": "stddev: 0.0007206352577485203",
            "extra": "mean: 24.679212069766564 msec\nrounds: 43"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1425.7960726822869,
            "unit": "iter/sec",
            "range": "stddev: 0.00024526326209601705",
            "extra": "mean: 701.3625715203046 usec\nrounds: 1566"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3976.311297462462,
            "unit": "iter/sec",
            "range": "stddev: 0.000005132501374880312",
            "extra": "mean: 251.4893641848826 usec\nrounds: 3976"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18324.7670617516,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027480632346860393",
            "extra": "mean: 54.570952887431325 usec\nrounds: 17299"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3930.047775552215,
            "unit": "iter/sec",
            "range": "stddev: 0.000008194370309053893",
            "extra": "mean: 254.44983295641717 usec\nrounds: 3981"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1018.1291462793189,
            "unit": "iter/sec",
            "range": "stddev: 0.000059275080087748795",
            "extra": "mean: 982.1936673303474 usec\nrounds: 1004"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 547.3102223491998,
            "unit": "iter/sec",
            "range": "stddev: 0.007586241268060968",
            "extra": "mean: 1.8271173443604551 msec\nrounds: 665"
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
          "id": "5cb1ad3651d768b13237d931804dc9a6203ac6e9",
          "message": "Merge pull request #1645 from nexi-lab/fix/move-agent-provisioning-to-services\n\nrefactor(#310): move agent_provisioning.py from core/ to services/agents/",
          "timestamp": "2026-02-16T14:25:36+08:00",
          "tree_id": "8226ab8572e066e5b5949238ad47cdc347942883",
          "url": "https://github.com/nexi-lab/nexus/commit/5cb1ad3651d768b13237d931804dc9a6203ac6e9"
        },
        "date": 1771224892721,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 417.05735207919787,
            "unit": "iter/sec",
            "range": "stddev: 0.006934550373414231",
            "extra": "mean: 2.3977517600747227 msec\nrounds: 546"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 360.1848065797374,
            "unit": "iter/sec",
            "range": "stddev: 0.0008679744563263554",
            "extra": "mean: 2.776352532734111 msec\nrounds: 336"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 22493.10663854683,
            "unit": "iter/sec",
            "range": "stddev: 0.000020720044830006377",
            "extra": "mean: 44.45806513388784 usec\nrounds: 21049"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 25230.81553254773,
            "unit": "iter/sec",
            "range": "stddev: 0.000006046636217467283",
            "extra": "mean: 39.63407360772785 usec\nrounds: 21506"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 55128.797480457826,
            "unit": "iter/sec",
            "range": "stddev: 0.00001172431126146115",
            "extra": "mean: 18.139339976615346 usec\nrounds: 48924"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 238.34673521619376,
            "unit": "iter/sec",
            "range": "stddev: 0.00024968173416732483",
            "extra": "mean: 4.195568271967075 msec\nrounds: 239"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 179.61637821727842,
            "unit": "iter/sec",
            "range": "stddev: 0.00029524121940284205",
            "extra": "mean: 5.567421022098105 msec\nrounds: 181"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 53.80033575771004,
            "unit": "iter/sec",
            "range": "stddev: 0.03060081681812234",
            "extra": "mean: 18.587244594597006 msec\nrounds: 74"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 21049.85903429719,
            "unit": "iter/sec",
            "range": "stddev: 0.000002094331353969576",
            "extra": "mean: 47.50625637780609 usec\nrounds: 21246"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2398.4930583010387,
            "unit": "iter/sec",
            "range": "stddev: 0.000027124025069748337",
            "extra": "mean: 416.92845286295943 usec\nrounds: 1729"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5050.4162478016115,
            "unit": "iter/sec",
            "range": "stddev: 0.0000341693172586191",
            "extra": "mean: 198.0034814824003 usec\nrounds: 4131"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 48.34208288776753,
            "unit": "iter/sec",
            "range": "stddev: 0.0005164891190946676",
            "extra": "mean: 20.685910500001228 msec\nrounds: 50"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1493.6120713976086,
            "unit": "iter/sec",
            "range": "stddev: 0.00020522386579538987",
            "extra": "mean: 669.5178883123756 usec\nrounds: 1540"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3279.357356176616,
            "unit": "iter/sec",
            "range": "stddev: 0.000010244704106606736",
            "extra": "mean: 304.9377946311695 usec\nrounds: 3316"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 15444.267859006723,
            "unit": "iter/sec",
            "range": "stddev: 0.000002885707245746652",
            "extra": "mean: 64.74894175166901 usec\nrounds: 15537"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3618.7789664728393,
            "unit": "iter/sec",
            "range": "stddev: 0.000009386120147470877",
            "extra": "mean: 276.3363027321568 usec\nrounds: 3660"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1097.4582430382604,
            "unit": "iter/sec",
            "range": "stddev: 0.000027820837940680043",
            "extra": "mean: 911.1963998115755 usec\nrounds: 1063"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 646.5252834839666,
            "unit": "iter/sec",
            "range": "stddev: 0.000027612255218970767",
            "extra": "mean: 1.5467299199982474 msec\nrounds: 650"
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
          "id": "7eaa971c03b2d6ec8f655632233592e55c946565",
          "message": "Merge pull request #1646 from nexi-lab/fix/identity-crypto-remove-server-import\n\nrefactor(#339): replace server.auth.oauth_crypto import with Protocol in identity/crypto",
          "timestamp": "2026-02-16T14:25:41+08:00",
          "tree_id": "8b4b32047e0446a108010a1edcc939198891dcf9",
          "url": "https://github.com/nexi-lab/nexus/commit/7eaa971c03b2d6ec8f655632233592e55c946565"
        },
        "date": 1771225206796,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 328.6456901177125,
            "unit": "iter/sec",
            "range": "stddev: 0.01053584812539005",
            "extra": "mean: 3.0427905494267264 msec\nrounds: 435"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 333.1714905923614,
            "unit": "iter/sec",
            "range": "stddev: 0.0009978405963258908",
            "extra": "mean: 3.001457292225252 msec\nrounds: 373"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14792.893578700981,
            "unit": "iter/sec",
            "range": "stddev: 0.000019026610624213804",
            "extra": "mean: 67.6000266398059 usec\nrounds: 16892"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16237.887483509698,
            "unit": "iter/sec",
            "range": "stddev: 0.00001547049841441534",
            "extra": "mean: 61.584365639652624 usec\nrounds: 18696"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 49119.8513149508,
            "unit": "iter/sec",
            "range": "stddev: 0.000024134205406439338",
            "extra": "mean: 20.358367813210908 usec\nrounds: 23202"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 243.29913027235742,
            "unit": "iter/sec",
            "range": "stddev: 0.00026691364793238046",
            "extra": "mean: 4.110166768292864 msec\nrounds: 246"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 185.07388851284807,
            "unit": "iter/sec",
            "range": "stddev: 0.00035141111578816897",
            "extra": "mean: 5.403247362636889 msec\nrounds: 182"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 57.937450766273145,
            "unit": "iter/sec",
            "range": "stddev: 0.023821461787063476",
            "extra": "mean: 17.25999309210417 msec\nrounds: 76"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23696.177633785035,
            "unit": "iter/sec",
            "range": "stddev: 0.0000019714138460566296",
            "extra": "mean: 42.20089904180331 usec\nrounds: 24000"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2492.858403698398,
            "unit": "iter/sec",
            "range": "stddev: 0.00008067777069129865",
            "extra": "mean: 401.1459289129309 usec\nrounds: 1674"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5018.829592196595,
            "unit": "iter/sec",
            "range": "stddev: 0.00002859459158304897",
            "extra": "mean: 199.24964209879246 usec\nrounds: 3202"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 43.075457026937364,
            "unit": "iter/sec",
            "range": "stddev: 0.0008072574417499771",
            "extra": "mean: 23.215075800000147 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1412.47315341885,
            "unit": "iter/sec",
            "range": "stddev: 0.00029493034004068517",
            "extra": "mean: 707.9780579046967 usec\nrounds: 1537"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3970.5948813557047,
            "unit": "iter/sec",
            "range": "stddev: 0.000005615083439562834",
            "extra": "mean: 251.85143029715584 usec\nrounds: 3974"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18288.89489823595,
            "unit": "iter/sec",
            "range": "stddev: 0.0000028141384037853045",
            "extra": "mean: 54.67798932435522 usec\nrounds: 17704"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3910.3086970438567,
            "unit": "iter/sec",
            "range": "stddev: 0.000011950737354869891",
            "extra": "mean: 255.73428531511775 usec\nrounds: 3936"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1012.5043945984467,
            "unit": "iter/sec",
            "range": "stddev: 0.00004323506170264439",
            "extra": "mean: 987.6500342466111 usec\nrounds: 1022"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 636.5972633224757,
            "unit": "iter/sec",
            "range": "stddev: 0.00004902475291435258",
            "extra": "mean: 1.57085186760132 msec\nrounds: 642"
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
          "id": "d9a9acb734d90d34fffeac96fa49c2fd22d82563",
          "message": "Merge pull request #1648 from nexi-lab/fix/delete-deprecated-database-shim\n\nrefactor(#352): delete deprecated storage/database.py shim",
          "timestamp": "2026-02-16T14:25:46+08:00",
          "tree_id": "d89594d22289a784c7f1a1bb2a2ae69cae79a450",
          "url": "https://github.com/nexi-lab/nexus/commit/d9a9acb734d90d34fffeac96fa49c2fd22d82563"
        },
        "date": 1771225355845,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 343.6385464443111,
            "unit": "iter/sec",
            "range": "stddev: 0.007426578022416065",
            "extra": "mean: 2.910034425262175 msec\nrounds: 475"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 327.6643698112417,
            "unit": "iter/sec",
            "range": "stddev: 0.0007164685885711651",
            "extra": "mean: 3.051903386920195 msec\nrounds: 367"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14603.711961844085,
            "unit": "iter/sec",
            "range": "stddev: 0.00001832097858662014",
            "extra": "mean: 68.4757411412081 usec\nrounds: 16847"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15185.907390791059,
            "unit": "iter/sec",
            "range": "stddev: 0.000019257609814128202",
            "extra": "mean: 65.85052669334819 usec\nrounds: 17701"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54807.01457367281,
            "unit": "iter/sec",
            "range": "stddev: 0.000015162980341601299",
            "extra": "mean: 18.2458396571807 usec\nrounds: 45041"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 229.48591694103408,
            "unit": "iter/sec",
            "range": "stddev: 0.000983726768994871",
            "extra": "mean: 4.35756587301585 msec\nrounds: 252"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 181.32882064192552,
            "unit": "iter/sec",
            "range": "stddev: 0.0005542116757865534",
            "extra": "mean: 5.514843125653614 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 57.90200958455505,
            "unit": "iter/sec",
            "range": "stddev: 0.02136230111374385",
            "extra": "mean: 17.270557743590693 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23731.565518932042,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017177300832375193",
            "extra": "mean: 42.13797017319579 usec\nrounds: 23670"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2613.7794329784692,
            "unit": "iter/sec",
            "range": "stddev: 0.000030886865823584224",
            "extra": "mean: 382.5877529614173 usec\nrounds: 1688"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4792.349510806197,
            "unit": "iter/sec",
            "range": "stddev: 0.000030700819467785974",
            "extra": "mean: 208.66591590306905 usec\nrounds: 2402"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.291540258256454,
            "unit": "iter/sec",
            "range": "stddev: 0.0018791332674410052",
            "extra": "mean: 23.645390872345278 msec\nrounds: 47"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1380.9218842344087,
            "unit": "iter/sec",
            "range": "stddev: 0.00034348444888868677",
            "extra": "mean: 724.1539231268002 usec\nrounds: 1535"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3914.3524808120183,
            "unit": "iter/sec",
            "range": "stddev: 0.000024093494341146303",
            "extra": "mean: 255.47009496512018 usec\nrounds: 4012"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 17738.941358052718,
            "unit": "iter/sec",
            "range": "stddev: 0.000007505587017015578",
            "extra": "mean: 56.37314988619898 usec\nrounds: 12743"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3814.5111713800225,
            "unit": "iter/sec",
            "range": "stddev: 0.000044652456780619316",
            "extra": "mean: 262.15678892302674 usec\nrounds: 3918"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1008.6645272555676,
            "unit": "iter/sec",
            "range": "stddev: 0.0001277525146804448",
            "extra": "mean: 991.4099018836893 usec\nrounds: 1009"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 639.5152642733556,
            "unit": "iter/sec",
            "range": "stddev: 0.00019484807320368434",
            "extra": "mean: 1.5636843338465776 msec\nrounds: 650"
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
          "id": "e1a2c769839d3ea3f589b75ce716e7672e23ccc2",
          "message": "Merge pull request #1651 from nexi-lab/fix/delete-nexus-fs-backward-compat-aliases\n\nfix(#1519): delete 16 backward-compat aliases from NexusFS kernel",
          "timestamp": "2026-02-16T14:29:36+08:00",
          "tree_id": "0b027a449893b08d3463874d89e57be78d52b0e8",
          "url": "https://github.com/nexi-lab/nexus/commit/e1a2c769839d3ea3f589b75ce716e7672e23ccc2"
        },
        "date": 1771225817998,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 344.011594658593,
            "unit": "iter/sec",
            "range": "stddev: 0.006574962848370962",
            "extra": "mean: 2.906878766666073 msec\nrounds: 480"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 326.82426639433623,
            "unit": "iter/sec",
            "range": "stddev: 0.0007941757609524661",
            "extra": "mean: 3.0597483198919826 msec\nrounds: 372"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16155.60771587681,
            "unit": "iter/sec",
            "range": "stddev: 0.00001723232145276086",
            "extra": "mean: 61.89801198361959 usec\nrounds: 16606"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 14744.310599076129,
            "unit": "iter/sec",
            "range": "stddev: 0.000020298766906294672",
            "extra": "mean: 67.82277091088 usec\nrounds: 16666"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 41620.028698771785,
            "unit": "iter/sec",
            "range": "stddev: 0.0008956825777711991",
            "extra": "mean: 24.026893571784353 usec\nrounds: 43682"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 247.99803780793783,
            "unit": "iter/sec",
            "range": "stddev: 0.0002633888278083313",
            "extra": "mean: 4.032289968255516 msec\nrounds: 252"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 177.20225308091088,
            "unit": "iter/sec",
            "range": "stddev: 0.0006474404593269729",
            "extra": "mean: 5.643269104165387 msec\nrounds: 192"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 59.96928640705127,
            "unit": "iter/sec",
            "range": "stddev: 0.021618714518884377",
            "extra": "mean: 16.67520258974465 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23698.69781313542,
            "unit": "iter/sec",
            "range": "stddev: 0.000005119321999203004",
            "extra": "mean: 42.19641129166737 usec\nrounds: 24000"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2536.962336338068,
            "unit": "iter/sec",
            "range": "stddev: 0.00003106746660856838",
            "extra": "mean: 394.1721899756036 usec\nrounds: 1616"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4881.12999379356,
            "unit": "iter/sec",
            "range": "stddev: 0.00007434487614468578",
            "extra": "mean: 204.8705937501187 usec\nrounds: 3712"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.79156291942598,
            "unit": "iter/sec",
            "range": "stddev: 0.0025286100502692234",
            "extra": "mean: 23.92827475555287 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1397.2455877299515,
            "unit": "iter/sec",
            "range": "stddev: 0.0002640877080743399",
            "extra": "mean: 715.6937969828623 usec\nrounds: 1591"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3932.135069392205,
            "unit": "iter/sec",
            "range": "stddev: 0.0000058233484566896655",
            "extra": "mean: 254.31476344340615 usec\nrounds: 3961"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 17881.303519510286,
            "unit": "iter/sec",
            "range": "stddev: 0.0000030864936944332017",
            "extra": "mean: 55.92433453796588 usec\nrounds: 18539"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3979.8723965938125,
            "unit": "iter/sec",
            "range": "stddev: 0.000007539462796915156",
            "extra": "mean: 251.26433723248348 usec\nrounds: 4018"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 959.627619155879,
            "unit": "iter/sec",
            "range": "stddev: 0.00002167491041963338",
            "extra": "mean: 1.0420708825363258 msec\nrounds: 962"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 544.9199013587055,
            "unit": "iter/sec",
            "range": "stddev: 0.007371305408506834",
            "extra": "mean: 1.8351320946557392 msec\nrounds: 655"
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
          "id": "f8f9ed84d87c1903f1aba9d8c51c77e98b3fdad8",
          "message": "Merge pull request #1656 from nexi-lab/fix/delete-config-backward-compat-aliases\n\nfix(#1391): delete backward-compat aliases from core/config.py",
          "timestamp": "2026-02-16T14:29:42+08:00",
          "tree_id": "41e2b55dcf0694a22e3de6deab03d0cf6d01a11a",
          "url": "https://github.com/nexi-lab/nexus/commit/f8f9ed84d87c1903f1aba9d8c51c77e98b3fdad8"
        },
        "date": 1771225964901,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 328.74433810961557,
            "unit": "iter/sec",
            "range": "stddev: 0.007369408987084741",
            "extra": "mean: 3.041877483731942 msec\nrounds: 461"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 336.37227728492445,
            "unit": "iter/sec",
            "range": "stddev: 0.0005586528964769896",
            "extra": "mean: 2.9728966015619327 msec\nrounds: 384"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16778.934552257942,
            "unit": "iter/sec",
            "range": "stddev: 0.000015120492673011905",
            "extra": "mean: 59.598539876623455 usec\nrounds: 16852"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16028.074406418149,
            "unit": "iter/sec",
            "range": "stddev: 0.00001664978725259352",
            "extra": "mean: 62.390526437759 usec\nrounds: 16813"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 48022.87173084537,
            "unit": "iter/sec",
            "range": "stddev: 0.000020426501627146394",
            "extra": "mean: 20.823411094711652 usec\nrounds: 43895"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 247.35793379492824,
            "unit": "iter/sec",
            "range": "stddev: 0.0002542127548390657",
            "extra": "mean: 4.042724583999188 msec\nrounds: 250"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 185.18722948456218,
            "unit": "iter/sec",
            "range": "stddev: 0.0004290269439009978",
            "extra": "mean: 5.399940388888227 msec\nrounds: 180"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 59.006152876395944,
            "unit": "iter/sec",
            "range": "stddev: 0.023893190379955832",
            "extra": "mean: 16.94738516667517 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23710.2827394715,
            "unit": "iter/sec",
            "range": "stddev: 0.000004397464884896335",
            "extra": "mean: 42.17579397884017 usec\nrounds: 23983"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2618.460663189673,
            "unit": "iter/sec",
            "range": "stddev: 0.00003089169789814367",
            "extra": "mean: 381.90377043199567 usec\nrounds: 1664"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5066.719657605131,
            "unit": "iter/sec",
            "range": "stddev: 0.00002940600145509953",
            "extra": "mean: 197.36635684964395 usec\nrounds: 2920"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.662224817716385,
            "unit": "iter/sec",
            "range": "stddev: 0.0008293322345637272",
            "extra": "mean: 24.002558777772265 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1447.4494016312271,
            "unit": "iter/sec",
            "range": "stddev: 0.0003063242840865862",
            "extra": "mean: 690.870436557598 usec\nrounds: 1592"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3968.899457038326,
            "unit": "iter/sec",
            "range": "stddev: 0.000006485945721508795",
            "extra": "mean: 251.9590155468994 usec\nrounds: 3988"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18223.33213224716,
            "unit": "iter/sec",
            "range": "stddev: 0.000002458939299696755",
            "extra": "mean: 54.87470637877727 usec\nrounds: 17308"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3918.964493804167,
            "unit": "iter/sec",
            "range": "stddev: 0.000009441082979995733",
            "extra": "mean: 255.16944631189875 usec\nrounds: 3986"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1012.4334152417546,
            "unit": "iter/sec",
            "range": "stddev: 0.00008310021467196381",
            "extra": "mean: 987.7192760979884 usec\nrounds: 1025"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 661.2214989913629,
            "unit": "iter/sec",
            "range": "stddev: 0.00002843485011149197",
            "extra": "mean: 1.5123525195799212 msec\nrounds: 664"
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
          "id": "109b2b18e4477b0b8cc1d0a14931893bb0881160",
          "message": "Merge pull request #1658 from nexi-lab/fix/mcp-private-export\n\nrefactor(#384): replace private _request_api_key export with public API",
          "timestamp": "2026-02-16T14:29:47+08:00",
          "tree_id": "055efddd41ee8ec17f96ad1bcc3e705249490446",
          "url": "https://github.com/nexi-lab/nexus/commit/109b2b18e4477b0b8cc1d0a14931893bb0881160"
        },
        "date": 1771226260808,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 346.4092616409523,
            "unit": "iter/sec",
            "range": "stddev: 0.007377721483937142",
            "extra": "mean: 2.886758844907802 msec\nrounds: 432"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 321.0789514905623,
            "unit": "iter/sec",
            "range": "stddev: 0.0009662340764264731",
            "extra": "mean: 3.1144987715876282 msec\nrounds: 359"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 17006.120739740476,
            "unit": "iter/sec",
            "range": "stddev: 0.00001481031934447894",
            "extra": "mean: 58.802358004148836 usec\nrounds: 16835"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15465.81971925215,
            "unit": "iter/sec",
            "range": "stddev: 0.00001747024790199709",
            "extra": "mean: 64.65871309460441 usec\nrounds: 17389"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 52139.470533911575,
            "unit": "iter/sec",
            "range": "stddev: 0.00001659666586577831",
            "extra": "mean: 19.179327863515585 usec\nrounds: 46254"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 240.31082055290054,
            "unit": "iter/sec",
            "range": "stddev: 0.0003200413558932139",
            "extra": "mean: 4.161277456001471 msec\nrounds: 250"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 180.64045618290837,
            "unit": "iter/sec",
            "range": "stddev: 0.000530266059664828",
            "extra": "mean: 5.535858473405565 msec\nrounds: 188"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 65.55063190677572,
            "unit": "iter/sec",
            "range": "stddev: 0.0015628157782669404",
            "extra": "mean: 15.255383066667793 msec\nrounds: 75"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23721.733885585396,
            "unit": "iter/sec",
            "range": "stddev: 0.000004324993468779287",
            "extra": "mean: 42.15543454045971 usec\nrounds: 23862"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2595.9845376118215,
            "unit": "iter/sec",
            "range": "stddev: 0.00003054717007059826",
            "extra": "mean: 385.21030673008204 usec\nrounds: 1679"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4917.187145507606,
            "unit": "iter/sec",
            "range": "stddev: 0.00005567439281019697",
            "extra": "mean: 203.36830191903732 usec\nrounds: 2971"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 39.47940033791266,
            "unit": "iter/sec",
            "range": "stddev: 0.00245638184628698",
            "extra": "mean: 25.329665380952736 msec\nrounds: 42"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1443.163261919132,
            "unit": "iter/sec",
            "range": "stddev: 0.00021588529608600163",
            "extra": "mean: 692.9222953404388 usec\nrounds: 1588"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3963.9506254723087,
            "unit": "iter/sec",
            "range": "stddev: 0.000007959362869298106",
            "extra": "mean: 252.27357615758623 usec\nrounds: 4018"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18402.395941307448,
            "unit": "iter/sec",
            "range": "stddev: 0.000002745394075002324",
            "extra": "mean: 54.340750149567334 usec\nrounds: 18359"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3926.332225167808,
            "unit": "iter/sec",
            "range": "stddev: 0.000012135340490150518",
            "extra": "mean: 254.69062286425873 usec\nrounds: 3980"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1004.0285273595887,
            "unit": "iter/sec",
            "range": "stddev: 0.00006195799856167388",
            "extra": "mean: 995.9876365563207 usec\nrounds: 941"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 541.4153432119484,
            "unit": "iter/sec",
            "range": "stddev: 0.007651562010400172",
            "extra": "mean: 1.8470108254921194 msec\nrounds: 659"
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
          "id": "d87a18d77360bcf2764fbde8f6a84cad5a051601",
          "message": "Merge pull request #1657 from nexi-lab/fix/remote-client-backward-compat\n\nrefactor(#389): delete backward-compat wrapper classes from remote client",
          "timestamp": "2026-02-16T14:30:25+08:00",
          "tree_id": "a6dbb78938bf3865c9e759be6433b1f51cf54377",
          "url": "https://github.com/nexi-lab/nexus/commit/d87a18d77360bcf2764fbde8f6a84cad5a051601"
        },
        "date": 1771226367938,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 424.8576802439263,
            "unit": "iter/sec",
            "range": "stddev: 0.007827840474703782",
            "extra": "mean: 2.3537293698583097 msec\nrounds: 584"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 355.0931004272363,
            "unit": "iter/sec",
            "range": "stddev: 0.0009192131004109129",
            "extra": "mean: 2.816162856436334 msec\nrounds: 404"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 18981.171746227552,
            "unit": "iter/sec",
            "range": "stddev: 0.000015429796326744977",
            "extra": "mean: 52.683786510637674 usec\nrounds: 21233"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 20023.039649182247,
            "unit": "iter/sec",
            "range": "stddev: 0.000021389018369020363",
            "extra": "mean: 49.94246715387394 usec\nrounds: 20916"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 59455.290379511396,
            "unit": "iter/sec",
            "range": "stddev: 0.00001649725783038083",
            "extra": "mean: 16.819361130302465 usec\nrounds: 53482"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 236.73725195812509,
            "unit": "iter/sec",
            "range": "stddev: 0.0006599630413593268",
            "extra": "mean: 4.224092286823045 msec\nrounds: 258"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 194.84643283104174,
            "unit": "iter/sec",
            "range": "stddev: 0.00040274915231305235",
            "extra": "mean: 5.132246895518665 msec\nrounds: 201"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 61.342008472682465,
            "unit": "iter/sec",
            "range": "stddev: 0.020668128892969175",
            "extra": "mean: 16.30204202468088 msec\nrounds: 81"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 19915.9536826985,
            "unit": "iter/sec",
            "range": "stddev: 0.000001604446987232644",
            "extra": "mean: 50.21100249237503 usec\nrounds: 20056"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2768.2642103686017,
            "unit": "iter/sec",
            "range": "stddev: 0.000022768117813711406",
            "extra": "mean: 361.2371955879339 usec\nrounds: 1723"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 6683.580789482617,
            "unit": "iter/sec",
            "range": "stddev: 0.000011518431172056643",
            "extra": "mean: 149.62039533862074 usec\nrounds: 5021"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 48.71029105250734,
            "unit": "iter/sec",
            "range": "stddev: 0.0006700806393617306",
            "extra": "mean: 20.5295426981138 msec\nrounds: 53"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1795.2675234264455,
            "unit": "iter/sec",
            "range": "stddev: 0.0003164548231441385",
            "extra": "mean: 557.0200468459437 usec\nrounds: 1900"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 6547.90725219042,
            "unit": "iter/sec",
            "range": "stddev: 0.0000036354403220720655",
            "extra": "mean: 152.7205504728977 usec\nrounds: 6429"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 26432.26300781194,
            "unit": "iter/sec",
            "range": "stddev: 0.000001298177478293209",
            "extra": "mean: 37.832553334705175 usec\nrounds: 26409"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 4334.878649219505,
            "unit": "iter/sec",
            "range": "stddev: 0.0000058653926549758095",
            "extra": "mean: 230.6869651772259 usec\nrounds: 4365"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 986.2406190125914,
            "unit": "iter/sec",
            "range": "stddev: 0.000024179585709747877",
            "extra": "mean: 1.0139513428286742 msec\nrounds: 983"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 609.5152631288042,
            "unit": "iter/sec",
            "range": "stddev: 0.0063869347918666134",
            "extra": "mean: 1.6406480042300065 msec\nrounds: 710"
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
          "id": "7173d3329e1b1c526fd70ea49747661dd99b366d",
          "message": "Merge pull request #1655 from nexi-lab/fix/delete-deprecated-agent-id-protocol\n\nfix(#1519): delete deprecated agent_id param from workspace versioning",
          "timestamp": "2026-02-16T14:41:44+08:00",
          "tree_id": "3f92a3417d7ea862119fc30c664736480104f067",
          "url": "https://github.com/nexi-lab/nexus/commit/7173d3329e1b1c526fd70ea49747661dd99b366d"
        },
        "date": 1771227014021,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 332.59506451629437,
            "unit": "iter/sec",
            "range": "stddev: 0.007988516267019876",
            "extra": "mean: 3.0066591681218657 msec\nrounds: 458"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 304.7728223591377,
            "unit": "iter/sec",
            "range": "stddev: 0.0008547967444798789",
            "extra": "mean: 3.2811324587912947 msec\nrounds: 364"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15088.878754676673,
            "unit": "iter/sec",
            "range": "stddev: 0.000017440388890866388",
            "extra": "mean: 66.27397676517603 usec\nrounds: 16656"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 14427.219493187748,
            "unit": "iter/sec",
            "range": "stddev: 0.000021053623162455543",
            "extra": "mean: 69.31342525648692 usec\nrounds: 17065"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 52354.86921991774,
            "unit": "iter/sec",
            "range": "stddev: 0.000021998486126469135",
            "extra": "mean: 19.10042016912465 usec\nrounds: 46467"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 247.62540564053612,
            "unit": "iter/sec",
            "range": "stddev: 0.00021887207592041197",
            "extra": "mean: 4.038357847060507 msec\nrounds: 255"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.65547301351836,
            "unit": "iter/sec",
            "range": "stddev: 0.0003665386700561529",
            "extra": "mean: 5.415490717281862 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 68.83775449397068,
            "unit": "iter/sec",
            "range": "stddev: 0.0017880206877817558",
            "extra": "mean: 14.526911973684259 msec\nrounds: 76"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23724.0400803497,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018616099450762576",
            "extra": "mean: 42.15133664473474 usec\nrounds: 23856"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2671.012616175032,
            "unit": "iter/sec",
            "range": "stddev: 0.0000404009684182749",
            "extra": "mean: 374.3898452385557 usec\nrounds: 1680"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4990.199156509913,
            "unit": "iter/sec",
            "range": "stddev: 0.00005156975698069163",
            "extra": "mean: 200.39280370112286 usec\nrounds: 4269"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 36.60529269246744,
            "unit": "iter/sec",
            "range": "stddev: 0.012141876247728254",
            "extra": "mean: 27.31845387499874 msec\nrounds: 40"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1445.997351573503,
            "unit": "iter/sec",
            "range": "stddev: 0.0003058612868158273",
            "extra": "mean: 691.5641988637266 usec\nrounds: 1584"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3856.576623800979,
            "unit": "iter/sec",
            "range": "stddev: 0.000017292996887981973",
            "extra": "mean: 259.2973244271798 usec\nrounds: 3930"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18282.91590708044,
            "unit": "iter/sec",
            "range": "stddev: 0.000004506564446424361",
            "extra": "mean: 54.69587045536478 usec\nrounds: 18318"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3867.3043737866215,
            "unit": "iter/sec",
            "range": "stddev: 0.00001704426597010156",
            "extra": "mean: 258.5780438638872 usec\nrounds: 3944"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1018.7864796352827,
            "unit": "iter/sec",
            "range": "stddev: 0.000013136854151674438",
            "extra": "mean: 981.5599440993681 usec\nrounds: 966"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 545.5185937984181,
            "unit": "iter/sec",
            "range": "stddev: 0.006903534261050639",
            "extra": "mean: 1.8331180850079758 msec\nrounds: 647"
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
          "id": "6f0a95dba77f4b918e8008e4d74418e87b89ba36",
          "message": "Merge pull request #1638 from nexi-lab/fix/move-workspace-registry-to-services\n\nrefactor(#1519): move workspace_registry.py from core/ to services/workspace/",
          "timestamp": "2026-02-16T15:01:49+08:00",
          "tree_id": "277523df3d15b4b429e3df7be16cfea1267b7e58",
          "url": "https://github.com/nexi-lab/nexus/commit/6f0a95dba77f4b918e8008e4d74418e87b89ba36"
        },
        "date": 1771227282941,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 339.4773617078851,
            "unit": "iter/sec",
            "range": "stddev: 0.0038392392642058134",
            "extra": "mean: 2.945704523474187 msec\nrounds: 426"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 335.10038781559575,
            "unit": "iter/sec",
            "range": "stddev: 0.0006541208705270873",
            "extra": "mean: 2.9841803720928413 msec\nrounds: 301"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14527.422116288099,
            "unit": "iter/sec",
            "range": "stddev: 0.000018374111349176384",
            "extra": "mean: 68.83533719852494 usec\nrounds: 16299"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16206.06223961486,
            "unit": "iter/sec",
            "range": "stddev: 0.00001507303664180964",
            "extra": "mean: 61.705304176578636 usec\nrounds: 17263"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 43968.54972492872,
            "unit": "iter/sec",
            "range": "stddev: 0.0007791937193557539",
            "extra": "mean: 22.743529323939306 usec\nrounds: 45492"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 246.80793951828514,
            "unit": "iter/sec",
            "range": "stddev: 0.000339279162201771",
            "extra": "mean: 4.051733513726424 msec\nrounds: 255"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.8018306836921,
            "unit": "iter/sec",
            "range": "stddev: 0.0003749072106510698",
            "extra": "mean: 5.411201806283001 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 72.13710705101566,
            "unit": "iter/sec",
            "range": "stddev: 0.0013135045580033177",
            "extra": "mean: 13.862491037972951 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23741.775779471453,
            "unit": "iter/sec",
            "range": "stddev: 0.000001669322575076445",
            "extra": "mean: 42.119848544128665 usec\nrounds: 24040"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2534.5360012781293,
            "unit": "iter/sec",
            "range": "stddev: 0.000027829049168279173",
            "extra": "mean: 394.54953470604266 usec\nrounds: 1700"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5010.017106874983,
            "unit": "iter/sec",
            "range": "stddev: 0.000053991448284679145",
            "extra": "mean: 199.60011685943195 usec\nrounds: 3286"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 37.847061992296155,
            "unit": "iter/sec",
            "range": "stddev: 0.0023198161677440783",
            "extra": "mean: 26.422130209302694 msec\nrounds: 43"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1458.2874348022356,
            "unit": "iter/sec",
            "range": "stddev: 0.00021473402039762216",
            "extra": "mean: 685.7358680702164 usec\nrounds: 1425"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3966.044918071164,
            "unit": "iter/sec",
            "range": "stddev: 0.000006030468702586795",
            "extra": "mean: 252.1403616594281 usec\nrounds: 3954"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18454.98716901237,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026203388784857293",
            "extra": "mean: 54.185895164375545 usec\nrounds: 18591"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3883.824927397861,
            "unit": "iter/sec",
            "range": "stddev: 0.000031678277302576874",
            "extra": "mean: 257.4781352644528 usec\nrounds: 3970"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1013.0861168011588,
            "unit": "iter/sec",
            "range": "stddev: 0.000017052506503474727",
            "extra": "mean: 987.0829176472397 usec\nrounds: 1020"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 537.941101856899,
            "unit": "iter/sec",
            "range": "stddev: 0.006612277339580425",
            "extra": "mean: 1.8589395689381922 msec\nrounds: 631"
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
          "id": "a8560777d0c53ab6e401ad9bcbd3da1b88c61683",
          "message": "Merge pull request #1666 from nexi-lab/fix/delete-shutil-rmtree-from-kernel\n\nfix(#440): replace direct shutil.rmtree/os calls with Backend ABC rmdir",
          "timestamp": "2026-02-16T15:01:54+08:00",
          "tree_id": "a4d2863057f2380bbf0d3dd737009add74fd34de",
          "url": "https://github.com/nexi-lab/nexus/commit/a8560777d0c53ab6e401ad9bcbd3da1b88c61683"
        },
        "date": 1771227683803,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 399.2653612650244,
            "unit": "iter/sec",
            "range": "stddev: 0.004549636930770255",
            "extra": "mean: 2.504599940329459 msec\nrounds: 486"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 337.0961563542812,
            "unit": "iter/sec",
            "range": "stddev: 0.001028135036478457",
            "extra": "mean: 2.966512614130848 msec\nrounds: 368"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16167.41234877428,
            "unit": "iter/sec",
            "range": "stddev: 0.0000166272557533948",
            "extra": "mean: 61.852817162532155 usec\nrounds: 17130"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 14950.270249310779,
            "unit": "iter/sec",
            "range": "stddev: 0.000018939749446312527",
            "extra": "mean: 66.88842297323026 usec\nrounds: 17786"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54800.11169190789,
            "unit": "iter/sec",
            "range": "stddev: 0.000014783118571162678",
            "extra": "mean: 18.248137989610445 usec\nrounds: 42438"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 248.78465409676366,
            "unit": "iter/sec",
            "range": "stddev: 0.0002311203096339792",
            "extra": "mean: 4.019540528456608 msec\nrounds: 246"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 183.90421311136782,
            "unit": "iter/sec",
            "range": "stddev: 0.0003734807292147613",
            "extra": "mean: 5.437613326424581 msec\nrounds: 193"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 60.27786564679697,
            "unit": "iter/sec",
            "range": "stddev: 0.02212244414207249",
            "extra": "mean: 16.58983756756719 msec\nrounds: 74"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23732.029541883217,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017238905422950365",
            "extra": "mean: 42.13714626619526 usec\nrounds: 24052"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2604.9448781321403,
            "unit": "iter/sec",
            "range": "stddev: 0.000032097019781039926",
            "extra": "mean: 383.8852823315954 usec\nrounds: 1647"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5014.975992492343,
            "unit": "iter/sec",
            "range": "stddev: 0.00002639841674248664",
            "extra": "mean: 199.40274918505042 usec\nrounds: 4601"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.49962030515599,
            "unit": "iter/sec",
            "range": "stddev: 0.0005930048997010949",
            "extra": "mean: 23.529621978262274 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1402.8229802386968,
            "unit": "iter/sec",
            "range": "stddev: 0.00042459912291926673",
            "extra": "mean: 712.8483166349652 usec\nrounds: 1557"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3915.9575081719718,
            "unit": "iter/sec",
            "range": "stddev: 0.000014916629993937444",
            "extra": "mean: 255.3653858381153 usec\nrounds: 4025"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 17884.039330022446,
            "unit": "iter/sec",
            "range": "stddev: 0.000004388295341968316",
            "extra": "mean: 55.915779514154366 usec\nrounds: 16958"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3915.730363754722,
            "unit": "iter/sec",
            "range": "stddev: 0.000012561903016849443",
            "extra": "mean: 255.38019912104423 usec\nrounds: 3867"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1021.6858407989517,
            "unit": "iter/sec",
            "range": "stddev: 0.00001271295366490394",
            "extra": "mean: 978.7744530334359 usec\nrounds: 1022"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 568.4848102188672,
            "unit": "iter/sec",
            "range": "stddev: 0.006238708208030151",
            "extra": "mean: 1.759061952095077 msec\nrounds: 668"
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
          "id": "ed1da5c328360fe6586d45f9713556fdf40537e4",
          "message": "Merge pull request #1661 from nexi-lab/fix/delete-rpc-server-backward-compat\n\nfix(#1300): delete dead executor fields and backward-compat from rpc_server",
          "timestamp": "2026-02-16T15:05:30+08:00",
          "tree_id": "7f2b89a57a0f1f14bd4285b187ae630f9e82f5ba",
          "url": "https://github.com/nexi-lab/nexus/commit/ed1da5c328360fe6586d45f9713556fdf40537e4"
        },
        "date": 1771228231473,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 352.11080229014414,
            "unit": "iter/sec",
            "range": "stddev: 0.00817519595152258",
            "extra": "mean: 2.8400151131290383 msec\nrounds: 495"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 340.8068636665706,
            "unit": "iter/sec",
            "range": "stddev: 0.0005204468564508882",
            "extra": "mean: 2.934213205806656 msec\nrounds: 379"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16736.612667064874,
            "unit": "iter/sec",
            "range": "stddev: 0.000013373464819130874",
            "extra": "mean: 59.74924674978283 usec\nrounds: 16770"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16726.41438323372,
            "unit": "iter/sec",
            "range": "stddev: 0.000012663247381500773",
            "extra": "mean: 59.785676540596974 usec\nrounds: 15900"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 48237.3057324125,
            "unit": "iter/sec",
            "range": "stddev: 0.000014993736605030412",
            "extra": "mean: 20.730842753683515 usec\nrounds: 47645"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 249.3793484529257,
            "unit": "iter/sec",
            "range": "stddev: 0.00020151622503404037",
            "extra": "mean: 4.009955139443978 msec\nrounds: 251"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 180.30963149825615,
            "unit": "iter/sec",
            "range": "stddev: 0.0007164267180960306",
            "extra": "mean: 5.54601543850236 msec\nrounds: 187"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 61.715105947376074,
            "unit": "iter/sec",
            "range": "stddev: 0.02195328690035478",
            "extra": "mean: 16.203488346154522 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23732.548210290915,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017687634541667612",
            "extra": "mean: 42.136225370286176 usec\nrounds: 24005"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2694.294404101464,
            "unit": "iter/sec",
            "range": "stddev: 0.000030369561136308894",
            "extra": "mean: 371.1546883954932 usec\nrounds: 1672"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4893.442071703389,
            "unit": "iter/sec",
            "range": "stddev: 0.000034110316864680934",
            "extra": "mean: 204.3551318983743 usec\nrounds: 2138"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 43.427686455408676,
            "unit": "iter/sec",
            "range": "stddev: 0.000662576934411866",
            "extra": "mean: 23.026785021734806 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1458.1797932471582,
            "unit": "iter/sec",
            "range": "stddev: 0.00026818055313634014",
            "extra": "mean: 685.7864884913423 usec\nrounds: 1564"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3989.7206719404103,
            "unit": "iter/sec",
            "range": "stddev: 0.000005160497822300766",
            "extra": "mean: 250.64411326661815 usec\nrounds: 4017"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18459.65551936469,
            "unit": "iter/sec",
            "range": "stddev: 0.000002526726613544501",
            "extra": "mean: 54.17219183483528 usec\nrounds: 18396"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3985.5561990162146,
            "unit": "iter/sec",
            "range": "stddev: 0.000013055775856773377",
            "extra": "mean: 250.9060091153244 usec\nrounds: 4059"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1036.9674558618117,
            "unit": "iter/sec",
            "range": "stddev: 0.000014913942526543327",
            "extra": "mean: 964.3504184698946 usec\nrounds: 1018"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 643.1946436976452,
            "unit": "iter/sec",
            "range": "stddev: 0.00007207938704341525",
            "extra": "mean: 1.5547393153822389 msec\nrounds: 650"
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
          "id": "34b35e2f964462680ab0373270f747eee657d92f",
          "message": "Merge pull request #1660 from nexi-lab/fix/auth-backward-compat-fields\n\nrefactor(#424): delete legacy_user_id backward-compat field",
          "timestamp": "2026-02-16T15:08:18+08:00",
          "tree_id": "ab892f8ed6dccb03634a8dd447948773899f7ece",
          "url": "https://github.com/nexi-lab/nexus/commit/34b35e2f964462680ab0373270f747eee657d92f"
        },
        "date": 1771228264554,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 352.70143151788454,
            "unit": "iter/sec",
            "range": "stddev: 0.008385816141564681",
            "extra": "mean: 2.835259260775903 msec\nrounds: 464"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 343.2603467813232,
            "unit": "iter/sec",
            "range": "stddev: 0.0005403141726067087",
            "extra": "mean: 2.913240662304225 msec\nrounds: 382"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16030.840391462752,
            "unit": "iter/sec",
            "range": "stddev: 0.000013943976727869342",
            "extra": "mean: 62.37976148353093 usec\nrounds: 16393"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15948.099929588216,
            "unit": "iter/sec",
            "range": "stddev: 0.000014806252533033464",
            "extra": "mean: 62.70339441156363 usec\nrounds: 17071"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54501.63295409157,
            "unit": "iter/sec",
            "range": "stddev: 0.000016039995468759866",
            "extra": "mean: 18.348074099767455 usec\nrounds: 48907"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 245.75057264716355,
            "unit": "iter/sec",
            "range": "stddev: 0.0002548596387527072",
            "extra": "mean: 4.0691665098813425 msec\nrounds: 253"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 183.6873898890856,
            "unit": "iter/sec",
            "range": "stddev: 0.00034297759878791867",
            "extra": "mean: 5.444031844558418 msec\nrounds: 193"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 70.9787359202062,
            "unit": "iter/sec",
            "range": "stddev: 0.0013652821669685772",
            "extra": "mean: 14.088726532467316 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23745.917308597138,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017562622963573446",
            "extra": "mean: 42.112502414802606 usec\nrounds: 24018"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2640.375020389555,
            "unit": "iter/sec",
            "range": "stddev: 0.000038525084467563736",
            "extra": "mean: 378.7340784084763 usec\nrounds: 1709"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5058.615937608932,
            "unit": "iter/sec",
            "range": "stddev: 0.00004058621187267447",
            "extra": "mean: 197.6825306237169 usec\nrounds: 3592"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 44.0399982170763,
            "unit": "iter/sec",
            "range": "stddev: 0.0019116092391417677",
            "extra": "mean: 22.70663125531769 msec\nrounds: 47"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1482.6717513767617,
            "unit": "iter/sec",
            "range": "stddev: 0.00024634790707734445",
            "extra": "mean: 674.4581186438818 usec\nrounds: 1593"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3955.816982732786,
            "unit": "iter/sec",
            "range": "stddev: 0.0000122745808994956",
            "extra": "mean: 252.79228143390316 usec\nrounds: 3905"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18476.797720301307,
            "unit": "iter/sec",
            "range": "stddev: 0.00000273742853343784",
            "extra": "mean: 54.121932552265484 usec\nrounds: 17154"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3908.4694114289423,
            "unit": "iter/sec",
            "range": "stddev: 0.000011989687526953997",
            "extra": "mean: 255.8546312466594 usec\nrounds: 3962"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1031.7314926188806,
            "unit": "iter/sec",
            "range": "stddev: 0.000014143225402649035",
            "extra": "mean: 969.244427599728 usec\nrounds: 1029"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 655.3392350572936,
            "unit": "iter/sec",
            "range": "stddev: 0.000022348294869783332",
            "extra": "mean: 1.5259272549316754 msec\nrounds: 659"
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
          "id": "85f9b148fc01c78ac4b52411d939f2c4dada181d",
          "message": "Merge pull request #1649 from nexi-lab/fix/eliminate-content-cache-model\n\nrefactor(#188): eliminate ContentCacheModel — L2 cache is now disk-only",
          "timestamp": "2026-02-16T15:20:49+08:00",
          "tree_id": "038b7812acb0304e21047e9bb0b38e971695d113",
          "url": "https://github.com/nexi-lab/nexus/commit/85f9b148fc01c78ac4b52411d939f2c4dada181d"
        },
        "date": 1771229508322,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 348.42403014829745,
            "unit": "iter/sec",
            "range": "stddev: 0.009119982611925613",
            "extra": "mean: 2.8700661075941762 msec\nrounds: 474"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 335.2351916116315,
            "unit": "iter/sec",
            "range": "stddev: 0.001072497863850173",
            "extra": "mean: 2.9829803821983454 msec\nrounds: 382"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16553.156033968335,
            "unit": "iter/sec",
            "range": "stddev: 0.000014669090435995355",
            "extra": "mean: 60.41144044965951 usec\nrounds: 13518"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16880.01621742827,
            "unit": "iter/sec",
            "range": "stddev: 0.000012918867941284096",
            "extra": "mean: 59.241649244834285 usec\nrounds: 17679"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54728.53657098202,
            "unit": "iter/sec",
            "range": "stddev: 0.000016198268149237816",
            "extra": "mean: 18.272003284849696 usec\nrounds: 44145"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 250.48036338249725,
            "unit": "iter/sec",
            "range": "stddev: 0.00022911236280579147",
            "extra": "mean: 3.992328925493234 msec\nrounds: 255"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 177.8991760107494,
            "unit": "iter/sec",
            "range": "stddev: 0.0008058958559288705",
            "extra": "mean: 5.62116150520886 msec\nrounds: 192"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 61.582390854273065,
            "unit": "iter/sec",
            "range": "stddev: 0.02144706110678184",
            "extra": "mean: 16.238408189873198 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23773.025682415304,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016777414921176014",
            "extra": "mean: 42.06448154135008 usec\nrounds: 24000"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2639.3860388553067,
            "unit": "iter/sec",
            "range": "stddev: 0.000030922768629918014",
            "extra": "mean: 378.8759905821495 usec\nrounds: 1699"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4965.576313419915,
            "unit": "iter/sec",
            "range": "stddev: 0.000029907265225679056",
            "extra": "mean: 201.386493104015 usec\nrounds: 2973"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 44.16611211181031,
            "unit": "iter/sec",
            "range": "stddev: 0.0005969923040456217",
            "extra": "mean: 22.641793723396212 msec\nrounds: 47"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1466.1309752386999,
            "unit": "iter/sec",
            "range": "stddev: 0.00019479761276799794",
            "extra": "mean: 682.0673029141824 usec\nrounds: 1578"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3968.9502565068897,
            "unit": "iter/sec",
            "range": "stddev: 0.000005052887118545283",
            "extra": "mean: 251.955790667961 usec\nrounds: 4008"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18339.600848774266,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027773830681763254",
            "extra": "mean: 54.52681376469736 usec\nrounds: 17290"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3896.5467219767597,
            "unit": "iter/sec",
            "range": "stddev: 0.00001412874607129537",
            "extra": "mean: 256.63749759753665 usec\nrounds: 3955"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1021.1207980612986,
            "unit": "iter/sec",
            "range": "stddev: 0.00004348006221234531",
            "extra": "mean: 979.3160631911537 usec\nrounds: 997"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 560.1949437974055,
            "unit": "iter/sec",
            "range": "stddev: 0.0063049380592711275",
            "extra": "mean: 1.7850928700307052 msec\nrounds: 654"
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
          "id": "05832e2738b1da57e7f3fe2b3c0d9d9fc536d669",
          "message": "Merge pull request #1650 from nexi-lab/fix/rebac-tracing-remove-server-telemetry-import\n\nrefactor(#333): remove server.telemetry import from rebac_tracing",
          "timestamp": "2026-02-16T15:22:56+08:00",
          "tree_id": "cc01e018df19067658f90a3aeb94acf227a55f29",
          "url": "https://github.com/nexi-lab/nexus/commit/05832e2738b1da57e7f3fe2b3c0d9d9fc536d669"
        },
        "date": 1771229632922,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 354.83859131126354,
            "unit": "iter/sec",
            "range": "stddev: 0.008118289474484968",
            "extra": "mean: 2.8181827582637493 msec\nrounds: 484"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 339.2117783990631,
            "unit": "iter/sec",
            "range": "stddev: 0.0005928521622674471",
            "extra": "mean: 2.9480108406600127 msec\nrounds: 364"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 17638.677062871506,
            "unit": "iter/sec",
            "range": "stddev: 0.000011859473693008873",
            "extra": "mean: 56.693594221130546 usec\nrounds: 16647"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16707.112834018273,
            "unit": "iter/sec",
            "range": "stddev: 0.000015181325886926823",
            "extra": "mean: 59.854746294874175 usec\nrounds: 17272"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54129.834275352194,
            "unit": "iter/sec",
            "range": "stddev: 0.000020524867868230178",
            "extra": "mean: 18.474100528612666 usec\nrounds: 45997"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 245.84581425617037,
            "unit": "iter/sec",
            "range": "stddev: 0.0002684059330519089",
            "extra": "mean: 4.067590099207481 msec\nrounds: 252"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 183.4106440403135,
            "unit": "iter/sec",
            "range": "stddev: 0.0003558152839824711",
            "extra": "mean: 5.4522462708336645 msec\nrounds: 192"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 70.75676189264631,
            "unit": "iter/sec",
            "range": "stddev: 0.0014063028902620363",
            "extra": "mean: 14.132924871791356 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23712.82952983777,
            "unit": "iter/sec",
            "range": "stddev: 0.0000019576939156983905",
            "extra": "mean: 42.171264240807005 usec\nrounds: 23857"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2664.81996059651,
            "unit": "iter/sec",
            "range": "stddev: 0.00003587907655524196",
            "extra": "mean: 375.259873007013 usec\nrounds: 1693"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4980.626517339009,
            "unit": "iter/sec",
            "range": "stddev: 0.00006120278850458931",
            "extra": "mean: 200.77795364071355 usec\nrounds: 4120"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.9365674930709,
            "unit": "iter/sec",
            "range": "stddev: 0.0012564631605346953",
            "extra": "mean: 23.290171021737585 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1442.5521036762939,
            "unit": "iter/sec",
            "range": "stddev: 0.00021074147735930266",
            "extra": "mean: 693.2158619792899 usec\nrounds: 1565"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3985.9822918702935,
            "unit": "iter/sec",
            "range": "stddev: 0.0000060538731827640314",
            "extra": "mean: 250.8791878076263 usec\nrounds: 3248"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18484.61534480089,
            "unit": "iter/sec",
            "range": "stddev: 0.000003976023943042209",
            "extra": "mean: 54.09904297961314 usec\nrounds: 17869"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3944.9024912875075,
            "unit": "iter/sec",
            "range": "stddev: 0.000007571391961121806",
            "extra": "mean: 253.491690151669 usec\nrounds: 3960"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1041.3752526556048,
            "unit": "iter/sec",
            "range": "stddev: 0.000014043282927628507",
            "extra": "mean: 960.2686423072817 usec\nrounds: 1040"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 562.0417176730998,
            "unit": "iter/sec",
            "range": "stddev: 0.006481157138817003",
            "extra": "mean: 1.779227357250427 msec\nrounds: 655"
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
          "id": "5b91494ff1976b1d9a9cb6f2ca50de3ff23af36f",
          "message": "Merge pull request #1668 from nexi-lab/fix/delete-config-deprecated-parsers-field\n\nfix(#492): delete deprecated parsers field and NEXUS_PARSERS env handler",
          "timestamp": "2026-02-16T15:30:25+08:00",
          "tree_id": "46bf1ed8844d562a9997381957973a2a9413a59e",
          "url": "https://github.com/nexi-lab/nexus/commit/5b91494ff1976b1d9a9cb6f2ca50de3ff23af36f"
        },
        "date": 1771229879285,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 353.38044246744255,
            "unit": "iter/sec",
            "range": "stddev: 0.007268996832685152",
            "extra": "mean: 2.8298113868939745 msec\nrounds: 473"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 329.9112598773284,
            "unit": "iter/sec",
            "range": "stddev: 0.0006497312309524921",
            "extra": "mean: 3.0311181266496696 msec\nrounds: 379"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16487.28478546168,
            "unit": "iter/sec",
            "range": "stddev: 0.000016682426349622552",
            "extra": "mean: 60.652800810585255 usec\nrounds: 16773"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15241.427968701377,
            "unit": "iter/sec",
            "range": "stddev: 0.000016827086967606182",
            "extra": "mean: 65.61065026541627 usec\nrounds: 18271"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53876.76863823398,
            "unit": "iter/sec",
            "range": "stddev: 0.00002053919866252644",
            "extra": "mean: 18.560875592125694 usec\nrounds: 45640"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 249.1022905892016,
            "unit": "iter/sec",
            "range": "stddev: 0.00020609812513382847",
            "extra": "mean: 4.0144151129028165 msec\nrounds: 248"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 185.59155838028587,
            "unit": "iter/sec",
            "range": "stddev: 0.0003747540159311401",
            "extra": "mean: 5.388176104168234 msec\nrounds: 192"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 70.15316113353846,
            "unit": "iter/sec",
            "range": "stddev: 0.0019681861370268025",
            "extra": "mean: 14.254525153848345 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23730.090065348108,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016891026424939472",
            "extra": "mean: 42.140590164057194 usec\nrounds: 23851"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2618.8936623975233,
            "unit": "iter/sec",
            "range": "stddev: 0.00003162853934933346",
            "extra": "mean: 381.84062772694944 usec\nrounds: 1695"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5030.2033441983285,
            "unit": "iter/sec",
            "range": "stddev: 0.000032695323897829386",
            "extra": "mean: 198.79912034835874 usec\nrounds: 4113"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.906324211721135,
            "unit": "iter/sec",
            "range": "stddev: 0.001492024294123687",
            "extra": "mean: 23.306587510631367 msec\nrounds: 47"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1427.0120324518414,
            "unit": "iter/sec",
            "range": "stddev: 0.0003867764584072444",
            "extra": "mean: 700.7649390887304 usec\nrounds: 1576"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3972.5350390160575,
            "unit": "iter/sec",
            "range": "stddev: 0.000004931742631444838",
            "extra": "mean: 251.72842786244783 usec\nrounds: 4027"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18371.891979195054,
            "unit": "iter/sec",
            "range": "stddev: 0.0000024595299487213376",
            "extra": "mean: 54.43097537980484 usec\nrounds: 18278"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3936.9689172971857,
            "unit": "iter/sec",
            "range": "stddev: 0.0000071350822371254425",
            "extra": "mean: 254.00251335652442 usec\nrounds: 3968"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1007.0519409563356,
            "unit": "iter/sec",
            "range": "stddev: 0.000034879771709076195",
            "extra": "mean: 992.9974406785427 usec\nrounds: 1003"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 553.0828011723887,
            "unit": "iter/sec",
            "range": "stddev: 0.006197044187176857",
            "extra": "mean: 1.8080475434785992 msec\nrounds: 644"
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
          "id": "955b6fea24d3ed54c0912d9c9f6c200ef4de184a",
          "message": "Merge pull request #1662 from nexi-lab/fix/delete-skills-manager-backward-compat\n\nfix(#1519): remove backward-compat static paths fallback from skills/manager",
          "timestamp": "2026-02-16T15:35:17+08:00",
          "tree_id": "f82fb3f9372b71ecec6117cd28d8b6417a3bddf2",
          "url": "https://github.com/nexi-lab/nexus/commit/955b6fea24d3ed54c0912d9c9f6c200ef4de184a"
        },
        "date": 1771230264929,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 313.98421995758224,
            "unit": "iter/sec",
            "range": "stddev: 0.00958064589637874",
            "extra": "mean: 3.1848734313307054 msec\nrounds: 466"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 334.82663740093045,
            "unit": "iter/sec",
            "range": "stddev: 0.0006185378298717743",
            "extra": "mean: 2.9866202037043217 msec\nrounds: 378"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16470.82441032159,
            "unit": "iter/sec",
            "range": "stddev: 0.000017559903263409248",
            "extra": "mean: 60.71341513259901 usec\nrounds: 16243"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15143.389668867696,
            "unit": "iter/sec",
            "range": "stddev: 0.000025962147062704218",
            "extra": "mean: 66.03541359408024 usec\nrounds: 16463"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54175.3032512456,
            "unit": "iter/sec",
            "range": "stddev: 0.000014490118120550108",
            "extra": "mean: 18.45859533747987 usec\nrounds: 42895"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 246.77733430738624,
            "unit": "iter/sec",
            "range": "stddev: 0.00022864039264775847",
            "extra": "mean: 4.052236007843405 msec\nrounds: 255"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 183.60631595152134,
            "unit": "iter/sec",
            "range": "stddev: 0.00043466676257951755",
            "extra": "mean: 5.446435732984457 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 61.81176100592279,
            "unit": "iter/sec",
            "range": "stddev: 0.020519129479855985",
            "extra": "mean: 16.178150949366742 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23740.142415539915,
            "unit": "iter/sec",
            "range": "stddev: 0.0000042091289013789284",
            "extra": "mean: 42.122746464461656 usec\nrounds: 24040"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2644.3000694758352,
            "unit": "iter/sec",
            "range": "stddev: 0.000026953028217735007",
            "extra": "mean: 378.1719070174303 usec\nrounds: 1710"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5069.06563541798,
            "unit": "iter/sec",
            "range": "stddev: 0.000022984328528361835",
            "extra": "mean: 197.27501514537855 usec\nrounds: 4952"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.12190461602681,
            "unit": "iter/sec",
            "range": "stddev: 0.001275341935068499",
            "extra": "mean: 24.31793977777627 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1487.2644058773162,
            "unit": "iter/sec",
            "range": "stddev: 0.00016932046372102432",
            "extra": "mean: 672.3754001294169 usec\nrounds: 1547"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3925.2341004971126,
            "unit": "iter/sec",
            "range": "stddev: 0.000005354833265274201",
            "extra": "mean: 254.76187518939437 usec\nrounds: 3958"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18244.801538953663,
            "unit": "iter/sec",
            "range": "stddev: 0.000002486799427517725",
            "extra": "mean: 54.81013305981676 usec\nrounds: 18285"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3893.1078560171904,
            "unit": "iter/sec",
            "range": "stddev: 0.000007802397561717583",
            "extra": "mean: 256.86419102270673 usec\nrounds: 3921"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 986.1224082634653,
            "unit": "iter/sec",
            "range": "stddev: 0.00015767724093878534",
            "extra": "mean: 1.0140728895522946 msec\nrounds: 1005"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 633.8952568546715,
            "unit": "iter/sec",
            "range": "stddev: 0.000023320457354130642",
            "extra": "mean: 1.5775476929136616 msec\nrounds: 635"
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
          "id": "1e146b41b9e3ed6984e447787420cb37a75d8d4c",
          "message": "Merge pull request #1670 from nexi-lab/fix/delete-pay-x402-tenant-id-compat\n\nfix(#503): delete tenant_id backward-compat in pay/x402.py",
          "timestamp": "2026-02-16T15:42:02+08:00",
          "tree_id": "3b6f51233df547379fbda36435e8068eca09f260",
          "url": "https://github.com/nexi-lab/nexus/commit/1e146b41b9e3ed6984e447787420cb37a75d8d4c"
        },
        "date": 1771230611755,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 420.2401392630187,
            "unit": "iter/sec",
            "range": "stddev: 0.0005003471279670571",
            "extra": "mean: 2.379591825173375 msec\nrounds: 429"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 340.0134233322052,
            "unit": "iter/sec",
            "range": "stddev: 0.0007172435623362628",
            "extra": "mean: 2.941060356381767 msec\nrounds: 376"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14524.590910486104,
            "unit": "iter/sec",
            "range": "stddev: 0.000021077898847748966",
            "extra": "mean: 68.84875492624339 usec\nrounds: 14869"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16593.429235910895,
            "unit": "iter/sec",
            "range": "stddev: 0.000022239474950494252",
            "extra": "mean: 60.264818427997774 usec\nrounds: 16996"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53400.42118729938,
            "unit": "iter/sec",
            "range": "stddev: 0.000021767987655113435",
            "extra": "mean: 18.726444057295893 usec\nrounds: 45064"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 237.26097907099177,
            "unit": "iter/sec",
            "range": "stddev: 0.0006394399195167508",
            "extra": "mean: 4.214768074866563 msec\nrounds: 187"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 181.1992762595771,
            "unit": "iter/sec",
            "range": "stddev: 0.00046703426050621086",
            "extra": "mean: 5.518785839781444 msec\nrounds: 181"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 60.07818926776856,
            "unit": "iter/sec",
            "range": "stddev: 0.02380075200956851",
            "extra": "mean: 16.64497569230988 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23737.717492494467,
            "unit": "iter/sec",
            "range": "stddev: 0.00000177151857257066",
            "extra": "mean: 42.12704950744258 usec\nrounds: 24057"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2611.073103229014,
            "unit": "iter/sec",
            "range": "stddev: 0.000025928404321714445",
            "extra": "mean: 382.9842982041898 usec\nrounds: 1670"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5077.433093362519,
            "unit": "iter/sec",
            "range": "stddev: 0.00006332205666292263",
            "extra": "mean: 196.94991181809 usec\nrounds: 2767"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 40.17079091940653,
            "unit": "iter/sec",
            "range": "stddev: 0.0024831244369415135",
            "extra": "mean: 24.893709511626756 msec\nrounds: 43"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1422.8046729448813,
            "unit": "iter/sec",
            "range": "stddev: 0.00022881598408958683",
            "extra": "mean: 702.8371631154598 usec\nrounds: 1502"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3972.6850004518315,
            "unit": "iter/sec",
            "range": "stddev: 0.000005765016428865998",
            "extra": "mean: 251.71892558465254 usec\nrounds: 4018"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18322.806398875357,
            "unit": "iter/sec",
            "range": "stddev: 0.000002735440043861799",
            "extra": "mean: 54.57679234450567 usec\nrounds: 18261"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3906.560708873092,
            "unit": "iter/sec",
            "range": "stddev: 0.000015981730461326756",
            "extra": "mean: 255.97963900283668 usec\nrounds: 3892"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 989.1547397283512,
            "unit": "iter/sec",
            "range": "stddev: 0.00014147691268368348",
            "extra": "mean: 1.0109641695440161 msec\nrounds: 985"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 641.9090318428117,
            "unit": "iter/sec",
            "range": "stddev: 0.00002354914556812422",
            "extra": "mean: 1.5578531386747592 msec\nrounds: 649"
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
          "id": "dadfb6f1b6e8db8b10a040dcaf7b76575e129a8b",
          "message": "Merge pull request #1672 from nexi-lab/fix/temporal-coref-resolver-env-vars\n\nfix(#491): remove direct env var reads from kernel resolvers",
          "timestamp": "2026-02-16T15:44:26+08:00",
          "tree_id": "10529ec50abaac103de159b94f1dbc4c84bc7d8b",
          "url": "https://github.com/nexi-lab/nexus/commit/dadfb6f1b6e8db8b10a040dcaf7b76575e129a8b"
        },
        "date": 1771230930900,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 350.51370740857146,
            "unit": "iter/sec",
            "range": "stddev: 0.007593975574537519",
            "extra": "mean: 2.8529554732487648 msec\nrounds: 486"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 318.6050444652891,
            "unit": "iter/sec",
            "range": "stddev: 0.0007577920242484563",
            "extra": "mean: 3.1386822568308284 msec\nrounds: 366"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16357.55934351239,
            "unit": "iter/sec",
            "range": "stddev: 0.000014544382674637295",
            "extra": "mean: 61.13381458686942 usec\nrounds: 16714"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 17482.324504271193,
            "unit": "iter/sec",
            "range": "stddev: 0.00001089037856855065",
            "extra": "mean: 57.200631400914965 usec\nrounds: 17751"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53701.26385047225,
            "unit": "iter/sec",
            "range": "stddev: 0.0000158248987343904",
            "extra": "mean: 18.621535664122103 usec\nrounds: 45368"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 245.9151685957684,
            "unit": "iter/sec",
            "range": "stddev: 0.00031423713762520456",
            "extra": "mean: 4.06644293522123 msec\nrounds: 247"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 180.94316756150786,
            "unit": "iter/sec",
            "range": "stddev: 0.00042431172209840274",
            "extra": "mean: 5.526597182289687 msec\nrounds: 192"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 69.67639867688237,
            "unit": "iter/sec",
            "range": "stddev: 0.0014086409978922947",
            "extra": "mean: 14.352062089738656 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23674.158708550003,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017346484998393677",
            "extra": "mean: 42.24014936754 usec\nrounds: 23874"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2656.179050331005,
            "unit": "iter/sec",
            "range": "stddev: 0.00002876381868677718",
            "extra": "mean: 376.4806442078455 usec\nrounds: 1692"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5084.514974328908,
            "unit": "iter/sec",
            "range": "stddev: 0.000025773786326528035",
            "extra": "mean: 196.67559345362878 usec\nrounds: 4216"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 39.93728835221641,
            "unit": "iter/sec",
            "range": "stddev: 0.0021326705144562737",
            "extra": "mean: 25.039256325586333 msec\nrounds: 43"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1439.8629109852568,
            "unit": "iter/sec",
            "range": "stddev: 0.0002855659631721702",
            "extra": "mean: 694.5105623393888 usec\nrounds: 1556"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3951.8849027005817,
            "unit": "iter/sec",
            "range": "stddev: 0.000005755794532291864",
            "extra": "mean: 253.04380684686302 usec\nrounds: 4002"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18129.40974206046,
            "unit": "iter/sec",
            "range": "stddev: 0.000004232652449948452",
            "extra": "mean: 55.15899382427147 usec\nrounds: 18135"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3924.0999627445435,
            "unit": "iter/sec",
            "range": "stddev: 0.000013284557990315173",
            "extra": "mean: 254.83550610178463 usec\nrounds: 4015"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1022.2363294175486,
            "unit": "iter/sec",
            "range": "stddev: 0.000043392712762100535",
            "extra": "mean: 978.2473692456044 usec\nrounds: 1021"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 545.2066191693884,
            "unit": "iter/sec",
            "range": "stddev: 0.007425516507897819",
            "extra": "mean: 1.8341670200620095 msec\nrounds: 648"
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
          "id": "f4be816c7677dcfed6de7b2a2919470c4ae0da5d",
          "message": "Merge pull request #1679 from nexi-lab/fix/sdk-init-remove-concrete-exports\n\nfix(#365): remove concrete driver implementations from SDK exports",
          "timestamp": "2026-02-16T15:49:27+08:00",
          "tree_id": "0529cb2eaa5c8d5488f3a0d765cc5ae2abd6ad8e",
          "url": "https://github.com/nexi-lab/nexus/commit/f4be816c7677dcfed6de7b2a2919470c4ae0da5d"
        },
        "date": 1771231285143,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 348.3726120342148,
            "unit": "iter/sec",
            "range": "stddev: 0.008061752252891291",
            "extra": "mean: 2.8704897154825324 msec\nrounds: 478"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 315.7790120047509,
            "unit": "iter/sec",
            "range": "stddev: 0.001120117783577826",
            "extra": "mean: 3.1667715775390257 msec\nrounds: 374"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16741.670416107223,
            "unit": "iter/sec",
            "range": "stddev: 0.000016225890188862397",
            "extra": "mean: 59.73119617967728 usec\nrounds: 16490"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16705.37545281856,
            "unit": "iter/sec",
            "range": "stddev: 0.000010810733468584273",
            "extra": "mean: 59.86097126786087 usec\nrounds: 15801"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53009.754459823205,
            "unit": "iter/sec",
            "range": "stddev: 0.000016210908009612136",
            "extra": "mean: 18.864452593492267 usec\nrounds: 44382"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 216.298536166402,
            "unit": "iter/sec",
            "range": "stddev: 0.0008764314079727155",
            "extra": "mean: 4.62323979497801 msec\nrounds: 239"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 177.9474584929894,
            "unit": "iter/sec",
            "range": "stddev: 0.0005033697402397851",
            "extra": "mean: 5.619636315510497 msec\nrounds: 187"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 65.88183981275998,
            "unit": "iter/sec",
            "range": "stddev: 0.0018765148085981844",
            "extra": "mean: 15.178689648650648 msec\nrounds: 74"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23674.426965352784,
            "unit": "iter/sec",
            "range": "stddev: 0.0000022953280033090035",
            "extra": "mean: 42.23967074106955 usec\nrounds: 23890"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2562.3008097128954,
            "unit": "iter/sec",
            "range": "stddev: 0.000036182076247229844",
            "extra": "mean: 390.2742395464682 usec\nrounds: 1674"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4704.999312098239,
            "unit": "iter/sec",
            "range": "stddev: 0.00006085214900671462",
            "extra": "mean: 212.5398822968245 usec\nrounds: 2107"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 39.82921019775002,
            "unit": "iter/sec",
            "range": "stddev: 0.0011696900395504449",
            "extra": "mean: 25.10720134883545 msec\nrounds: 43"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1362.8319349943977,
            "unit": "iter/sec",
            "range": "stddev: 0.00038783711371149707",
            "extra": "mean: 733.7661925306371 usec\nrounds: 1553"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3881.4171405813786,
            "unit": "iter/sec",
            "range": "stddev: 0.00002594231332608958",
            "extra": "mean: 257.63785848851455 usec\nrounds: 4035"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18269.298018787387,
            "unit": "iter/sec",
            "range": "stddev: 0.0000035938794491642883",
            "extra": "mean: 54.7366406181366 usec\nrounds: 18440"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3872.0302137740937,
            "unit": "iter/sec",
            "range": "stddev: 0.00003085616518276399",
            "extra": "mean: 258.2624475508143 usec\nrounds: 3899"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1001.1520285333312,
            "unit": "iter/sec",
            "range": "stddev: 0.00004639688375165282",
            "extra": "mean: 998.8492971092325 usec\nrounds: 865"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 534.8992961884766,
            "unit": "iter/sec",
            "range": "stddev: 0.007634548705981409",
            "extra": "mean: 1.869510779179715 msec\nrounds: 634"
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
          "id": "bf831eab0c0aeedc43bb526bccfc1dc05b97b4df",
          "message": "Merge pull request #1675 from nexi-lab/fix/search-semantic-import-protocol\n\nfix(#483): use NexusFilesystem protocol in search/semantic",
          "timestamp": "2026-02-16T15:57:23+08:00",
          "tree_id": "d11f7feff0b69ab253101e2726202cbc7b578d82",
          "url": "https://github.com/nexi-lab/nexus/commit/bf831eab0c0aeedc43bb526bccfc1dc05b97b4df"
        },
        "date": 1771232392715,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 333.24579472076766,
            "unit": "iter/sec",
            "range": "stddev: 0.007983730737436625",
            "extra": "mean: 3.0007880544686754 msec\nrounds: 459"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 317.2740971599281,
            "unit": "iter/sec",
            "range": "stddev: 0.0008728277449914166",
            "extra": "mean: 3.1518488554580326 msec\nrounds: 339"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 17464.81492851424,
            "unit": "iter/sec",
            "range": "stddev: 0.000012977090221661125",
            "extra": "mean: 57.25797863264685 usec\nrounds: 16614"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15465.427381227832,
            "unit": "iter/sec",
            "range": "stddev: 0.000018603964591923698",
            "extra": "mean: 64.66035340308895 usec\nrounds: 17821"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 51661.62510195491,
            "unit": "iter/sec",
            "range": "stddev: 0.00009015905506304694",
            "extra": "mean: 19.35672751343936 usec\nrounds: 44960"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 236.11746174624187,
            "unit": "iter/sec",
            "range": "stddev: 0.00035223228876385025",
            "extra": "mean: 4.235180204819885 msec\nrounds: 249"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 182.73861058875394,
            "unit": "iter/sec",
            "range": "stddev: 0.00038572310215342226",
            "extra": "mean: 5.472297270829429 msec\nrounds: 192"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 53.848585164268314,
            "unit": "iter/sec",
            "range": "stddev: 0.023631133225220067",
            "extra": "mean: 18.570590052634447 msec\nrounds: 76"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23750.35986064188,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018183411457112132",
            "extra": "mean: 42.10462518747595 usec\nrounds: 23988"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2642.7883042376898,
            "unit": "iter/sec",
            "range": "stddev: 0.000032206852711890576",
            "extra": "mean: 378.388234273819 usec\nrounds: 1669"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4991.200298897554,
            "unit": "iter/sec",
            "range": "stddev: 0.000025610232678356045",
            "extra": "mean: 200.35260861418004 usec\nrounds: 4806"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 39.967816352712454,
            "unit": "iter/sec",
            "range": "stddev: 0.0018042605462201182",
            "extra": "mean: 25.02013097676111 msec\nrounds: 43"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1371.8587473399607,
            "unit": "iter/sec",
            "range": "stddev: 0.00037062346442026737",
            "extra": "mean: 728.9380207248041 usec\nrounds: 1544"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3974.818637355702,
            "unit": "iter/sec",
            "range": "stddev: 0.0000062485630712472525",
            "extra": "mean: 251.58380576208188 usec\nrounds: 4026"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18174.863176630515,
            "unit": "iter/sec",
            "range": "stddev: 0.0000041880941468272136",
            "extra": "mean: 55.02104694168006 usec\nrounds: 18278"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3911.7771867865845,
            "unit": "iter/sec",
            "range": "stddev: 0.000009723560379389356",
            "extra": "mean: 255.63828210304382 usec\nrounds: 3956"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 998.2639572411355,
            "unit": "iter/sec",
            "range": "stddev: 0.00004034417781532036",
            "extra": "mean: 1.0017390618445872 msec\nrounds: 954"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 658.9652113857826,
            "unit": "iter/sec",
            "range": "stddev: 0.00002860646780036225",
            "extra": "mean: 1.517530793313098 msec\nrounds: 658"
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
          "id": "65e29c8c4458389455059772a0cb3122d1a9f625",
          "message": "Merge pull request #1677 from nexi-lab/fix/sandbox-auth-service-imports\n\nfix(#500): remove top-level services import from sandbox/auth_service",
          "timestamp": "2026-02-16T16:04:48+08:00",
          "tree_id": "085cfbd380d1ea33b6036e26594af7728f22ebd9",
          "url": "https://github.com/nexi-lab/nexus/commit/65e29c8c4458389455059772a0cb3122d1a9f625"
        },
        "date": 1771232454780,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 359.4001563890525,
            "unit": "iter/sec",
            "range": "stddev: 0.0018015395900475798",
            "extra": "mean: 2.7824139256007863 msec\nrounds: 457"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 309.96712644913293,
            "unit": "iter/sec",
            "range": "stddev: 0.001076640396165017",
            "extra": "mean: 3.226148564383664 msec\nrounds: 365"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15706.199643808475,
            "unit": "iter/sec",
            "range": "stddev: 0.000016615656911417304",
            "extra": "mean: 63.66912573877851 usec\nrounds: 16415"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 14458.859834654553,
            "unit": "iter/sec",
            "range": "stddev: 0.000016834143860586843",
            "extra": "mean: 69.16174659935707 usec\nrounds: 16468"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 51082.89436146481,
            "unit": "iter/sec",
            "range": "stddev: 0.00001651077689997298",
            "extra": "mean: 19.576024665398872 usec\nrounds: 44840"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 235.8812109823312,
            "unit": "iter/sec",
            "range": "stddev: 0.0003406464648868357",
            "extra": "mean: 4.239422020242661 msec\nrounds: 247"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 179.16800310107251,
            "unit": "iter/sec",
            "range": "stddev: 0.000601800957556807",
            "extra": "mean: 5.581353716577834 msec\nrounds: 187"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 55.39180427352874,
            "unit": "iter/sec",
            "range": "stddev: 0.023677451698740498",
            "extra": "mean: 18.053212259740224 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23720.374037163052,
            "unit": "iter/sec",
            "range": "stddev: 0.000004345343809910569",
            "extra": "mean: 42.157851239330604 usec\nrounds: 24005"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2641.832781117014,
            "unit": "iter/sec",
            "range": "stddev: 0.000024119399201547484",
            "extra": "mean: 378.5250933169139 usec\nrounds: 1661"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4994.916357499007,
            "unit": "iter/sec",
            "range": "stddev: 0.000027800875562730037",
            "extra": "mean: 200.20355265782823 usec\nrounds: 2953"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 38.64383141307043,
            "unit": "iter/sec",
            "range": "stddev: 0.0038966517798761442",
            "extra": "mean: 25.8773512727253 msec\nrounds: 44"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1375.1484246943894,
            "unit": "iter/sec",
            "range": "stddev: 0.0003638765842866789",
            "extra": "mean: 727.1942301226417 usec\nrounds: 1547"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3959.0576510249834,
            "unit": "iter/sec",
            "range": "stddev: 0.000010733481129481757",
            "extra": "mean: 252.58535948348822 usec\nrounds: 4028"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18400.85755172048,
            "unit": "iter/sec",
            "range": "stddev: 0.000002632405769398567",
            "extra": "mean: 54.34529326631844 usec\nrounds: 18311"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3956.705347057313,
            "unit": "iter/sec",
            "range": "stddev: 0.000014863185672295665",
            "extra": "mean: 252.73552420164967 usec\nrounds: 4008"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 959.1612443205095,
            "unit": "iter/sec",
            "range": "stddev: 0.000014842651052262365",
            "extra": "mean: 1.042577570686169 msec\nrounds: 962"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 559.7849852682305,
            "unit": "iter/sec",
            "range": "stddev: 0.006528096030979263",
            "extra": "mean: 1.7864001827788094 msec\nrounds: 662"
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
          "id": "a6624f4d32209505b734ea7737eb7da2ec3ad189",
          "message": "Merge pull request #1680 from nexi-lab/fix/sandbox-docker-hardcoded-localhost\n\nfix(#501): replace hardcoded localhost→host.docker.internal with configurable alias",
          "timestamp": "2026-02-16T16:04:54+08:00",
          "tree_id": "a514604d505cc125bcbb9f684a2e035945ddafa8",
          "url": "https://github.com/nexi-lab/nexus/commit/a6624f4d32209505b734ea7737eb7da2ec3ad189"
        },
        "date": 1771232634417,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 368.1167500190445,
            "unit": "iter/sec",
            "range": "stddev: 0.00689034133548354",
            "extra": "mean: 2.7165294704689886 msec\nrounds: 491"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 341.0331825914617,
            "unit": "iter/sec",
            "range": "stddev: 0.0005590546147460098",
            "extra": "mean: 2.932265981864712 msec\nrounds: 386"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 17307.324613478453,
            "unit": "iter/sec",
            "range": "stddev: 0.000013468297878269152",
            "extra": "mean: 57.77900526700865 usec\nrounds: 16328"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15202.548808013595,
            "unit": "iter/sec",
            "range": "stddev: 0.000019399360824454332",
            "extra": "mean: 65.77844364313951 usec\nrounds: 16360"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53802.53130875599,
            "unit": "iter/sec",
            "range": "stddev: 0.000013979222774578706",
            "extra": "mean: 18.58648609414511 usec\nrounds: 44981"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 241.1605857076982,
            "unit": "iter/sec",
            "range": "stddev: 0.00024928395990749734",
            "extra": "mean: 4.14661457661271 msec\nrounds: 248"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 178.76113180313118,
            "unit": "iter/sec",
            "range": "stddev: 0.0007150278042241576",
            "extra": "mean: 5.594057219895516 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 70.90990605153522,
            "unit": "iter/sec",
            "range": "stddev: 0.0012441597930155915",
            "extra": "mean: 14.102401987011936 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23732.9373042274,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017383472463969038",
            "extra": "mean: 42.135534560312365 usec\nrounds: 24016"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2641.1090387806676,
            "unit": "iter/sec",
            "range": "stddev: 0.00002883425037544058",
            "extra": "mean: 378.6288204373699 usec\nrounds: 1693"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5103.829443458716,
            "unit": "iter/sec",
            "range": "stddev: 0.000029774607758829784",
            "extra": "mean: 195.93131218004206 usec\nrounds: 2931"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.86759957112125,
            "unit": "iter/sec",
            "range": "stddev: 0.0006315679068278082",
            "extra": "mean: 23.327641622221208 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1428.8294843692536,
            "unit": "iter/sec",
            "range": "stddev: 0.0003390108857597792",
            "extra": "mean: 699.873575496269 usec\nrounds: 1510"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3928.727616411034,
            "unit": "iter/sec",
            "range": "stddev: 0.0000057575822251191444",
            "extra": "mean: 254.53533500841647 usec\nrounds: 3979"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18288.32189786832,
            "unit": "iter/sec",
            "range": "stddev: 0.000003703509157872998",
            "extra": "mean: 54.67970246720994 usec\nrounds: 17914"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3915.707606019329,
            "unit": "iter/sec",
            "range": "stddev: 0.000007051081273931644",
            "extra": "mean: 255.38168336746435 usec\nrounds: 3932"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 982.9777007931442,
            "unit": "iter/sec",
            "range": "stddev: 0.0001018467147832136",
            "extra": "mean: 1.0173170756499572 msec\nrounds: 846"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 652.7600771595135,
            "unit": "iter/sec",
            "range": "stddev: 0.00006171605482999732",
            "extra": "mean: 1.531956433903712 msec\nrounds: 643"
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
          "id": "97e0cef0f1a89e91b7dc1dae2993a42e2f13fbaa",
          "message": "Merge pull request #1665 from nexi-lab/fix/delete-permissions-deprecated-aliases\n\nfix(#419): delete deprecated aliases and params from services/permissions/",
          "timestamp": "2026-02-16T16:13:03+08:00",
          "tree_id": "18fa0310c7f437b6d2ce9fbdcb2b8eb4c1072a67",
          "url": "https://github.com/nexi-lab/nexus/commit/97e0cef0f1a89e91b7dc1dae2993a42e2f13fbaa"
        },
        "date": 1771232732917,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 409.1982099067661,
            "unit": "iter/sec",
            "range": "stddev: 0.0027606572863687075",
            "extra": "mean: 2.4438034570773057 msec\nrounds: 431"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 328.202154449677,
            "unit": "iter/sec",
            "range": "stddev: 0.0010919629387190224",
            "extra": "mean: 3.0469026069520497 msec\nrounds: 374"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16081.059679427022,
            "unit": "iter/sec",
            "range": "stddev: 0.000019374488801789434",
            "extra": "mean: 62.184956708999074 usec\nrounds: 13906"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15452.744898869043,
            "unit": "iter/sec",
            "range": "stddev: 0.00001684943555344588",
            "extra": "mean: 64.71342189006097 usec\nrounds: 17917"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 47799.91400320986,
            "unit": "iter/sec",
            "range": "stddev: 0.00001731270670269968",
            "extra": "mean: 20.920539730110143 usec\nrounds: 41178"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 244.54801681803525,
            "unit": "iter/sec",
            "range": "stddev: 0.00026818175969706045",
            "extra": "mean: 4.089176485712767 msec\nrounds: 245"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 170.4453265170101,
            "unit": "iter/sec",
            "range": "stddev: 0.0010062753872895353",
            "extra": "mean: 5.866983979171773 msec\nrounds: 192"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 59.910063703661216,
            "unit": "iter/sec",
            "range": "stddev: 0.02243125486099367",
            "extra": "mean: 16.691686474352522 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23736.325577214157,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018128393010174923",
            "extra": "mean: 42.12951986806065 usec\nrounds: 23908"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2608.0574662308586,
            "unit": "iter/sec",
            "range": "stddev: 0.000030298190996729822",
            "extra": "mean: 383.42713415942904 usec\nrounds: 1692"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4908.93526524432,
            "unit": "iter/sec",
            "range": "stddev: 0.000030750088152663",
            "extra": "mean: 203.71016238084968 usec\nrounds: 2722"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.39790474301031,
            "unit": "iter/sec",
            "range": "stddev: 0.0010256917442575649",
            "extra": "mean: 23.586071200012764 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1423.3611420498833,
            "unit": "iter/sec",
            "range": "stddev: 0.0003094750186623448",
            "extra": "mean: 702.5623859309726 usec\nrounds: 1578"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3975.0427519895647,
            "unit": "iter/sec",
            "range": "stddev: 0.000008350412053835774",
            "extra": "mean: 251.56962135803093 usec\nrounds: 3980"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 17913.170695654208,
            "unit": "iter/sec",
            "range": "stddev: 0.000002799123507635524",
            "extra": "mean: 55.8248462536341 usec\nrounds: 17711"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3922.6494084335513,
            "unit": "iter/sec",
            "range": "stddev: 0.000009167261181318413",
            "extra": "mean: 254.92974157976926 usec\nrounds: 3978"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1012.5059990868352,
            "unit": "iter/sec",
            "range": "stddev: 0.000016840288844368787",
            "extra": "mean: 987.6484691467367 usec\nrounds: 1021"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 627.2093300239738,
            "unit": "iter/sec",
            "range": "stddev: 0.00006864540064746397",
            "extra": "mean: 1.5943640378592216 msec\nrounds: 634"
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
          "id": "8ce02c72d2e42989f364fa51b114bb8600a332a3",
          "message": "Merge pull request #1669 from nexi-lab/fix/core-virtual-views-parser-import\n\nfix(#416): remove parsers import from core/virtual_views via callback injection",
          "timestamp": "2026-02-16T16:16:10+08:00",
          "tree_id": "c2dd8a0ffee881ba9a3ff02e27e3c87d97da0ea3",
          "url": "https://github.com/nexi-lab/nexus/commit/8ce02c72d2e42989f364fa51b114bb8600a332a3"
        },
        "date": 1771232976720,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 405.3308942837259,
            "unit": "iter/sec",
            "range": "stddev: 0.000437436920013427",
            "extra": "mean: 2.467120108787005 msec\nrounds: 478"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 338.7912011928591,
            "unit": "iter/sec",
            "range": "stddev: 0.0010284664941119992",
            "extra": "mean: 2.9516705170591 msec\nrounds: 381"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 13286.592419479986,
            "unit": "iter/sec",
            "range": "stddev: 0.000016830590914716853",
            "extra": "mean: 75.26384255859774 usec\nrounds: 14164"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 17228.325771640946,
            "unit": "iter/sec",
            "range": "stddev: 0.000012573930711597457",
            "extra": "mean: 58.04394537547412 usec\nrounds: 17959"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53732.6229851207,
            "unit": "iter/sec",
            "range": "stddev: 0.000014899733638333544",
            "extra": "mean: 18.61066786702212 usec\nrounds: 43874"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 237.5692706308162,
            "unit": "iter/sec",
            "range": "stddev: 0.0005665402129708298",
            "extra": "mean: 4.209298607284968 msec\nrounds: 247"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.95831123910173,
            "unit": "iter/sec",
            "range": "stddev: 0.00033560860065573",
            "extra": "mean: 5.406623759163041 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 60.377332551976366,
            "unit": "iter/sec",
            "range": "stddev: 0.02229957454183007",
            "extra": "mean: 16.56250711538376 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23745.32866325567,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016849920655400338",
            "extra": "mean: 42.11354638133243 usec\nrounds: 24029"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2585.670193210401,
            "unit": "iter/sec",
            "range": "stddev: 0.000044958580455055866",
            "extra": "mean: 386.74692643549696 usec\nrounds: 1672"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4964.53517836382,
            "unit": "iter/sec",
            "range": "stddev: 0.00004546858094261048",
            "extra": "mean: 201.4287267734849 usec\nrounds: 2551"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.62005338579601,
            "unit": "iter/sec",
            "range": "stddev: 0.0009656049537744878",
            "extra": "mean: 23.463133444437922 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1472.9439877759908,
            "unit": "iter/sec",
            "range": "stddev: 0.00024125030232155072",
            "extra": "mean: 678.9124422238945 usec\nrounds: 1601"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3979.2171571785043,
            "unit": "iter/sec",
            "range": "stddev: 0.000007901029080158142",
            "extra": "mean: 251.3057117770014 usec\nrounds: 4042"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18508.28251310848,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025566800606295133",
            "extra": "mean: 54.029864699317756 usec\nrounds: 18433"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3918.845492476754,
            "unit": "iter/sec",
            "range": "stddev: 0.0000093070537377485",
            "extra": "mean: 255.17719489573165 usec\nrounds: 3879"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1022.4592567524195,
            "unit": "iter/sec",
            "range": "stddev: 0.000014082023782397013",
            "extra": "mean: 978.0340814520517 usec\nrounds: 1019"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 648.6773096343913,
            "unit": "iter/sec",
            "range": "stddev: 0.000026345723478823663",
            "extra": "mean: 1.5415985500766503 msec\nrounds: 649"
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
          "id": "3a55c0c2ae998696802771a3de2799a7b8eda4b1",
          "message": "Merge pull request #1684 from nexi-lab/fix/skills-hardcoded-default-zone-id\n\nfix(#520): skills — change zone ID fallback from \"default\" to \"root\"",
          "timestamp": "2026-02-16T16:16:16+08:00",
          "tree_id": "20bea976e2651875c57922143eb203a6ad0b88f3",
          "url": "https://github.com/nexi-lab/nexus/commit/3a55c0c2ae998696802771a3de2799a7b8eda4b1"
        },
        "date": 1771233254288,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 419.85715297647283,
            "unit": "iter/sec",
            "range": "stddev: 0.007969902286845925",
            "extra": "mean: 2.381762446848288 msec\nrounds: 555"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 353.95654539013776,
            "unit": "iter/sec",
            "range": "stddev: 0.0009187876440743604",
            "extra": "mean: 2.825205559902221 msec\nrounds: 409"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 25025.82282381139,
            "unit": "iter/sec",
            "range": "stddev: 0.000010901587565482962",
            "extra": "mean: 39.958726114232974 usec\nrounds: 20567"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 23148.446081509926,
            "unit": "iter/sec",
            "range": "stddev: 0.00001530398831161836",
            "extra": "mean: 43.19944399199913 usec\nrounds: 20622"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54816.47837401622,
            "unit": "iter/sec",
            "range": "stddev: 0.00001102241534521795",
            "extra": "mean: 18.242689601052774 usec\nrounds: 47774"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 235.58103598287087,
            "unit": "iter/sec",
            "range": "stddev: 0.00026416609396113616",
            "extra": "mean: 4.244823849372621 msec\nrounds: 239"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 178.62231362040865,
            "unit": "iter/sec",
            "range": "stddev: 0.0003692503761376222",
            "extra": "mean: 5.598404699454884 msec\nrounds: 183"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 56.45706751978146,
            "unit": "iter/sec",
            "range": "stddev: 0.02602997363528992",
            "extra": "mean: 17.712574243244557 msec\nrounds: 74"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 21076.231660785543,
            "unit": "iter/sec",
            "range": "stddev: 0.000002089449809610669",
            "extra": "mean: 47.44681193937534 usec\nrounds: 21291"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2291.664787460615,
            "unit": "iter/sec",
            "range": "stddev: 0.000037469723809388995",
            "extra": "mean: 436.36399418961105 usec\nrounds: 1721"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4997.525283837152,
            "unit": "iter/sec",
            "range": "stddev: 0.00004038270633198143",
            "extra": "mean: 200.09903766453576 usec\nrounds: 4779"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 47.52415015825881,
            "unit": "iter/sec",
            "range": "stddev: 0.0005951577257678206",
            "extra": "mean: 21.041933346938947 msec\nrounds: 49"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1443.023977070287,
            "unit": "iter/sec",
            "range": "stddev: 0.0002746447526646377",
            "extra": "mean: 692.9891782049662 usec\nrounds: 1560"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3276.1486998355927,
            "unit": "iter/sec",
            "range": "stddev: 0.000006290679124984811",
            "extra": "mean: 305.2364503632522 usec\nrounds: 3304"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 15480.966030474474,
            "unit": "iter/sec",
            "range": "stddev: 0.00000281094889085883",
            "extra": "mean: 64.59545212046119 usec\nrounds: 15445"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3563.9631756397084,
            "unit": "iter/sec",
            "range": "stddev: 0.00002261473865687464",
            "extra": "mean: 280.5865130243683 usec\nrounds: 3647"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1076.1537410084868,
            "unit": "iter/sec",
            "range": "stddev: 0.000016561495978571526",
            "extra": "mean: 929.2352587678398 usec\nrounds: 1055"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 600.3128878639064,
            "unit": "iter/sec",
            "range": "stddev: 0.00005621511103743822",
            "extra": "mean: 1.6657979867103976 msec\nrounds: 602"
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
            "email": "songym@sudoprivacy.com",
            "name": "elfenlieds7",
            "username": "elfenlieds7"
          },
          "distinct": true,
          "id": "36c56dae2e17d335e54ae1c741388713e4c26854",
          "message": "chore(#30): bump version to 0.7.2.dev0\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>",
          "timestamp": "2026-02-16T16:17:45+08:00",
          "tree_id": "e065d945a414b7d0782b54dfc256551eb5753ddc",
          "url": "https://github.com/nexi-lab/nexus/commit/36c56dae2e17d335e54ae1c741388713e4c26854"
        },
        "date": 1771233379373,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 390.0912116966519,
            "unit": "iter/sec",
            "range": "stddev: 0.00444840329216832",
            "extra": "mean: 2.563503021897437 msec\nrounds: 411"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 313.9128873176205,
            "unit": "iter/sec",
            "range": "stddev: 0.0006191233110151086",
            "extra": "mean: 3.185597152588989 msec\nrounds: 367"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14814.152312253556,
            "unit": "iter/sec",
            "range": "stddev: 0.000019968724545180566",
            "extra": "mean: 67.50301866228605 usec\nrounds: 14039"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16159.537454857922,
            "unit": "iter/sec",
            "range": "stddev: 0.0000171878401529259",
            "extra": "mean: 61.88295938503966 usec\nrounds: 17629"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 52666.147816202676,
            "unit": "iter/sec",
            "range": "stddev: 0.000019802903200540842",
            "extra": "mean: 18.987528829521708 usec\nrounds: 43341"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 227.0277090522148,
            "unit": "iter/sec",
            "range": "stddev: 0.00039832602824150544",
            "extra": "mean: 4.404748672198454 msec\nrounds: 241"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 177.27351811297868,
            "unit": "iter/sec",
            "range": "stddev: 0.0004992091276439103",
            "extra": "mean: 5.6410004756756 msec\nrounds: 185"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 64.45019047504952,
            "unit": "iter/sec",
            "range": "stddev: 0.0016229479505710288",
            "extra": "mean: 15.515857945945529 msec\nrounds: 74"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23735.167935857295,
            "unit": "iter/sec",
            "range": "stddev: 0.000001810837309871015",
            "extra": "mean: 42.13157466180282 usec\nrounds: 23948"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2513.4763456164037,
            "unit": "iter/sec",
            "range": "stddev: 0.0000664662464725614",
            "extra": "mean: 397.85534554325017 usec\nrounds: 1638"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5002.614460245579,
            "unit": "iter/sec",
            "range": "stddev: 0.00008735944639905176",
            "extra": "mean: 199.8954762448174 usec\nrounds: 5241"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.64310869737637,
            "unit": "iter/sec",
            "range": "stddev: 0.00048000945048289084",
            "extra": "mean: 24.01357706666608 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1402.8884440694042,
            "unit": "iter/sec",
            "range": "stddev: 0.00022990307490648048",
            "extra": "mean: 712.8150525634579 usec\nrounds: 1541"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3960.550294589808,
            "unit": "iter/sec",
            "range": "stddev: 0.000006663416140420521",
            "extra": "mean: 252.49016566359987 usec\nrounds: 3821"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18290.190013874322,
            "unit": "iter/sec",
            "range": "stddev: 0.000002784648252160723",
            "extra": "mean: 54.674117613946805 usec\nrounds: 17736"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3947.38739265693,
            "unit": "iter/sec",
            "range": "stddev: 0.000009728564888986862",
            "extra": "mean: 253.3321157837803 usec\nrounds: 3757"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1021.1239453674015,
            "unit": "iter/sec",
            "range": "stddev: 0.000018758732913413596",
            "extra": "mean: 979.3130447452185 usec\nrounds: 961"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 532.3087618737165,
            "unit": "iter/sec",
            "range": "stddev: 0.008052473384614827",
            "extra": "mean: 1.8786089420734298 msec\nrounds: 656"
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
          "id": "9a81442146b752efe55aa676b7fe2b3bd97e96a1",
          "message": "Merge pull request #1683 from nexi-lab/fix/rebac-namespace-configs-to-services\n\nrefactor(#319): move default namespace configs from core/rebac to services/",
          "timestamp": "2026-02-16T16:18:14+08:00",
          "tree_id": "ed7f757a10b5746f9ab2dd1c41dc90b10e0a13fe",
          "url": "https://github.com/nexi-lab/nexus/commit/9a81442146b752efe55aa676b7fe2b3bd97e96a1"
        },
        "date": 1771233448225,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 443.7027422502141,
            "unit": "iter/sec",
            "range": "stddev: 0.007262217329766497",
            "extra": "mean: 2.2537611440681093 msec\nrounds: 590"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 371.7540939174757,
            "unit": "iter/sec",
            "range": "stddev: 0.0009046024305892695",
            "extra": "mean: 2.6899502019256483 msec\nrounds: 416"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 17834.121406426188,
            "unit": "iter/sec",
            "range": "stddev: 0.000016512818335525344",
            "extra": "mean: 56.072288463824684 usec\nrounds: 21299"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 19623.02378651468,
            "unit": "iter/sec",
            "range": "stddev: 0.000015123261921014385",
            "extra": "mean: 50.960545677329264 usec\nrounds: 20448"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 57317.8386569238,
            "unit": "iter/sec",
            "range": "stddev: 0.000040598104525841894",
            "extra": "mean: 17.446575506545265 usec\nrounds: 50585"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 241.57908410819905,
            "unit": "iter/sec",
            "range": "stddev: 0.0008099076317568158",
            "extra": "mean: 4.139431208175777 msec\nrounds: 269"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 186.2790253962489,
            "unit": "iter/sec",
            "range": "stddev: 0.0009691021287165658",
            "extra": "mean: 5.368290916665582 msec\nrounds: 204"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 72.65427478166838,
            "unit": "iter/sec",
            "range": "stddev: 0.0012434319783928376",
            "extra": "mean: 13.763815040547527 msec\nrounds: 74"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 19949.975469016073,
            "unit": "iter/sec",
            "range": "stddev: 0.0000012437577231220233",
            "extra": "mean: 50.12537491853466 usec\nrounds: 20015"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2816.370877388139,
            "unit": "iter/sec",
            "range": "stddev: 0.00000595649967998873",
            "extra": "mean: 355.06687277187916 usec\nrounds: 2861"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 6682.430123789028,
            "unit": "iter/sec",
            "range": "stddev: 0.000012770677511045999",
            "extra": "mean: 149.64615887864855 usec\nrounds: 5350"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 50.77347575308488,
            "unit": "iter/sec",
            "range": "stddev: 0.00037640265884259595",
            "extra": "mean: 19.695322905665805 msec\nrounds: 53"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1812.5626841265014,
            "unit": "iter/sec",
            "range": "stddev: 0.0003686670643106215",
            "extra": "mean: 551.7050575726232 usec\nrounds: 1789"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 6617.097944683483,
            "unit": "iter/sec",
            "range": "stddev: 0.0000030925177544045237",
            "extra": "mean: 151.1236509357477 usec\nrounds: 6569"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 26588.41301073865,
            "unit": "iter/sec",
            "range": "stddev: 0.000004521944666060136",
            "extra": "mean: 37.61036808011502 usec\nrounds: 26717"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 4331.835862707282,
            "unit": "iter/sec",
            "range": "stddev: 0.0000080902176340113",
            "extra": "mean: 230.84900529334152 usec\nrounds: 4345"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1003.7812501936688,
            "unit": "iter/sec",
            "range": "stddev: 0.000011483029525447625",
            "extra": "mean: 996.2329937992574 usec\nrounds: 968"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 620.5524042006823,
            "unit": "iter/sec",
            "range": "stddev: 0.005898348296541589",
            "extra": "mean: 1.6114674493737149 msec\nrounds: 721"
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
          "id": "04acb55e6820f73c5992af6856c340ca70f56c0f",
          "message": "Merge pull request #1685 from nexi-lab/fix/rebac-tracing-global-singleton\n\nfix(#479): replace rebac_tracing global singleton with injectable set_tracer()",
          "timestamp": "2026-02-16T16:23:44+08:00",
          "tree_id": "b9a3b6c5ce0e08f02c716b765091985ee1ff6215",
          "url": "https://github.com/nexi-lab/nexus/commit/04acb55e6820f73c5992af6856c340ca70f56c0f"
        },
        "date": 1771233525787,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 365.86121330840285,
            "unit": "iter/sec",
            "range": "stddev: 0.00541706553172086",
            "extra": "mean: 2.733276891959164 msec\nrounds: 398"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 332.82628340285845,
            "unit": "iter/sec",
            "range": "stddev: 0.0006424177770206278",
            "extra": "mean: 3.0045704016397745 msec\nrounds: 366"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 17527.787345694975,
            "unit": "iter/sec",
            "range": "stddev: 0.000012967561755439474",
            "extra": "mean: 57.0522667965624 usec\nrounds: 16953"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16004.520207179228,
            "unit": "iter/sec",
            "range": "stddev: 0.000019074662774799887",
            "extra": "mean: 62.48234792764515 usec\nrounds: 15851"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53003.984907226244,
            "unit": "iter/sec",
            "range": "stddev: 0.000021295788571758916",
            "extra": "mean: 18.866506013657588 usec\nrounds: 46145"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 233.79616369927987,
            "unit": "iter/sec",
            "range": "stddev: 0.0005544787068360664",
            "extra": "mean: 4.277230148593239 msec\nrounds: 249"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 181.3679303043807,
            "unit": "iter/sec",
            "range": "stddev: 0.0004192431220888195",
            "extra": "mean: 5.513653920633875 msec\nrounds: 189"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 56.40968148608076,
            "unit": "iter/sec",
            "range": "stddev: 0.02522801597592109",
            "extra": "mean: 17.72745340259992 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23714.915888631418,
            "unit": "iter/sec",
            "range": "stddev: 0.0000019014123895608888",
            "extra": "mean: 42.16755415436178 usec\nrounds: 23867"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2526.8878658166004,
            "unit": "iter/sec",
            "range": "stddev: 0.00004210427107015815",
            "extra": "mean: 395.7437184007512 usec\nrounds: 1701"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4956.812759792252,
            "unit": "iter/sec",
            "range": "stddev: 0.000040455061652244357",
            "extra": "mean: 201.7425407131803 usec\nrounds: 2665"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.12207632442102,
            "unit": "iter/sec",
            "range": "stddev: 0.002036331138290707",
            "extra": "mean: 23.740520108697307 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1449.9897902337227,
            "unit": "iter/sec",
            "range": "stddev: 0.00020765016342791563",
            "extra": "mean: 689.6600284604837 usec\nrounds: 1546"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3898.423053197937,
            "unit": "iter/sec",
            "range": "stddev: 0.00002889868179987536",
            "extra": "mean: 256.5139766397811 usec\nrounds: 4024"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 17862.664160916276,
            "unit": "iter/sec",
            "range": "stddev: 0.0000034584065773229836",
            "extra": "mean: 55.98269054332958 usec\nrounds: 17004"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3940.36727020824,
            "unit": "iter/sec",
            "range": "stddev: 0.00000818675069260625",
            "extra": "mean: 253.7834499744873 usec\nrounds: 3958"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1002.6550774007488,
            "unit": "iter/sec",
            "range": "stddev: 0.000019778660814597994",
            "extra": "mean: 997.35195336802 usec\nrounds: 965"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 644.5003010927192,
            "unit": "iter/sec",
            "range": "stddev: 0.000018169886950158218",
            "extra": "mean: 1.5515896552795219 msec\nrounds: 644"
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
          "id": "0de5cc4a00055eeb670d6688c1b285c35fb3b24c",
          "message": "Merge pull request #1686 from nexi-lab/fix/ipc-hardcoded-default-zone-id\n\nfix(#521): change hardcoded zone ID default from \"default\" to \"root\" in IPC modules",
          "timestamp": "2026-02-16T16:26:39+08:00",
          "tree_id": "8d25a48102e599a1c23074e8b7e5e7b3c4358e1a",
          "url": "https://github.com/nexi-lab/nexus/commit/0de5cc4a00055eeb670d6688c1b285c35fb3b24c"
        },
        "date": 1771233537695,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 397.9112626079969,
            "unit": "iter/sec",
            "range": "stddev: 0.008578320300118665",
            "extra": "mean: 2.5131231356604053 msec\nrounds: 516"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 356.12258538417854,
            "unit": "iter/sec",
            "range": "stddev: 0.0010045163715733968",
            "extra": "mean: 2.8080218470873404 msec\nrounds: 412"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 20539.90580643181,
            "unit": "iter/sec",
            "range": "stddev: 0.000015634083576143784",
            "extra": "mean: 48.685714989348334 usec\nrounds: 21508"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 18633.62448751304,
            "unit": "iter/sec",
            "range": "stddev: 0.00001681421932253148",
            "extra": "mean: 53.666424407668536 usec\nrounds: 23124"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 58284.507545484965,
            "unit": "iter/sec",
            "range": "stddev: 0.000022340379552550078",
            "extra": "mean: 17.15721796601961 usec\nrounds: 53568"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 249.2642835741361,
            "unit": "iter/sec",
            "range": "stddev: 0.0004374686171219341",
            "extra": "mean: 4.011806206895182 msec\nrounds: 261"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 192.14604371463912,
            "unit": "iter/sec",
            "range": "stddev: 0.0005718215263899419",
            "extra": "mean: 5.204374655171796 msec\nrounds: 203"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 70.16960158559193,
            "unit": "iter/sec",
            "range": "stddev: 0.001590916025638735",
            "extra": "mean: 14.251185376622288 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 19906.70435578323,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016326389566697618",
            "extra": "mean: 50.23433221930999 usec\nrounds: 20050"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2823.685343657211,
            "unit": "iter/sec",
            "range": "stddev: 0.000006654296219613967",
            "extra": "mean: 354.14710858144315 usec\nrounds: 2855"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 6658.508117318641,
            "unit": "iter/sec",
            "range": "stddev: 0.00000964732478891785",
            "extra": "mean: 150.18379228209105 usec\nrounds: 5546"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 47.81223197909751,
            "unit": "iter/sec",
            "range": "stddev: 0.0007464446526626937",
            "extra": "mean: 20.915149923082 msec\nrounds: 52"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1795.0235192187267,
            "unit": "iter/sec",
            "range": "stddev: 0.0003602299025830326",
            "extra": "mean: 557.0957646478327 usec\nrounds: 1997"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 6555.392938870478,
            "unit": "iter/sec",
            "range": "stddev: 0.0000033613034528121527",
            "extra": "mean: 152.54615693141108 usec\nrounds: 6608"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 27821.272549800222,
            "unit": "iter/sec",
            "range": "stddev: 0.000001200863284155235",
            "extra": "mean: 35.943718901067335 usec\nrounds: 27734"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 4353.047831827256,
            "unit": "iter/sec",
            "range": "stddev: 0.000006093675414715206",
            "extra": "mean: 229.72410105133974 usec\nrounds: 4374"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1001.8335470104054,
            "unit": "iter/sec",
            "range": "stddev: 0.000013169098066171337",
            "extra": "mean: 998.1698087313238 usec\nrounds: 962"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 602.7090223575648,
            "unit": "iter/sec",
            "range": "stddev: 0.0061874447173995125",
            "extra": "mean: 1.659175427785014 msec\nrounds: 727"
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
          "id": "8a1eebd0d7070ee855de4adf485b3092b409041e",
          "message": "Merge pull request #1667 from nexi-lab/fix/delete-agent-provisioning-backward-compat\n\nfix(#490): delete backward-compat DEFAULT_AGENT_METADATA constant",
          "timestamp": "2026-02-16T16:40:14+08:00",
          "tree_id": "06ceac073ea1dccec641cacb096260e65176029e",
          "url": "https://github.com/nexi-lab/nexus/commit/8a1eebd0d7070ee855de4adf485b3092b409041e"
        },
        "date": 1771234427281,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 374.72103498105423,
            "unit": "iter/sec",
            "range": "stddev: 0.0028928932215566026",
            "extra": "mean: 2.668651894737 msec\nrounds: 437"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 315.71291223091356,
            "unit": "iter/sec",
            "range": "stddev: 0.0008735853561754171",
            "extra": "mean: 3.1674345940865303 msec\nrounds: 372"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 12106.661344939665,
            "unit": "iter/sec",
            "range": "stddev: 0.000020288490442909866",
            "extra": "mean: 82.59915525083878 usec\nrounds: 16045"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 13154.357668866127,
            "unit": "iter/sec",
            "range": "stddev: 0.000022833764263341615",
            "extra": "mean: 76.02043559806881 usec\nrounds: 17484"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 52625.28279282716,
            "unit": "iter/sec",
            "range": "stddev: 0.00001962622977264478",
            "extra": "mean: 19.0022731837234 usec\nrounds: 45204"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 236.23408052419822,
            "unit": "iter/sec",
            "range": "stddev: 0.0003702978880993942",
            "extra": "mean: 4.2330894754093995 msec\nrounds: 244"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 180.9896617593611,
            "unit": "iter/sec",
            "range": "stddev: 0.00042689474351815073",
            "extra": "mean: 5.525177461956765 msec\nrounds: 184"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 67.67068684261722,
            "unit": "iter/sec",
            "range": "stddev: 0.0014263467350627466",
            "extra": "mean: 14.77744717333394 msec\nrounds: 75"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23732.054573744284,
            "unit": "iter/sec",
            "range": "stddev: 0.000001807835099843156",
            "extra": "mean: 42.137101821194186 usec\nrounds: 23885"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2623.838333121816,
            "unit": "iter/sec",
            "range": "stddev: 0.000040528979820957594",
            "extra": "mean: 381.1210421681012 usec\nrounds: 1660"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4865.62829213211,
            "unit": "iter/sec",
            "range": "stddev: 0.00010072193921272106",
            "extra": "mean: 205.52330345847312 usec\nrounds: 2689"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 40.00274485810645,
            "unit": "iter/sec",
            "range": "stddev: 0.0014554901369660804",
            "extra": "mean: 24.998284581397986 msec\nrounds: 43"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1392.8190595779176,
            "unit": "iter/sec",
            "range": "stddev: 0.0002577503732340338",
            "extra": "mean: 717.9683485255018 usec\nrounds: 1492"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3946.0449113645946,
            "unit": "iter/sec",
            "range": "stddev: 0.000006102107005997081",
            "extra": "mean: 253.41830173295892 usec\nrounds: 4040"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18327.934677049485,
            "unit": "iter/sec",
            "range": "stddev: 0.0000029348485971581667",
            "extra": "mean: 54.56152139456362 usec\nrounds: 18416"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3928.556296381379,
            "unit": "iter/sec",
            "range": "stddev: 0.000008213821176958105",
            "extra": "mean: 254.54643501509884 usec\nrounds: 3924"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1016.3529559725326,
            "unit": "iter/sec",
            "range": "stddev: 0.000019590807817303796",
            "extra": "mean: 983.9101604649885 usec\nrounds: 860"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 521.6657672278192,
            "unit": "iter/sec",
            "range": "stddev: 0.008851868067131046",
            "extra": "mean: 1.9169362124605063 msec\nrounds: 626"
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
          "id": "6c9af215b6f096d106ccb828682628ca6663fcd1",
          "message": "Merge pull request #1687 from nexi-lab/fix/a2a-hardcoded-default-zone-id\n\nfix(#522): change hardcoded zone ID default from \"default\" to \"root\" in A2A modules",
          "timestamp": "2026-02-16T16:44:27+08:00",
          "tree_id": "fba4f6a61fea9a6ceae895727e46e6a28ae54da4",
          "url": "https://github.com/nexi-lab/nexus/commit/6c9af215b6f096d106ccb828682628ca6663fcd1"
        },
        "date": 1771234479662,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 342.470869064066,
            "unit": "iter/sec",
            "range": "stddev: 0.008450777555369125",
            "extra": "mean: 2.919956382663689 msec\nrounds: 473"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 340.970672862865,
            "unit": "iter/sec",
            "range": "stddev: 0.0008180414448492585",
            "extra": "mean: 2.93280355053348 msec\nrounds: 376"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15745.085370825365,
            "unit": "iter/sec",
            "range": "stddev: 0.000017276198953989543",
            "extra": "mean: 63.51188173631221 usec\nrounds: 14882"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 14586.069159133036,
            "unit": "iter/sec",
            "range": "stddev: 0.000019137677672084257",
            "extra": "mean: 68.55856701967247 usec\nrounds: 17368"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53328.22199655261,
            "unit": "iter/sec",
            "range": "stddev: 0.00001781574237280736",
            "extra": "mean: 18.75179712656921 usec\nrounds: 45102"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 229.51747341854858,
            "unit": "iter/sec",
            "range": "stddev: 0.000758771544443703",
            "extra": "mean: 4.356966749003889 msec\nrounds: 251"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.90486875873768,
            "unit": "iter/sec",
            "range": "stddev: 0.0003593746563319645",
            "extra": "mean: 5.408186418848666 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 58.78340948779316,
            "unit": "iter/sec",
            "range": "stddev: 0.02519121769686667",
            "extra": "mean: 17.011602571430597 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23693.720554825803,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017407610645117376",
            "extra": "mean: 42.20527534652322 usec\nrounds: 23879"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2606.7055010354707,
            "unit": "iter/sec",
            "range": "stddev: 0.00004077700504172334",
            "extra": "mean: 383.6259982582484 usec\nrounds: 1722"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4913.129064947289,
            "unit": "iter/sec",
            "range": "stddev: 0.00011522953950113733",
            "extra": "mean: 203.53627734603967 usec\nrounds: 3602"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 43.08106939345903,
            "unit": "iter/sec",
            "range": "stddev: 0.000809474198651721",
            "extra": "mean: 23.212051466666455 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1461.2250325445762,
            "unit": "iter/sec",
            "range": "stddev: 0.0001743983901547023",
            "extra": "mean: 684.3572877057825 usec\nrounds: 1578"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3992.9978051671897,
            "unit": "iter/sec",
            "range": "stddev: 0.00000510383169418739",
            "extra": "mean: 250.43840462570182 usec\nrounds: 4021"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18409.956945611946,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025248920219882553",
            "extra": "mean: 54.31843230020982 usec\nrounds: 17585"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3937.331295524702,
            "unit": "iter/sec",
            "range": "stddev: 0.000013557985487473853",
            "extra": "mean: 253.97913585189858 usec\nrounds: 3997"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1028.9561281616056,
            "unit": "iter/sec",
            "range": "stddev: 0.00002940280588960861",
            "extra": "mean: 971.8587339449153 usec\nrounds: 981"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 656.7217955844236,
            "unit": "iter/sec",
            "range": "stddev: 0.00002122789390118598",
            "extra": "mean: 1.5227148036256197 msec\nrounds: 662"
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
          "id": "91425e20c2121959e6a5e3469b5f32f69fd5c49b",
          "message": "Merge pull request #1691 from nexi-lab/fix/event-bus-global-singleton\n\nfix(#345): remove global event bus singleton, use constructor DI",
          "timestamp": "2026-02-16T16:47:36+08:00",
          "tree_id": "be8d904e117fc2ce41d0f88ed38b5ac69c42be72",
          "url": "https://github.com/nexi-lab/nexus/commit/91425e20c2121959e6a5e3469b5f32f69fd5c49b"
        },
        "date": 1771234519644,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 357.33306824369606,
            "unit": "iter/sec",
            "range": "stddev: 0.007008918098334326",
            "extra": "mean: 2.7985095387757792 msec\nrounds: 490"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 337.7727911061704,
            "unit": "iter/sec",
            "range": "stddev: 0.0007553602002159424",
            "extra": "mean: 2.960570023195489 msec\nrounds: 388"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16621.123587114118,
            "unit": "iter/sec",
            "range": "stddev: 0.000016446895128856747",
            "extra": "mean: 60.1644043351721 usec\nrounds: 16793"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15997.648180348579,
            "unit": "iter/sec",
            "range": "stddev: 0.0000184416009783099",
            "extra": "mean: 62.50918814606727 usec\nrounds: 16737"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54003.91097324385,
            "unit": "iter/sec",
            "range": "stddev: 0.000017759775243468833",
            "extra": "mean: 18.517177403974472 usec\nrounds: 45309"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 247.86494023427085,
            "unit": "iter/sec",
            "range": "stddev: 0.00018523953337761342",
            "extra": "mean: 4.034455211999102 msec\nrounds: 250"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.77260773778184,
            "unit": "iter/sec",
            "range": "stddev: 0.0005159996765476001",
            "extra": "mean: 5.412057621761445 msec\nrounds: 193"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 61.89797180545569,
            "unit": "iter/sec",
            "range": "stddev: 0.022649656067807424",
            "extra": "mean: 16.15561820253148 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23734.166897897652,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016562277303775469",
            "extra": "mean: 42.13335164878187 usec\nrounds: 23805"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2611.7587698615116,
            "unit": "iter/sec",
            "range": "stddev: 0.000045912592666744474",
            "extra": "mean: 382.88375310137275 usec\nrounds: 1693"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4968.2024318186395,
            "unit": "iter/sec",
            "range": "stddev: 0.00007646420943422916",
            "extra": "mean: 201.28004317930825 usec\nrounds: 3636"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 43.97228361578996,
            "unit": "iter/sec",
            "range": "stddev: 0.0009132358890378667",
            "extra": "mean: 22.741598065216497 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1442.8040214109067,
            "unit": "iter/sec",
            "range": "stddev: 0.00022271322207091483",
            "extra": "mean: 693.0948244946725 usec\nrounds: 1584"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3886.641698006009,
            "unit": "iter/sec",
            "range": "stddev: 0.000024404922131716907",
            "extra": "mean: 257.29153281946134 usec\nrounds: 4022"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18508.583125807723,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026384129725674397",
            "extra": "mean: 54.02898715707929 usec\nrounds: 18376"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3938.9831533813503,
            "unit": "iter/sec",
            "range": "stddev: 0.000008343344917368805",
            "extra": "mean: 253.8726267822617 usec\nrounds: 3928"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1010.9340240453208,
            "unit": "iter/sec",
            "range": "stddev: 0.00003249268678329637",
            "extra": "mean: 989.1842357807212 usec\nrounds: 967"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 665.3784196686526,
            "unit": "iter/sec",
            "range": "stddev: 0.00007340246636545629",
            "extra": "mean: 1.5029041676734625 msec\nrounds: 662"
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
          "id": "09cfd76f021aa51660260dfd1d3f2769d68e4412",
          "message": "Merge pull request #1693 from nexi-lab/fix/server-pay-router-hardcoded-default-zone-id\n\nfix(#512): change 6 hardcoded zone ID fallbacks from \"default\" to \"root\" in pay router",
          "timestamp": "2026-02-16T16:47:41+08:00",
          "tree_id": "d768babf77222fb53e0859bfa194fee725509679",
          "url": "https://github.com/nexi-lab/nexus/commit/09cfd76f021aa51660260dfd1d3f2769d68e4412"
        },
        "date": 1771234774298,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 334.2622094250736,
            "unit": "iter/sec",
            "range": "stddev: 0.006667525839936544",
            "extra": "mean: 2.9916633463291773 msec\nrounds: 436"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 273.13518565584917,
            "unit": "iter/sec",
            "range": "stddev: 0.0008568061164957812",
            "extra": "mean: 3.6611906942666907 msec\nrounds: 314"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 17290.985253805193,
            "unit": "iter/sec",
            "range": "stddev: 0.000017692605268962357",
            "extra": "mean: 57.833604350563654 usec\nrounds: 14159"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15329.203382746206,
            "unit": "iter/sec",
            "range": "stddev: 0.0000420795205279484",
            "extra": "mean: 65.23496198931973 usec\nrounds: 16890"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 41339.59411392381,
            "unit": "iter/sec",
            "range": "stddev: 0.0009539091942102822",
            "extra": "mean: 24.189884333266463 usec\nrounds: 46729"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 223.51131192789921,
            "unit": "iter/sec",
            "range": "stddev: 0.0007918819522904098",
            "extra": "mean: 4.474046487287329 msec\nrounds: 236"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 175.95318154938704,
            "unit": "iter/sec",
            "range": "stddev: 0.0005060442102020423",
            "extra": "mean: 5.683330026739625 msec\nrounds: 187"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 62.443486116538956,
            "unit": "iter/sec",
            "range": "stddev: 0.0016963951116330618",
            "extra": "mean: 16.014480647888384 msec\nrounds: 71"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23609.8480630292,
            "unit": "iter/sec",
            "range": "stddev: 0.000002381733063566117",
            "extra": "mean: 42.355206917485674 usec\nrounds: 23420"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2557.8045009873154,
            "unit": "iter/sec",
            "range": "stddev: 0.00004166771058602124",
            "extra": "mean: 390.9602941170832 usec\nrounds: 1649"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4711.609332566305,
            "unit": "iter/sec",
            "range": "stddev: 0.00005340274788615862",
            "extra": "mean: 212.24170541646396 usec\nrounds: 3656"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 38.843664575268114,
            "unit": "iter/sec",
            "range": "stddev: 0.0014248401722377168",
            "extra": "mean: 25.74422395349133 msec\nrounds: 43"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1242.089644002944,
            "unit": "iter/sec",
            "range": "stddev: 0.0004165860833095628",
            "extra": "mean: 805.094869624104 usec\nrounds: 1465"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3945.847224206628,
            "unit": "iter/sec",
            "range": "stddev: 0.00000890863720934344",
            "extra": "mean: 253.4309980034934 usec\nrounds: 4007"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 17979.72496470035,
            "unit": "iter/sec",
            "range": "stddev: 0.000012698581943078534",
            "extra": "mean: 55.61820339094748 usec\nrounds: 17341"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3920.5077439479514,
            "unit": "iter/sec",
            "range": "stddev: 0.000010217568076245452",
            "extra": "mean: 255.06900261673758 usec\nrounds: 3822"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 998.3241613952129,
            "unit": "iter/sec",
            "range": "stddev: 0.00003574460213857266",
            "extra": "mean: 1.0016786517542007 msec\nrounds: 827"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 463.94163387824614,
            "unit": "iter/sec",
            "range": "stddev: 0.011586584792057384",
            "extra": "mean: 2.1554435450008214 msec\nrounds: 400"
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
          "id": "9f8c370e7710ff50f7b4bb90551ed0d716ef8d09",
          "message": "Merge pull request #1692 from nexi-lab/fix/server-v2-dependencies-hardcoded-default-zone-id\n\nfix(#510): change 3 hardcoded zone ID fallbacks from \"default\" to \"root\" in v2/dependencies.py",
          "timestamp": "2026-02-16T16:51:26+08:00",
          "tree_id": "8851f4d18270870cc9aa36052728852ef67a17ed",
          "url": "https://github.com/nexi-lab/nexus/commit/9f8c370e7710ff50f7b4bb90551ed0d716ef8d09"
        },
        "date": 1771235449201,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 354.45190751032567,
            "unit": "iter/sec",
            "range": "stddev: 0.00613941541415633",
            "extra": "mean: 2.821257210954264 msec\nrounds: 493"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 340.3933993527709,
            "unit": "iter/sec",
            "range": "stddev: 0.0007427262182027778",
            "extra": "mean: 2.937777295039842 msec\nrounds: 383"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16389.008609686472,
            "unit": "iter/sec",
            "range": "stddev: 0.000015650607836334363",
            "extra": "mean: 61.01650342711795 usec\nrounds: 16924"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 13082.964732635915,
            "unit": "iter/sec",
            "range": "stddev: 0.00001746909561535241",
            "extra": "mean: 76.435274453157 usec\nrounds: 16961"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 51672.35211690107,
            "unit": "iter/sec",
            "range": "stddev: 0.000029406900700618303",
            "extra": "mean: 19.352709118749765 usec\nrounds: 48219"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 246.91281419419659,
            "unit": "iter/sec",
            "range": "stddev: 0.00031155182121414467",
            "extra": "mean: 4.050012565218674 msec\nrounds: 253"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 178.0612888791893,
            "unit": "iter/sec",
            "range": "stddev: 0.0007982675301780454",
            "extra": "mean: 5.616043814433345 msec\nrounds: 194"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 61.190992272301465,
            "unit": "iter/sec",
            "range": "stddev: 0.021386333213585237",
            "extra": "mean: 16.34227462025742 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23631.600773335576,
            "unit": "iter/sec",
            "range": "stddev: 0.000002339531248750038",
            "extra": "mean: 42.31621926891798 usec\nrounds: 23966"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2535.5772443200212,
            "unit": "iter/sec",
            "range": "stddev: 0.000044623007322353704",
            "extra": "mean: 394.3875116564138 usec\nrounds: 1673"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4866.1322836007685,
            "unit": "iter/sec",
            "range": "stddev: 0.00004588003312902312",
            "extra": "mean: 205.50201715026844 usec\nrounds: 2449"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 43.781263053016964,
            "unit": "iter/sec",
            "range": "stddev: 0.0007836384199692444",
            "extra": "mean: 22.840821170212678 msec\nrounds: 47"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1404.8141639290352,
            "unit": "iter/sec",
            "range": "stddev: 0.00045097870142624217",
            "extra": "mean: 711.8379253830726 usec\nrounds: 1501"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3961.9625326364026,
            "unit": "iter/sec",
            "range": "stddev: 0.000009457209068580312",
            "extra": "mean: 252.40016576698207 usec\nrounds: 3891"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 17897.515562485605,
            "unit": "iter/sec",
            "range": "stddev: 0.000002570614007839462",
            "extra": "mean: 55.87367679657545 usec\nrounds: 16881"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3924.4245221151655,
            "unit": "iter/sec",
            "range": "stddev: 0.000008429734206171255",
            "extra": "mean: 254.81443059096605 usec\nrounds: 3962"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1026.4283674471192,
            "unit": "iter/sec",
            "range": "stddev: 0.00003190099325878394",
            "extra": "mean: 974.2521073215753 usec\nrounds: 997"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 638.9643425574674,
            "unit": "iter/sec",
            "range": "stddev: 0.000041394785954667665",
            "extra": "mean: 1.5650325587770366 msec\nrounds: 655"
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
            "email": "songym@sudoprivacy.com",
            "name": "elfenlieds7",
            "username": "elfenlieds7"
          },
          "distinct": true,
          "id": "0aa6fa0cdc98d2b9fc8d453c3ccd1afde146175d",
          "message": "fix: sync __version__ to 0.7.2.dev0 to match pyproject.toml\n\nCI test_version_consistency was failing because pyproject.toml was\nbumped to 0.7.2.dev0 but __init__.py still had 0.7.1.dev0.\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>",
          "timestamp": "2026-02-16T17:56:52+08:00",
          "tree_id": "aee49b8cba9b5851ef69d4b22faa5fae93166e6b",
          "url": "https://github.com/nexi-lab/nexus/commit/0aa6fa0cdc98d2b9fc8d453c3ccd1afde146175d"
        },
        "date": 1771236029722,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 343.9525327178564,
            "unit": "iter/sec",
            "range": "stddev: 0.009180135118119073",
            "extra": "mean: 2.907377922465534 msec\nrounds: 503"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 337.8218307378189,
            "unit": "iter/sec",
            "range": "stddev: 0.0008152892324423665",
            "extra": "mean: 2.9601402544529245 msec\nrounds: 393"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 17117.66361229044,
            "unit": "iter/sec",
            "range": "stddev: 0.000013457957601702337",
            "extra": "mean: 58.41918749250351 usec\nrounds: 16694"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16607.933441543966,
            "unit": "iter/sec",
            "range": "stddev: 0.000015155119400154409",
            "extra": "mean: 60.21218735731123 usec\nrounds: 18382"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 55547.46579693593,
            "unit": "iter/sec",
            "range": "stddev: 0.000011458357190203148",
            "extra": "mean: 18.002621463518885 usec\nrounds: 39975"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 248.1745438700492,
            "unit": "iter/sec",
            "range": "stddev: 0.0002893582644525309",
            "extra": "mean: 4.0294221333338145 msec\nrounds: 255"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 187.81370960436476,
            "unit": "iter/sec",
            "range": "stddev: 0.0003372813718532771",
            "extra": "mean: 5.324424942708017 msec\nrounds: 192"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 60.17812186276596,
            "unit": "iter/sec",
            "range": "stddev: 0.022829300456734633",
            "extra": "mean: 16.617334822785992 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23732.935023502792,
            "unit": "iter/sec",
            "range": "stddev: 0.000001717866276033959",
            "extra": "mean: 42.13553860951868 usec\nrounds: 23919"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2433.591650809016,
            "unit": "iter/sec",
            "range": "stddev: 0.000036293021973402806",
            "extra": "mean: 410.91528221982645 usec\nrounds: 1676"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4969.215000760505,
            "unit": "iter/sec",
            "range": "stddev: 0.0000410255009749342",
            "extra": "mean: 201.2390286689058 usec\nrounds: 2930"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.99878250804118,
            "unit": "iter/sec",
            "range": "stddev: 0.0012516113934369956",
            "extra": "mean: 23.256472431818054 msec\nrounds: 44"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1464.4841088015044,
            "unit": "iter/sec",
            "range": "stddev: 0.00032965883070765005",
            "extra": "mean: 682.8343127726896 usec\nrounds: 1605"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3984.431490706503,
            "unit": "iter/sec",
            "range": "stddev: 0.000005352447705320764",
            "extra": "mean: 250.97683379233706 usec\nrounds: 3995"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18390.598845591463,
            "unit": "iter/sec",
            "range": "stddev: 0.000002650283980865836",
            "extra": "mean: 54.375608341852164 usec\nrounds: 18557"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3913.715608452279,
            "unit": "iter/sec",
            "range": "stddev: 0.000008006928614800028",
            "extra": "mean: 255.51166718408055 usec\nrounds: 3864"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1023.8607354935473,
            "unit": "iter/sec",
            "range": "stddev: 0.00002601310066204626",
            "extra": "mean: 976.6953310480792 usec\nrounds: 1021"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 639.5642528781117,
            "unit": "iter/sec",
            "range": "stddev: 0.000021677287677204794",
            "extra": "mean: 1.5635645605580466 msec\nrounds: 644"
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
          "id": "15e87b93f9ff7ff48a5deeb889b5c5bf542ba3b6",
          "message": "Merge pull request #1697 from nexi-lab/fix/auth-routes-global-state\n\nfix(#528): add reset_* functions to auth_routes injectable DI",
          "timestamp": "2026-02-16T18:09:54+08:00",
          "tree_id": "7fb47d52bd8b8c904d4e2f20d79a6011b7d35251",
          "url": "https://github.com/nexi-lab/nexus/commit/15e87b93f9ff7ff48a5deeb889b5c5bf542ba3b6"
        },
        "date": 1771236776263,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 339.81071591379504,
            "unit": "iter/sec",
            "range": "stddev: 0.007615211539923758",
            "extra": "mean: 2.942814788259018 msec\nrounds: 477"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 320.2420508708899,
            "unit": "iter/sec",
            "range": "stddev: 0.001275922230597905",
            "extra": "mean: 3.122638008595455 msec\nrounds: 349"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 17347.836644716477,
            "unit": "iter/sec",
            "range": "stddev: 0.000014245137893398746",
            "extra": "mean: 57.64407519392707 usec\nrounds: 15347"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 13689.021536275046,
            "unit": "iter/sec",
            "range": "stddev: 0.00001984184447206124",
            "extra": "mean: 73.0512401744758 usec\nrounds: 17429"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 52005.64837594576,
            "unit": "iter/sec",
            "range": "stddev: 0.000019779088149060003",
            "extra": "mean: 19.228680561216333 usec\nrounds: 45893"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 247.08920117256676,
            "unit": "iter/sec",
            "range": "stddev: 0.0002995498631260851",
            "extra": "mean: 4.047121425195759 msec\nrounds: 254"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 182.0934474475114,
            "unit": "iter/sec",
            "range": "stddev: 0.0004939792854648732",
            "extra": "mean: 5.4916858020838495 msec\nrounds: 192"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 67.50343278240499,
            "unit": "iter/sec",
            "range": "stddev: 0.0017683202853105789",
            "extra": "mean: 14.814061430378894 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23719.105853470795,
            "unit": "iter/sec",
            "range": "stddev: 0.000001699406244383353",
            "extra": "mean: 42.16010528295994 usec\nrounds: 23983"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2516.2217107069227,
            "unit": "iter/sec",
            "range": "stddev: 0.00004465529820107562",
            "extra": "mean: 397.4212589235842 usec\nrounds: 1653"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5017.262653750784,
            "unit": "iter/sec",
            "range": "stddev: 0.00008717063763981948",
            "extra": "mean: 199.31186964119254 usec\nrounds: 4595"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.664357072475994,
            "unit": "iter/sec",
            "range": "stddev: 0.0017797944467302256",
            "extra": "mean: 24.00133039999825 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1428.8804792149524,
            "unit": "iter/sec",
            "range": "stddev: 0.00031235500543319334",
            "extra": "mean: 699.8485979383066 usec\nrounds: 1552"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 4004.896345266008,
            "unit": "iter/sec",
            "range": "stddev: 0.000005785764405932543",
            "extra": "mean: 249.69435255972382 usec\nrounds: 4005"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18414.86241042735,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025527573824564967",
            "extra": "mean: 54.3039626206359 usec\nrounds: 18379"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3946.854913257674,
            "unit": "iter/sec",
            "range": "stddev: 0.000013766283621552683",
            "extra": "mean: 253.3662934102169 usec\nrounds: 3991"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1030.8285949997103,
            "unit": "iter/sec",
            "range": "stddev: 0.00009363387289289428",
            "extra": "mean: 970.0933839541782 usec\nrounds: 1047"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 541.2144328256669,
            "unit": "iter/sec",
            "range": "stddev: 0.007351982951125046",
            "extra": "mean: 1.8476964754598748 msec\nrounds: 652"
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
          "id": "d442ce5ae4c5e5faacc5ccb5b52de3f93746589c",
          "message": "Merge pull request #1664 from nexi-lab/fix/delete-deprecated-agent-id-params\n\nfix(#418): delete deprecated agent_id params from router/filesystem/callers",
          "timestamp": "2026-02-16T18:09:59+08:00",
          "tree_id": "4e82d6a7b952a61f5f7f81da8472402ece74e024",
          "url": "https://github.com/nexi-lab/nexus/commit/d442ce5ae4c5e5faacc5ccb5b52de3f93746589c"
        },
        "date": 1771236782033,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 397.05535196499744,
            "unit": "iter/sec",
            "range": "stddev: 0.003967070084243744",
            "extra": "mean: 2.5185405386203064 msec\nrounds: 479"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 334.4000238358467,
            "unit": "iter/sec",
            "range": "stddev: 0.0009400265172151658",
            "extra": "mean: 2.990430408853347 msec\nrounds: 384"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14583.451039029036,
            "unit": "iter/sec",
            "range": "stddev: 0.000019969619718010325",
            "extra": "mean: 68.57087511891011 usec\nrounds: 16832"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15421.616362924036,
            "unit": "iter/sec",
            "range": "stddev: 0.00001909780028589827",
            "extra": "mean: 64.84404594606279 usec\nrounds: 16650"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53753.10293576333,
            "unit": "iter/sec",
            "range": "stddev: 0.000015654044541396605",
            "extra": "mean: 18.6035771961859 usec\nrounds: 45598"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 237.13579995735836,
            "unit": "iter/sec",
            "range": "stddev: 0.0007524229259794654",
            "extra": "mean: 4.216992964283839 msec\nrounds: 252"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.46854765145054,
            "unit": "iter/sec",
            "range": "stddev: 0.00042208102093219086",
            "extra": "mean: 5.420978333333437 msec\nrounds: 192"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 68.95480485943563,
            "unit": "iter/sec",
            "range": "stddev: 0.001580748382578862",
            "extra": "mean: 14.502252628203358 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23727.349542999833,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017680133626097104",
            "extra": "mean: 42.14545742615509 usec\nrounds: 23982"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2596.48225426379,
            "unit": "iter/sec",
            "range": "stddev: 0.000047789614051481225",
            "extra": "mean: 385.1364662161118 usec\nrounds: 1628"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4832.720385866109,
            "unit": "iter/sec",
            "range": "stddev: 0.00013117988519975926",
            "extra": "mean: 206.92279299349167 usec\nrounds: 3454"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 43.29263881126141,
            "unit": "iter/sec",
            "range": "stddev: 0.0005821582350322645",
            "extra": "mean: 23.09861508695739 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1440.2470444412277,
            "unit": "iter/sec",
            "range": "stddev: 0.0002680293346462717",
            "extra": "mean: 694.3253269358173 usec\nrounds: 1563"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3938.610798655986,
            "unit": "iter/sec",
            "range": "stddev: 0.000008630916196593155",
            "extra": "mean: 253.89662780116294 usec\nrounds: 3928"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18524.963003852296,
            "unit": "iter/sec",
            "range": "stddev: 0.000003931546652671804",
            "extra": "mean: 53.98121441819066 usec\nrounds: 17783"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3940.616819010572,
            "unit": "iter/sec",
            "range": "stddev: 0.000007450016621501088",
            "extra": "mean: 253.7673785422975 usec\nrounds: 3952"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1020.3778934510368,
            "unit": "iter/sec",
            "range": "stddev: 0.00004702505019682638",
            "extra": "mean: 980.0290719920281 usec\nrounds: 1014"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 545.5944506760062,
            "unit": "iter/sec",
            "range": "stddev: 0.007354197111355068",
            "extra": "mean: 1.8328632169205041 msec\nrounds: 650"
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
          "id": "9b270637c8a174cda6b4b56c27173ea8ba0c5f6b",
          "message": "Merge pull request #1698 from nexi-lab/fix/server-auth-token-manager-hardcoded-default-zone-id\n\nfix(#516): change 8 hardcoded zone ID defaults from \"default\" to \"root\" in token_manager.py",
          "timestamp": "2026-02-16T18:11:33+08:00",
          "tree_id": "33e40e1689ca405d91afb9e1baaa04fbb8ee3b29",
          "url": "https://github.com/nexi-lab/nexus/commit/9b270637c8a174cda6b4b56c27173ea8ba0c5f6b"
        },
        "date": 1771236933540,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 337.2608870541869,
            "unit": "iter/sec",
            "range": "stddev: 0.010473537281218253",
            "extra": "mean: 2.9650636595738193 msec\nrounds: 470"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 336.46158630716747,
            "unit": "iter/sec",
            "range": "stddev: 0.0009526516435840031",
            "extra": "mean: 2.9721074877387794 msec\nrounds: 367"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14882.583043368417,
            "unit": "iter/sec",
            "range": "stddev: 0.000019983462499446705",
            "extra": "mean: 67.1926369962769 usec\nrounds: 16526"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 17563.96299574602,
            "unit": "iter/sec",
            "range": "stddev: 0.000013377536363748482",
            "extra": "mean: 56.934758985896245 usec\nrounds: 17472"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54132.35071661085,
            "unit": "iter/sec",
            "range": "stddev: 0.000016482059479180196",
            "extra": "mean: 18.4732417262852 usec\nrounds: 48406"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 247.6991025695307,
            "unit": "iter/sec",
            "range": "stddev: 0.000224740689909137",
            "extra": "mean: 4.0371563305090845 msec\nrounds: 236"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.36407305235176,
            "unit": "iter/sec",
            "range": "stddev: 0.0003349740521027119",
            "extra": "mean: 5.424050268818054 msec\nrounds: 186"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 58.77843629279214,
            "unit": "iter/sec",
            "range": "stddev: 0.025677230861810046",
            "extra": "mean: 17.01304190908916 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23611.76300826677,
            "unit": "iter/sec",
            "range": "stddev: 0.000001770259898183411",
            "extra": "mean: 42.35177185413421 usec\nrounds: 23897"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2504.202674034453,
            "unit": "iter/sec",
            "range": "stddev: 0.000039690162046866215",
            "extra": "mean: 399.32870065541744 usec\nrounds: 1677"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4949.542222297949,
            "unit": "iter/sec",
            "range": "stddev: 0.00004148358117019361",
            "extra": "mean: 202.03888664590986 usec\nrounds: 2576"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.76103578901896,
            "unit": "iter/sec",
            "range": "stddev: 0.0038022702730894768",
            "extra": "mean: 23.94576621739228 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1440.7457825919869,
            "unit": "iter/sec",
            "range": "stddev: 0.00023506336629785734",
            "extra": "mean: 694.0849746587082 usec\nrounds: 1539"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3986.6860015588245,
            "unit": "iter/sec",
            "range": "stddev: 0.000005500217612543587",
            "extra": "mean: 250.83490387981206 usec\nrounds: 3995"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 17593.697075600412,
            "unit": "iter/sec",
            "range": "stddev: 0.000005951004509487603",
            "extra": "mean: 56.83853687505152 usec\nrounds: 16583"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3919.9437098153544,
            "unit": "iter/sec",
            "range": "stddev: 0.000009554334897086075",
            "extra": "mean: 255.10570406815972 usec\nrounds: 3859"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1037.6212702176385,
            "unit": "iter/sec",
            "range": "stddev: 0.000017444110866206143",
            "extra": "mean: 963.7427727269435 usec\nrounds: 1012"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 663.6166057687483,
            "unit": "iter/sec",
            "range": "stddev: 0.000022135687979544818",
            "extra": "mean: 1.5068941785167922 msec\nrounds: 661"
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
          "id": "95fc9004c94f7f9fc14891262ad5114a46d429cd",
          "message": "Merge pull request #1690 from nexi-lab/fix/server-dependencies-hardcoded-default-zone-id\n\nfix(#509): change hardcoded zone ID fallback from \"default\" to \"root\" in server/dependencies.py",
          "timestamp": "2026-02-16T18:11:38+08:00",
          "tree_id": "02f3a4c325665899e741d619eafd8a3b2c7749cb",
          "url": "https://github.com/nexi-lab/nexus/commit/95fc9004c94f7f9fc14891262ad5114a46d429cd"
        },
        "date": 1771237028565,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 353.5139703773342,
            "unit": "iter/sec",
            "range": "stddev: 0.007170837858541205",
            "extra": "mean: 2.8287425216395796 msec\nrounds: 439"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 336.0395740731798,
            "unit": "iter/sec",
            "range": "stddev: 0.0009605223381403613",
            "extra": "mean: 2.975839981817822 msec\nrounds: 385"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15512.57782857492,
            "unit": "iter/sec",
            "range": "stddev: 0.000018594297486710767",
            "extra": "mean: 64.46381839631783 usec\nrounds: 15253"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16673.94641603013,
            "unit": "iter/sec",
            "range": "stddev: 0.00001551166781308217",
            "extra": "mean: 59.973804344160065 usec\nrounds: 17219"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53484.26110091282,
            "unit": "iter/sec",
            "range": "stddev: 0.000014816385949386816",
            "extra": "mean: 18.697089188784414 usec\nrounds: 44759"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 236.25540271801768,
            "unit": "iter/sec",
            "range": "stddev: 0.0006633304560414432",
            "extra": "mean: 4.2327074365090755 msec\nrounds: 252"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.26852820320673,
            "unit": "iter/sec",
            "range": "stddev: 0.00041887694709600215",
            "extra": "mean: 5.42686268648776 msec\nrounds: 185"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 61.412148295639795,
            "unit": "iter/sec",
            "range": "stddev: 0.021640324262421417",
            "extra": "mean: 16.283423194804588 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23734.74200496503,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016844442174846404",
            "extra": "mean: 42.13233073234214 usec\nrounds: 23965"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2632.6389471229277,
            "unit": "iter/sec",
            "range": "stddev: 0.00004946582472943082",
            "extra": "mean: 379.8469976647756 usec\nrounds: 1713"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4834.632741077136,
            "unit": "iter/sec",
            "range": "stddev: 0.000042930008743543565",
            "extra": "mean: 206.84094398806477 usec\nrounds: 2678"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.58127687810216,
            "unit": "iter/sec",
            "range": "stddev: 0.0024943510876521448",
            "extra": "mean: 24.04928552173989 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1453.7633656753394,
            "unit": "iter/sec",
            "range": "stddev: 0.00020137587424730804",
            "extra": "mean: 687.8698580600526 usec\nrounds: 1557"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3979.849577636661,
            "unit": "iter/sec",
            "range": "stddev: 0.00000555946922211466",
            "extra": "mean: 251.26577788747136 usec\nrounds: 4025"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18489.568949232347,
            "unit": "iter/sec",
            "range": "stddev: 0.0000032383960280132575",
            "extra": "mean: 54.084549117707695 usec\nrounds: 18588"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3996.036768156888,
            "unit": "iter/sec",
            "range": "stddev: 0.000007258281942002666",
            "extra": "mean: 250.2479476587086 usec\nrounds: 3993"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1030.5941941609194,
            "unit": "iter/sec",
            "range": "stddev: 0.000022261259835291896",
            "extra": "mean: 970.3140243422113 usec\nrounds: 1027"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 668.6596557788035,
            "unit": "iter/sec",
            "range": "stddev: 0.000024217963268622973",
            "extra": "mean: 1.495529140060464 msec\nrounds: 664"
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
          "id": "690c81304772d02fc5db65f91863a7e617b65791",
          "message": "Merge pull request #1681 from nexi-lab/fix/cross-zone-rebac-constants-to-services\n\nrefactor(#303): move cross-zone ReBAC constants from core/ to services/",
          "timestamp": "2026-02-16T18:11:51+08:00",
          "tree_id": "ebf444f0cf5723534c2783ac2b714ec8a8eff8a7",
          "url": "https://github.com/nexi-lab/nexus/commit/690c81304772d02fc5db65f91863a7e617b65791"
        },
        "date": 1771237222188,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 338.3112824984135,
            "unit": "iter/sec",
            "range": "stddev: 0.009017694327215976",
            "extra": "mean: 2.9558576723041723 msec\nrounds: 473"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 335.7359312120571,
            "unit": "iter/sec",
            "range": "stddev: 0.0008133020517664105",
            "extra": "mean: 2.978531360613831 msec\nrounds: 391"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16204.161740220428,
            "unit": "iter/sec",
            "range": "stddev: 0.0000113958276796797",
            "extra": "mean: 61.71254126141528 usec\nrounds: 14808"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15774.363116785576,
            "unit": "iter/sec",
            "range": "stddev: 0.000019835966722378075",
            "extra": "mean: 63.39400155787558 usec\nrounds: 11554"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54261.87831061402,
            "unit": "iter/sec",
            "range": "stddev: 0.000013691860152616214",
            "extra": "mean: 18.429144569519863 usec\nrounds: 46469"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 244.31170321737372,
            "unit": "iter/sec",
            "range": "stddev: 0.000287566333356155",
            "extra": "mean: 4.093131793650755 msec\nrounds: 252"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.28768468502108,
            "unit": "iter/sec",
            "range": "stddev: 0.00041963906522392356",
            "extra": "mean: 5.426298570678608 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 68.20640003072977,
            "unit": "iter/sec",
            "range": "stddev: 0.00168050093839413",
            "extra": "mean: 14.661380743587982 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23743.464809533238,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016904368499502472",
            "extra": "mean: 42.11685228006361 usec\nrounds: 23971"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2546.8981201453385,
            "unit": "iter/sec",
            "range": "stddev: 0.00004282527561738781",
            "extra": "mean: 392.6344725335676 usec\nrounds: 1693"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4981.960754878082,
            "unit": "iter/sec",
            "range": "stddev: 0.00011632511273105592",
            "extra": "mean: 200.72418254616937 usec\nrounds: 3873"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.13646176299654,
            "unit": "iter/sec",
            "range": "stddev: 0.0014717163426333276",
            "extra": "mean: 23.7324150666628 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1422.5669317327895,
            "unit": "iter/sec",
            "range": "stddev: 0.00037161163808791274",
            "extra": "mean: 702.9546221645456 usec\nrounds: 1543"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3989.869934401462,
            "unit": "iter/sec",
            "range": "stddev: 0.000005057053923057474",
            "extra": "mean: 250.6347365807087 usec\nrounds: 4024"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18242.76179392928,
            "unit": "iter/sec",
            "range": "stddev: 0.00000391425135211352",
            "extra": "mean: 54.816261446376735 usec\nrounds: 18390"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3956.3528426078606,
            "unit": "iter/sec",
            "range": "stddev: 0.000009938710091059094",
            "extra": "mean: 252.75804251595576 usec\nrounds: 4022"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 976.2904357864368,
            "unit": "iter/sec",
            "range": "stddev: 0.000033689784116324587",
            "extra": "mean: 1.024285359504177 msec\nrounds: 968"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 645.8253560490065,
            "unit": "iter/sec",
            "range": "stddev: 0.000022514069093180448",
            "extra": "mean: 1.54840622257036 msec\nrounds: 638"
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
          "id": "159ac36b2c499bf9f67724105a9c8b64a2a08bc3",
          "message": "Merge pull request #1689 from nexi-lab/fix/server-streaming-hardcoded-default-zone-id\n\nfix(#508): change hardcoded zone ID default from \"default\" to \"root\" in server/streaming.py",
          "timestamp": "2026-02-16T18:11:44+08:00",
          "tree_id": "855d0af025073026c2eab5c52593c01ea0347b7f",
          "url": "https://github.com/nexi-lab/nexus/commit/159ac36b2c499bf9f67724105a9c8b64a2a08bc3"
        },
        "date": 1771237248014,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 314.42222002616876,
            "unit": "iter/sec",
            "range": "stddev: 0.004481408341637218",
            "extra": "mean: 3.180436802197923 msec\nrounds: 364"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 270.5722647026833,
            "unit": "iter/sec",
            "range": "stddev: 0.0011830606186229097",
            "extra": "mean: 3.6958703106500734 msec\nrounds: 338"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15845.337543666552,
            "unit": "iter/sec",
            "range": "stddev: 0.00002222762388578519",
            "extra": "mean: 63.110047182283225 usec\nrounds: 15048"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16119.075380177477,
            "unit": "iter/sec",
            "range": "stddev: 0.000036928368144088554",
            "extra": "mean: 62.03829788089183 usec\nrounds: 17695"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 52423.49254086351,
            "unit": "iter/sec",
            "range": "stddev: 0.000023496637077242743",
            "extra": "mean: 19.075417365993147 usec\nrounds: 41587"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 234.36509618463836,
            "unit": "iter/sec",
            "range": "stddev: 0.00034135773225709207",
            "extra": "mean: 4.266846967742059 msec\nrounds: 248"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 171.56970492856072,
            "unit": "iter/sec",
            "range": "stddev: 0.0006374772315550362",
            "extra": "mean: 5.828534824469077 msec\nrounds: 188"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 59.67534820316428,
            "unit": "iter/sec",
            "range": "stddev: 0.0017992679137891336",
            "extra": "mean: 16.75733833333502 msec\nrounds: 69"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23386.251664609103,
            "unit": "iter/sec",
            "range": "stddev: 0.0000024676602181906875",
            "extra": "mean: 42.76016585904276 usec\nrounds: 23936"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2486.9622739283436,
            "unit": "iter/sec",
            "range": "stddev: 0.00004008593708953742",
            "extra": "mean: 402.09697207043877 usec\nrounds: 1647"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4921.153918652611,
            "unit": "iter/sec",
            "range": "stddev: 0.0000370985410616279",
            "extra": "mean: 203.2043737160319 usec\nrounds: 2823"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 36.721614270199304,
            "unit": "iter/sec",
            "range": "stddev: 0.002201493971831551",
            "extra": "mean: 27.231918309526225 msec\nrounds: 42"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1102.4157394433314,
            "unit": "iter/sec",
            "range": "stddev: 0.006766378285859577",
            "extra": "mean: 907.0988051249643 usec\nrounds: 1483"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3900.645557711276,
            "unit": "iter/sec",
            "range": "stddev: 0.00002060747614752929",
            "extra": "mean: 256.36782045553383 usec\nrounds: 3999"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18369.001500795002,
            "unit": "iter/sec",
            "range": "stddev: 0.0000028628404448274628",
            "extra": "mean: 54.439540437552935 usec\nrounds: 18102"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3748.8068505656393,
            "unit": "iter/sec",
            "range": "stddev: 0.00001621647301064223",
            "extra": "mean: 266.75153985303746 usec\nrounds: 3538"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1018.5235234163819,
            "unit": "iter/sec",
            "range": "stddev: 0.00001857344802159269",
            "extra": "mean: 981.8133572858001 usec\nrounds: 1002"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 633.7967797104485,
            "unit": "iter/sec",
            "range": "stddev: 0.00008671975964963468",
            "extra": "mean: 1.5777928067997637 msec\nrounds: 647"
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
          "id": "f0082706ea87f86f05268160e3e85f5c637b2aa7",
          "message": "Merge pull request #1676 from nexi-lab/fix/kernel-env-vars-temporal-coref\n\nfix(#491): remove direct env var reads from kernel temporal/coref resolvers",
          "timestamp": "2026-02-16T18:14:49+08:00",
          "tree_id": "ebf444f0cf5723534c2783ac2b714ec8a8eff8a7",
          "url": "https://github.com/nexi-lab/nexus/commit/f0082706ea87f86f05268160e3e85f5c637b2aa7"
        },
        "date": 1771237389942,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 354.57451471285503,
            "unit": "iter/sec",
            "range": "stddev: 0.008522315192605696",
            "extra": "mean: 2.8202816573261886 msec\nrounds: 464"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 335.3545589119456,
            "unit": "iter/sec",
            "range": "stddev: 0.0012129510138988568",
            "extra": "mean: 2.9819186094994197 msec\nrounds: 379"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15894.081014801886,
            "unit": "iter/sec",
            "range": "stddev: 0.000016083529509678744",
            "extra": "mean: 62.91650326110185 usec\nrounds: 16558"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16852.9531775014,
            "unit": "iter/sec",
            "range": "stddev: 0.000014487979211894745",
            "extra": "mean: 59.33678148082644 usec\nrounds: 17463"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 52631.725691984655,
            "unit": "iter/sec",
            "range": "stddev: 0.000018695916034762607",
            "extra": "mean: 18.99994702534124 usec\nrounds: 43568"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 245.55641914255278,
            "unit": "iter/sec",
            "range": "stddev: 0.0002449834535904726",
            "extra": "mean: 4.072383868000088 msec\nrounds: 250"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.97477910694255,
            "unit": "iter/sec",
            "range": "stddev: 0.00036771380877545067",
            "extra": "mean: 5.406142420215317 msec\nrounds: 188"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 59.940568511351564,
            "unit": "iter/sec",
            "range": "stddev: 0.02268088296524907",
            "extra": "mean: 16.683191782050244 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23737.248986079136,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016900408198887845",
            "extra": "mean: 42.12788097670697 usec\nrounds: 24029"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2555.895572329486,
            "unit": "iter/sec",
            "range": "stddev: 0.00004173237830579768",
            "extra": "mean: 391.2522916922553 usec\nrounds: 1697"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4998.803569334051,
            "unit": "iter/sec",
            "range": "stddev: 0.00003801633106281218",
            "extra": "mean: 200.04786868094956 usec\nrounds: 3145"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.79666356912268,
            "unit": "iter/sec",
            "range": "stddev: 0.001357350737007177",
            "extra": "mean: 23.366307478265405 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1460.5250206633307,
            "unit": "iter/sec",
            "range": "stddev: 0.00019814226306150045",
            "extra": "mean: 684.685291831445 usec\nrounds: 1518"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3956.526409302165,
            "unit": "iter/sec",
            "range": "stddev: 0.00000566059331997271",
            "extra": "mean: 252.74695441155302 usec\nrounds: 4058"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18041.977086014136,
            "unit": "iter/sec",
            "range": "stddev: 0.000006699812693592888",
            "extra": "mean: 55.426298084326056 usec\nrounds: 18478"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3803.97096431607,
            "unit": "iter/sec",
            "range": "stddev: 0.000044825621766737914",
            "extra": "mean: 262.88318427787834 usec\nrounds: 3918"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 971.0972369665079,
            "unit": "iter/sec",
            "range": "stddev: 0.00012779715054196764",
            "extra": "mean: 1.0297629958497028 msec\nrounds: 964"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 651.1213947122914,
            "unit": "iter/sec",
            "range": "stddev: 0.0001789095594515396",
            "extra": "mean: 1.535811920973456 msec\nrounds: 658"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "joezhoujinjing@gmail.com",
            "name": "joezhoujinjing",
            "username": "joezhoujinjing"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "430c7c1132d07d4d0ace39e2fd2cb23a64d7d08a",
          "message": "chore: prune non-protocol unit tests, keep only RPC/MCP/IPC (#1699)\n\n* chore: prune non-protocol unit tests, keep only RPC/MCP/IPC\n\nAggressive cleanup of the unit test suite to focus exclusively on\ncore protocol tests. Removed ~537 files (~180k lines) covering\nbackends, connectors, storage, permissions, workflows, skills,\nsearch, payments, and all other non-protocol subsystems.\n\nRemaining unit tests (33 files):\n- tests/unit/ipc/ — IPC envelope, delivery, driver, discovery, storage\n- tests/unit/mcp/ — MCP server, tools, formatters, provider registry\n- tests/unit/server/test_rpc_* + test_protocol — RPC protocol tests\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* chore: restore trusted computing base tests, add time budget and README\n\nRestore kernel, system service, and storage pillar unit tests that\nform the trusted computing base. Feature module tests (search, skills,\npay, connectors, LLM, workflows, etc.) remain pruned — they are\nself-contained and covered by integration/e2e tests.\n\nTiers restored:\n- Kernel (core/): NexusFS, VFS, mounts, namespaces, permissions, ReBaC\n- System services (services/): event bus, agent registry, protocol contracts\n- Storage (backends/, storage/): backend contracts, CAS, record store\n\nAlso adds:\n- Per-test timeout: 60s (pytest-timeout in pyproject.toml)\n- Suite budget: 180s (conftest.py hook)\n- CI job timeout: 3 min (test.yml timeout-minutes)\n- tests/unit/README.md documenting test philosophy and rules\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* fix: update MCP resource tests for fastmcp 2.x compatibility\n\nfastmcp 2.x requires an active Context when calling resource\nfunctions. Updated tests to set up Context via _current_context\ncontextvar before invoking resource.fn(). Bumped fastmcp pin\nfrom >=0.2.0 to >=2.0.0.\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* chore: move misplaced root-level tests to integration/, delete dead files\n\n- Moved 4 test files from tests/ root to tests/integration/:\n  test_auth_integration.py, test_oauth_api_key_simple.py,\n  test_oauth_provision_integration.py, test_user_auth.py\n- Deleted test_share_link_e2e.py (0 tests collected, empty)\n- Deleted test_skills_segfault.sh (one-off debug script)\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* ci: add concurrency groups to cancel stale workflow runs\n\nNew pushes to the same branch now auto-cancel in-progress runs,\nreducing CI queue saturation on the free-tier 20-job limit.\n\nSkipped: docs.yml (already has pages group), release.yml (tag-only),\nlabel-sync.yml (main-only, rare).\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* ci: re-trigger workflows after clearing queue\n\n* fix: handle fastmcp version differences in resource tests\n\nresource.fn() returns a coroutine in some fastmcp versions but a\nstring in others. Use inspect.iscoroutine() to handle both cases.\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* ci: drop coverage from unit test step to stay within 3-min budget\n\n--cov roughly doubles pytest runtime on CI. Unit tests finish in\n~1m36s without coverage but were timing out at 3 min with it.\nCoverage can be measured in a separate job if needed.\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.6 <noreply@anthropic.com>",
          "timestamp": "2026-02-16T02:29:00-08:00",
          "tree_id": "c2cf033d3d07d939c41d188acb504c2973c8bf8b",
          "url": "https://github.com/nexi-lab/nexus/commit/430c7c1132d07d4d0ace39e2fd2cb23a64d7d08a"
        },
        "date": 1771237927619,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 349.28593941525895,
            "unit": "iter/sec",
            "range": "stddev: 0.008049218558345603",
            "extra": "mean: 2.8629838397563447 msec\nrounds: 493"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 327.59776170197176,
            "unit": "iter/sec",
            "range": "stddev: 0.0011413164004330024",
            "extra": "mean: 3.0525239086027036 msec\nrounds: 372"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16124.1462414071,
            "unit": "iter/sec",
            "range": "stddev: 0.000018509878758939996",
            "extra": "mean: 62.0187875393974 usec\nrounds: 16436"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16214.39333353139,
            "unit": "iter/sec",
            "range": "stddev: 0.000016132240966070723",
            "extra": "mean: 61.67359946375536 usec\nrounds: 17901"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54466.208920529556,
            "unit": "iter/sec",
            "range": "stddev: 0.00001599516331983099",
            "extra": "mean: 18.360007421465262 usec\nrounds: 41366"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 248.6227064211869,
            "unit": "iter/sec",
            "range": "stddev: 0.0002695323413437333",
            "extra": "mean: 4.022158773808533 msec\nrounds: 252"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 168.85683460011563,
            "unit": "iter/sec",
            "range": "stddev: 0.0009539183168770589",
            "extra": "mean: 5.922176631867972 msec\nrounds: 182"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 57.37414666107512,
            "unit": "iter/sec",
            "range": "stddev: 0.0214855177107051",
            "extra": "mean: 17.42945312820555 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23636.472482574787,
            "unit": "iter/sec",
            "range": "stddev: 0.0000023368979455096755",
            "extra": "mean: 42.307497480312136 usec\nrounds: 23812"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2530.8038000644788,
            "unit": "iter/sec",
            "range": "stddev: 0.00004576903310849948",
            "extra": "mean: 395.131380778914 usec\nrounds: 1644"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4853.730404543929,
            "unit": "iter/sec",
            "range": "stddev: 0.000047916268126369834",
            "extra": "mean: 206.0271001174329 usec\nrounds: 2547"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.71265743904458,
            "unit": "iter/sec",
            "range": "stddev: 0.0010184207527640732",
            "extra": "mean: 23.973538522721004 msec\nrounds: 44"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1328.901185017479,
            "unit": "iter/sec",
            "range": "stddev: 0.0005517997896220321",
            "extra": "mean: 752.5013983540447 usec\nrounds: 1579"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3911.8400453970107,
            "unit": "iter/sec",
            "range": "stddev: 0.0000073904971079423805",
            "extra": "mean: 255.63417430032226 usec\nrounds: 3821"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18548.660560912824,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026487348926169937",
            "extra": "mean: 53.9122486346684 usec\nrounds: 18312"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3936.2911144663562,
            "unit": "iter/sec",
            "range": "stddev: 0.000007011797422645522",
            "extra": "mean: 254.04625087938146 usec\nrounds: 3978"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1018.3528375805317,
            "unit": "iter/sec",
            "range": "stddev: 0.00009227901672987351",
            "extra": "mean: 981.9779187495216 usec\nrounds: 960"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 524.2577298370328,
            "unit": "iter/sec",
            "range": "stddev: 0.008068468959585778",
            "extra": "mean: 1.907458761382218 msec\nrounds: 637"
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
          "id": "b05e8407640e6c5ff94d1d0f8b533811d43a5439",
          "message": "Merge pull request #1671 from nexi-lab/fix/delete-pay-sdk-backward-compat\n\nfix(#1357): delete backward-compat shims from pay/sdk.py and pay/protocol.py",
          "timestamp": "2026-02-16T18:51:50+08:00",
          "tree_id": "9aa91953ded2a8183f7d9fbe87a4e5407baa007b",
          "url": "https://github.com/nexi-lab/nexus/commit/b05e8407640e6c5ff94d1d0f8b533811d43a5439"
        },
        "date": 1771239290839,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 376.96224344799526,
            "unit": "iter/sec",
            "range": "stddev: 0.0028048059784574197",
            "extra": "mean: 2.652785570388185 msec\nrounds: 412"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 325.9686566091826,
            "unit": "iter/sec",
            "range": "stddev: 0.0015027775147511105",
            "extra": "mean: 3.0677796153847448 msec\nrounds: 377"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14522.070805741416,
            "unit": "iter/sec",
            "range": "stddev: 0.000020812417248350647",
            "extra": "mean: 68.86070267641459 usec\nrounds: 16477"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 13392.69808046601,
            "unit": "iter/sec",
            "range": "stddev: 0.000016756755582567362",
            "extra": "mean: 74.66755346770306 usec\nrounds: 16019"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53428.59886195113,
            "unit": "iter/sec",
            "range": "stddev: 0.000015826058031146478",
            "extra": "mean: 18.7165679299171 usec\nrounds: 44303"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 232.82511435408176,
            "unit": "iter/sec",
            "range": "stddev: 0.0006035746930080864",
            "extra": "mean: 4.295069296000406 msec\nrounds: 250"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 180.4789899421103,
            "unit": "iter/sec",
            "range": "stddev: 0.0004932623169360481",
            "extra": "mean: 5.540811151041769 msec\nrounds: 192"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 70.82898210690912,
            "unit": "iter/sec",
            "range": "stddev: 0.0014535123797551144",
            "extra": "mean: 14.118514346155674 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23738.039063170374,
            "unit": "iter/sec",
            "range": "stddev: 0.000001723915209678794",
            "extra": "mean: 42.126478827457255 usec\nrounds: 24017"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2542.265575607253,
            "unit": "iter/sec",
            "range": "stddev: 0.0000434305548885494",
            "extra": "mean: 393.3499354256634 usec\nrounds: 1657"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4799.127504843312,
            "unit": "iter/sec",
            "range": "stddev: 0.000042769021406591706",
            "extra": "mean: 208.37120893137208 usec\nrounds: 2508"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.63318816173544,
            "unit": "iter/sec",
            "range": "stddev: 0.0009268729072863474",
            "extra": "mean: 23.45590473333472 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1412.265002648181,
            "unit": "iter/sec",
            "range": "stddev: 0.00038335480858673834",
            "extra": "mean: 708.0824053027369 usec\nrounds: 1584"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3969.479027778749,
            "unit": "iter/sec",
            "range": "stddev: 0.000005751015353103147",
            "extra": "mean: 251.92222782937398 usec\nrounds: 4003"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 17710.64394639963,
            "unit": "iter/sec",
            "range": "stddev: 0.0000061093807169092435",
            "extra": "mean: 56.46322081943771 usec\nrounds: 17109"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3954.6758818701987,
            "unit": "iter/sec",
            "range": "stddev: 0.00001001039615785944",
            "extra": "mean: 252.86522331309024 usec\nrounds: 3972"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 985.673758828915,
            "unit": "iter/sec",
            "range": "stddev: 0.00002313066681454285",
            "extra": "mean: 1.014534465428101 msec\nrounds: 969"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 554.8220013436221,
            "unit": "iter/sec",
            "range": "stddev: 0.007298455518280616",
            "extra": "mean: 1.802379858005419 msec\nrounds: 662"
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
          "id": "5ba5ffb3800c171c1237da0ae1625129145b78dd",
          "message": "refactor(#1498): Decompose memory_api.py god class (3,591 → 2,135 LOC) (#1700)\n\n* refactor(#1498): Decompose memory_api.py god class via Composition+Facade\n\nExtract 5 service classes from Memory (3,591 → 2,135 LOC, -40%):\n\n- EnrichmentPipeline: 8-step enrichment pipeline from store()\n- MemoryVersioning: version tracking, rollback, diff, history, GC\n- MemoryStateManager: approve, deactivate, delete, invalidate, revalidate\n- AceFacade: lazy-loaded ACE service composition (trajectory, feedback, etc.)\n- Pydantic v2 response models replacing hand-built dicts\n\nAll 63 public methods preserved (zero breaking changes).\n32 new TDD tests for enrichment pipeline + response models + helpers.\n929 tests passing across memory, ACE, and remote test suites.\n\n* fix(#1498): ruff format memory_api.py for CI\n\n* fix(#1498): Resolve mypy no-any-return errors in memory decomposition\n\nAdd proper type annotations to AceFacade properties using TYPE_CHECKING\nimports (lazy loading preserved at runtime). Cast content_bytes to bytes\nin _read_content/_read_version_content to satisfy type checker.",
          "timestamp": "2026-02-16T04:41:56-08:00",
          "tree_id": "40437314421d12e4144c1c151a91dbdf9916f432",
          "url": "https://github.com/nexi-lab/nexus/commit/5ba5ffb3800c171c1237da0ae1625129145b78dd"
        },
        "date": 1771245992619,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 396.91137586640787,
            "unit": "iter/sec",
            "range": "stddev: 0.0008051748374532327",
            "extra": "mean: 2.5194541169729012 msec\nrounds: 436"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 327.67243286032266,
            "unit": "iter/sec",
            "range": "stddev: 0.0009124614566238",
            "extra": "mean: 3.0518282886075783 msec\nrounds: 395"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15308.792594703416,
            "unit": "iter/sec",
            "range": "stddev: 0.000018114997614788463",
            "extra": "mean: 65.32193795257133 usec\nrounds: 16294"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16175.776779531181,
            "unit": "iter/sec",
            "range": "stddev: 0.00001555967873318748",
            "extra": "mean: 61.82083331326625 usec\nrounds: 16642"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54237.13103290957,
            "unit": "iter/sec",
            "range": "stddev: 0.000014417374096456613",
            "extra": "mean: 18.437553405861898 usec\nrounds: 44761"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 249.6244751113671,
            "unit": "iter/sec",
            "range": "stddev: 0.00032704230717932987",
            "extra": "mean: 4.006017437007575 msec\nrounds: 254"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 187.4713950705688,
            "unit": "iter/sec",
            "range": "stddev: 0.0004075811345211739",
            "extra": "mean: 5.334147108808657 msec\nrounds: 193"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 72.74634462556543,
            "unit": "iter/sec",
            "range": "stddev: 0.0013282034103871696",
            "extra": "mean: 13.746395164556041 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23715.178076542183,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016639332414817545",
            "extra": "mean: 42.16708796250397 usec\nrounds: 23942"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2609.8226456559237,
            "unit": "iter/sec",
            "range": "stddev: 0.00004013244089370579",
            "extra": "mean: 383.16779941522475 usec\nrounds: 1710"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5039.798216140966,
            "unit": "iter/sec",
            "range": "stddev: 0.00003971213908270539",
            "extra": "mean: 198.42064247677595 usec\nrounds: 3004"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.25864741022173,
            "unit": "iter/sec",
            "range": "stddev: 0.0014056146869895604",
            "extra": "mean: 24.237343266668805 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1458.7867392397511,
            "unit": "iter/sec",
            "range": "stddev: 0.00025138854393672673",
            "extra": "mean: 685.5011586691221 usec\nrounds: 1563"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3955.4422982908764,
            "unit": "iter/sec",
            "range": "stddev: 0.00000859222981147992",
            "extra": "mean: 252.81622751318966 usec\nrounds: 3969"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 17751.91808301642,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027456444574740537",
            "extra": "mean: 56.331940882304885 usec\nrounds: 17389"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3893.1848208870556,
            "unit": "iter/sec",
            "range": "stddev: 0.000028156734780846962",
            "extra": "mean: 256.85911304158213 usec\nrounds: 3972"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1018.5290249938992,
            "unit": "iter/sec",
            "range": "stddev: 0.000013974445940420154",
            "extra": "mean: 981.808054027709 usec\nrounds: 1018"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 555.7068567976887,
            "unit": "iter/sec",
            "range": "stddev: 0.006801472328540559",
            "extra": "mean: 1.7995099174456672 msec\nrounds: 642"
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
          "id": "bf98b60a30acc2910710555c7341c370a46893c1",
          "message": "refactor(#1400): extract Skills (9,391 LOC) into self-contained brick (#1701)\n\n* test(#1400): add targeted skill service tests + brick contract skeleton\n\n- Add 21 targeted tests for _discover_impl covering all filter modes\n  (subscribed, owned, public, shared, all), metadata loading with\n  system context fallback, error paths for _find_public_skills and\n  _find_direct_viewer_skills, and _load_assigned_skills edge cases\n- Add brick contract test skeleton with AST-based zero-core-imports\n  check (skipped until Phase 1), protocol satisfaction tests, and\n  module boundary verification\n- Baseline: 289 + 45 + 23 = 357 tests passing\n\n* refactor(#1400): decouple skills from nexus.core — local exceptions, narrow protocol, DI\n\nPhase 1 of skills brick extraction:\n\n- Create nexus.skills.exceptions with local exception types (SkillValidationError,\n  SkillPermissionDeniedError, etc.) replacing nexus.core.exceptions imports\n- Add SkillOperationContext and DatabaseConnection Protocols to types.py\n- Narrow protocols.py from 1,078 LOC to 123 LOC (6 methods skills actually uses)\n- Inject ServiceMap via DI in skill_generator.py (lazy runtime fallback)\n- Move OperationContext imports to TYPE_CHECKING blocks\n- Add from __future__ import annotations to registry.py, manager.py, importer.py\n- Update scoped_filesystem.py to import from core.filesystem directly\n- Update all tests to use new exception types\n\n* refactor(#1400): extract MCP subsystem (2,449 LOC) from skills to nexus/mcp/\n\nMove mcp_models.py, mcp_mount.py, mcp_exporter.py from nexus/skills/ to\nnexus/mcp/ as models.py, mount.py, exporter.py. Update all 12 importing\nfiles across CLI, services, tests. MCPMountError is now a standalone\nexception to break the circular import between nexus.mcp and nexus.skills.\n\n- git mv 3 MCP source files + 1 test file\n- Update nexus/mcp/__init__.py with new re-exports\n- Update all lazy imports in cli/commands/skills.py (8 locations)\n- Update services/mcp_service.py, oauth_service.py, connection_manager.py\n- Move test_mcp_skills.py to tests/unit/mcp/\n- 621 tests pass, ruff clean\n\n* refactor(#1400): pipeline discover + batch ReBAC + _build_skill_info helper\n\nExtract _build_skill_info() to eliminate 6x SkillInfo construction\nrepetition. Pre-compute public_set and shared_set in the \"all\" filter\nto use O(1) set lookups instead of O(n) per-skill ReBAC calls. Pass\npre-computed paths to _collect_skill_paths to avoid double queries.\nRemove dead filter code in the \"all\" path (already handled by early returns).\n\n* perf(#1400): lazy imports + subscription caching\n\n- Convert skills/__init__.py to lazy imports via __getattr__ (PEP 562):\n  eager: Skill, SkillMetadata, SkillParser, SkillRegistry, SkillManager\n  lazy: Analytics, Governance, Audit, Exporter, Templates, MCP compat\n- Add request-scoped subscription cache to SkillService keyed by\n  (user_id, zone_id) — avoids repeated YAML parsing in discover flow\n- Invalidate cache on _save_subscriptions writes\n\n* test(#1400): finalize contract tests + E2E timing assertions\n\n- Un-skip all brick contract tests (13 tests, all green):\n  - Zero core/backends/services imports at module level (AST scan)\n  - Protocol satisfaction (NexusFS ABC satisfies narrow Skills Protocol)\n  - Local exception hierarchy + is_expected attribute\n  - Lazy imports via __getattr__ resolve correctly\n  - MCP backward-compat re-exports match canonical nexus.mcp source\n- Add function-scoped import detection to AST checker (allows\n  skill_generator.py's lazy try/except pattern)\n- Add E2E timing assertions for discover (10 skills < 2s)\n  and prompt context generation (< 2s)\n\n* test(#1400): add cross-user permission enforcement E2E tests\n\n- Add 3 tests validating non-owner permission enforcement:\n  - bob cannot discover admin's private skills\n  - bob sees admin's skill after public share\n  - bob cannot load admin's private skill content\n- Extend rpc/rpc_result helpers with custom headers parameter\n- All 20 E2E tests pass with real FastAPI server + permissions\n\n* style: ruff format test_protocol_compatibility.py\n\n* fix(#1400): fix connection_manager import after MCP extraction + rebase\n\n* fix(#1400): fix mypy errors — write signature, TYPE_CHECKING imports, cast fix\n\n- Protocol write() now uses explicit params (if_match, if_none_match, force)\n  instead of **kwargs to match NexusFilesystem ABC signature\n- Add TYPE_CHECKING block in skills/__init__.py so mypy resolves lazy imports\n- Fix mcp_service.py cast to use narrow Protocol instead of core ABC\n\n* chore: remove accidentally committed plan files\n\n* fix(#1400): add delete() to narrow NexusFilesystem Protocol\n\nconnection_manager.py calls filesystem.delete() which was missing\nfrom the narrow Protocol, causing mypy attr-defined error.",
          "timestamp": "2026-02-16T05:00:40-08:00",
          "tree_id": "1e4c9a99cd9bdb6af465b4ceb006b9ee88957210",
          "url": "https://github.com/nexi-lab/nexus/commit/bf98b60a30acc2910710555c7341c370a46893c1"
        },
        "date": 1771247045885,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 376.6780973015206,
            "unit": "iter/sec",
            "range": "stddev: 0.006516368957305489",
            "extra": "mean: 2.654786692308067 msec\nrounds: 481"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 339.422651021442,
            "unit": "iter/sec",
            "range": "stddev: 0.0008505603064876234",
            "extra": "mean: 2.94617933420368 msec\nrounds: 383"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15199.917246479841,
            "unit": "iter/sec",
            "range": "stddev: 0.00004114676977030321",
            "extra": "mean: 65.78983186448536 usec\nrounds: 15999"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16303.450366792107,
            "unit": "iter/sec",
            "range": "stddev: 0.000017773982163636456",
            "extra": "mean: 61.336709561606845 usec\nrounds: 17267"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53472.40413536487,
            "unit": "iter/sec",
            "range": "stddev: 0.00001715269165240883",
            "extra": "mean: 18.701235079472205 usec\nrounds: 45474"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 247.05990461907598,
            "unit": "iter/sec",
            "range": "stddev: 0.0002755243542063121",
            "extra": "mean: 4.0476013359667915 msec\nrounds: 253"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 182.98686390752212,
            "unit": "iter/sec",
            "range": "stddev: 0.0004098965929191968",
            "extra": "mean: 5.464873153437833 msec\nrounds: 189"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 66.77751846025686,
            "unit": "iter/sec",
            "range": "stddev: 0.0015529212534797399",
            "extra": "mean: 14.975099750002803 msec\nrounds: 76"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23740.91469864493,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017084321227167037",
            "extra": "mean: 42.12137622722167 usec\nrounds: 23834"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2587.443017746507,
            "unit": "iter/sec",
            "range": "stddev: 0.000045646140431646614",
            "extra": "mean: 386.48194110606323 usec\nrounds: 1664"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4893.5535313970395,
            "unit": "iter/sec",
            "range": "stddev: 0.000037534288156264",
            "extra": "mean: 204.3504773339047 usec\nrounds: 3331"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.63167529934348,
            "unit": "iter/sec",
            "range": "stddev: 0.0011215542460379932",
            "extra": "mean: 23.456737108696263 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1419.0339990357663,
            "unit": "iter/sec",
            "range": "stddev: 0.00028010372993568386",
            "extra": "mean: 704.7047503298018 usec\nrounds: 1514"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3976.242315791892,
            "unit": "iter/sec",
            "range": "stddev: 0.0000057300004735956925",
            "extra": "mean: 251.49372713741266 usec\nrounds: 4035"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 17737.205328428234,
            "unit": "iter/sec",
            "range": "stddev: 0.000003157356096882647",
            "extra": "mean: 56.37866741031937 usec\nrounds: 17481"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3903.5334575660722,
            "unit": "iter/sec",
            "range": "stddev: 0.000009566822283268522",
            "extra": "mean: 256.17815522030116 usec\nrounds: 3975"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1018.4024737940487,
            "unit": "iter/sec",
            "range": "stddev: 0.00001827600724197138",
            "extra": "mean: 981.9300578429563 usec\nrounds: 1020"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 522.1660439358978,
            "unit": "iter/sec",
            "range": "stddev: 0.0074076795366806",
            "extra": "mean: 1.915099634710759 msec\nrounds: 605"
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
          "id": "8b34b62feeac614fc8ea48e340228c797e6b4ec9",
          "message": "refactor(#1585,#1586,#1587): split A2A router, extract serialization, unify messaging (#1710)\n\n* chore: fix pre-existing mypy errors in async_local.py and nexus_fs_events.py\n\nAdd type: ignore comments for async override pattern and mixin attr access\nthat were hidden by ruff format CI failure on main.\n\n* chore: remove unused type: ignore comments after main merge\n\nMain fixed the async override type issues in Backend, making the\ntype: ignore[override] comments unnecessary.\n\n* refactor(#1589): extract HeartbeatBuffer from AgentRegistry (SRP)\n\nExtract heartbeat buffering (~160 LOC) into a standalone HeartbeatBuffer\nclass composed via DI. AgentRegistry delegates heartbeat(), flush_heartbeats(),\nand detect_stale() to the new class while keeping the public API 100%\nbackward-compatible.\n\n- HeartbeatBuffer accepts flush_callback (no SQLAlchemy dependency)\n- Separate locks by owner (buffer lock vs cache/known-agents lock)\n- _restore_buffer() extracted as named method (fixes 5-level nesting)\n- stats() method for observability (buffer_size, total_flushed, etc.)\n- Fixed f-string logging to %-style in touched code\n- 30 dedicated HeartbeatBuffer tests (all mock-based, no DB)\n- 114 total tests pass (unit + integration + async)\n\n* fix: sort imports in agent_registry.py after path relocation\n\nruff isort fix — nexus.core before nexus.services\n\n* refactor(#1585,#1586,#1587): split A2A router, extract serialization, unify messaging\n\n- Split router.py (536→241 LOC) into router + handlers + streaming modules\n- Extract StreamRegistry from TaskManager (SRP, bounded queues)\n- Extract stores/serialization.py (DRY across DB/InMemory stores)\n- Add core/messaging.py shared primitives + messaging_adapters.py\n- Add public properties (store, stream_registry) to TaskManager\n- Narrow exception handling to ValidationError where appropriate\n- Add 55 new tests (stream_registry, handlers, serialization, messaging)\n- Add database backend to parameterized task store tests\n- Fix mypy, ruff lint, ruff format — all clean\n\nStream 10\n\n356 tests pass (337 unit/integration + 14 e2e + 5 e2e auth)",
          "timestamp": "2026-02-16T05:05:49-08:00",
          "tree_id": "0e104e85c5eba83cbc92026e2ab59d4e583442d3",
          "url": "https://github.com/nexi-lab/nexus/commit/8b34b62feeac614fc8ea48e340228c797e6b4ec9"
        },
        "date": 1771247336380,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 472.5485119211842,
            "unit": "iter/sec",
            "range": "stddev: 0.003738608746819489",
            "extra": "mean: 2.1161848461535078 msec\nrounds: 598"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 365.6186954696207,
            "unit": "iter/sec",
            "range": "stddev: 0.0004826847501603015",
            "extra": "mean: 2.7350898966354693 msec\nrounds: 416"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 18116.289296442694,
            "unit": "iter/sec",
            "range": "stddev: 0.000029624370632951745",
            "extra": "mean: 55.198941882450484 usec\nrounds: 15331"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 18604.64598821603,
            "unit": "iter/sec",
            "range": "stddev: 0.000016344991058031192",
            "extra": "mean: 53.75001494967378 usec\nrounds: 22609"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 49066.37778207374,
            "unit": "iter/sec",
            "range": "stddev: 0.0006657527898358019",
            "extra": "mean: 20.380554775032675 usec\nrounds: 55381"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 243.78169342796272,
            "unit": "iter/sec",
            "range": "stddev: 0.0007623614833998618",
            "extra": "mean: 4.102030738807298 msec\nrounds: 268"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 197.1723076966331,
            "unit": "iter/sec",
            "range": "stddev: 0.00041887588710121207",
            "extra": "mean: 5.071706121828161 msec\nrounds: 197"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 72.50404425810791,
            "unit": "iter/sec",
            "range": "stddev: 0.0014866675981327175",
            "extra": "mean: 13.792334072291052 msec\nrounds: 83"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 19927.99147470604,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014479238694311737",
            "extra": "mean: 50.18067180876045 usec\nrounds: 20031"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2773.151482314777,
            "unit": "iter/sec",
            "range": "stddev: 0.000008930403925536196",
            "extra": "mean: 360.6005681180064 usec\nrounds: 2848"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 6753.592774857228,
            "unit": "iter/sec",
            "range": "stddev: 0.000009513409939522561",
            "extra": "mean: 148.06933632760234 usec\nrounds: 5789"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 47.77339367182455,
            "unit": "iter/sec",
            "range": "stddev: 0.0009158708713400852",
            "extra": "mean: 20.932153300002483 msec\nrounds: 50"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1815.289646893703,
            "unit": "iter/sec",
            "range": "stddev: 0.0003923560366833269",
            "extra": "mean: 550.876275701338 usec\nrounds: 1926"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 6521.987131204708,
            "unit": "iter/sec",
            "range": "stddev: 0.0000029950950485740323",
            "extra": "mean: 153.32750278139312 usec\nrounds: 6472"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 27680.062572934177,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014464098210546709",
            "extra": "mean: 36.127085961785696 usec\nrounds: 27582"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 4348.409959875643,
            "unit": "iter/sec",
            "range": "stddev: 0.000004349920748901697",
            "extra": "mean: 229.96911726984413 usec\nrounds: 4366"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1000.5953532311229,
            "unit": "iter/sec",
            "range": "stddev: 0.000015040198065022074",
            "extra": "mean: 999.4050010034522 usec\nrounds: 996"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 614.3009779589017,
            "unit": "iter/sec",
            "range": "stddev: 0.006299905285180751",
            "extra": "mean: 1.6278665277770443 msec\nrounds: 720"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "joezhoujinjing@gmail.com",
            "name": "joezhoujinjing",
            "username": "joezhoujinjing"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "0e1a422f9522dcda00ed63b53bc81e94d53404ee",
          "message": "fix(ci): remove per-test timeouts; keep unit suite lean without flaky limits (#1712)\n\n* chore: prune non-protocol unit tests, keep only RPC/MCP/IPC\n\nAggressive cleanup of the unit test suite to focus exclusively on\ncore protocol tests. Removed ~537 files (~180k lines) covering\nbackends, connectors, storage, permissions, workflows, skills,\nsearch, payments, and all other non-protocol subsystems.\n\nRemaining unit tests (33 files):\n- tests/unit/ipc/ — IPC envelope, delivery, driver, discovery, storage\n- tests/unit/mcp/ — MCP server, tools, formatters, provider registry\n- tests/unit/server/test_rpc_* + test_protocol — RPC protocol tests\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* chore: restore trusted computing base tests, add time budget and README\n\nRestore kernel, system service, and storage pillar unit tests that\nform the trusted computing base. Feature module tests (search, skills,\npay, connectors, LLM, workflows, etc.) remain pruned — they are\nself-contained and covered by integration/e2e tests.\n\nTiers restored:\n- Kernel (core/): NexusFS, VFS, mounts, namespaces, permissions, ReBaC\n- System services (services/): event bus, agent registry, protocol contracts\n- Storage (backends/, storage/): backend contracts, CAS, record store\n\nAlso adds:\n- Per-test timeout: 60s (pytest-timeout in pyproject.toml)\n- Suite budget: 180s (conftest.py hook)\n- CI job timeout: 3 min (test.yml timeout-minutes)\n- tests/unit/README.md documenting test philosophy and rules\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* fix: update MCP resource tests for fastmcp 2.x compatibility\n\nfastmcp 2.x requires an active Context when calling resource\nfunctions. Updated tests to set up Context via _current_context\ncontextvar before invoking resource.fn(). Bumped fastmcp pin\nfrom >=0.2.0 to >=2.0.0.\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* chore: move misplaced root-level tests to integration/, delete dead files\n\n- Moved 4 test files from tests/ root to tests/integration/:\n  test_auth_integration.py, test_oauth_api_key_simple.py,\n  test_oauth_provision_integration.py, test_user_auth.py\n- Deleted test_share_link_e2e.py (0 tests collected, empty)\n- Deleted test_skills_segfault.sh (one-off debug script)\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* ci: add concurrency groups to cancel stale workflow runs\n\nNew pushes to the same branch now auto-cancel in-progress runs,\nreducing CI queue saturation on the free-tier 20-job limit.\n\nSkipped: docs.yml (already has pages group), release.yml (tag-only),\nlabel-sync.yml (main-only, rare).\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* ci: re-trigger workflows after clearing queue\n\n* fix: handle fastmcp version differences in resource tests\n\nresource.fn() returns a coroutine in some fastmcp versions but a\nstring in others. Use inspect.iscoroutine() to handle both cases.\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* ci: drop coverage from unit test step to stay within 3-min budget\n\n--cov roughly doubles pytest runtime on CI. Unit tests finish in\n~1m36s without coverage but were timing out at 3 min with it.\nCoverage can be measured in a separate job if needed.\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* fix(ci): add pytest-timeout to docker integration test deps\n\npyproject.toml addopts includes --timeout=60, which requires\npytest-timeout. The docker integration job was missing it.\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* fix: remove per-test timeout from addopts; add hypothesis to test deps\n\n- Remove --timeout=60 from pyproject.toml addopts. Per-test timeouts cause\n  flaky CI (variable runner load) and mask real issues; slow tests should\n  be profiled and optimized, not killed.\n- Add comment above addopts explaining why we do not use per-test timeouts.\n- Remove pytest-timeout from docker-integration workflow pip install.\n- Add hypothesis to test optional deps for test_kernel_eventlog_invariants.\n\nCo-authored-by: Cursor <cursoragent@cursor.com>\n\n* ci: remove 3-minute step timeout for unit tests\n\nStep-level timeout caused flaky failures on busy runners. Rely on\ndefault job timeout; keep tests fast via practices (see tests/unit/README).\n\nCo-authored-by: Cursor <cursoragent@cursor.com>\n\n* docs: document best practices for keeping unit tests lean\n\n- Replace hard timeout references with ~3 min target and practices.\n- Recommend @pytest.mark.slow, pytest --durations=N, and moving slow\n  tests to integration/e2e. No CI timeouts to avoid flakiness.\n\nCo-authored-by: Cursor <cursoragent@cursor.com>\n\n* fix: replace conftest 180s abort with warning; add CI duration step\n\n- Conftest: warn (do not fail) when suite exceeds 3 min to avoid flakiness.\n- CI: record unit test duration in job summary; fail only if >5 min.\n- README: document enforcement (warning + 5min cap).\n\nCo-authored-by: Cursor <cursoragent@cursor.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.6 <noreply@anthropic.com>\nCo-authored-by: Cursor <cursoragent@cursor.com>",
          "timestamp": "2026-02-16T05:17:48-08:00",
          "tree_id": "2bfc5c3ba2ab81d06eeecc40d192512bdfaa209f",
          "url": "https://github.com/nexi-lab/nexus/commit/0e1a422f9522dcda00ed63b53bc81e94d53404ee"
        },
        "date": 1771248060977,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 329.17328804860165,
            "unit": "iter/sec",
            "range": "stddev: 0.009063146410613138",
            "extra": "mean: 3.0379135741182997 msec\nrounds: 425"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 337.2639446507473,
            "unit": "iter/sec",
            "range": "stddev: 0.0007915777945370416",
            "extra": "mean: 2.9650367786439404 msec\nrounds: 384"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14458.011847891223,
            "unit": "iter/sec",
            "range": "stddev: 0.00001914983311316664",
            "extra": "mean: 69.16580305236471 usec\nrounds: 16512"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15546.342023952899,
            "unit": "iter/sec",
            "range": "stddev: 0.000014153012041532102",
            "extra": "mean: 64.32381318121384 usec\nrounds: 15234"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54590.29149672639,
            "unit": "iter/sec",
            "range": "stddev: 0.00001376325707213947",
            "extra": "mean: 18.31827551351263 usec\nrounds: 47896"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 247.25049186832553,
            "unit": "iter/sec",
            "range": "stddev: 0.0002600757315181335",
            "extra": "mean: 4.044481337301263 msec\nrounds: 252"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 185.4775352934731,
            "unit": "iter/sec",
            "range": "stddev: 0.0003295439942197815",
            "extra": "mean: 5.391488507854836 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 72.49828053714388,
            "unit": "iter/sec",
            "range": "stddev: 0.0012390993698929151",
            "extra": "mean: 13.793430583331675 msec\nrounds: 72"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23736.52112684662,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016742521140665092",
            "extra": "mean: 42.12917279057268 usec\nrounds: 23977"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2576.2451400409705,
            "unit": "iter/sec",
            "range": "stddev: 0.000044100854680349424",
            "extra": "mean: 388.16181909773417 usec\nrounds: 1686"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4911.534070420786,
            "unit": "iter/sec",
            "range": "stddev: 0.00010012920807472144",
            "extra": "mean: 203.60237466790636 usec\nrounds: 5274"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 43.12942375624767,
            "unit": "iter/sec",
            "range": "stddev: 0.0008408012975716008",
            "extra": "mean: 23.186027377774582 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1397.8643592026253,
            "unit": "iter/sec",
            "range": "stddev: 0.00038583238803436664",
            "extra": "mean: 715.3769916348848 usec\nrounds: 1554"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3963.745504968742,
            "unit": "iter/sec",
            "range": "stddev: 0.000016880178355242783",
            "extra": "mean: 252.2866311034482 usec\nrounds: 3993"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18358.074721538538,
            "unit": "iter/sec",
            "range": "stddev: 0.0000028944414202474057",
            "extra": "mean: 54.47194300972934 usec\nrounds: 18389"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3924.3549767024856,
            "unit": "iter/sec",
            "range": "stddev: 0.000022109978860195887",
            "extra": "mean: 254.8189462820382 usec\nrounds: 4021"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1017.9943986358847,
            "unit": "iter/sec",
            "range": "stddev: 0.000028584311090193766",
            "extra": "mean: 982.3236761813254 usec\nrounds: 1016"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 635.6545900215908,
            "unit": "iter/sec",
            "range": "stddev: 0.00003241169635826542",
            "extra": "mean: 1.5731814348513298 msec\nrounds: 637"
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
          "id": "085239364632c49d894bb9908e06190001585b9c",
          "message": "Merge pull request #1715 from nexi-lab/fix/fuse-mount-private-attr-access\n\nfix: replace private _permission_enforcer access in fuse/mount with public property",
          "timestamp": "2026-02-16T21:27:23+08:00",
          "tree_id": "b6f8a8e3e3c2ebb645b694ad40e0891f38dab160",
          "url": "https://github.com/nexi-lab/nexus/commit/085239364632c49d894bb9908e06190001585b9c"
        },
        "date": 1771248639970,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 370.6864258148813,
            "unit": "iter/sec",
            "range": "stddev: 0.003133979230279197",
            "extra": "mean: 2.697697920288547 msec\nrounds: 414"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 320.0595830838431,
            "unit": "iter/sec",
            "range": "stddev: 0.0010319200842032038",
            "extra": "mean: 3.1244182422684688 msec\nrounds: 388"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16069.101421275465,
            "unit": "iter/sec",
            "range": "stddev: 0.00001756971896404506",
            "extra": "mean: 62.23123333306003 usec\nrounds: 13080"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16263.2985518127,
            "unit": "iter/sec",
            "range": "stddev: 0.000015839491358373394",
            "extra": "mean: 61.488141339478794 usec\nrounds: 17320"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 43483.97396562888,
            "unit": "iter/sec",
            "range": "stddev: 0.0008146031355354051",
            "extra": "mean: 22.996978169254536 usec\nrounds: 45166"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 246.43223442471054,
            "unit": "iter/sec",
            "range": "stddev: 0.0002528914629558384",
            "extra": "mean: 4.057910696360293 msec\nrounds: 247"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 181.83047355816308,
            "unit": "iter/sec",
            "range": "stddev: 0.00044685680455264584",
            "extra": "mean: 5.499628200000947 msec\nrounds: 195"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 71.8473707368337,
            "unit": "iter/sec",
            "range": "stddev: 0.0012573219272651647",
            "extra": "mean: 13.918393808213974 msec\nrounds: 73"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23706.75922027273,
            "unit": "iter/sec",
            "range": "stddev: 0.0000020417592945476564",
            "extra": "mean: 42.18206253787968 usec\nrounds: 23250"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2535.47736897205,
            "unit": "iter/sec",
            "range": "stddev: 0.00004045141642368401",
            "extra": "mean: 394.40304703071615 usec\nrounds: 1701"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4889.465338906333,
            "unit": "iter/sec",
            "range": "stddev: 0.00004254892252897473",
            "extra": "mean: 204.52133938711552 usec\nrounds: 2808"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 38.05687625823243,
            "unit": "iter/sec",
            "range": "stddev: 0.0019119537317418614",
            "extra": "mean: 26.27646034883593 msec\nrounds: 43"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1461.0387704786388,
            "unit": "iter/sec",
            "range": "stddev: 0.00019260746464489216",
            "extra": "mean: 684.4445337151445 usec\nrounds: 1572"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3965.777264039134,
            "unit": "iter/sec",
            "range": "stddev: 0.0000056920594611693036",
            "extra": "mean: 252.15737884923533 usec\nrounds: 3962"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18186.95013445304,
            "unit": "iter/sec",
            "range": "stddev: 0.000007670017065802594",
            "extra": "mean: 54.984480223851136 usec\nrounds: 17521"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3929.8784986168744,
            "unit": "iter/sec",
            "range": "stddev: 0.000009952337535468155",
            "extra": "mean: 254.4607932158593 usec\nrounds: 3980"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1014.0934500931924,
            "unit": "iter/sec",
            "range": "stddev: 0.00002973525646836812",
            "extra": "mean: 986.1024148298196 usec\nrounds: 998"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 533.9964529022324,
            "unit": "iter/sec",
            "range": "stddev: 0.007801099809879202",
            "extra": "mean: 1.872671615260873 msec\nrounds: 629"
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
          "id": "fb11757e3ab02fa08e8a84846b9ebbaf90a28c0f",
          "message": "fix(#577): change hardcoded zone ID fallback from \"default\" to \"root\" in event_bus_nats.py (#1742)\n\n* fix(#577): change hardcoded zone ID fallback from \"default\" to \"root\" in event_bus_nats.py\n\nAligns with federation-memo.md canonical ROOT_ZONE_ID = \"root\".\nNote: Moving NATS event bus from core/ to services/ is a larger architectural\nchange tracked separately.\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* fix(#577): update test expectation from \"default\" to \"root\" zone_id\n\nThe test_publish_default_zone test still expected \"nexus.events.default.file_write\"\nafter the source changed the fallback zone_id from \"default\" to \"root\". Updated\nthe test to use ROOT_ZONE_ID constant and renamed to test_publish_root_zone.\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n* fix(#577): remove xfail marker from test_bulk_check_returns_dict_of_results\n\nThe test now passes consistently, so the @pytest.mark.xfail decorator\ncauses an XPASS CI failure. Remove it to let the test pass normally.\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.6 <noreply@anthropic.com>",
          "timestamp": "2026-02-16T22:13:39+08:00",
          "tree_id": "fb94ee76dbfe133c5dc6dc7ede6af95a8ea3ab02",
          "url": "https://github.com/nexi-lab/nexus/commit/fb11757e3ab02fa08e8a84846b9ebbaf90a28c0f"
        },
        "date": 1771251439117,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 360.5743002608483,
            "unit": "iter/sec",
            "range": "stddev: 0.006911162169706576",
            "extra": "mean: 2.7733535065493453 msec\nrounds: 458"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 330.1337343572122,
            "unit": "iter/sec",
            "range": "stddev: 0.0010473021939055142",
            "extra": "mean: 3.0290754804172098 msec\nrounds: 383"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15369.834561154494,
            "unit": "iter/sec",
            "range": "stddev: 0.000024260265487887806",
            "extra": "mean: 65.06250903489789 usec\nrounds: 16270"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15161.844347235972,
            "unit": "iter/sec",
            "range": "stddev: 0.00001903440429212697",
            "extra": "mean: 65.95503667614827 usec\nrounds: 17341"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54781.99479721047,
            "unit": "iter/sec",
            "range": "stddev: 0.000013444123486499464",
            "extra": "mean: 18.25417281173778 usec\nrounds: 38215"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 236.71100118428126,
            "unit": "iter/sec",
            "range": "stddev: 0.0005135433574606851",
            "extra": "mean: 4.224560730160119 msec\nrounds: 252"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 185.9907259752568,
            "unit": "iter/sec",
            "range": "stddev: 0.00030061268596748265",
            "extra": "mean: 5.376612165775592 msec\nrounds: 187"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 70.09651313900311,
            "unit": "iter/sec",
            "range": "stddev: 0.0013180707072722015",
            "extra": "mean: 14.26604484615341 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23733.644097013534,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016988842186120792",
            "extra": "mean: 42.1342797554562 usec\nrounds: 24046"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2567.144115177467,
            "unit": "iter/sec",
            "range": "stddev: 0.00004268727436919595",
            "extra": "mean: 389.53792819335735 usec\nrounds: 1699"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5074.20616485075,
            "unit": "iter/sec",
            "range": "stddev: 0.0000366092138506345",
            "extra": "mean: 197.07516161385877 usec\nrounds: 4189"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 43.334910828583695,
            "unit": "iter/sec",
            "range": "stddev: 0.0010155061687423317",
            "extra": "mean: 23.07608302127624 msec\nrounds: 47"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1456.408774603566,
            "unit": "iter/sec",
            "range": "stddev: 0.0003129409965721979",
            "extra": "mean: 686.6204169033516 usec\nrounds: 1408"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3983.713576481385,
            "unit": "iter/sec",
            "range": "stddev: 0.000005502355157447213",
            "extra": "mean: 251.02206290725596 usec\nrounds: 3990"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18307.05225919514,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026142862442286334",
            "extra": "mean: 54.62375842062322 usec\nrounds: 17071"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3933.9057196065473,
            "unit": "iter/sec",
            "range": "stddev: 0.000012770270257706018",
            "extra": "mean: 254.2002964168688 usec\nrounds: 3991"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 970.9821989757455,
            "unit": "iter/sec",
            "range": "stddev: 0.00004205554040259435",
            "extra": "mean: 1.0298849979483293 msec\nrounds: 975"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 547.3117453285395,
            "unit": "iter/sec",
            "range": "stddev: 0.007639535197843647",
            "extra": "mean: 1.8271122601246597 msec\nrounds: 642"
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
          "id": "5d5906da61f9639aed50b1acd3d519abdcf138ed",
          "message": "chore(#1633, #1504): add brick zero-core-imports lint + fix gRPC docs (#1744)\n\n#1633: Add automated CI check and pre-commit hook enforcing LEGO\nArchitecture Principle 3 — bricks must not import from nexus.core or\nnexus.services internals (only from protocols and storage ABCs).\nIncludes 29 unit tests.\n\n#1504: Update KERNEL-ARCHITECTURE.md §6 gRPC table — add missing\nGetClusterInfo RPC to ZoneApiService, add ExchangeService (17 RPCs),\nadd SSOT note pointing to proto files as source of truth.",
          "timestamp": "2026-02-16T06:25:10-08:00",
          "tree_id": "abca95d05947a1c6f750f44c154185bd52d60163",
          "url": "https://github.com/nexi-lab/nexus/commit/5d5906da61f9639aed50b1acd3d519abdcf138ed"
        },
        "date": 1771252120147,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 329.43231180825296,
            "unit": "iter/sec",
            "range": "stddev: 0.009060880414454724",
            "extra": "mean: 3.03552494444459 msec\nrounds: 486"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 330.9818918565246,
            "unit": "iter/sec",
            "range": "stddev: 0.0007518753911503026",
            "extra": "mean: 3.021313324396261 msec\nrounds: 373"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 13660.42888606141,
            "unit": "iter/sec",
            "range": "stddev: 0.00001983424876165199",
            "extra": "mean: 73.20414376011009 usec\nrounds: 15978"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15166.07224089004,
            "unit": "iter/sec",
            "range": "stddev: 0.000018904732500204448",
            "extra": "mean: 65.9366501831534 usec\nrounds: 17472"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54074.63724214095,
            "unit": "iter/sec",
            "range": "stddev: 0.00001625648252750691",
            "extra": "mean: 18.49295808536075 usec\nrounds: 47215"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 246.0505672517128,
            "unit": "iter/sec",
            "range": "stddev: 0.00024657551287133044",
            "extra": "mean: 4.064205220778814 msec\nrounds: 231"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 179.58767114504516,
            "unit": "iter/sec",
            "range": "stddev: 0.0004876679867655623",
            "extra": "mean: 5.56831097382149 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 69.0299884735468,
            "unit": "iter/sec",
            "range": "stddev: 0.0013949749609220457",
            "extra": "mean: 14.486457583332976 msec\nrounds: 72"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23692.277904911458,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018104666262527736",
            "extra": "mean: 42.207845274037496 usec\nrounds: 23868"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2666.3361351463313,
            "unit": "iter/sec",
            "range": "stddev: 0.00004525693208873519",
            "extra": "mean: 375.0464867570491 usec\nrounds: 1699"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5001.578040264859,
            "unit": "iter/sec",
            "range": "stddev: 0.00002815723113253878",
            "extra": "mean: 199.9368983048088 usec\nrounds: 4779"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.592511889422575,
            "unit": "iter/sec",
            "range": "stddev: 0.0013573764277938426",
            "extra": "mean: 23.4783053555557 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1436.8165346830135,
            "unit": "iter/sec",
            "range": "stddev: 0.00023622903758773885",
            "extra": "mean: 695.9830819462397 usec\nrounds: 1562"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3979.9835821533384,
            "unit": "iter/sec",
            "range": "stddev: 0.000005968795263736079",
            "extra": "mean: 251.25731786535613 usec\nrounds: 4030"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18304.78035401233,
            "unit": "iter/sec",
            "range": "stddev: 0.000002742680951194935",
            "extra": "mean: 54.63053807039013 usec\nrounds: 17888"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3951.123597191833,
            "unit": "iter/sec",
            "range": "stddev: 0.00000809956798315767",
            "extra": "mean: 253.0925635205961 usec\nrounds: 3920"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1034.3740125405957,
            "unit": "iter/sec",
            "range": "stddev: 0.000015495018407928217",
            "extra": "mean: 966.7682945203087 usec\nrounds: 1022"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 535.4258235602928,
            "unit": "iter/sec",
            "range": "stddev: 0.007415097145962708",
            "extra": "mean: 1.867672338533356 msec\nrounds: 641"
          }
        ]
      }
    ]
  }
}