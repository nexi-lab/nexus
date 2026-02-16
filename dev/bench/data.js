window.BENCHMARK_DATA = {
  "lastUpdate": 1771232455744,
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
      }
    ]
  }
}