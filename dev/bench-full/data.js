window.BENCHMARK_DATA = {
  "lastUpdate": 1786530089655,
  "repoUrl": "https://github.com/nexi-lab/nexus",
  "entries": {
    "Benchmark": [
      {
        "commit": {
          "author": {
            "name": "elfenlieds7",
            "username": "elfenlieds7",
            "email": "songym@sudoprivacy.com"
          },
          "committer": {
            "name": "elfenlieds7",
            "username": "elfenlieds7",
            "email": "songym@sudoprivacy.com"
          },
          "id": "04c2ca75a4fc9f30971e35ad7ebc6f97e8e11b9e",
          "message": "fix(benchmarks): green the full suite (exposed by the nightly split #4531)\n\nSplitting Full-Suite benchmarks to a nightly workflow (#4531) made them\nactually run, surfacing 3 pre-existing failures that main-only gating had\nhidden:\n\n- test_search_benchmarks: the section-filter benchmark mocked the stale\n  `metastore_get_file_metadata`; `_filter_results_by_section` now reads\n  md_structure via the consolidated kernel `get_xattr` API (self._kernel IS\n  the metadata_store). Rewire the mock -> get_xattr; the benchmark + result\n  assertions (1000 results, lines 501-1500) are unchanged.\n- test_thread_pool_exhaustion (in-process + async): both open a raft metastore\n  via create_nexus_fs, which needs the nexus-cluster kernel binary -- absent in\n  the Python-only benchmark env (kernel=None -> NoneType.sys_setattr). Skip when\n  `shutil.which(\"nexus-cluster\") is None`, matching how the HTTP / pg-fts\n  benchmarks skip on a missing dependency.\n\nVerified locally: the search path returns 1000 filtered results calling\nget_xattr once; the guard's shutil.which is None in a kernel-less env so the\ntwo tests skip.",
          "timestamp": "2026-07-26T14:51:50Z",
          "url": "https://github.com/nexi-lab/nexus/commit/04c2ca75a4fc9f30971e35ad7ebc6f97e8e11b9e"
        },
        "date": 1785077715305,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 234.90403784431822,
            "unit": "iter/sec",
            "range": "stddev: 0.000689240252905158",
            "extra": "mean: 4.257057516664513 msec\nrounds: 120"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 421.3138699468976,
            "unit": "iter/sec",
            "range": "stddev: 0.0002793520193059281",
            "extra": "mean: 2.3735273660134664 msec\nrounds: 153"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1376.4764390454836,
            "unit": "iter/sec",
            "range": "stddev: 0.0000211631614221581",
            "extra": "mean: 726.4926384744 usec\nrounds: 603"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 452.1273101613537,
            "unit": "iter/sec",
            "range": "stddev: 0.00025773816276939473",
            "extra": "mean: 2.211766415178776 msec\nrounds: 224"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 177.70893619554118,
            "unit": "iter/sec",
            "range": "stddev: 0.0002995299139128216",
            "extra": "mean: 5.627179034483976 msec\nrounds: 87"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25751.612546629964,
            "unit": "iter/sec",
            "range": "stddev: 0.0000028458395440853375",
            "extra": "mean: 38.83251964082797 usec\nrounds: 10692"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1903.6545225143398,
            "unit": "iter/sec",
            "range": "stddev: 0.00005670479298035022",
            "extra": "mean: 525.3053997839921 usec\nrounds: 928"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31091.897252006474,
            "unit": "iter/sec",
            "range": "stddev: 0.000003335525986240354",
            "extra": "mean: 32.162720463623884 usec\nrounds: 8718"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 62221.897961482144,
            "unit": "iter/sec",
            "range": "stddev: 0.0000024616445943443024",
            "extra": "mean: 16.0715123254363 usec\nrounds: 9371"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 50943.228017855865,
            "unit": "iter/sec",
            "range": "stddev: 0.0000028183929574000335",
            "extra": "mean: 19.629694444362553 usec\nrounds: 8748"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 41582.8224799246,
            "unit": "iter/sec",
            "range": "stddev: 0.000003077942408863727",
            "extra": "mean: 24.048391628124357 usec\nrounds: 8863"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 141.2119357998103,
            "unit": "iter/sec",
            "range": "stddev: 0.008908601026105243",
            "extra": "mean: 7.081554362498466 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 16968.85486644859,
            "unit": "iter/sec",
            "range": "stddev: 0.000010767037802546992",
            "extra": "mean: 58.93149584166901 usec\nrounds: 25732"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7797.508571313084,
            "unit": "iter/sec",
            "range": "stddev: 0.00002304378277134633",
            "extra": "mean: 128.2460917938564 usec\nrounds: 11101"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1607.90293474152,
            "unit": "iter/sec",
            "range": "stddev: 0.00003968305336829511",
            "extra": "mean: 621.9280893101646 usec\nrounds: 2900"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7717.990014240992,
            "unit": "iter/sec",
            "range": "stddev: 0.000023429265782532473",
            "extra": "mean: 129.5674130382174 usec\nrounds: 12517"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 559.770141747843,
            "unit": "iter/sec",
            "range": "stddev: 0.0002484615522980609",
            "extra": "mean: 1.7864475530573498 msec\nrounds: 1112"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4291.0357787791545,
            "unit": "iter/sec",
            "range": "stddev: 0.0005734712368204788",
            "extra": "mean: 233.04396690080983 usec\nrounds: 7402"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 82992.43490362313,
            "unit": "iter/sec",
            "range": "stddev: 0.00000202943537547979",
            "extra": "mean: 12.049291012623899 usec\nrounds: 54276"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 17143.409035052875,
            "unit": "iter/sec",
            "range": "stddev: 0.000012148078172232782",
            "extra": "mean: 58.331455427290734 usec\nrounds: 28145"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5157333.114773698,
            "unit": "iter/sec",
            "range": "stddev: 1.4158843403309805e-8",
            "extra": "mean: 193.89866385310648 nsec\nrounds: 110175"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 3615662.8988032904,
            "unit": "iter/sec",
            "range": "stddev: 3.5823933626101654e-8",
            "extra": "mean: 276.5744561892037 nsec\nrounds: 386175"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2414374.2170184497,
            "unit": "iter/sec",
            "range": "stddev: 5.435961479539497e-7",
            "extra": "mean: 414.18600022780083 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 21368.10212752138,
            "unit": "iter/sec",
            "range": "stddev: 0.0003575074614789005",
            "extra": "mean: 46.79872803078914 usec\nrounds: 61518"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 38997.5317892936,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018067747402784113",
            "extra": "mean: 25.642648498963222 usec\nrounds: 71886"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3835.9907050969973,
            "unit": "iter/sec",
            "range": "stddev: 0.0000157647367713125",
            "extra": "mean: 260.6888485603653 usec\nrounds: 7640"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8175.485454728488,
            "unit": "iter/sec",
            "range": "stddev: 0.0000047674573964238575",
            "extra": "mean: 122.31689549660027 usec\nrounds: 14478"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1250.6062139929788,
            "unit": "iter/sec",
            "range": "stddev: 0.000011873022383620505",
            "extra": "mean: 799.6122111109341 usec\nrounds: 2520"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 414.80771078930917,
            "unit": "iter/sec",
            "range": "stddev: 0.00004723288844697826",
            "extra": "mean: 2.41075557177365 msec\nrounds: 829"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1819.093593843304,
            "unit": "iter/sec",
            "range": "stddev: 0.00001805109091328211",
            "extra": "mean: 549.7243261064112 usec\nrounds: 3367"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 3961.3366663610177,
            "unit": "iter/sec",
            "range": "stddev: 0.00000870708493709332",
            "extra": "mean: 252.44004340550654 usec\nrounds: 7787"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 379.5697017416934,
            "unit": "iter/sec",
            "range": "stddev: 0.00004588679796902971",
            "extra": "mean: 2.6345622303661234 msec\nrounds: 764"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 383.41576128991824,
            "unit": "iter/sec",
            "range": "stddev: 0.000027032881424506294",
            "extra": "mean: 2.608134826371559 msec\nrounds: 766"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 878.9072712321814,
            "unit": "iter/sec",
            "range": "stddev: 0.00001966782936254524",
            "extra": "mean: 1.1377764557551708 msec\nrounds: 1729"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 356.6486760886205,
            "unit": "iter/sec",
            "range": "stddev: 0.00004358749983497203",
            "extra": "mean: 2.8038797478993547 msec\nrounds: 714"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 346.70416333396247,
            "unit": "iter/sec",
            "range": "stddev: 0.000023494888255870708",
            "extra": "mean: 2.884303408369374 msec\nrounds: 693"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 319.2529009743026,
            "unit": "iter/sec",
            "range": "stddev: 0.00003611609981809996",
            "extra": "mean: 3.132312962382423 msec\nrounds: 638"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 320.4742740902422,
            "unit": "iter/sec",
            "range": "stddev: 0.00007463137435791436",
            "extra": "mean: 3.120375271427904 msec\nrounds: 630"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 167.95182767517545,
            "unit": "iter/sec",
            "range": "stddev: 0.0003219221989106242",
            "extra": "mean: 5.954088227810383 msec\nrounds: 338"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 787.0977813152481,
            "unit": "iter/sec",
            "range": "stddev: 0.000017861896314339617",
            "extra": "mean: 1.2704901776358588 msec\nrounds: 1565"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 937.3539947772233,
            "unit": "iter/sec",
            "range": "stddev: 0.000015119680405239948",
            "extra": "mean: 1.0668328140401913 msec\nrounds: 1866"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1611.577165821577,
            "unit": "iter/sec",
            "range": "stddev: 0.00001117082801985329",
            "extra": "mean: 620.510156887339 usec\nrounds: 3187"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 858.4750982166306,
            "unit": "iter/sec",
            "range": "stddev: 0.000043172802011029494",
            "extra": "mean: 1.1648561525865675 msec\nrounds: 1527"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 462.64821189325806,
            "unit": "iter/sec",
            "range": "stddev: 0.000038834395398018066",
            "extra": "mean: 2.161469501649602 msec\nrounds: 909"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1500.338765234757,
            "unit": "iter/sec",
            "range": "stddev: 0.0000401095043344932",
            "extra": "mean: 666.5161383359516 usec\nrounds: 2812"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 29263.706663392804,
            "unit": "iter/sec",
            "range": "stddev: 0.0004851057858236723",
            "extra": "mean: 34.17202104649791 usec\nrounds: 44283"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2137.511623261278,
            "unit": "iter/sec",
            "range": "stddev: 0.000018411895917644208",
            "extra": "mean: 467.83371333170305 usec\nrounds: 4193"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 43.50999381047637,
            "unit": "iter/sec",
            "range": "stddev: 0.0001395938323333583",
            "extra": "mean: 22.983225517242413 msec\nrounds: 87"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "elfenlieds7",
            "username": "elfenlieds7",
            "email": "elfenliedsp@gmail.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "2ad0020c3496741325cb0634dc8f1e21e4d3afeb",
          "message": "Merge pull request #4538 from nexi-lab/docs/welcome-zhuotao-liu\n\ndocs: welcome Zhuotao Liu; credit BlockA2A for signed authorship",
          "timestamp": "2026-07-28T06:46:17Z",
          "url": "https://github.com/nexi-lab/nexus/commit/2ad0020c3496741325cb0634dc8f1e21e4d3afeb"
        },
        "date": 1785238171609,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 242.3950345567949,
            "unit": "iter/sec",
            "range": "stddev: 0.0002642731342870287",
            "extra": "mean: 4.1254970500053405 msec\nrounds: 120"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 424.76260678928037,
            "unit": "iter/sec",
            "range": "stddev: 0.00032110954576107314",
            "extra": "mean: 2.354256198677319 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1390.4768669161033,
            "unit": "iter/sec",
            "range": "stddev: 0.00002101589419068858",
            "extra": "mean: 719.1777323256516 usec\nrounds: 594"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 455.1833710078365,
            "unit": "iter/sec",
            "range": "stddev: 0.00010745991355603598",
            "extra": "mean: 2.196916811319067 msec\nrounds: 212"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 179.0400069155937,
            "unit": "iter/sec",
            "range": "stddev: 0.0007281076598268663",
            "extra": "mean: 5.585343841454598 msec\nrounds: 82"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25553.482223063384,
            "unit": "iter/sec",
            "range": "stddev: 0.0000030408595452314213",
            "extra": "mean: 39.133609708090844 usec\nrounds: 7766"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1881.3134906054504,
            "unit": "iter/sec",
            "range": "stddev: 0.00001404792746467649",
            "extra": "mean: 531.5435226471357 usec\nrounds: 861"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31910.48460739142,
            "unit": "iter/sec",
            "range": "stddev: 0.00000414828288207163",
            "extra": "mean: 31.337662599093534 usec\nrounds: 8195"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 60671.31641743898,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025811300687224877",
            "extra": "mean: 16.48225321368775 usec\nrounds: 9723"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 52218.20296669253,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026805387758611337",
            "extra": "mean: 19.150410071328036 usec\nrounds: 14934"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 42039.00285001017,
            "unit": "iter/sec",
            "range": "stddev: 0.00000338314570011909",
            "extra": "mean: 23.787433863925678 usec\nrounds: 13253"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 137.89771268925765,
            "unit": "iter/sec",
            "range": "stddev: 0.010383279120869686",
            "extra": "mean: 7.2517519000001585 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 17037.962721906813,
            "unit": "iter/sec",
            "range": "stddev: 0.000010479141811578921",
            "extra": "mean: 58.692463196567225 usec\nrounds: 24957"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7742.8405940767325,
            "unit": "iter/sec",
            "range": "stddev: 0.000023554715617706388",
            "extra": "mean: 129.15156754809072 usec\nrounds: 12369"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1623.0958370799906,
            "unit": "iter/sec",
            "range": "stddev: 0.000040377085931180794",
            "extra": "mean: 616.1065644768315 usec\nrounds: 2939"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7802.648669764074,
            "unit": "iter/sec",
            "range": "stddev: 0.000023773220211257508",
            "extra": "mean: 128.1616079774404 usec\nrounds: 12961"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 563.1529398250384,
            "unit": "iter/sec",
            "range": "stddev: 0.0002542281728488457",
            "extra": "mean: 1.775716558117733 msec\nrounds: 1084"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4240.9342706950265,
            "unit": "iter/sec",
            "range": "stddev: 0.0004198277669906832",
            "extra": "mean: 235.79709945283227 usec\nrounds: 7873"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 81753.29823110785,
            "unit": "iter/sec",
            "range": "stddev: 0.0000019865795246177585",
            "extra": "mean: 12.231922401137954 usec\nrounds: 51663"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 17094.084421923573,
            "unit": "iter/sec",
            "range": "stddev: 0.000012581895402063836",
            "extra": "mean: 58.49976958797957 usec\nrounds: 28844"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5123320.866045358,
            "unit": "iter/sec",
            "range": "stddev: 1.5945704641218093e-8",
            "extra": "mean: 195.18590112664373 nsec\nrounds: 106073"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4418819.929696225,
            "unit": "iter/sec",
            "range": "stddev: 1.9032393133649988e-8",
            "extra": "mean: 226.3047636948505 nsec\nrounds: 100569"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2498475.9286755915,
            "unit": "iter/sec",
            "range": "stddev: 1.6893146275010999e-7",
            "extra": "mean: 400.24400016136497 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 21517.364155007603,
            "unit": "iter/sec",
            "range": "stddev: 0.00040129081202961705",
            "extra": "mean: 46.47409379681275 usec\nrounds: 58765"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 38596.61355335806,
            "unit": "iter/sec",
            "range": "stddev: 0.000001971538529131471",
            "extra": "mean: 25.90900879471059 usec\nrounds: 68109"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3809.9251477976895,
            "unit": "iter/sec",
            "range": "stddev: 0.000011798444521042723",
            "extra": "mean: 262.4723481977187 usec\nrounds: 7108"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7830.472859431788,
            "unit": "iter/sec",
            "range": "stddev: 0.000006640502635844243",
            "extra": "mean: 127.70620854595033 usec\nrounds: 15680"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1243.460478370651,
            "unit": "iter/sec",
            "range": "stddev: 0.000016104654591163087",
            "extra": "mean: 804.2073048516463 usec\nrounds: 2411"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 413.09409889254596,
            "unit": "iter/sec",
            "range": "stddev: 0.000033657426204938366",
            "extra": "mean: 2.4207559553159337 msec\nrounds: 828"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1707.984297580655,
            "unit": "iter/sec",
            "range": "stddev: 0.00004199587912531211",
            "extra": "mean: 585.4854763105792 usec\nrounds: 3208"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 3939.5501797172096,
            "unit": "iter/sec",
            "range": "stddev: 0.000013016599445223486",
            "extra": "mean: 253.83608645182494 usec\nrounds: 7750"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 380.3058609883835,
            "unit": "iter/sec",
            "range": "stddev: 0.00003297082663903545",
            "extra": "mean: 2.629462500002189 msec\nrounds: 752"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 382.9608924170119,
            "unit": "iter/sec",
            "range": "stddev: 0.00004728202577512753",
            "extra": "mean: 2.6112326866814506 msec\nrounds: 766"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 871.397961535741,
            "unit": "iter/sec",
            "range": "stddev: 0.000019105182540881046",
            "extra": "mean: 1.1475812936693268 msec\nrounds: 1706"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 343.21779060615063,
            "unit": "iter/sec",
            "range": "stddev: 0.000028510367316648827",
            "extra": "mean: 2.9136018801179224 msec\nrounds: 684"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 345.21934227671596,
            "unit": "iter/sec",
            "range": "stddev: 0.00005554267965010369",
            "extra": "mean: 2.896709070253759 msec\nrounds: 669"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 318.1173888201696,
            "unit": "iter/sec",
            "range": "stddev: 0.0000338273202340988",
            "extra": "mean: 3.1434936760570977 msec\nrounds: 639"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 319.42256200114855,
            "unit": "iter/sec",
            "range": "stddev: 0.00003870541577604691",
            "extra": "mean: 3.1306492369703185 msec\nrounds: 633"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 164.111501692464,
            "unit": "iter/sec",
            "range": "stddev: 0.00004807737204305372",
            "extra": "mean: 6.093418131496629 msec\nrounds: 327"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 776.3631250371992,
            "unit": "iter/sec",
            "range": "stddev: 0.000020181958264685978",
            "extra": "mean: 1.2880570544255117 msec\nrounds: 1525"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 890.7907185633663,
            "unit": "iter/sec",
            "range": "stddev: 0.000033182726562350806",
            "extra": "mean: 1.1225981357470387 msec\nrounds: 1768"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1540.3013442534984,
            "unit": "iter/sec",
            "range": "stddev: 0.000013341397038235258",
            "extra": "mean: 649.2236105167111 usec\nrounds: 3081"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 810.3089251039713,
            "unit": "iter/sec",
            "range": "stddev: 0.00003615533050439551",
            "extra": "mean: 1.2340972301047892 msec\nrounds: 1621"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 489.8685426126008,
            "unit": "iter/sec",
            "range": "stddev: 0.000025151285505985475",
            "extra": "mean: 2.0413639844410727 msec\nrounds: 964"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1514.6616344895763,
            "unit": "iter/sec",
            "range": "stddev: 0.000018035573571686717",
            "extra": "mean: 660.2134610328258 usec\nrounds: 3041"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28311.44190137685,
            "unit": "iter/sec",
            "range": "stddev: 0.0006063113366201428",
            "extra": "mean: 35.32140833672508 usec\nrounds: 36656"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2123.943511309837,
            "unit": "iter/sec",
            "range": "stddev: 0.000025702450297266108",
            "extra": "mean: 470.82231456490075 usec\nrounds: 4209"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 43.02304167684672,
            "unit": "iter/sec",
            "range": "stddev: 0.00011930008339710933",
            "extra": "mean: 23.24335893103904 msec\nrounds: 87"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "elfenlieds7",
            "username": "elfenlieds7",
            "email": "elfenliedsp@gmail.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "bd52314f7215d96ac678c0399ea2691649a94ba5",
          "message": "Merge pull request #4540 from nexi-lab/chore/bump-nexus-vfs-195\n\nchore(pins): bump nexus-vfs b7e6a8450 -> 27f485ace (#195 agent pid = OS host_pid)",
          "timestamp": "2026-07-28T15:24:41Z",
          "url": "https://github.com/nexi-lab/nexus/commit/bd52314f7215d96ac678c0399ea2691649a94ba5"
        },
        "date": 1785324946758,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 259.7700412112763,
            "unit": "iter/sec",
            "range": "stddev: 0.00033408247395122633",
            "extra": "mean: 3.8495586147544993 msec\nrounds: 122"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 441.89549241166856,
            "unit": "iter/sec",
            "range": "stddev: 0.00033190631033959086",
            "extra": "mean: 2.2629785032258325 msec\nrounds: 155"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1674.2978519128108,
            "unit": "iter/sec",
            "range": "stddev: 0.000028891522922728836",
            "extra": "mean: 597.2652947368621 usec\nrounds: 665"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 494.74018981979253,
            "unit": "iter/sec",
            "range": "stddev: 0.00007197226905434999",
            "extra": "mean: 2.021262918551749 msec\nrounds: 221"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 198.3832914062239,
            "unit": "iter/sec",
            "range": "stddev: 0.00019462066574019185",
            "extra": "mean: 5.040747095743703 msec\nrounds: 94"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25788.153363925085,
            "unit": "iter/sec",
            "range": "stddev: 0.000002792997375975925",
            "extra": "mean: 38.777495460333924 usec\nrounds: 11014"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 2000.072182430046,
            "unit": "iter/sec",
            "range": "stddev: 0.00001304590587299066",
            "extra": "mean: 499.98195504375286 usec\nrounds: 912"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 35041.519011037155,
            "unit": "iter/sec",
            "range": "stddev: 0.0000021251851600030793",
            "extra": "mean: 28.53757565946346 usec\nrounds: 8948"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 68733.74686306402,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014376796980498772",
            "extra": "mean: 14.548894038793886 usec\nrounds: 7012"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 54131.30020463839,
            "unit": "iter/sec",
            "range": "stddev: 0.000001827876614688054",
            "extra": "mean: 18.473600231651414 usec\nrounds: 16407"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 45883.207572535845,
            "unit": "iter/sec",
            "range": "stddev: 0.000001956057324029657",
            "extra": "mean: 21.794465838490478 usec\nrounds: 15134"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 137.8707200837453,
            "unit": "iter/sec",
            "range": "stddev: 0.011649606978658772",
            "extra": "mean: 7.25317166250079 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 30096.888665770275,
            "unit": "iter/sec",
            "range": "stddev: 0.000007333831080942774",
            "extra": "mean: 33.22602582297212 usec\nrounds: 31871"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 12766.507106287665,
            "unit": "iter/sec",
            "range": "stddev: 0.000014694545695187053",
            "extra": "mean: 78.32996070691 usec\nrounds: 17484"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 2406.159308888748,
            "unit": "iter/sec",
            "range": "stddev: 0.000027295835443291257",
            "extra": "mean: 415.6000794734728 usec\nrounds: 4253"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 12824.410534921262,
            "unit": "iter/sec",
            "range": "stddev: 0.000017599322068180334",
            "extra": "mean: 77.97629351282613 usec\nrounds: 16047"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 817.9300192140462,
            "unit": "iter/sec",
            "range": "stddev: 0.00015863447267235167",
            "extra": "mean: 1.2225984821548743 msec\nrounds: 1541"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4405.796499214356,
            "unit": "iter/sec",
            "range": "stddev: 0.0005090988298313489",
            "extra": "mean: 226.973715235899 usec\nrounds: 7817"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 92919.41749866787,
            "unit": "iter/sec",
            "range": "stddev: 0.0000011901109506926468",
            "extra": "mean: 10.76201322521567 usec\nrounds: 49224"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 30082.498931040147,
            "unit": "iter/sec",
            "range": "stddev: 0.00000958151976097858",
            "extra": "mean: 33.24191923989952 usec\nrounds: 38732"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5545682.198124985,
            "unit": "iter/sec",
            "range": "stddev: 1.4344572993733387e-8",
            "extra": "mean: 180.3204663148753 nsec\nrounds: 119147"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4378310.3769836845,
            "unit": "iter/sec",
            "range": "stddev: 1.6957162126011318e-8",
            "extra": "mean: 228.39860902892914 nsec\nrounds: 104445"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2516191.6936876443,
            "unit": "iter/sec",
            "range": "stddev: 4.254051128343925e-7",
            "extra": "mean: 397.4259999779406 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 30097.371676256473,
            "unit": "iter/sec",
            "range": "stddev: 0.0005828505624789532",
            "extra": "mean: 33.22549260302654 usec\nrounds: 60700"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 36046.798686736445,
            "unit": "iter/sec",
            "range": "stddev: 0.000002290671595243972",
            "extra": "mean: 27.741714560853744 usec\nrounds: 40499"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3604.580080264443,
            "unit": "iter/sec",
            "range": "stddev: 0.00000818032720650889",
            "extra": "mean: 277.42482556432395 usec\nrounds: 5492"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7942.126416631565,
            "unit": "iter/sec",
            "range": "stddev: 0.0000058427924623597825",
            "extra": "mean: 125.91086411139281 usec\nrounds: 15601"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1333.8966749160838,
            "unit": "iter/sec",
            "range": "stddev: 0.000025822774062819472",
            "extra": "mean: 749.6832541867686 usec\nrounds: 2687"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 420.47502598420675,
            "unit": "iter/sec",
            "range": "stddev: 0.00003423563151792638",
            "extra": "mean: 2.378262532142778 msec\nrounds: 840"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1899.8229156330606,
            "unit": "iter/sec",
            "range": "stddev: 0.000053964104612664025",
            "extra": "mean: 526.3648478872985 usec\nrounds: 3550"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4319.100081291716,
            "unit": "iter/sec",
            "range": "stddev: 0.000007724807619200397",
            "extra": "mean: 231.52971248143186 usec\nrounds: 8076"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 411.87488630824106,
            "unit": "iter/sec",
            "range": "stddev: 0.000040012969974896426",
            "extra": "mean: 2.4279217627549516 msec\nrounds: 784"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 419.8782755962419,
            "unit": "iter/sec",
            "range": "stddev: 0.0000515916524610416",
            "extra": "mean: 2.381642628640324 msec\nrounds: 824"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 984.9282495434444,
            "unit": "iter/sec",
            "range": "stddev: 0.000021822367756781573",
            "extra": "mean: 1.015302384172189 msec\nrounds: 1908"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 336.0180300285146,
            "unit": "iter/sec",
            "range": "stddev: 0.000040355187177631294",
            "extra": "mean: 2.9760307800005243 msec\nrounds: 650"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 388.1092632309383,
            "unit": "iter/sec",
            "range": "stddev: 0.00004766511974809566",
            "extra": "mean: 2.576594002614583 msec\nrounds: 765"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 355.6146712860765,
            "unit": "iter/sec",
            "range": "stddev: 0.000043181892915309744",
            "extra": "mean: 2.812032463068835 msec\nrounds: 704"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 354.36113646856995,
            "unit": "iter/sec",
            "range": "stddev: 0.000045259207868630926",
            "extra": "mean: 2.8219798874267776 msec\nrounds: 684"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 159.95371615373708,
            "unit": "iter/sec",
            "range": "stddev: 0.00008385607618970208",
            "extra": "mean: 6.251808485892664 msec\nrounds: 319"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 871.1320657711929,
            "unit": "iter/sec",
            "range": "stddev: 0.00002530095037230103",
            "extra": "mean: 1.1479315700710928 msec\nrounds: 1684"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 966.327764110266,
            "unit": "iter/sec",
            "range": "stddev: 0.000018737602520274975",
            "extra": "mean: 1.034845563938378 msec\nrounds: 1869"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1679.3811763081635,
            "unit": "iter/sec",
            "range": "stddev: 0.00001483408205560096",
            "extra": "mean: 595.4574304556227 usec\nrounds: 3336"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 897.8959504700382,
            "unit": "iter/sec",
            "range": "stddev: 0.000026421705287113285",
            "extra": "mean: 1.113714790089555 msec\nrounds: 1796"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 544.1286737902462,
            "unit": "iter/sec",
            "range": "stddev: 0.00003489804803016374",
            "extra": "mean: 1.837800594176159 msec\nrounds: 1099"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1630.9175088623113,
            "unit": "iter/sec",
            "range": "stddev: 0.000016846132506470542",
            "extra": "mean: 613.1517961920563 usec\nrounds: 3204"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28657.990658649916,
            "unit": "iter/sec",
            "range": "stddev: 0.0007635985437003795",
            "extra": "mean: 34.89428173493271 usec\nrounds: 44407"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2121.372691118908,
            "unit": "iter/sec",
            "range": "stddev: 0.000022635036080454074",
            "extra": "mean: 471.3928882871377 usec\nrounds: 4064"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 42.36636532024716,
            "unit": "iter/sec",
            "range": "stddev: 0.00023410657703537975",
            "extra": "mean: 23.603629729409278 msec\nrounds: 85"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "elfenlieds7",
            "username": "elfenlieds7",
            "email": "elfenliedsp@gmail.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "bd52314f7215d96ac678c0399ea2691649a94ba5",
          "message": "Merge pull request #4540 from nexi-lab/chore/bump-nexus-vfs-195\n\nchore(pins): bump nexus-vfs b7e6a8450 -> 27f485ace (#195 agent pid = OS host_pid)",
          "timestamp": "2026-07-28T15:24:41Z",
          "url": "https://github.com/nexi-lab/nexus/commit/bd52314f7215d96ac678c0399ea2691649a94ba5"
        },
        "date": 1785410572460,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 221.7006772495136,
            "unit": "iter/sec",
            "range": "stddev: 0.0006276021951565326",
            "extra": "mean: 4.510586130842295 msec\nrounds: 107"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 415.18778390893567,
            "unit": "iter/sec",
            "range": "stddev: 0.0003617940793323758",
            "extra": "mean: 2.4085487067686286 msec\nrounds: 133"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1336.3876999832437,
            "unit": "iter/sec",
            "range": "stddev: 0.00004891039844799238",
            "extra": "mean: 748.2858455016749 usec\nrounds: 589"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 419.54739227249155,
            "unit": "iter/sec",
            "range": "stddev: 0.0005076704106926605",
            "extra": "mean: 2.3835209523850662 msec\nrounds: 189"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 166.88912244955918,
            "unit": "iter/sec",
            "range": "stddev: 0.0006734257182474638",
            "extra": "mean: 5.992002266668048 msec\nrounds: 75"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 24764.04989388686,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027213718883107006",
            "extra": "mean: 40.381117155108605 usec\nrounds: 10866"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1828.7587364958872,
            "unit": "iter/sec",
            "range": "stddev: 0.000020048872733458498",
            "extra": "mean: 546.8189871322859 usec\nrounds: 855"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 30368.198761433825,
            "unit": "iter/sec",
            "range": "stddev: 0.0000037004919201380108",
            "extra": "mean: 32.929183843131085 usec\nrounds: 8529"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 57556.14429126489,
            "unit": "iter/sec",
            "range": "stddev: 0.000002850942862789138",
            "extra": "mean: 17.37433965241773 usec\nrounds: 8862"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 49073.01166016152,
            "unit": "iter/sec",
            "range": "stddev: 0.0000028846666584068368",
            "extra": "mean: 20.377799653405432 usec\nrounds: 14425"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 39581.95423977977,
            "unit": "iter/sec",
            "range": "stddev: 0.0000032783907135346806",
            "extra": "mean: 25.264038100347314 usec\nrounds: 12966"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 119.7411549087789,
            "unit": "iter/sec",
            "range": "stddev: 0.01433556889931141",
            "extra": "mean: 8.351347544307712 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 15973.00346301972,
            "unit": "iter/sec",
            "range": "stddev: 0.000012251253868187713",
            "extra": "mean: 62.605633456173344 usec\nrounds: 25143"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7456.262267630621,
            "unit": "iter/sec",
            "range": "stddev: 0.000023396835620773894",
            "extra": "mean: 134.11545411180532 usec\nrounds: 12574"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1587.0190572033216,
            "unit": "iter/sec",
            "range": "stddev: 0.00004698606657130228",
            "extra": "mean: 630.1121561591208 usec\nrounds: 3125"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7526.208324323763,
            "unit": "iter/sec",
            "range": "stddev: 0.000024992600884708084",
            "extra": "mean: 132.8690300490521 usec\nrounds: 12047"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 552.6905200071941,
            "unit": "iter/sec",
            "range": "stddev: 0.0002972997189368524",
            "extra": "mean: 1.8093308348892676 msec\nrounds: 1072"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4081.7052309376973,
            "unit": "iter/sec",
            "range": "stddev: 0.0005050590927998316",
            "extra": "mean: 244.99564359042859 usec\nrounds: 7107"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 80222.67165392598,
            "unit": "iter/sec",
            "range": "stddev: 0.0000022341382698999443",
            "extra": "mean: 12.4653041264185 usec\nrounds: 46040"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 16300.916412380748,
            "unit": "iter/sec",
            "range": "stddev: 0.00001333174384311985",
            "extra": "mean: 61.3462442663952 usec\nrounds: 27773"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5066084.346347451,
            "unit": "iter/sec",
            "range": "stddev: 1.59497362557248e-8",
            "extra": "mean: 197.39110753672722 nsec\nrounds: 106696"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4309668.094533554,
            "unit": "iter/sec",
            "range": "stddev: 1.9044275835608762e-8",
            "extra": "mean: 232.03643019944263 nsec\nrounds: 96025"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2403020.0983112496,
            "unit": "iter/sec",
            "range": "stddev: 1.4717849137793822e-7",
            "extra": "mean: 416.1430030080737 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 21988.546720682956,
            "unit": "iter/sec",
            "range": "stddev: 0.0006234348664344813",
            "extra": "mean: 45.4782215806639 usec\nrounds: 33613"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 38462.00735748992,
            "unit": "iter/sec",
            "range": "stddev: 0.000003102094067861966",
            "extra": "mean: 25.999683030201087 usec\nrounds: 64167"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3852.51895036214,
            "unit": "iter/sec",
            "range": "stddev: 0.000011009918247641446",
            "extra": "mean: 259.57042986277827 usec\nrounds: 7521"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7757.000760951767,
            "unit": "iter/sec",
            "range": "stddev: 0.000015900545545196227",
            "extra": "mean: 128.9158053243896 usec\nrounds: 13936"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1255.9568065271292,
            "unit": "iter/sec",
            "range": "stddev: 0.000016831822849035815",
            "extra": "mean: 796.2057252312043 usec\nrounds: 2493"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 412.8573064535287,
            "unit": "iter/sec",
            "range": "stddev: 0.00003164804424710047",
            "extra": "mean: 2.4221443689347915 msec\nrounds: 824"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1722.433513055703,
            "unit": "iter/sec",
            "range": "stddev: 0.00006556755178816693",
            "extra": "mean: 580.573933577232 usec\nrounds: 3026"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 3891.960638626559,
            "unit": "iter/sec",
            "range": "stddev: 0.000010287874005959692",
            "extra": "mean: 256.9399058344258 usec\nrounds: 7508"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 377.4003049853632,
            "unit": "iter/sec",
            "range": "stddev: 0.00005876932335214212",
            "extra": "mean: 2.6497063907745995 msec\nrounds: 737"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 375.594857933845,
            "unit": "iter/sec",
            "range": "stddev: 0.00020116745697180616",
            "extra": "mean: 2.662443265333877 msec\nrounds: 750"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 869.3706408189763,
            "unit": "iter/sec",
            "range": "stddev: 0.000017453959243170247",
            "extra": "mean: 1.1502573851101832 msec\nrounds: 1706"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 341.3045529884307,
            "unit": "iter/sec",
            "range": "stddev: 0.00008083918701416634",
            "extra": "mean: 2.9299345445119136 msec\nrounds: 674"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 331.6512058567107,
            "unit": "iter/sec",
            "range": "stddev: 0.00003940512555353339",
            "extra": "mean: 3.0152159327050607 msec\nrounds: 639"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 312.98002337890404,
            "unit": "iter/sec",
            "range": "stddev: 0.00004731711909911027",
            "extra": "mean: 3.195092099502359 msec\nrounds: 603"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 310.090315032878,
            "unit": "iter/sec",
            "range": "stddev: 0.00006122769267472742",
            "extra": "mean: 3.2248669227027382 msec\nrounds: 621"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 157.80969532559385,
            "unit": "iter/sec",
            "range": "stddev: 0.00013095682390398507",
            "extra": "mean: 6.3367462812522035 msec\nrounds: 320"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 767.4113838516466,
            "unit": "iter/sec",
            "range": "stddev: 0.000057303698077873376",
            "extra": "mean: 1.3030820509607095 msec\nrounds: 1511"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 858.0768656863357,
            "unit": "iter/sec",
            "range": "stddev: 0.000022630889372439645",
            "extra": "mean: 1.1653967610467468 msec\nrounds: 1720"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1453.6791865944479,
            "unit": "iter/sec",
            "range": "stddev: 0.000011622515000965081",
            "extra": "mean: 687.909690956443 usec\nrounds: 3074"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 816.8566493687647,
            "unit": "iter/sec",
            "range": "stddev: 0.000018076333531113936",
            "extra": "mean: 1.2242050068059818 msec\nrounds: 1616"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 485.4729535667507,
            "unit": "iter/sec",
            "range": "stddev: 0.000019136749896963093",
            "extra": "mean: 2.0598469856107107 msec\nrounds: 973"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1491.5457499965296,
            "unit": "iter/sec",
            "range": "stddev: 0.00001451089039286484",
            "extra": "mean: 670.445408732737 usec\nrounds: 2931"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 26378.34937359434,
            "unit": "iter/sec",
            "range": "stddev: 0.0007492306639488575",
            "extra": "mean: 37.90987775000946 usec\nrounds: 35591"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2038.428106647137,
            "unit": "iter/sec",
            "range": "stddev: 0.000026494240890197025",
            "extra": "mean: 490.57408340234656 usec\nrounds: 3621"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 40.19267020385274,
            "unit": "iter/sec",
            "range": "stddev: 0.00024325775695827927",
            "extra": "mean: 24.8801583703723 msec\nrounds: 81"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "oliverfeng",
            "username": "windoliver",
            "email": "oliverfengpet@gmail.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "e13d7fea3db77dacfdf7b175af51432090a9b066",
          "message": "Merge pull request #4551 from nexi-lab/fix/4541-search-fusion-params\n\nfix(search): honor alpha/fusion/rrf_k on the backend hybrid path",
          "timestamp": "2026-07-31T06:01:24Z",
          "url": "https://github.com/nexi-lab/nexus/commit/e13d7fea3db77dacfdf7b175af51432090a9b066"
        },
        "date": 1785497853257,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 240.0259907927637,
            "unit": "iter/sec",
            "range": "stddev: 0.0006089378031603214",
            "extra": "mean: 4.166215486486174 msec\nrounds: 74"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 657.1994455635609,
            "unit": "iter/sec",
            "range": "stddev: 0.00022291583957406247",
            "extra": "mean: 1.521608100479271 msec\nrounds: 209"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 2628.653186820391,
            "unit": "iter/sec",
            "range": "stddev: 0.00001418584101527892",
            "extra": "mean: 380.422950054357 usec\nrounds: 921"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 494.1360361859889,
            "unit": "iter/sec",
            "range": "stddev: 0.0004224806854426316",
            "extra": "mean: 2.0237342083336904 msec\nrounds: 240"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 191.8434225665569,
            "unit": "iter/sec",
            "range": "stddev: 0.0009276955615168285",
            "extra": "mean: 5.212584234693095 msec\nrounds: 98"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 44750.13753333655,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014835543619191606",
            "extra": "mean: 22.34630003662115 usec\nrounds: 19111"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 3189.848727210186,
            "unit": "iter/sec",
            "range": "stddev: 0.000006670495165104669",
            "extra": "mean: 313.4944900269899 usec\nrounds: 1504"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 56835.488928805906,
            "unit": "iter/sec",
            "range": "stddev: 0.00000448820003874952",
            "extra": "mean: 17.594640581919418 usec\nrounds: 14504"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 121776.07024680135,
            "unit": "iter/sec",
            "range": "stddev: 0.0000010432809009366158",
            "extra": "mean: 8.211793975395317 usec\nrounds: 16333"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 98062.60707261445,
            "unit": "iter/sec",
            "range": "stddev: 0.0000011534323184046337",
            "extra": "mean: 10.197566940674026 usec\nrounds: 26038"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 81357.0061374225,
            "unit": "iter/sec",
            "range": "stddev: 0.000001348560284686435",
            "extra": "mean: 12.291504413410577 usec\nrounds: 24131"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 229.35177048620136,
            "unit": "iter/sec",
            "range": "stddev: 0.006472560242959679",
            "extra": "mean: 4.360114586776925 msec\nrounds: 121"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 41832.303488982136,
            "unit": "iter/sec",
            "range": "stddev: 0.0000021485239471342746",
            "extra": "mean: 23.904970957752344 usec\nrounds: 35741"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 17767.65480610816,
            "unit": "iter/sec",
            "range": "stddev: 0.0000077516680783184",
            "extra": "mean: 56.28204796370876 usec\nrounds: 29147"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 3623.9199663700747,
            "unit": "iter/sec",
            "range": "stddev: 0.000013347903938866135",
            "extra": "mean: 275.9442838914727 usec\nrounds: 6897"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 17919.389635429725,
            "unit": "iter/sec",
            "range": "stddev: 0.000008482267707225875",
            "extra": "mean: 55.80547219213468 usec\nrounds: 23986"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 1263.1190314534,
            "unit": "iter/sec",
            "range": "stddev: 0.00007966254539895046",
            "extra": "mean: 791.691024439206 usec\nrounds: 2496"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 7691.8919363573705,
            "unit": "iter/sec",
            "range": "stddev: 0.0001639111632254325",
            "extra": "mean: 130.00702665533902 usec\nrounds: 13018"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 153812.33294254966,
            "unit": "iter/sec",
            "range": "stddev: 8.443910994435334e-7",
            "extra": "mean: 6.501429247377122 usec\nrounds: 87283"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 41537.31182028508,
            "unit": "iter/sec",
            "range": "stddev: 0.000004348034908461945",
            "extra": "mean: 24.074740424382544 usec\nrounds: 64617"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 10534082.920845758,
            "unit": "iter/sec",
            "range": "stddev: 9.014118013341523e-9",
            "extra": "mean: 94.92995332523093 nsec\nrounds: 218747"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 8863993.498788046,
            "unit": "iter/sec",
            "range": "stddev: 8.478499645828033e-9",
            "extra": "mean: 112.81596722027469 nsec\nrounds: 205677"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 4825532.859691518,
            "unit": "iter/sec",
            "range": "stddev: 1.5340126364590824e-7",
            "extra": "mean: 207.23099999031547 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 42431.58251327072,
            "unit": "iter/sec",
            "range": "stddev: 0.00025109048480832037",
            "extra": "mean: 23.56735103356667 usec\nrounds: 95498"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 52305.03489040775,
            "unit": "iter/sec",
            "range": "stddev: 0.0000010137645071881176",
            "extra": "mean: 19.118618352807765 usec\nrounds: 100061"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 5296.3588251256,
            "unit": "iter/sec",
            "range": "stddev: 0.0000030051195394170884",
            "extra": "mean: 188.8089597056872 usec\nrounds: 10597"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 11292.87407091794,
            "unit": "iter/sec",
            "range": "stddev: 0.000002491119999579879",
            "extra": "mean: 88.5514169130122 usec\nrounds: 22338"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 2005.242157513997,
            "unit": "iter/sec",
            "range": "stddev: 0.000007420400035625558",
            "extra": "mean: 498.692886668487 usec\nrounds: 3953"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 629.239021151372,
            "unit": "iter/sec",
            "range": "stddev: 0.000011894316392699387",
            "extra": "mean: 1.5892212122671845 msec\nrounds: 1239"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 3335.9130260373995,
            "unit": "iter/sec",
            "range": "stddev: 0.000016947324257268367",
            "extra": "mean: 299.76800719767596 usec\nrounds: 4307"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 6129.996542307227,
            "unit": "iter/sec",
            "range": "stddev: 0.000003672316359742877",
            "extra": "mean: 163.13222904749256 usec\nrounds: 11884"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 580.8857830509761,
            "unit": "iter/sec",
            "range": "stddev: 0.00005399998069852918",
            "extra": "mean: 1.7215088218336445 msec\nrounds: 1145"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 597.4775521123389,
            "unit": "iter/sec",
            "range": "stddev: 0.000030693930289496816",
            "extra": "mean: 1.6737030478627555 msec\nrounds: 1170"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1333.654864367324,
            "unit": "iter/sec",
            "range": "stddev: 0.00006421298934133383",
            "extra": "mean: 749.8191823972332 usec\nrounds: 2511"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 477.05829529590744,
            "unit": "iter/sec",
            "range": "stddev: 0.0001372885548222788",
            "extra": "mean: 2.0961798796093145 msec\nrounds: 922"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 606.3344498564677,
            "unit": "iter/sec",
            "range": "stddev: 0.000014391742359239785",
            "extra": "mean: 1.6492547969799858 msec\nrounds: 1192"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 551.9072754736486,
            "unit": "iter/sec",
            "range": "stddev: 0.0000179703684672558",
            "extra": "mean: 1.8118985641959453 msec\nrounds: 1106"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 556.4398349160022,
            "unit": "iter/sec",
            "range": "stddev: 0.000018348241570824292",
            "extra": "mean: 1.7971394879573204 msec\nrounds: 1121"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 266.8736570856226,
            "unit": "iter/sec",
            "range": "stddev: 0.000033657773690434554",
            "extra": "mean: 3.7470914548870753 msec\nrounds: 532"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1467.6316482321417,
            "unit": "iter/sec",
            "range": "stddev: 0.000016489441634379916",
            "extra": "mean: 681.369879972652 usec\nrounds: 2916"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 1534.556220338146,
            "unit": "iter/sec",
            "range": "stddev: 0.0000189448846461177",
            "extra": "mean: 651.6541960122163 usec\nrounds: 2959"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 2680.7483486666856,
            "unit": "iter/sec",
            "range": "stddev: 0.000009862985249392084",
            "extra": "mean: 373.03016543770946 usec\nrounds: 5289"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 1457.9136269220162,
            "unit": "iter/sec",
            "range": "stddev: 0.00001281425394836043",
            "extra": "mean: 685.9116901947237 usec\nrounds: 2876"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 869.2695956523913,
            "unit": "iter/sec",
            "range": "stddev: 0.00003481969969689405",
            "extra": "mean: 1.1503910927075447 msec\nrounds: 1769"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 2680.883587099738,
            "unit": "iter/sec",
            "range": "stddev: 0.000007685087666057963",
            "extra": "mean: 373.0113477556221 usec\nrounds: 5547"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 50910.609222697785,
            "unit": "iter/sec",
            "range": "stddev: 0.00034002282695754914",
            "extra": "mean: 19.64227133141758 usec\nrounds: 78089"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 3510.072330596794,
            "unit": "iter/sec",
            "range": "stddev: 0.0015599730958293938",
            "extra": "mean: 284.8944140789192 usec\nrounds: 7245"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 73.95758972276703,
            "unit": "iter/sec",
            "range": "stddev: 0.0005496203082160034",
            "extra": "mean: 13.52126270946011 msec\nrounds: 148"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "oliverfeng",
            "username": "windoliver",
            "email": "oliverfengpet@gmail.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "4159cdf8e8e7052eec4dd0bb913ceeacc2d30a09",
          "message": "Merge pull request #4558 from nexi-lab/fix/4542-pooling-hardening\n\nfix(search): harden #4542 final-list pooling — 10-round adversarial review follow-up",
          "timestamp": "2026-08-01T03:37:04Z",
          "url": "https://github.com/nexi-lab/nexus/commit/4159cdf8e8e7052eec4dd0bb913ceeacc2d30a09"
        },
        "date": 1785581400614,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 235.94568408544993,
            "unit": "iter/sec",
            "range": "stddev: 0.0007126973997659223",
            "extra": "mean: 4.238263581197106 msec\nrounds: 117"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 416.013971763692,
            "unit": "iter/sec",
            "range": "stddev: 0.0003189915691754881",
            "extra": "mean: 2.4037654210518418 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1358.548079114902,
            "unit": "iter/sec",
            "range": "stddev: 0.00003372534423642938",
            "extra": "mean: 736.0799484192734 usec\nrounds: 601"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 446.7677711209231,
            "unit": "iter/sec",
            "range": "stddev: 0.0002814493921260057",
            "extra": "mean: 2.238299323809859 msec\nrounds: 210"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 164.1615569870022,
            "unit": "iter/sec",
            "range": "stddev: 0.0006876875103570968",
            "extra": "mean: 6.091560157894804 msec\nrounds: 76"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25209.512706831865,
            "unit": "iter/sec",
            "range": "stddev: 0.000003130004240222216",
            "extra": "mean: 39.66756563799016 usec\nrounds: 10337"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1850.9888786032661,
            "unit": "iter/sec",
            "range": "stddev: 0.000013283858861052027",
            "extra": "mean: 540.2517603209956 usec\nrounds: 872"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 30210.602419981165,
            "unit": "iter/sec",
            "range": "stddev: 0.000004255069819528146",
            "extra": "mean: 33.100961910597455 usec\nrounds: 8375"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 61476.25202444337,
            "unit": "iter/sec",
            "range": "stddev: 0.0000028528140447176196",
            "extra": "mean: 16.266443822931713 usec\nrounds: 10547"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 49330.423388851756,
            "unit": "iter/sec",
            "range": "stddev: 0.0000032538883103605023",
            "extra": "mean: 20.271465990011983 usec\nrounds: 14187"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 40631.81173115825,
            "unit": "iter/sec",
            "range": "stddev: 0.000004705922107614446",
            "extra": "mean: 24.611257962517982 usec\nrounds: 12967"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 128.27508190831347,
            "unit": "iter/sec",
            "range": "stddev: 0.01302753428749577",
            "extra": "mean: 7.795746337661784 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 16135.170253452952,
            "unit": "iter/sec",
            "range": "stddev: 0.000011242233330985088",
            "extra": "mean: 61.976414521315526 usec\nrounds: 21830"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7361.162028788569,
            "unit": "iter/sec",
            "range": "stddev: 0.00002451341033581998",
            "extra": "mean: 135.84811692625797 usec\nrounds: 10528"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1567.3238861528364,
            "unit": "iter/sec",
            "range": "stddev: 0.000052385417874289344",
            "extra": "mean: 638.0302175159256 usec\nrounds: 2786"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7363.484579633554,
            "unit": "iter/sec",
            "range": "stddev: 0.000025146641307100705",
            "extra": "mean: 135.80526844123102 usec\nrounds: 11496"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 541.6282998685039,
            "unit": "iter/sec",
            "range": "stddev: 0.00024794659774396465",
            "extra": "mean: 1.8462846203619332 msec\nrounds: 1051"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4197.547315508398,
            "unit": "iter/sec",
            "range": "stddev: 0.0004375830002492059",
            "extra": "mean: 238.23436040979612 usec\nrounds: 6931"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 82558.34714265272,
            "unit": "iter/sec",
            "range": "stddev: 0.000002092402817979915",
            "extra": "mean: 12.11264559684193 usec\nrounds: 49486"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 16042.265683779327,
            "unit": "iter/sec",
            "range": "stddev: 0.00001344794629135253",
            "extra": "mean: 62.335334653578336 usec\nrounds: 26508"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5171082.64283397,
            "unit": "iter/sec",
            "range": "stddev: 1.5631947822216444e-8",
            "extra": "mean: 193.38310158043774 nsec\nrounds: 110779"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4327618.304566558,
            "unit": "iter/sec",
            "range": "stddev: 3.7707954619551715e-8",
            "extra": "mean: 231.0739833373907 nsec\nrounds: 99866"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2293609.5477017555,
            "unit": "iter/sec",
            "range": "stddev: 2.451358918242184e-7",
            "extra": "mean: 435.9939995026707 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 20732.956539034036,
            "unit": "iter/sec",
            "range": "stddev: 0.00043329978308733066",
            "extra": "mean: 48.23238779849344 usec\nrounds: 59714"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 38475.72716616709,
            "unit": "iter/sec",
            "range": "stddev: 0.000004462978028190496",
            "extra": "mean: 25.990411972754895 usec\nrounds: 65883"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3843.6565231643135,
            "unit": "iter/sec",
            "range": "stddev: 0.0000145727577799517",
            "extra": "mean: 260.1689287201823 usec\nrounds: 7688"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7780.445974693913,
            "unit": "iter/sec",
            "range": "stddev: 0.000008342640900154017",
            "extra": "mean: 128.52733676867933 usec\nrounds: 15622"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1252.9935719496543,
            "unit": "iter/sec",
            "range": "stddev: 0.000025244718301641743",
            "extra": "mean: 798.0886912643956 usec\nrounds: 2507"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 410.97111028293347,
            "unit": "iter/sec",
            "range": "stddev: 0.00019239814950776477",
            "extra": "mean: 2.4332610613713186 msec\nrounds: 831"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1759.410423378947,
            "unit": "iter/sec",
            "range": "stddev: 0.00007134707574510538",
            "extra": "mean: 568.3722153239836 usec\nrounds: 3302"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 3822.8709624642665,
            "unit": "iter/sec",
            "range": "stddev: 0.000010683239146391355",
            "extra": "mean: 261.5835087866499 usec\nrounds: 7170"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 367.857319053449,
            "unit": "iter/sec",
            "range": "stddev: 0.00008796627208084209",
            "extra": "mean: 2.7184452998601394 msec\nrounds: 717"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 371.038661870595,
            "unit": "iter/sec",
            "range": "stddev: 0.000025695430291476295",
            "extra": "mean: 2.695136929824214 msec\nrounds: 741"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 826.9646611569917,
            "unit": "iter/sec",
            "range": "stddev: 0.00010072717449848684",
            "extra": "mean: 1.2092415153519591 msec\nrounds: 1661"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 339.3188721328415,
            "unit": "iter/sec",
            "range": "stddev: 0.00003635409564970572",
            "extra": "mean: 2.947080407624677 msec\nrounds: 682"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 331.90437321074893,
            "unit": "iter/sec",
            "range": "stddev: 0.00014761709401601343",
            "extra": "mean: 3.0129160104950805 msec\nrounds: 667"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 311.05929552269043,
            "unit": "iter/sec",
            "range": "stddev: 0.00003995072977418836",
            "extra": "mean: 3.214821143086702 msec\nrounds: 622"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 299.2766556346726,
            "unit": "iter/sec",
            "range": "stddev: 0.00004431532299203168",
            "extra": "mean: 3.3413899185665232 msec\nrounds: 614"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 153.92162231165855,
            "unit": "iter/sec",
            "range": "stddev: 0.0002835523173110362",
            "extra": "mean: 6.496813020689274 msec\nrounds: 290"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 687.4087035627554,
            "unit": "iter/sec",
            "range": "stddev: 0.00002384893072320429",
            "extra": "mean: 1.4547386362976233 msec\nrounds: 1372"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 861.0353177087723,
            "unit": "iter/sec",
            "range": "stddev: 0.000056082586707195545",
            "extra": "mean: 1.1613925461977737 msec\nrounds: 1591"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1504.4063835690724,
            "unit": "iter/sec",
            "range": "stddev: 0.000026605546003597936",
            "extra": "mean: 664.7140100719246 usec\nrounds: 2780"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 746.1979182941848,
            "unit": "iter/sec",
            "range": "stddev: 0.00009214255317643605",
            "extra": "mean: 1.3401270299520658 msec\nrounds: 1469"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 443.5636061996035,
            "unit": "iter/sec",
            "range": "stddev: 0.000042689073906646113",
            "extra": "mean: 2.2544680988773464 msec\nrounds: 890"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1391.0564025260046,
            "unit": "iter/sec",
            "range": "stddev: 0.000015153002684598551",
            "extra": "mean: 718.8781117603216 usec\nrounds: 2738"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 27251.636769992896,
            "unit": "iter/sec",
            "range": "stddev: 0.0007597214480974732",
            "extra": "mean: 36.69504362031979 usec\nrounds: 41632"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2100.878666646194,
            "unit": "iter/sec",
            "range": "stddev: 0.000025671881524740447",
            "extra": "mean: 475.9913153844256 usec\nrounds: 4030"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 42.85933617839445,
            "unit": "iter/sec",
            "range": "stddev: 0.00015729626488798245",
            "extra": "mean: 23.332139252873066 msec\nrounds: 87"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "elfenlieds7",
            "username": "elfenlieds7",
            "email": "elfenliedsp@gmail.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "1e92c1e5b5474f0b6dde112800f14d31327f474c",
          "message": "Merge pull request #4570 from nexi-lab/feat/daemon-readiness-pin\n\nchore(deps): daemon Call-readiness gate (nexus-vfs#201) + drop E2E workaround",
          "timestamp": "2026-08-02T08:15:45Z",
          "url": "https://github.com/nexi-lab/nexus/commit/1e92c1e5b5474f0b6dde112800f14d31327f474c"
        },
        "date": 1785667926889,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 225.50004751303626,
            "unit": "iter/sec",
            "range": "stddev: 0.0004829315096692333",
            "extra": "mean: 4.4345888660719215 msec\nrounds: 112"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 415.0159092257591,
            "unit": "iter/sec",
            "range": "stddev: 0.00048140176900159917",
            "extra": "mean: 2.409546183098304 msec\nrounds: 142"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1379.2131729151433,
            "unit": "iter/sec",
            "range": "stddev: 0.00004259311450170682",
            "extra": "mean: 725.0510795850159 usec\nrounds: 578"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 431.7681356276087,
            "unit": "iter/sec",
            "range": "stddev: 0.0003664557772755293",
            "extra": "mean: 2.3160578965523286 msec\nrounds: 174"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 159.99153934975703,
            "unit": "iter/sec",
            "range": "stddev: 0.0009012103913651082",
            "extra": "mean: 6.250330511627261 msec\nrounds: 86"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25020.605015177982,
            "unit": "iter/sec",
            "range": "stddev: 0.0000030993272418626236",
            "extra": "mean: 39.9670591256039 usec\nrounds: 6816"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1882.028760762869,
            "unit": "iter/sec",
            "range": "stddev: 0.00001694879882669809",
            "extra": "mean: 531.3415080833599 usec\nrounds: 866"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31331.042844661115,
            "unit": "iter/sec",
            "range": "stddev: 0.0000035840396581760376",
            "extra": "mean: 31.917226788715155 usec\nrounds: 8735"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 62272.468193106266,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025635590429755602",
            "extra": "mean: 16.05846097024789 usec\nrounds: 10018"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 52280.38821901485,
            "unit": "iter/sec",
            "range": "stddev: 0.0000028850371735552394",
            "extra": "mean: 19.127631489857432 usec\nrounds: 14735"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 42694.669351278804,
            "unit": "iter/sec",
            "range": "stddev: 0.000003133720076281722",
            "extra": "mean: 23.422127754926567 usec\nrounds: 13158"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 133.10120778476644,
            "unit": "iter/sec",
            "range": "stddev: 0.01156949987827787",
            "extra": "mean: 7.513079833333045 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 16519.60080676813,
            "unit": "iter/sec",
            "range": "stddev: 0.000010843551066131022",
            "extra": "mean: 60.53415041302311 usec\nrounds: 24938"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7556.702513533558,
            "unit": "iter/sec",
            "range": "stddev: 0.000023908830384952767",
            "extra": "mean: 132.33285261780065 usec\nrounds: 12281"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1564.0715272208956,
            "unit": "iter/sec",
            "range": "stddev: 0.00004799577452286496",
            "extra": "mean: 639.3569492162802 usec\nrounds: 2934"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7510.368706420383,
            "unit": "iter/sec",
            "range": "stddev: 0.000025690808987679383",
            "extra": "mean: 133.1492552616133 usec\nrounds: 11498"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 548.4229413454289,
            "unit": "iter/sec",
            "range": "stddev: 0.0002491026614292465",
            "extra": "mean: 1.8234102270534698 msec\nrounds: 1035"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4202.024073370806,
            "unit": "iter/sec",
            "range": "stddev: 0.0004883399537077534",
            "extra": "mean: 237.98054997762395 usec\nrounds: 6653"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 81167.69405117517,
            "unit": "iter/sec",
            "range": "stddev: 0.0000023067821733394977",
            "extra": "mean: 12.320172596864868 usec\nrounds: 47666"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 16642.122466271587,
            "unit": "iter/sec",
            "range": "stddev: 0.000013008159741687834",
            "extra": "mean: 60.08848943556865 usec\nrounds: 27971"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5103160.230622524,
            "unit": "iter/sec",
            "range": "stddev: 1.4437409687297218e-8",
            "extra": "mean: 195.95700601350939 nsec\nrounds: 109927"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4380185.161468007,
            "unit": "iter/sec",
            "range": "stddev: 1.86838726030526e-8",
            "extra": "mean: 228.30085102266608 nsec\nrounds: 99069"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2484379.467160517,
            "unit": "iter/sec",
            "range": "stddev: 1.6833926958601745e-7",
            "extra": "mean: 402.5149995072752 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 20863.45258648195,
            "unit": "iter/sec",
            "range": "stddev: 0.00047244519380391833",
            "extra": "mean: 47.930705421591135 usec\nrounds: 58100"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 38896.62209701915,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018005472545290153",
            "extra": "mean: 25.709173344300126 usec\nrounds: 74851"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3822.819877463129,
            "unit": "iter/sec",
            "range": "stddev: 0.000014378027945502653",
            "extra": "mean: 261.5870043721789 usec\nrounds: 7319"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8171.177335398348,
            "unit": "iter/sec",
            "range": "stddev: 0.00000473362450538332",
            "extra": "mean: 122.38138507506149 usec\nrounds: 16402"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1254.5111591003547,
            "unit": "iter/sec",
            "range": "stddev: 0.00001521275038875594",
            "extra": "mean: 797.1232401927203 usec\nrounds: 2498"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 412.6755938202453,
            "unit": "iter/sec",
            "range": "stddev: 0.0000898551840269298",
            "extra": "mean: 2.42321090700504 msec\nrounds: 828"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1790.3856980956505,
            "unit": "iter/sec",
            "range": "stddev: 0.000059085830864834705",
            "extra": "mean: 558.5388673868728 usec\nrounds: 3333"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 3935.3804079897955,
            "unit": "iter/sec",
            "range": "stddev: 0.000007983680325347896",
            "extra": "mean: 254.1050410196058 usec\nrounds: 7728"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 380.4180949898714,
            "unit": "iter/sec",
            "range": "stddev: 0.00003296053322865875",
            "extra": "mean: 2.6286867348584586 msec\nrounds: 743"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 381.90406583707477,
            "unit": "iter/sec",
            "range": "stddev: 0.000025539058692408434",
            "extra": "mean: 2.618458637794689 msec\nrounds: 762"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 872.7343865150989,
            "unit": "iter/sec",
            "range": "stddev: 0.000027668154883052916",
            "extra": "mean: 1.1458239934753613 msec\nrounds: 1686"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 353.0819165282536,
            "unit": "iter/sec",
            "range": "stddev: 0.00030254848589852246",
            "extra": "mean: 2.832203953781303 msec\nrounds: 714"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 342.124264223386,
            "unit": "iter/sec",
            "range": "stddev: 0.000027260986072018028",
            "extra": "mean: 2.9229145797944978 msec\nrounds: 683"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 321.6021688173573,
            "unit": "iter/sec",
            "range": "stddev: 0.00008998221401715761",
            "extra": "mean: 3.109431766823423 msec\nrounds: 639"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 320.39083117588046,
            "unit": "iter/sec",
            "range": "stddev: 0.000048125964902774174",
            "extra": "mean: 3.1211879451414264 msec\nrounds: 638"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 165.1653134741382,
            "unit": "iter/sec",
            "range": "stddev: 0.0006521537909412407",
            "extra": "mean: 6.054540017911093 msec\nrounds: 335"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 780.1438826651395,
            "unit": "iter/sec",
            "range": "stddev: 0.000026012017232327643",
            "extra": "mean: 1.2818148321355602 msec\nrounds: 1531"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 914.2885698170577,
            "unit": "iter/sec",
            "range": "stddev: 0.000051159981988313495",
            "extra": "mean: 1.0937465839697553 msec\nrounds: 1834"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1551.5932074342406,
            "unit": "iter/sec",
            "range": "stddev: 0.000015157255723796713",
            "extra": "mean: 644.4988255998032 usec\nrounds: 3125"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 848.8335236012979,
            "unit": "iter/sec",
            "range": "stddev: 0.00003063208288828416",
            "extra": "mean: 1.178087307105116 msec\nrounds: 1703"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 510.6668664419521,
            "unit": "iter/sec",
            "range": "stddev: 0.00004410810080547451",
            "extra": "mean: 1.9582237770142679 msec\nrounds: 1018"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1554.2962942983938,
            "unit": "iter/sec",
            "range": "stddev: 0.000014913509428355619",
            "extra": "mean: 643.377973471524 usec\nrounds: 3091"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 27666.912134884795,
            "unit": "iter/sec",
            "range": "stddev: 0.0006271220657857825",
            "extra": "mean: 36.14425762892112 usec\nrounds: 42142"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2068.840419521904,
            "unit": "iter/sec",
            "range": "stddev: 0.00007537566535355672",
            "extra": "mean: 483.3625593176944 usec\nrounds: 3869"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 42.272855768318905,
            "unit": "iter/sec",
            "range": "stddev: 0.0001550796193128041",
            "extra": "mean: 23.655842072288923 msec\nrounds: 83"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "elfenlieds7",
            "username": "elfenlieds7",
            "email": "elfenliedsp@gmail.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "795498407be6471e5b81d90cda0acf0640e9abb4",
          "message": "Merge pull request #4575 from nexi-lab/feat/search-plugin-p1-tantivy-keyword\n\nfeat(search-plugin): Phase 1 — tantivy + Query keyword-only RPC [WIP]",
          "timestamp": "2026-08-03T06:50:28Z",
          "url": "https://github.com/nexi-lab/nexus/commit/795498407be6471e5b81d90cda0acf0640e9abb4"
        },
        "date": 1785760695858,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 243.29474200748183,
            "unit": "iter/sec",
            "range": "stddev: 0.0002947537227592629",
            "extra": "mean: 4.110240902654805 msec\nrounds: 113"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 422.38373275579573,
            "unit": "iter/sec",
            "range": "stddev: 0.000338786150061292",
            "extra": "mean: 2.3675154189192162 msec\nrounds: 148"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1347.067413230348,
            "unit": "iter/sec",
            "range": "stddev: 0.000021687300983376996",
            "extra": "mean: 742.3533448871282 usec\nrounds: 577"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 432.4304110411527,
            "unit": "iter/sec",
            "range": "stddev: 0.0008001961114154781",
            "extra": "mean: 2.312510809756241 msec\nrounds: 205"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 181.85953607549246,
            "unit": "iter/sec",
            "range": "stddev: 0.00021883131737777538",
            "extra": "mean: 5.49874931818195 msec\nrounds: 88"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25098.74892956241,
            "unit": "iter/sec",
            "range": "stddev: 0.0000031619246610208774",
            "extra": "mean: 39.84262334375384 usec\nrounds: 11018"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1867.2470558055716,
            "unit": "iter/sec",
            "range": "stddev: 0.0000130869739906422",
            "extra": "mean: 535.5477717267456 usec\nrounds: 863"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31582.038712720045,
            "unit": "iter/sec",
            "range": "stddev: 0.000004044234851370072",
            "extra": "mean: 31.66356703873072 usec\nrounds: 8331"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 61302.307512872365,
            "unit": "iter/sec",
            "range": "stddev: 0.000002331729393588201",
            "extra": "mean: 16.31259964871662 usec\nrounds: 10246"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 50048.755965767064,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027201460143892598",
            "extra": "mean: 19.98051661232083 usec\nrounds: 14748"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 41277.024059580326,
            "unit": "iter/sec",
            "range": "stddev: 0.0000032130100928417116",
            "extra": "mean: 24.226552732013193 usec\nrounds: 13891"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 132.9774202013283,
            "unit": "iter/sec",
            "range": "stddev: 0.01230075727557783",
            "extra": "mean: 7.520073697369044 msec\nrounds: 76"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 16646.055915852598,
            "unit": "iter/sec",
            "range": "stddev: 0.000010245977783697988",
            "extra": "mean: 60.07429057400117 usec\nrounds: 25398"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7559.014477010997,
            "unit": "iter/sec",
            "range": "stddev: 0.000022322697964422936",
            "extra": "mean: 132.29237793382586 usec\nrounds: 12952"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1570.0398937380596,
            "unit": "iter/sec",
            "range": "stddev: 0.00003942852682929507",
            "extra": "mean: 636.9264908416632 usec\nrounds: 2948"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7473.887832798087,
            "unit": "iter/sec",
            "range": "stddev: 0.000024552578009539562",
            "extra": "mean: 133.79917151173223 usec\nrounds: 12384"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 542.4702258923303,
            "unit": "iter/sec",
            "range": "stddev: 0.000247276584175682",
            "extra": "mean: 1.8434191449955826 msec\nrounds: 1069"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4233.244157310823,
            "unit": "iter/sec",
            "range": "stddev: 0.000577538169533724",
            "extra": "mean: 236.22544857777638 usec\nrounds: 7312"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 82802.20964950886,
            "unit": "iter/sec",
            "range": "stddev: 0.0000020252983209269583",
            "extra": "mean: 12.076972392800528 usec\nrounds: 50965"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 16495.942875925743,
            "unit": "iter/sec",
            "range": "stddev: 0.00001255934037735711",
            "extra": "mean: 60.62096647166527 usec\nrounds: 28543"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 4998181.223650892,
            "unit": "iter/sec",
            "range": "stddev: 1.732934782090247e-8",
            "extra": "mean: 200.07277752717332 nsec\nrounds: 108791"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4464480.387540888,
            "unit": "iter/sec",
            "range": "stddev: 2.681394528906115e-8",
            "extra": "mean: 223.99023250067788 nsec\nrounds: 101849"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2478394.595467369,
            "unit": "iter/sec",
            "range": "stddev: 1.5606066389024256e-7",
            "extra": "mean: 403.4869999429702 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 20879.640994671598,
            "unit": "iter/sec",
            "range": "stddev: 0.0004063250483157074",
            "extra": "mean: 47.8935437757381 usec\nrounds: 58320"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 38935.06096331272,
            "unit": "iter/sec",
            "range": "stddev: 0.0000020736999588947673",
            "extra": "mean: 25.683791812789725 usec\nrounds: 74433"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3864.638943157983,
            "unit": "iter/sec",
            "range": "stddev: 0.000008023380983320478",
            "extra": "mean: 258.75638441474996 usec\nrounds: 7289"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8163.372465656582,
            "unit": "iter/sec",
            "range": "stddev: 0.0000057898245229785",
            "extra": "mean: 122.49839195834974 usec\nrounds: 16290"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1260.6713356780253,
            "unit": "iter/sec",
            "range": "stddev: 0.000016958353668286517",
            "extra": "mean: 793.2281568551499 usec\nrounds: 2531"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 414.6018085078871,
            "unit": "iter/sec",
            "range": "stddev: 0.000028834271146273693",
            "extra": "mean: 2.4119528170870885 msec\nrounds: 831"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1836.5780371637527,
            "unit": "iter/sec",
            "range": "stddev: 0.00004165465428660122",
            "extra": "mean: 544.4908845497852 usec\nrounds: 3508"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 3913.8960457086487,
            "unit": "iter/sec",
            "range": "stddev: 0.000012702680849132777",
            "extra": "mean: 255.4998876621774 usec\nrounds: 6249"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 383.86654254487024,
            "unit": "iter/sec",
            "range": "stddev: 0.000023106622251866023",
            "extra": "mean: 2.605072047619544 msec\nrounds: 777"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 381.8974148615467,
            "unit": "iter/sec",
            "range": "stddev: 0.000030118563737062126",
            "extra": "mean: 2.6185042398428924 msec\nrounds: 763"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 886.4275200565356,
            "unit": "iter/sec",
            "range": "stddev: 0.000016477970289020813",
            "extra": "mean: 1.1281238199105335 msec\nrounds: 1788"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 356.9908890796125,
            "unit": "iter/sec",
            "range": "stddev: 0.00002699766090794845",
            "extra": "mean: 2.801191936797553 msec\nrounds: 712"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 345.9427248019644,
            "unit": "iter/sec",
            "range": "stddev: 0.000029589077790713647",
            "extra": "mean: 2.8906519152048995 msec\nrounds: 684"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 319.0264429203536,
            "unit": "iter/sec",
            "range": "stddev: 0.00003254079589312353",
            "extra": "mean: 3.1345364065939028 msec\nrounds: 637"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 318.07577668529564,
            "unit": "iter/sec",
            "range": "stddev: 0.00004926787784290093",
            "extra": "mean: 3.143904922346226 msec\nrounds: 631"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 168.35250109650002,
            "unit": "iter/sec",
            "range": "stddev: 0.00004579255129778196",
            "extra": "mean: 5.939917693452014 msec\nrounds: 336"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 777.6587109448594,
            "unit": "iter/sec",
            "range": "stddev: 0.00001927587053489856",
            "extra": "mean: 1.2859111406146209 msec\nrounds: 1529"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 924.6054275341945,
            "unit": "iter/sec",
            "range": "stddev: 0.000015752712153563365",
            "extra": "mean: 1.0815424290411892 msec\nrounds: 1825"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1595.5856572917744,
            "unit": "iter/sec",
            "range": "stddev: 0.000015540972120271752",
            "extra": "mean: 626.7291232094201 usec\nrounds: 3141"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 841.4469732284596,
            "unit": "iter/sec",
            "range": "stddev: 0.00001665791918925863",
            "extra": "mean: 1.1884290178895112 msec\nrounds: 1677"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 506.97539339227245,
            "unit": "iter/sec",
            "range": "stddev: 0.00002366525091960205",
            "extra": "mean: 1.972482319721284 msec\nrounds: 1004"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1581.2935540561132,
            "unit": "iter/sec",
            "range": "stddev: 0.000023910136864273536",
            "extra": "mean: 632.3936485005834 usec\nrounds: 3101"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28550.911201714593,
            "unit": "iter/sec",
            "range": "stddev: 0.0005498405084642314",
            "extra": "mean: 35.02515183963537 usec\nrounds: 44165"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2110.942913846537,
            "unit": "iter/sec",
            "range": "stddev: 0.00002026387448271237",
            "extra": "mean: 473.72195308579467 usec\nrounds: 3986"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 43.211847155515244,
            "unit": "iter/sec",
            "range": "stddev: 0.00012940575443868587",
            "extra": "mean: 23.141801747124976 msec\nrounds: 87"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "elfenlieds7",
            "username": "elfenlieds7",
            "email": "elfenliedsp@gmail.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "45d8a194257fecedc3e9c1b5858b213d264cbd9f",
          "message": "Merge pull request #4578 from nexi-lab/feat/search-plugin-p3-hybrid\n\nfeat(search-plugin): Phase 3 — hybrid fusion (RRF + weighted + pooling) [WIP]",
          "timestamp": "2026-08-04T02:14:53Z",
          "url": "https://github.com/nexi-lab/nexus/commit/45d8a194257fecedc3e9c1b5858b213d264cbd9f"
        },
        "date": 1785843339748,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 241.62215111198944,
            "unit": "iter/sec",
            "range": "stddev: 0.00022574072722560606",
            "extra": "mean: 4.138693391304632 msec\nrounds: 115"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 423.3438234683441,
            "unit": "iter/sec",
            "range": "stddev: 0.00036475698007227566",
            "extra": "mean: 2.3621461907894727 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1387.6411087664758,
            "unit": "iter/sec",
            "range": "stddev: 0.000059171224620958166",
            "extra": "mean: 720.6474308684442 usec\nrounds: 622"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 435.5479795706803,
            "unit": "iter/sec",
            "range": "stddev: 0.0009565066106674619",
            "extra": "mean: 2.295958302884794 msec\nrounds: 208"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 175.85606153398683,
            "unit": "iter/sec",
            "range": "stddev: 0.00043408307685453926",
            "extra": "mean: 5.686468759035269 msec\nrounds: 83"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25722.285784507072,
            "unit": "iter/sec",
            "range": "stddev: 0.0000029277551114456855",
            "extra": "mean: 38.87679378021355 usec\nrounds: 11158"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1900.6413209749949,
            "unit": "iter/sec",
            "range": "stddev: 0.00001959581127794595",
            "extra": "mean: 526.1381981777697 usec\nrounds: 878"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31964.889264183348,
            "unit": "iter/sec",
            "range": "stddev: 0.000003286271979775268",
            "extra": "mean: 31.284325490234057 usec\nrounds: 8925"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 64161.89408586829,
            "unit": "iter/sec",
            "range": "stddev: 0.000002559535948452676",
            "extra": "mean: 15.585574806468358 usec\nrounds: 10320"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 52025.16541069832,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027200628443767187",
            "extra": "mean: 19.22146699786105 usec\nrounds: 14893"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 42977.59777784196,
            "unit": "iter/sec",
            "range": "stddev: 0.0000030552922675300963",
            "extra": "mean: 23.26793612730891 usec\nrounds: 13840"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 132.90608770380223,
            "unit": "iter/sec",
            "range": "stddev: 0.011806446859853529",
            "extra": "mean: 7.524109822784225 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 16720.48289628229,
            "unit": "iter/sec",
            "range": "stddev: 0.000010787717093084415",
            "extra": "mean: 59.80688513621485 usec\nrounds: 25108"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7620.012467471174,
            "unit": "iter/sec",
            "range": "stddev: 0.00002294515059039695",
            "extra": "mean: 131.23338108288772 usec\nrounds: 12761"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1566.3835577103303,
            "unit": "iter/sec",
            "range": "stddev: 0.00004313194859694389",
            "extra": "mean: 638.4132386206578 usec\nrounds: 2900"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7577.786111409772,
            "unit": "iter/sec",
            "range": "stddev: 0.000025005545309007132",
            "extra": "mean: 131.96466425653176 usec\nrounds: 11756"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 549.617660837789,
            "unit": "iter/sec",
            "range": "stddev: 0.00024869331489692405",
            "extra": "mean: 1.8194466285448099 msec\nrounds: 1058"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4301.588104279466,
            "unit": "iter/sec",
            "range": "stddev: 0.0005489744299715789",
            "extra": "mean: 232.47228134305627 usec\nrounds: 7386"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 82625.58222483714,
            "unit": "iter/sec",
            "range": "stddev: 0.000002125148049601188",
            "extra": "mean: 12.102789149235203 usec\nrounds: 31961"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 16595.26405218502,
            "unit": "iter/sec",
            "range": "stddev: 0.00001342173311520004",
            "extra": "mean: 60.25815539032262 usec\nrounds: 27846"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5173664.119805004,
            "unit": "iter/sec",
            "range": "stddev: 1.4392444980953128e-8",
            "extra": "mean: 193.28661019410941 nsec\nrounds: 108785"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4471909.622007709,
            "unit": "iter/sec",
            "range": "stddev: 1.6702845384385154e-8",
            "extra": "mean: 223.61811497233253 nsec\nrounds: 101590"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2541166.9033645364,
            "unit": "iter/sec",
            "range": "stddev: 1.6048895384665016e-7",
            "extra": "mean: 393.5200000739769 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 20984.91223559875,
            "unit": "iter/sec",
            "range": "stddev: 0.0004427451306844016",
            "extra": "mean: 47.6532848349779 usec\nrounds: 58167"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39083.91268827706,
            "unit": "iter/sec",
            "range": "stddev: 0.00000179887138831763",
            "extra": "mean: 25.585974668803896 usec\nrounds: 69677"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3862.3488954103973,
            "unit": "iter/sec",
            "range": "stddev: 0.000008126924402905307",
            "extra": "mean: 258.90980516759976 usec\nrounds: 7160"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8147.981959197285,
            "unit": "iter/sec",
            "range": "stddev: 0.000005267165168012623",
            "extra": "mean: 122.7297759135585 usec\nrounds: 16092"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1241.783387742576,
            "unit": "iter/sec",
            "range": "stddev: 0.000014165604942002283",
            "extra": "mean: 805.2934270749819 usec\nrounds: 2482"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 414.897374391311,
            "unit": "iter/sec",
            "range": "stddev: 0.00003677747760655876",
            "extra": "mean: 2.410234582629218 msec\nrounds: 829"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1840.0826993155908,
            "unit": "iter/sec",
            "range": "stddev: 0.00004223695031568193",
            "extra": "mean: 543.4538351846603 usec\nrounds: 3331"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 3904.5915613282223,
            "unit": "iter/sec",
            "range": "stddev: 0.000024514389385932375",
            "extra": "mean: 256.1087336007638 usec\nrounds: 7729"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 382.8267023761898,
            "unit": "iter/sec",
            "range": "stddev: 0.00003295932458447727",
            "extra": "mean: 2.612147987047509 msec\nrounds: 772"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 383.9266679844334,
            "unit": "iter/sec",
            "range": "stddev: 0.000028012494240981406",
            "extra": "mean: 2.6046640762150592 msec\nrounds: 761"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 888.902995552526,
            "unit": "iter/sec",
            "range": "stddev: 0.00002022279769757963",
            "extra": "mean: 1.1249821465371688 msec\nrounds: 1747"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 358.3232157070569,
            "unit": "iter/sec",
            "range": "stddev: 0.000022571823588229334",
            "extra": "mean: 2.7907764726512694 msec\nrounds: 713"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 344.2926145669176,
            "unit": "iter/sec",
            "range": "stddev: 0.000029169367466860005",
            "extra": "mean: 2.9045061023394023 msec\nrounds: 684"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 320.71471291285224,
            "unit": "iter/sec",
            "range": "stddev: 0.000033204707174352554",
            "extra": "mean: 3.118035935793597 msec\nrounds: 623"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 320.2929118238312,
            "unit": "iter/sec",
            "range": "stddev: 0.00015000010598369053",
            "extra": "mean: 3.1221421489028263 msec\nrounds: 638"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 166.81231556183852,
            "unit": "iter/sec",
            "range": "stddev: 0.00048783933814910984",
            "extra": "mean: 5.994761217910753 msec\nrounds: 335"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 753.5412913157679,
            "unit": "iter/sec",
            "range": "stddev: 0.00001817850006453708",
            "extra": "mean: 1.3270672908366938 msec\nrounds: 1506"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 922.4281701089138,
            "unit": "iter/sec",
            "range": "stddev: 0.000013949515696855397",
            "extra": "mean: 1.084095252513729 msec\nrounds: 1790"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1560.2219245609242,
            "unit": "iter/sec",
            "range": "stddev: 0.000010064244019871779",
            "extra": "mean: 640.9344621159703 usec\nrounds: 3062"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 845.2772768329917,
            "unit": "iter/sec",
            "range": "stddev: 0.00001639838303958218",
            "extra": "mean: 1.1830437507402416 msec\nrounds: 1689"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 504.86569012261555,
            "unit": "iter/sec",
            "range": "stddev: 0.00003651178370460778",
            "extra": "mean: 1.9807248136769449 msec\nrounds: 1009"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1558.7444638152658,
            "unit": "iter/sec",
            "range": "stddev: 0.0000169067260132429",
            "extra": "mean: 641.5419738218968 usec\nrounds: 3056"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28610.284536161616,
            "unit": "iter/sec",
            "range": "stddev: 0.0005970642877443848",
            "extra": "mean: 34.95246608736318 usec\nrounds: 45912"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2090.6763896603543,
            "unit": "iter/sec",
            "range": "stddev: 0.000023652190328744918",
            "extra": "mean: 478.3141020511823 usec\nrounds: 3900"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 42.29756651142387,
            "unit": "iter/sec",
            "range": "stddev: 0.00018441026332319306",
            "extra": "mean: 23.642022047058347 msec\nrounds: 85"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "elfenlieds7",
            "username": "elfenlieds7",
            "email": "elfenliedsp@gmail.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "b2e47ccf04206102649dbec951328d92837525cf",
          "message": "Merge pull request #4584 from nexi-lab/chore/pin-founder-secret-fix\n\nchore(nexusd): pin the founder api-key-secret self-generate fix + runbook agent-mint --zone fix",
          "timestamp": "2026-08-05T10:01:28Z",
          "url": "https://github.com/nexi-lab/nexus/commit/b2e47ccf04206102649dbec951328d92837525cf"
        },
        "date": 1785929428904,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 203.11814942296502,
            "unit": "iter/sec",
            "range": "stddev: 0.005668749256893548",
            "extra": "mean: 4.923242963963996 msec\nrounds: 111"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 431.17072839235857,
            "unit": "iter/sec",
            "range": "stddev: 0.000297057226273727",
            "extra": "mean: 2.319266903225434 msec\nrounds: 155"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1422.0350814802116,
            "unit": "iter/sec",
            "range": "stddev: 0.00002176145796267411",
            "extra": "mean: 703.2175317075084 usec\nrounds: 615"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 412.4632127244773,
            "unit": "iter/sec",
            "range": "stddev: 0.0004008195223214553",
            "extra": "mean: 2.4244586405527353 msec\nrounds: 217"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 169.2118287867492,
            "unit": "iter/sec",
            "range": "stddev: 0.000414862005244301",
            "extra": "mean: 5.909752333332792 msec\nrounds: 54"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25587.48343535289,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027338710940538757",
            "extra": "mean: 39.081608104466895 usec\nrounds: 10957"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1904.5144595550503,
            "unit": "iter/sec",
            "range": "stddev: 0.000017256620292178064",
            "extra": "mean: 525.068210946337 usec\nrounds: 877"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31733.243133084346,
            "unit": "iter/sec",
            "range": "stddev: 0.0000032689106304421503",
            "extra": "mean: 31.51269461511241 usec\nrounds: 8134"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 60980.2727290838,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025992424287180703",
            "extra": "mean: 16.398745942686844 usec\nrounds: 9982"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 50059.87491039484,
            "unit": "iter/sec",
            "range": "stddev: 0.000002713505566740168",
            "extra": "mean: 19.976078681578002 usec\nrounds: 15442"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 40686.15704715164,
            "unit": "iter/sec",
            "range": "stddev: 0.000003064883568717403",
            "extra": "mean: 24.578384211639573 usec\nrounds: 13529"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 137.73016643339642,
            "unit": "iter/sec",
            "range": "stddev: 0.010755788176659528",
            "extra": "mean: 7.260573524998826 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 16986.024096909427,
            "unit": "iter/sec",
            "range": "stddev: 0.000010381699724618683",
            "extra": "mean: 58.87192872768548 usec\nrounds: 25606"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7706.857363481719,
            "unit": "iter/sec",
            "range": "stddev: 0.00002269590692876044",
            "extra": "mean: 129.75457476849306 usec\nrounds: 12960"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1609.4524075198444,
            "unit": "iter/sec",
            "range": "stddev: 0.00006697686595852069",
            "extra": "mean: 621.3293386792303 usec\nrounds: 3180"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7745.703872137874,
            "unit": "iter/sec",
            "range": "stddev: 0.000023538707662067174",
            "extra": "mean: 129.1038253601596 usec\nrounds: 12563"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 572.5238519228851,
            "unit": "iter/sec",
            "range": "stddev: 0.0002440944299155649",
            "extra": "mean: 1.7466521205036063 msec\nrounds: 1112"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4196.082810721222,
            "unit": "iter/sec",
            "range": "stddev: 0.0005378237424594339",
            "extra": "mean: 238.31750828295023 usec\nrounds: 7606"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 83807.7512290991,
            "unit": "iter/sec",
            "range": "stddev: 0.0000019385627007344764",
            "extra": "mean: 11.932070546391028 usec\nrounds: 51172"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 16926.73298633607,
            "unit": "iter/sec",
            "range": "stddev: 0.000013313006864643937",
            "extra": "mean: 59.078145842274445 usec\nrounds: 27900"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5071886.080001874,
            "unit": "iter/sec",
            "range": "stddev: 1.8381254816166553e-8",
            "extra": "mean: 197.16531172554068 nsec\nrounds: 107274"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4417955.792240975,
            "unit": "iter/sec",
            "range": "stddev: 2.0925571879852186e-8",
            "extra": "mean: 226.34902815375557 nsec\nrounds: 102475"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2420100.388483655,
            "unit": "iter/sec",
            "range": "stddev: 1.4527886804363163e-7",
            "extra": "mean: 413.2059995356485 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 21381.44953112282,
            "unit": "iter/sec",
            "range": "stddev: 0.0003944314628794507",
            "extra": "mean: 46.76951385098568 usec\nrounds: 62306"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 38540.85761729096,
            "unit": "iter/sec",
            "range": "stddev: 0.0000020832083109133617",
            "extra": "mean: 25.946490603036303 usec\nrounds: 66830"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3808.188889575769,
            "unit": "iter/sec",
            "range": "stddev: 0.000007661282751526293",
            "extra": "mean: 262.59201657179347 usec\nrounds: 7543"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7789.6938358364705,
            "unit": "iter/sec",
            "range": "stddev: 0.00000446911807949491",
            "extra": "mean: 128.37475016020554 usec\nrounds: 15586"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1248.8918648005315,
            "unit": "iter/sec",
            "range": "stddev: 0.00002924200406922903",
            "extra": "mean: 800.709835802891 usec\nrounds: 2497"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 413.0040001615329,
            "unit": "iter/sec",
            "range": "stddev: 0.000044858551278422024",
            "extra": "mean: 2.4212840544132335 msec\nrounds: 827"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1821.838627549705,
            "unit": "iter/sec",
            "range": "stddev: 0.00003801173568634854",
            "extra": "mean: 548.8960355094442 usec\nrounds: 3492"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 3927.7430809335974,
            "unit": "iter/sec",
            "range": "stddev: 0.000007508624508852749",
            "extra": "mean: 254.59913731483348 usec\nrounds: 6882"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 380.3794811834903,
            "unit": "iter/sec",
            "range": "stddev: 0.0000303031922828946",
            "extra": "mean: 2.628953583112998 msec\nrounds: 758"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 383.36342186849123,
            "unit": "iter/sec",
            "range": "stddev: 0.00007395568390614507",
            "extra": "mean: 2.6084909069468796 msec\nrounds: 763"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 875.2533209330624,
            "unit": "iter/sec",
            "range": "stddev: 0.000016365592952324116",
            "extra": "mean: 1.14252637046147 msec\nrounds: 1625"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 346.7893695923654,
            "unit": "iter/sec",
            "range": "stddev: 0.000043015356880829685",
            "extra": "mean: 2.8835947341046038 msec\nrounds: 692"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 341.86959917655366,
            "unit": "iter/sec",
            "range": "stddev: 0.00003027886417162875",
            "extra": "mean: 2.9250919134332394 msec\nrounds: 670"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 317.94671651099725,
            "unit": "iter/sec",
            "range": "stddev: 0.000038566233400770746",
            "extra": "mean: 3.1451810887482834 msec\nrounds: 631"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 317.97420744145063,
            "unit": "iter/sec",
            "range": "stddev: 0.00008835061317176783",
            "extra": "mean: 3.144909167464888 msec\nrounds: 627"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 164.56813985326613,
            "unit": "iter/sec",
            "range": "stddev: 0.00003213433891406545",
            "extra": "mean: 6.076510319018189 msec\nrounds: 326"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 778.0901264153463,
            "unit": "iter/sec",
            "range": "stddev: 0.00002195354166848501",
            "extra": "mean: 1.2851981615638672 msec\nrounds: 1535"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 846.2791698145882,
            "unit": "iter/sec",
            "range": "stddev: 0.00011622008734339682",
            "extra": "mean: 1.18164316890736 msec\nrounds: 1711"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1486.6961114034793,
            "unit": "iter/sec",
            "range": "stddev: 0.000013040707134507761",
            "extra": "mean: 672.632417835528 usec\nrounds: 2994"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 796.9917876223249,
            "unit": "iter/sec",
            "range": "stddev: 0.00001578690171108473",
            "extra": "mean: 1.2547180730472922 msec\nrounds: 1588"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 472.30051843999206,
            "unit": "iter/sec",
            "range": "stddev: 0.000025057891939555855",
            "extra": "mean: 2.1172960031951664 msec\nrounds: 939"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1476.3122565948736,
            "unit": "iter/sec",
            "range": "stddev: 0.000017121452272648844",
            "extra": "mean: 677.3634747885303 usec\nrounds: 2955"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 29211.78774094897,
            "unit": "iter/sec",
            "range": "stddev: 0.0005302422535233684",
            "extra": "mean: 34.232755929490885 usec\nrounds: 46167"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2144.1222539426476,
            "unit": "iter/sec",
            "range": "stddev: 0.000021653126503612437",
            "extra": "mean: 466.39131614868666 usec\nrounds: 4248"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 43.65839963267365,
            "unit": "iter/sec",
            "range": "stddev: 0.000105368640932762",
            "extra": "mean: 22.905099784088442 msec\nrounds: 88"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "elfenlieds7",
            "username": "elfenlieds7",
            "email": "elfenliedsp@gmail.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "a12edbcb1e7075dd289bc226a27a38d063d05eab",
          "message": "Merge pull request #4590 from nexi-lab/feat/search-plugin-p7-cache\n\nfeat(search-plugin): P7 zone-scoped query cache [WIP]",
          "timestamp": "2026-08-06T11:28:26Z",
          "url": "https://github.com/nexi-lab/nexus/commit/a12edbcb1e7075dd289bc226a27a38d063d05eab"
        },
        "date": 1786016073826,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 223.26495223988843,
            "unit": "iter/sec",
            "range": "stddev: 0.0008720570590793367",
            "extra": "mean: 4.478983333333678 msec\nrounds: 111"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 414.24741427240536,
            "unit": "iter/sec",
            "range": "stddev: 0.00039416356703121576",
            "extra": "mean: 2.4140162751683687 msec\nrounds: 149"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1386.635318757098,
            "unit": "iter/sec",
            "range": "stddev: 0.00002279590408635951",
            "extra": "mean: 721.1701494062215 usec\nrounds: 589"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 432.32476091353146,
            "unit": "iter/sec",
            "range": "stddev: 0.0004494377379736099",
            "extra": "mean: 2.313075933672946 msec\nrounds: 196"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 165.13279652079314,
            "unit": "iter/sec",
            "range": "stddev: 0.0007724396898077621",
            "extra": "mean: 6.055732241378728 msec\nrounds: 87"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 24769.986243180123,
            "unit": "iter/sec",
            "range": "stddev: 0.0000036334467878338826",
            "extra": "mean: 40.37143945832139 usec\nrounds: 10852"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1886.62910510189,
            "unit": "iter/sec",
            "range": "stddev: 0.000019406777133481417",
            "extra": "mean: 530.0458883496307 usec\nrounds: 824"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31116.20893354929,
            "unit": "iter/sec",
            "range": "stddev: 0.000004006047712415776",
            "extra": "mean: 32.13759112286352 usec\nrounds: 8719"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 59068.3977605385,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027984084294408478",
            "extra": "mean: 16.92952641197362 usec\nrounds: 9844"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 49594.168074208916,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027844515329187437",
            "extra": "mean: 20.16366114869951 usec\nrounds: 13894"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 40525.770996221436,
            "unit": "iter/sec",
            "range": "stddev: 0.0000038086958825058347",
            "extra": "mean: 24.67565638894911 usec\nrounds: 13038"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 133.14349879340392,
            "unit": "iter/sec",
            "range": "stddev: 0.011975068863693865",
            "extra": "mean: 7.51069341772128 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 16115.586448917142,
            "unit": "iter/sec",
            "range": "stddev: 0.000011848917946847031",
            "extra": "mean: 62.0517288135793 usec\nrounds: 21830"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7461.090822402264,
            "unit": "iter/sec",
            "range": "stddev: 0.000024572202593938595",
            "extra": "mean: 134.0286593211618 usec\nrounds: 11844"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1543.2162272272044,
            "unit": "iter/sec",
            "range": "stddev: 0.00004786115084949847",
            "extra": "mean: 647.997333333362 usec\nrounds: 2997"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7393.049341163471,
            "unit": "iter/sec",
            "range": "stddev: 0.00002650711648925374",
            "extra": "mean: 135.26218395867306 usec\nrounds: 12019"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 530.953852681364,
            "unit": "iter/sec",
            "range": "stddev: 0.00026077444098717426",
            "extra": "mean: 1.883402851208088 msec\nrounds: 1035"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4146.857790031715,
            "unit": "iter/sec",
            "range": "stddev: 0.0003982169075035214",
            "extra": "mean: 241.14644162715595 usec\nrounds: 7375"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 80846.07643137618,
            "unit": "iter/sec",
            "range": "stddev: 0.0000023477897740509356",
            "extra": "mean: 12.369184061131534 usec\nrounds: 49000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 16220.995866495197,
            "unit": "iter/sec",
            "range": "stddev: 0.000013481943902044652",
            "extra": "mean: 61.64849607449323 usec\nrounds: 28149"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5132513.189117654,
            "unit": "iter/sec",
            "range": "stddev: 1.613338540300185e-8",
            "extra": "mean: 194.8363234838395 nsec\nrounds: 109446"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4527267.470616102,
            "unit": "iter/sec",
            "range": "stddev: 3.568505722748846e-8",
            "extra": "mean: 220.88379060667978 nsec\nrounds: 100261"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2378647.357227246,
            "unit": "iter/sec",
            "range": "stddev: 4.5423947941989073e-7",
            "extra": "mean: 420.4070002060689 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 20561.845636588074,
            "unit": "iter/sec",
            "range": "stddev: 0.000502406785134269",
            "extra": "mean: 48.63376652437193 usec\nrounds: 57612"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 38832.18730125265,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026614065961344045",
            "extra": "mean: 25.751832938026183 usec\nrounds: 73368"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3860.393943086626,
            "unit": "iter/sec",
            "range": "stddev: 0.000010783883712142944",
            "extra": "mean: 259.04092036794503 usec\nrounds: 7723"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8003.87957191455,
            "unit": "iter/sec",
            "range": "stddev: 0.0000062450102939428615",
            "extra": "mean: 124.93941107122346 usec\nrounds: 16114"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1255.4765771002503,
            "unit": "iter/sec",
            "range": "stddev: 0.000015498021481029198",
            "extra": "mean: 796.5102800322093 usec\nrounds: 2489"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 414.7750220606166,
            "unit": "iter/sec",
            "range": "stddev: 0.000020864491906527317",
            "extra": "mean: 2.410945565217417 msec\nrounds: 828"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1831.4535668598794,
            "unit": "iter/sec",
            "range": "stddev: 0.00003824280183698716",
            "extra": "mean: 546.014388841182 usec\nrounds: 3495"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 3894.279134832254,
            "unit": "iter/sec",
            "range": "stddev: 0.00000805741954769796",
            "extra": "mean: 256.78693421217093 usec\nrounds: 7281"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 377.5304591772337,
            "unit": "iter/sec",
            "range": "stddev: 0.000029381991602184348",
            "extra": "mean: 2.6487929005234108 msec\nrounds: 764"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 379.18053336075104,
            "unit": "iter/sec",
            "range": "stddev: 0.000027737555811020837",
            "extra": "mean: 2.6372661885798956 msec\nrounds: 753"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 875.0173266818676,
            "unit": "iter/sec",
            "range": "stddev: 0.00001795022198423546",
            "extra": "mean: 1.1428345125371129 msec\nrounds: 1715"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 355.86932577400376,
            "unit": "iter/sec",
            "range": "stddev: 0.000021129162703240607",
            "extra": "mean: 2.810020216901341 msec\nrounds: 710"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 341.5413888981019,
            "unit": "iter/sec",
            "range": "stddev: 0.00002705606109225406",
            "extra": "mean: 2.92790283258568 msec\nrounds: 669"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 317.8242263590885,
            "unit": "iter/sec",
            "range": "stddev: 0.000045162107079427884",
            "extra": "mean: 3.1463932484182826 msec\nrounds: 632"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 316.63332346877536,
            "unit": "iter/sec",
            "range": "stddev: 0.00004283526195081982",
            "extra": "mean: 3.158227280201651 msec\nrounds: 596"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 166.49233921790508,
            "unit": "iter/sec",
            "range": "stddev: 0.00004549094590218106",
            "extra": "mean: 6.006282359281412 msec\nrounds: 334"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 768.4697551658202,
            "unit": "iter/sec",
            "range": "stddev: 0.000022083694849336257",
            "extra": "mean: 1.3012873874056634 msec\nrounds: 1461"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 924.8448261817102,
            "unit": "iter/sec",
            "range": "stddev: 0.000021428978295221537",
            "extra": "mean: 1.0812624687847079 msec\nrounds: 1794"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1587.673275264207,
            "unit": "iter/sec",
            "range": "stddev: 0.000015096279131581307",
            "extra": "mean: 629.8525115840276 usec\nrounds: 3194"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 850.898551588931,
            "unit": "iter/sec",
            "range": "stddev: 0.000017704802201310782",
            "extra": "mean: 1.1752282315355262 msec\nrounds: 1706"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 512.9210396599622,
            "unit": "iter/sec",
            "range": "stddev: 0.00002168535487606156",
            "extra": "mean: 1.949617821610406 msec\nrounds: 981"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1573.1574147333743,
            "unit": "iter/sec",
            "range": "stddev: 0.000011101285755895069",
            "extra": "mean: 635.6642956607649 usec\nrounds: 3088"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 27704.47368123002,
            "unit": "iter/sec",
            "range": "stddev: 0.0006358404773785702",
            "extra": "mean: 36.095253478051355 usec\nrounds: 43558"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2036.0036023249563,
            "unit": "iter/sec",
            "range": "stddev: 0.000021042308791741786",
            "extra": "mean: 491.1582665463256 usec\nrounds: 3883"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 40.91480197012978,
            "unit": "iter/sec",
            "range": "stddev: 0.00016407618381600696",
            "extra": "mean: 24.4410323855425 msec\nrounds: 83"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "elfenlieds7",
            "username": "elfenlieds7",
            "email": "elfenliedsp@gmail.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "f27bd2e60ac83e94dbf52bb705cf2bdc9fdee9dc",
          "message": "Merge pull request #4593 from nexi-lab/chore/pin-auth-cli-daemon-client\n\nchore(deps): bump nexus-vfs pin to c9ecc9da (auth CLI = live-daemon client + control zone)",
          "timestamp": "2026-08-07T03:06:22Z",
          "url": "https://github.com/nexi-lab/nexus/commit/f27bd2e60ac83e94dbf52bb705cf2bdc9fdee9dc"
        },
        "date": 1786097536668,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 244.94920227109432,
            "unit": "iter/sec",
            "range": "stddev: 0.0002423308560095871",
            "extra": "mean: 4.082479104762559 msec\nrounds: 105"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 401.564562372571,
            "unit": "iter/sec",
            "range": "stddev: 0.00037539550715528337",
            "extra": "mean: 2.490259583892768 msec\nrounds: 149"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1332.4005567524277,
            "unit": "iter/sec",
            "range": "stddev: 0.00002319802958614439",
            "extra": "mean: 750.5250541454174 usec\nrounds: 591"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 447.60478522139454,
            "unit": "iter/sec",
            "range": "stddev: 0.0003532193365989893",
            "extra": "mean: 2.2341137383180105 msec\nrounds: 214"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 179.8574815640657,
            "unit": "iter/sec",
            "range": "stddev: 0.0002930061197121628",
            "extra": "mean: 5.55995775824203 msec\nrounds: 91"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25046.78666394121,
            "unit": "iter/sec",
            "range": "stddev: 0.000002987944239629762",
            "extra": "mean: 39.92528117148286 usec\nrounds: 10346"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1909.27460419658,
            "unit": "iter/sec",
            "range": "stddev: 0.000011068432892956354",
            "extra": "mean: 523.7591270538052 usec\nrounds: 913"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31298.584529000018,
            "unit": "iter/sec",
            "range": "stddev: 0.0000036767091380078484",
            "extra": "mean: 31.950326669675423 usec\nrounds: 8654"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 61126.161213510815,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026967247682449344",
            "extra": "mean: 16.35960741108945 usec\nrounds: 9850"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 50837.60150012486,
            "unit": "iter/sec",
            "range": "stddev: 0.000003324078463416822",
            "extra": "mean: 19.67047953663872 usec\nrounds: 14416"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 41047.0496802924,
            "unit": "iter/sec",
            "range": "stddev: 0.0000032231194840793433",
            "extra": "mean: 24.362286882706755 usec\nrounds: 13204"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 135.2620415178851,
            "unit": "iter/sec",
            "range": "stddev: 0.010573683793422302",
            "extra": "mean: 7.393057126583249 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 16653.97834856796,
            "unit": "iter/sec",
            "range": "stddev: 0.000010242830911423467",
            "extra": "mean: 60.04571274622727 usec\nrounds: 24682"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7580.331272022211,
            "unit": "iter/sec",
            "range": "stddev: 0.000023037804294716897",
            "extra": "mean: 131.92035599959067 usec\nrounds: 12559"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1582.7877427146204,
            "unit": "iter/sec",
            "range": "stddev: 0.000043628888458350065",
            "extra": "mean: 631.7966541015234 usec\nrounds: 2865"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7522.2771263311615,
            "unit": "iter/sec",
            "range": "stddev: 0.00002497044046395775",
            "extra": "mean: 132.9384683927126 usec\nrounds: 11548"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 550.249541319787,
            "unit": "iter/sec",
            "range": "stddev: 0.0002453956309605082",
            "extra": "mean: 1.817357262309525 msec\nrounds: 1056"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4238.900757057867,
            "unit": "iter/sec",
            "range": "stddev: 0.0005740776550408613",
            "extra": "mean: 235.91021760416942 usec\nrounds: 7555"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 81576.36124743438,
            "unit": "iter/sec",
            "range": "stddev: 0.0000021998716242883074",
            "extra": "mean: 12.258453119364288 usec\nrounds: 46917"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 16671.3925108893,
            "unit": "iter/sec",
            "range": "stddev: 0.000013044490028994007",
            "extra": "mean: 59.982991783489425 usec\nrounds: 27627"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5177659.516509248,
            "unit": "iter/sec",
            "range": "stddev: 1.8774822703273877e-8",
            "extra": "mean: 193.13745850059195 nsec\nrounds: 111025"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4421459.359266177,
            "unit": "iter/sec",
            "range": "stddev: 1.7151648725293002e-8",
            "extra": "mean: 226.16966904926352 nsec\nrounds: 100166"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2265821.666540115,
            "unit": "iter/sec",
            "range": "stddev: 9.719141545324414e-7",
            "extra": "mean: 441.34099994153075 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 21188.988954263194,
            "unit": "iter/sec",
            "range": "stddev: 0.000447081107415037",
            "extra": "mean: 47.19432353089228 usec\nrounds: 55531"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 38790.37383200138,
            "unit": "iter/sec",
            "range": "stddev: 0.0000022254765426628244",
            "extra": "mean: 25.779591718577805 usec\nrounds: 74963"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3848.48175920027,
            "unit": "iter/sec",
            "range": "stddev: 0.000011860718119938247",
            "extra": "mean: 259.8427282679401 usec\nrounds: 7673"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7810.04073462578,
            "unit": "iter/sec",
            "range": "stddev: 0.0000055663042357349415",
            "extra": "mean: 128.0403052914314 usec\nrounds: 15667"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1234.4911722157194,
            "unit": "iter/sec",
            "range": "stddev: 0.00001718529324329919",
            "extra": "mean: 810.0503450382362 usec\nrounds: 2449"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 414.43329986875284,
            "unit": "iter/sec",
            "range": "stddev: 0.000024209527875992817",
            "extra": "mean: 2.412933517448262 msec\nrounds: 831"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1850.5863360696394,
            "unit": "iter/sec",
            "range": "stddev: 0.00004208366099427995",
            "extra": "mean: 540.3692767578983 usec\nrounds: 3541"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 3837.889270997933,
            "unit": "iter/sec",
            "range": "stddev: 0.000008823689267344382",
            "extra": "mean: 260.55988836279755 usec\nrounds: 7605"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 374.8473676774932,
            "unit": "iter/sec",
            "range": "stddev: 0.00002178922940406198",
            "extra": "mean: 2.6677524940240964 msec\nrounds: 753"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 372.9956801111562,
            "unit": "iter/sec",
            "range": "stddev: 0.00001990392279181721",
            "extra": "mean: 2.680996197333949 msec\nrounds: 750"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 850.9438315613091,
            "unit": "iter/sec",
            "range": "stddev: 0.000024130261444021574",
            "extra": "mean: 1.175165695913446 msec\nrounds: 1664"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 343.75408268454066,
            "unit": "iter/sec",
            "range": "stddev: 0.000047931389663932596",
            "extra": "mean: 2.9090563585180425 msec\nrounds: 675"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 335.7161213907651,
            "unit": "iter/sec",
            "range": "stddev: 0.00002541538442221643",
            "extra": "mean: 2.9787071167667434 msec\nrounds: 668"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 313.5318434728553,
            "unit": "iter/sec",
            "range": "stddev: 0.00003283875607247041",
            "extra": "mean: 3.189468696140834 msec\nrounds: 622"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 313.53464526900603,
            "unit": "iter/sec",
            "range": "stddev: 0.00006645929240606252",
            "extra": "mean: 3.189440194534232 msec\nrounds: 622"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 163.43448026939504,
            "unit": "iter/sec",
            "range": "stddev: 0.000029541435323309685",
            "extra": "mean: 6.118659895706606 msec\nrounds: 326"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 762.214843956184,
            "unit": "iter/sec",
            "range": "stddev: 0.000015422976562156494",
            "extra": "mean: 1.3119660525234866 msec\nrounds: 1466"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 891.2741504050198,
            "unit": "iter/sec",
            "range": "stddev: 0.000025115538168361436",
            "extra": "mean: 1.1219892325448595 msec\nrounds: 1776"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1531.738903793213,
            "unit": "iter/sec",
            "range": "stddev: 0.000013881870564078269",
            "extra": "mean: 652.8527789713967 usec\nrounds: 3072"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 813.4291310450665,
            "unit": "iter/sec",
            "range": "stddev: 0.000018075638566417604",
            "extra": "mean: 1.2293633972946525 msec\nrounds: 1626"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 491.26970157528547,
            "unit": "iter/sec",
            "range": "stddev: 0.00005212986895113195",
            "extra": "mean: 2.035541774291068 msec\nrounds: 988"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1475.420387297833,
            "unit": "iter/sec",
            "range": "stddev: 0.000011277953526828002",
            "extra": "mean: 677.7729307587077 usec\nrounds: 2874"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28061.659832609268,
            "unit": "iter/sec",
            "range": "stddev: 0.0005696495960618999",
            "extra": "mean: 35.635810781154944 usec\nrounds: 42036"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2097.7572736346615,
            "unit": "iter/sec",
            "range": "stddev: 0.000023063397585681262",
            "extra": "mean: 476.69957462111836 usec\nrounds: 4027"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 42.638340402828334,
            "unit": "iter/sec",
            "range": "stddev: 0.00010621845536170851",
            "extra": "mean: 23.45307041860538 msec\nrounds: 86"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "elfenlieds7",
            "username": "elfenlieds7",
            "email": "elfenliedsp@gmail.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "f7f45118a5aa3a65f54c93a65a657409b36703fe",
          "message": "Merge pull request #4605 from nexi-lab/fix/edge-smoke-plugin-sidecar\n\nfix(ci): edge-smoke gets a search-plugin sidecar; P12 topology alignment",
          "timestamp": "2026-08-08T09:23:59Z",
          "url": "https://github.com/nexi-lab/nexus/commit/f7f45118a5aa3a65f54c93a65a657409b36703fe"
        },
        "date": 1786182900978,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 243.85058283415654,
            "unit": "iter/sec",
            "range": "stddev: 0.000301827268556962",
            "extra": "mean: 4.100871887930253 msec\nrounds: 116"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 422.420541900594,
            "unit": "iter/sec",
            "range": "stddev: 0.0003028368392043981",
            "extra": "mean: 2.3673091168831575 msec\nrounds: 154"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1387.1295430659493,
            "unit": "iter/sec",
            "range": "stddev: 0.000038939494271000364",
            "extra": "mean: 720.9132016536225 usec\nrounds: 605"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 453.8555583517845,
            "unit": "iter/sec",
            "range": "stddev: 0.0001874779079831323",
            "extra": "mean: 2.203344173268663 msec\nrounds: 202"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 184.61521591870456,
            "unit": "iter/sec",
            "range": "stddev: 0.000332124854350515",
            "extra": "mean: 5.416671616278642 msec\nrounds: 86"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25251.31545913151,
            "unit": "iter/sec",
            "range": "stddev: 0.0000031131373600035473",
            "extra": "mean: 39.60189724050098 usec\nrounds: 10763"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1891.524555591688,
            "unit": "iter/sec",
            "range": "stddev: 0.000019230556866773296",
            "extra": "mean: 528.6740777664343 usec\nrounds: 913"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 32277.82179243601,
            "unit": "iter/sec",
            "range": "stddev: 0.0000035478260584531538",
            "extra": "mean: 30.981024879266798 usec\nrounds: 9124"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 62460.70176974688,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026886267787566265",
            "extra": "mean: 16.01006667658599 usec\nrounds: 6689"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 51620.33645992796,
            "unit": "iter/sec",
            "range": "stddev: 0.000003047337703853025",
            "extra": "mean: 19.37221003540502 usec\nrounds: 14688"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 41529.5142347371,
            "unit": "iter/sec",
            "range": "stddev: 0.00000305087875437082",
            "extra": "mean: 24.079260699937496 usec\nrounds: 12827"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 137.53448650324012,
            "unit": "iter/sec",
            "range": "stddev: 0.010098646427715907",
            "extra": "mean: 7.270903650601418 msec\nrounds: 83"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 17220.670880649046,
            "unit": "iter/sec",
            "range": "stddev: 0.000011123535263415033",
            "extra": "mean: 58.069746929761315 usec\nrounds: 18647"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 8013.605882730866,
            "unit": "iter/sec",
            "range": "stddev: 0.000022235564186963373",
            "extra": "mean: 124.78776903103964 usec\nrounds: 13084"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1634.8329232303952,
            "unit": "iter/sec",
            "range": "stddev: 0.00008706010454706298",
            "extra": "mean: 611.6833015718947 usec\nrounds: 3054"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7913.086759109909,
            "unit": "iter/sec",
            "range": "stddev: 0.000023842708557126766",
            "extra": "mean: 126.37293516954735 usec\nrounds: 12309"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 569.3218964802311,
            "unit": "iter/sec",
            "range": "stddev: 0.0002503575484708999",
            "extra": "mean: 1.756475565374155 msec\nrounds: 1109"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4237.026608782712,
            "unit": "iter/sec",
            "range": "stddev: 0.0003713785805176834",
            "extra": "mean: 236.01456689631166 usec\nrounds: 7691"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 82156.46510612425,
            "unit": "iter/sec",
            "range": "stddev: 0.000002138529079975776",
            "extra": "mean: 12.171896620774357 usec\nrounds: 50900"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 17109.567203607752,
            "unit": "iter/sec",
            "range": "stddev: 0.000012828225117455752",
            "extra": "mean: 58.44683200339155 usec\nrounds: 28572"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5088545.512313973,
            "unit": "iter/sec",
            "range": "stddev: 1.676448751504477e-8",
            "extra": "mean: 196.51981053919246 nsec\nrounds: 111527"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4281961.6443846375,
            "unit": "iter/sec",
            "range": "stddev: 1.7055370097624185e-8",
            "extra": "mean: 233.53782286009022 nsec\nrounds: 99966"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2470868.460205242,
            "unit": "iter/sec",
            "range": "stddev: 1.603247435638819e-7",
            "extra": "mean: 404.71600010505426 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 21653.355458289,
            "unit": "iter/sec",
            "range": "stddev: 0.0004063859488136924",
            "extra": "mean: 46.18221882175751 usec\nrounds: 58783"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39060.08079897759,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017443767049860835",
            "extra": "mean: 25.601585545777347 usec\nrounds: 68741"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3845.1514391695036,
            "unit": "iter/sec",
            "range": "stddev: 0.00001672137247131848",
            "extra": "mean: 260.0677803774578 usec\nrounds: 7736"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7722.970676931217,
            "unit": "iter/sec",
            "range": "stddev: 0.00001329283720579287",
            "extra": "mean: 129.4838530187658 usec\nrounds: 15553"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1227.5888339230073,
            "unit": "iter/sec",
            "range": "stddev: 0.00001258659168311776",
            "extra": "mean: 814.604998323664 usec\nrounds: 2386"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 412.9931504391777,
            "unit": "iter/sec",
            "range": "stddev: 0.0000446612220751246",
            "extra": "mean: 2.421347663845267 msec\nrounds: 827"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1813.3141050037775,
            "unit": "iter/sec",
            "range": "stddev: 0.00003308947139109467",
            "extra": "mean: 551.4764360132283 usec\nrounds: 3321"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 3922.3260730792276,
            "unit": "iter/sec",
            "range": "stddev: 0.000010134721722617296",
            "extra": "mean: 254.9507566093169 usec\nrounds: 7149"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 384.16320832730173,
            "unit": "iter/sec",
            "range": "stddev: 0.0000336262336983546",
            "extra": "mean: 2.6030603095859557 msec\nrounds: 772"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 382.5875844192647,
            "unit": "iter/sec",
            "range": "stddev: 0.00002778976093306642",
            "extra": "mean: 2.6137805844324893 msec\nrounds: 758"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 879.23395506713,
            "unit": "iter/sec",
            "range": "stddev: 0.000016186145416255657",
            "extra": "mean: 1.1373537091429204 msec\nrounds: 1750"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 346.8491426831085,
            "unit": "iter/sec",
            "range": "stddev: 0.000026225975873425082",
            "extra": "mean: 2.8830977994188935 msec\nrounds: 688"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 343.1464517056965,
            "unit": "iter/sec",
            "range": "stddev: 0.00009672908161518096",
            "extra": "mean: 2.914207607361948 msec\nrounds: 652"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 318.18937429703647,
            "unit": "iter/sec",
            "range": "stddev: 0.0000333381223757246",
            "extra": "mean: 3.142782508715954 msec\nrounds: 631"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 318.59570866311293,
            "unit": "iter/sec",
            "range": "stddev: 0.00002961702688339188",
            "extra": "mean: 3.138774229559421 msec\nrounds: 636"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 162.27407500750002,
            "unit": "iter/sec",
            "range": "stddev: 0.0007306114489897225",
            "extra": "mean: 6.162413804877838 msec\nrounds: 328"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 770.6933574902002,
            "unit": "iter/sec",
            "range": "stddev: 0.00002159577731988465",
            "extra": "mean: 1.297532916666815 msec\nrounds: 1500"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 890.0130672786444,
            "unit": "iter/sec",
            "range": "stddev: 0.00013239888543929503",
            "extra": "mean: 1.1235790088539463 msec\nrounds: 1807"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1559.3322280010457,
            "unit": "iter/sec",
            "range": "stddev: 0.00003883822437019856",
            "extra": "mean: 641.3001553119502 usec\nrounds: 3097"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 837.1896983661472,
            "unit": "iter/sec",
            "range": "stddev: 0.00006896644935222096",
            "extra": "mean: 1.1944724140198955 msec\nrounds: 1669"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 506.801885901347,
            "unit": "iter/sec",
            "range": "stddev: 0.000027409453946418288",
            "extra": "mean: 1.9731576140872091 msec\nrounds: 1008"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1534.1614945313568,
            "unit": "iter/sec",
            "range": "stddev: 0.000014422772419667634",
            "extra": "mean: 651.8218607132177 usec\nrounds: 3001"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28216.825760404507,
            "unit": "iter/sec",
            "range": "stddev: 0.000550238615600819",
            "extra": "mean: 35.4398474332736 usec\nrounds: 42447"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2119.0034499428575,
            "unit": "iter/sec",
            "range": "stddev: 0.00004345355177984513",
            "extra": "mean: 471.9199489868535 usec\nrounds: 4097"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 42.98097258478158,
            "unit": "iter/sec",
            "range": "stddev: 0.00008722011764949986",
            "extra": "mean: 23.26610916091911 msec\nrounds: 87"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "elfenlieds7",
            "username": "elfenlieds7",
            "email": "elfenliedsp@gmail.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "b821f85c340e60bd517236f05aab30af597b6b02",
          "message": "Merge pull request #4609 from nexi-lab/fix/search-daemon-async-audit-remnants\n\nfix(search): two more sync daemon.get_stats() call sites — P12 async audit",
          "timestamp": "2026-08-08T11:51:26Z",
          "url": "https://github.com/nexi-lab/nexus/commit/b821f85c340e60bd517236f05aab30af597b6b02"
        },
        "date": 1786269432304,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 209.87078363672245,
            "unit": "iter/sec",
            "range": "stddev: 0.0010816708740844778",
            "extra": "mean: 4.764836642202462 msec\nrounds: 109"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 416.5446574870662,
            "unit": "iter/sec",
            "range": "stddev: 0.0003117268966971435",
            "extra": "mean: 2.4007029787221557 msec\nrounds: 141"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1366.8726758471676,
            "unit": "iter/sec",
            "range": "stddev: 0.00004692054206146007",
            "extra": "mean: 731.5970372882133 usec\nrounds: 590"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 352.3620681480914,
            "unit": "iter/sec",
            "range": "stddev: 0.0016824030690844331",
            "extra": "mean: 2.8379899268263977 msec\nrounds: 82"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 154.40038998612386,
            "unit": "iter/sec",
            "range": "stddev: 0.0010729029312400485",
            "extra": "mean: 6.476667578947638 msec\nrounds: 57"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 24766.786873307286,
            "unit": "iter/sec",
            "range": "stddev: 0.0000033379095055773414",
            "extra": "mean: 40.376654634911986 usec\nrounds: 10777"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1903.6778492007222,
            "unit": "iter/sec",
            "range": "stddev: 0.00001904369062465197",
            "extra": "mean: 525.2989629625936 usec\nrounds: 864"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31156.490812026837,
            "unit": "iter/sec",
            "range": "stddev: 0.00000359943941075122",
            "extra": "mean: 32.096040790767944 usec\nrounds: 8801"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 58523.30477602865,
            "unit": "iter/sec",
            "range": "stddev: 0.000004223762139988681",
            "extra": "mean: 17.08721002388784 usec\nrounds: 9637"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 49377.95663660375,
            "unit": "iter/sec",
            "range": "stddev: 0.0000049422339445190755",
            "extra": "mean: 20.25195184481779 usec\nrounds: 14474"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 39904.233320621286,
            "unit": "iter/sec",
            "range": "stddev: 0.0000035238443622755862",
            "extra": "mean: 25.059997819410068 usec\nrounds: 12840"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 128.49392589853372,
            "unit": "iter/sec",
            "range": "stddev: 0.012611693604057771",
            "extra": "mean: 7.782469038961874 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 16274.78398880078,
            "unit": "iter/sec",
            "range": "stddev: 0.000011323631656997125",
            "extra": "mean: 61.444747941854914 usec\nrounds: 23808"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7447.591466522064,
            "unit": "iter/sec",
            "range": "stddev: 0.000023930447739217195",
            "extra": "mean: 134.27159699818873 usec\nrounds: 12593"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1597.1222230780522,
            "unit": "iter/sec",
            "range": "stddev: 0.000042817960655260845",
            "extra": "mean: 626.1261571282572 usec\nrounds: 2883"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7451.41674386665,
            "unit": "iter/sec",
            "range": "stddev: 0.00002556099224029551",
            "extra": "mean: 134.20266700599078 usec\nrounds: 11787"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 543.2492689305494,
            "unit": "iter/sec",
            "range": "stddev: 0.0002627859337284804",
            "extra": "mean: 1.8407756019048465 msec\nrounds: 1050"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4220.524261299264,
            "unit": "iter/sec",
            "range": "stddev: 0.0004894495663773219",
            "extra": "mean: 236.93738931195145 usec\nrounds: 6961"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 81699.26595838356,
            "unit": "iter/sec",
            "range": "stddev: 0.000002211258143187134",
            "extra": "mean: 12.240012052365142 usec\nrounds: 45966"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 16162.407551333528,
            "unit": "iter/sec",
            "range": "stddev: 0.00001409151274739862",
            "extra": "mean: 61.87197030045762 usec\nrounds: 26667"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5153639.80531477,
            "unit": "iter/sec",
            "range": "stddev: 2.5024498145605668e-8",
            "extra": "mean: 194.0376195807737 nsec\nrounds: 110657"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4460000.205877332,
            "unit": "iter/sec",
            "range": "stddev: 2.488244897841733e-8",
            "extra": "mean: 224.21523628680836 nsec\nrounds: 97424"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2282818.366130747,
            "unit": "iter/sec",
            "range": "stddev: 5.587958127404034e-7",
            "extra": "mean: 438.0550002736072 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 20463.939653720867,
            "unit": "iter/sec",
            "range": "stddev: 0.0004721316532210045",
            "extra": "mean: 48.86644590051723 usec\nrounds: 56313"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 38764.98777487098,
            "unit": "iter/sec",
            "range": "stddev: 0.0000030963063790505127",
            "extra": "mean: 25.79647401948209 usec\nrounds: 64837"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3808.150436406259,
            "unit": "iter/sec",
            "range": "stddev: 0.00002038391520595917",
            "extra": "mean: 262.5946681202272 usec\nrounds: 7108"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8139.904920535693,
            "unit": "iter/sec",
            "range": "stddev: 0.000005367630819274481",
            "extra": "mean: 122.85155782067652 usec\nrounds: 16335"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1238.6567673532195,
            "unit": "iter/sec",
            "range": "stddev: 0.00004687975007437272",
            "extra": "mean: 807.326150679187 usec\nrounds: 2429"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 415.0897347772879,
            "unit": "iter/sec",
            "range": "stddev: 0.000020718684746348372",
            "extra": "mean: 2.4091176346158973 msec\nrounds: 832"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1816.8785518228274,
            "unit": "iter/sec",
            "range": "stddev: 0.000040224818836861055",
            "extra": "mean: 550.3945208647688 usec\nrounds: 3331"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 3903.8995184433616,
            "unit": "iter/sec",
            "range": "stddev: 0.000009604675164124695",
            "extra": "mean: 256.15413390525464 usec\nrounds: 7692"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 381.5674098810944,
            "unit": "iter/sec",
            "range": "stddev: 0.000021839002507946523",
            "extra": "mean: 2.6207688971959735 msec\nrounds: 749"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 381.91128503672417,
            "unit": "iter/sec",
            "range": "stddev: 0.00007328687619512184",
            "extra": "mean: 2.6184091415466844 msec\nrounds: 763"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 872.7323062690423,
            "unit": "iter/sec",
            "range": "stddev: 0.00005309787655326393",
            "extra": "mean: 1.14582672466318 msec\nrounds: 1707"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 356.75578498997714,
            "unit": "iter/sec",
            "range": "stddev: 0.000025563325634338934",
            "extra": "mean: 2.8030379382021637 msec\nrounds: 712"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 343.1992962127054,
            "unit": "iter/sec",
            "range": "stddev: 0.000046368082060120635",
            "extra": "mean: 2.913758888888361 msec\nrounds: 666"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 315.4303234141005,
            "unit": "iter/sec",
            "range": "stddev: 0.00022082134790875045",
            "extra": "mean: 3.1702722464231465 msec\nrounds: 629"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 317.49481056376425,
            "unit": "iter/sec",
            "range": "stddev: 0.00004059259986750777",
            "extra": "mean: 3.1496577793644422 msec\nrounds: 630"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 167.10115508002036,
            "unit": "iter/sec",
            "range": "stddev: 0.00010163809883189334",
            "extra": "mean: 5.984399087613285 msec\nrounds: 331"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 780.3318470123912,
            "unit": "iter/sec",
            "range": "stddev: 0.00002034538610786623",
            "extra": "mean: 1.2815060718444837 msec\nrounds: 1545"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 930.6813461803207,
            "unit": "iter/sec",
            "range": "stddev: 0.000046241777962029595",
            "extra": "mean: 1.0744816194116011 msec\nrounds: 1834"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1586.2482542700814,
            "unit": "iter/sec",
            "range": "stddev: 0.000030324688868462957",
            "extra": "mean: 630.4183454941951 usec\nrounds: 3207"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 855.4009915086547,
            "unit": "iter/sec",
            "range": "stddev: 0.000020365376504013776",
            "extra": "mean: 1.1690423671783672 msec\nrounds: 1694"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 511.65651861278565,
            "unit": "iter/sec",
            "range": "stddev: 0.000036213022624179006",
            "extra": "mean: 1.954436157114194 msec\nrounds: 1012"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1574.86265512512,
            "unit": "iter/sec",
            "range": "stddev: 0.0000208078445348988",
            "extra": "mean: 634.9760067937809 usec\nrounds: 3091"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28050.552906418165,
            "unit": "iter/sec",
            "range": "stddev: 0.0006245087881714644",
            "extra": "mean: 35.64992117396706 usec\nrounds: 44668"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2129.4156867443176,
            "unit": "iter/sec",
            "range": "stddev: 0.000022522723032484533",
            "extra": "mean: 469.6123947170263 usec\nrounds: 4051"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 42.838649789183414,
            "unit": "iter/sec",
            "range": "stddev: 0.00031333664991872533",
            "extra": "mean: 23.343406127904988 msec\nrounds: 86"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "oliverfeng",
            "username": "windoliver",
            "email": "oliverfengpet@gmail.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "670dcfa59c406515207081eedc5bfd3d4edd899c",
          "message": "Merge pull request #4611 from nexi-lab/fix/4610-batch-search-serial-embed-cache\n\nsearch-plugin: parallel BatchQuery + query-embedding cache",
          "timestamp": "2026-08-10T00:05:29Z",
          "url": "https://github.com/nexi-lab/nexus/commit/670dcfa59c406515207081eedc5bfd3d4edd899c"
        },
        "date": 1786358262339,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 57.84394924578599,
            "unit": "iter/sec",
            "range": "stddev: 0.03328315217545313",
            "extra": "mean: 17.28789290909025 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 630.3111612129358,
            "unit": "iter/sec",
            "range": "stddev: 0.00027743293329518927",
            "extra": "mean: 1.58651799545427 msec\nrounds: 220"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 2448.216883293334,
            "unit": "iter/sec",
            "range": "stddev: 0.00004328999604896867",
            "extra": "mean: 408.4605440081775 usec\nrounds: 943"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 372.412741259985,
            "unit": "iter/sec",
            "range": "stddev: 0.0020245675734700506",
            "extra": "mean: 2.6851927692288333 msec\nrounds: 208"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 123.85590890903998,
            "unit": "iter/sec",
            "range": "stddev: 0.004074838342342411",
            "extra": "mean: 8.073898199999501 msec\nrounds: 30"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 39959.316156429115,
            "unit": "iter/sec",
            "range": "stddev: 0.000001777655029689902",
            "extra": "mean: 25.02545329067421 usec\nrounds: 16228"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 3024.6624246965475,
            "unit": "iter/sec",
            "range": "stddev: 0.000021152670199519474",
            "extra": "mean: 330.61540746991824 usec\nrounds: 1178"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 56404.97477176303,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016452085078468066",
            "extra": "mean: 17.728932670325584 usec\nrounds: 8748"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 120532.11268402256,
            "unit": "iter/sec",
            "range": "stddev: 0.0000010419474907015676",
            "extra": "mean: 8.296544196661689 usec\nrounds: 15827"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 92799.765131389,
            "unit": "iter/sec",
            "range": "stddev: 0.0000012168932242308507",
            "extra": "mean: 10.775889341790538 usec\nrounds: 25032"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 80261.19031311233,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013608660288477955",
            "extra": "mean: 12.459321822898874 usec\nrounds: 22270"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 228.93773471539052,
            "unit": "iter/sec",
            "range": "stddev: 0.006477935761007406",
            "extra": "mean: 4.367999889765548 msec\nrounds: 127"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 40517.19267208707,
            "unit": "iter/sec",
            "range": "stddev: 0.000004673299075273739",
            "extra": "mean: 24.6808807335982 usec\nrounds: 53980"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 17627.239654185614,
            "unit": "iter/sec",
            "range": "stddev: 0.000009066887570021443",
            "extra": "mean: 56.73037977687837 usec\nrounds: 29046"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 3603.150858655325,
            "unit": "iter/sec",
            "range": "stddev: 0.000023230902414983942",
            "extra": "mean: 277.53486857144645 usec\nrounds: 6300"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 17567.110436690735,
            "unit": "iter/sec",
            "range": "stddev: 0.000010458543119463654",
            "extra": "mean: 56.924558173858586 usec\nrounds: 25037"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 1281.4808695212873,
            "unit": "iter/sec",
            "range": "stddev: 0.00008227264007509739",
            "extra": "mean: 780.3471934571775 usec\nrounds: 2476"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 7563.972760414257,
            "unit": "iter/sec",
            "range": "stddev: 0.00019452436169962228",
            "extra": "mean: 132.20565854407346 usec\nrounds: 13173"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 151196.0717899583,
            "unit": "iter/sec",
            "range": "stddev: 0.000001049023793694125",
            "extra": "mean: 6.6139284451067 usec\nrounds: 68954"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 39143.182395315685,
            "unit": "iter/sec",
            "range": "stddev: 0.000007091176131020913",
            "extra": "mean: 25.547232974079062 usec\nrounds: 65870"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 10273481.884856077,
            "unit": "iter/sec",
            "range": "stddev: 1.0199621875746312e-8",
            "extra": "mean: 97.33798250757408 nsec\nrounds: 215867"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 7953209.89473473,
            "unit": "iter/sec",
            "range": "stddev: 1.144949387683229e-8",
            "extra": "mean: 125.73539655504766 nsec\nrounds: 206037"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 4351344.989558656,
            "unit": "iter/sec",
            "range": "stddev: 7.255012643257025e-7",
            "extra": "mean: 229.81400059052248 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 39530.17337219519,
            "unit": "iter/sec",
            "range": "stddev: 0.00045548368511383363",
            "extra": "mean: 25.29713165142306 usec\nrounds: 96558"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 51128.591584360074,
            "unit": "iter/sec",
            "range": "stddev: 0.000001725099078842303",
            "extra": "mean: 19.558528193565454 usec\nrounds: 101601"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 5274.05529334572,
            "unit": "iter/sec",
            "range": "stddev: 0.000005089681199217647",
            "extra": "mean: 189.60741675607778 usec\nrounds: 9322"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 10731.05112309623,
            "unit": "iter/sec",
            "range": "stddev: 0.000004683743735049817",
            "extra": "mean: 93.1875161649095 usec\nrounds: 21590"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1977.2169024241625,
            "unit": "iter/sec",
            "range": "stddev: 0.000007886665389022913",
            "extra": "mean: 505.7614057284015 usec\nrounds: 4015"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 625.9901813843829,
            "unit": "iter/sec",
            "range": "stddev: 0.000014411322398235282",
            "extra": "mean: 1.5974691452643728 msec\nrounds: 1246"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 3366.5325224311855,
            "unit": "iter/sec",
            "range": "stddev: 0.000016069987333415287",
            "extra": "mean: 297.0415385376515 usec\nrounds: 6072"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 6125.302698355503,
            "unit": "iter/sec",
            "range": "stddev: 0.0000039111943943963036",
            "extra": "mean: 163.25723792694785 usec\nrounds: 11886"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 583.392982870751,
            "unit": "iter/sec",
            "range": "stddev: 0.000020789644080264343",
            "extra": "mean: 1.7141104356093138 msec\nrounds: 1157"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 599.4028014958175,
            "unit": "iter/sec",
            "range": "stddev: 0.000015034434163747595",
            "extra": "mean: 1.6683272041847104 msec\nrounds: 1195"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1489.4803112345392,
            "unit": "iter/sec",
            "range": "stddev: 0.00004311251435800601",
            "extra": "mean: 671.3751047646687 usec\nrounds: 3064"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 468.5255066696527,
            "unit": "iter/sec",
            "range": "stddev: 0.00009921047106607984",
            "extra": "mean: 2.1343555169667177 msec\nrounds: 1002"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 557.7556123257714,
            "unit": "iter/sec",
            "range": "stddev: 0.0001130633316425466",
            "extra": "mean: 1.7928999330551325 msec\nrounds: 956"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 556.870090636483,
            "unit": "iter/sec",
            "range": "stddev: 0.000037906081805812496",
            "extra": "mean: 1.7957509602590347 msec\nrounds: 1082"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 550.0794460912276,
            "unit": "iter/sec",
            "range": "stddev: 0.00009264741402945479",
            "extra": "mean: 1.8179192244062787 msec\nrounds: 967"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 257.24913142463663,
            "unit": "iter/sec",
            "range": "stddev: 0.00005142030060613178",
            "extra": "mean: 3.8872823183582206 msec\nrounds: 512"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1449.9200697633678,
            "unit": "iter/sec",
            "range": "stddev: 0.000023662737825583716",
            "extra": "mean: 689.693191268953 usec\nrounds: 2886"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 1306.5259734094022,
            "unit": "iter/sec",
            "range": "stddev: 0.000030100802277903758",
            "extra": "mean: 765.3885344433548 usec\nrounds: 2584"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 2510.1231922493293,
            "unit": "iter/sec",
            "range": "stddev: 0.00002179706560953094",
            "extra": "mean: 398.3868214467581 usec\nrounds: 3982"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 1229.9576318679806,
            "unit": "iter/sec",
            "range": "stddev: 0.00004809571338106409",
            "extra": "mean: 813.0361356279112 usec\nrounds: 2411"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 694.2973635559678,
            "unit": "iter/sec",
            "range": "stddev: 0.000020799482354666096",
            "extra": "mean: 1.4403050515391873 msec\nrounds: 1494"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 2217.642642068405,
            "unit": "iter/sec",
            "range": "stddev: 0.000008557959795753438",
            "extra": "mean: 450.9292800517651 usec\nrounds: 4567"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 46829.28773715947,
            "unit": "iter/sec",
            "range": "stddev: 0.0005604131620818158",
            "extra": "mean: 21.354157799980605 usec\nrounds: 77782"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 3787.153553705575,
            "unit": "iter/sec",
            "range": "stddev: 0.000020530491685225722",
            "extra": "mean: 264.0505556004036 usec\nrounds: 7410"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 75.19297571968883,
            "unit": "iter/sec",
            "range": "stddev: 0.0000662551884245784",
            "extra": "mean: 13.299114583892655 msec\nrounds: 149"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "elfenlieds7",
            "username": "elfenlieds7",
            "email": "elfenliedsp@gmail.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "32f2482cdf68ce64a3f24d61468ad4142f434a46",
          "message": "Merge pull request #4640 from nexi-lab/test/prove-cohost-seam\n\ntest(managed_agent): prove the in-process co-host seam",
          "timestamp": "2026-08-11T09:50:32Z",
          "url": "https://github.com/nexi-lab/nexus/commit/32f2482cdf68ce64a3f24d61468ad4142f434a46"
        },
        "date": 1786443089858,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 102.54200555069531,
            "unit": "iter/sec",
            "range": "stddev: 0.020324836895616028",
            "extra": "mean: 9.752101049999595 msec\nrounds: 100"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 667.6620569200147,
            "unit": "iter/sec",
            "range": "stddev: 0.00022181200673431598",
            "extra": "mean: 1.4977637109005266 msec\nrounds: 211"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 2255.0167279564803,
            "unit": "iter/sec",
            "range": "stddev: 0.00003329022623172237",
            "extra": "mean: 443.45569041796443 usec\nrounds: 814"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 330.13953624390774,
            "unit": "iter/sec",
            "range": "stddev: 0.004561134458388719",
            "extra": "mean: 3.0290222473118096 msec\nrounds: 186"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 138.37048273846278,
            "unit": "iter/sec",
            "range": "stddev: 0.004383912963740201",
            "extra": "mean: 7.226974859154917 msec\nrounds: 71"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 44849.05581542104,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014844552878355786",
            "extra": "mean: 22.297013433583967 usec\nrounds: 18759"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 3181.0575061133572,
            "unit": "iter/sec",
            "range": "stddev: 0.000006126817429818826",
            "extra": "mean: 314.3608683836114 usec\nrounds: 1398"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 57601.06091228053,
            "unit": "iter/sec",
            "range": "stddev: 0.0000015959948701697853",
            "extra": "mean: 17.360791349362113 usec\nrounds: 13525"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 118699.79672052419,
            "unit": "iter/sec",
            "range": "stddev: 0.000001215452597220258",
            "extra": "mean: 8.424614259066304 usec\nrounds: 13942"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 95024.27527660684,
            "unit": "iter/sec",
            "range": "stddev: 0.0000011870943697793273",
            "extra": "mean: 10.523626695274368 usec\nrounds: 25660"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 77869.44349117296,
            "unit": "iter/sec",
            "range": "stddev: 0.000001478217195336549",
            "extra": "mean: 12.84200779107092 usec\nrounds: 22205"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 230.36727899437665,
            "unit": "iter/sec",
            "range": "stddev: 0.006495244582638085",
            "extra": "mean: 4.340894264000099 msec\nrounds: 125"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 41759.58330208406,
            "unit": "iter/sec",
            "range": "stddev: 0.00000396373441597079",
            "extra": "mean: 23.946599101962157 usec\nrounds: 53894"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 17592.784069329704,
            "unit": "iter/sec",
            "range": "stddev: 0.00000831638429291417",
            "extra": "mean: 56.84148660378008 usec\nrounds: 27881"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 3628.150223890517,
            "unit": "iter/sec",
            "range": "stddev: 0.000016992243950305",
            "extra": "mean: 275.6225454544949 usec\nrounds: 6413"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 16646.791057319988,
            "unit": "iter/sec",
            "range": "stddev: 0.000010349518119068167",
            "extra": "mean: 60.07163762413395 usec\nrounds: 25675"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 1072.0725111869183,
            "unit": "iter/sec",
            "range": "stddev: 0.00011101496342469712",
            "extra": "mean: 932.7727271850996 usec\nrounds: 2071"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 7423.365112247996,
            "unit": "iter/sec",
            "range": "stddev: 0.0001967509726792579",
            "extra": "mean: 134.70979601287223 usec\nrounds: 11236"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 154508.9051502441,
            "unit": "iter/sec",
            "range": "stddev: 8.885487081617346e-7",
            "extra": "mean: 6.472118866078317 usec\nrounds: 78660"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 41482.315729856775,
            "unit": "iter/sec",
            "range": "stddev: 0.00000512687607502593",
            "extra": "mean: 24.106658039831967 usec\nrounds: 64759"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 10628039.064562522,
            "unit": "iter/sec",
            "range": "stddev: 8.567738464931424e-9",
            "extra": "mean: 94.09073432316771 nsec\nrounds: 230389"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 8812180.015464008,
            "unit": "iter/sec",
            "range": "stddev: 9.659556640203144e-9",
            "extra": "mean: 113.47929777253248 nsec\nrounds: 199921"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 4555725.635381736,
            "unit": "iter/sec",
            "range": "stddev: 1.40464594997064e-7",
            "extra": "mean: 219.50400002879178 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 39352.895223982254,
            "unit": "iter/sec",
            "range": "stddev: 0.00045204759071938846",
            "extra": "mean: 25.411090958069707 usec\nrounds: 92735"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 52768.071868670995,
            "unit": "iter/sec",
            "range": "stddev: 9.612841090246002e-7",
            "extra": "mean: 18.950853510221044 usec\nrounds: 101420"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 5309.500620698037,
            "unit": "iter/sec",
            "range": "stddev: 0.0000031783912243493548",
            "extra": "mean: 188.3416297385291 usec\nrounds: 10552"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 10886.565588324194,
            "unit": "iter/sec",
            "range": "stddev: 0.0000023415719142019963",
            "extra": "mean: 91.85633355963948 usec\nrounds: 20626"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1996.835523813327,
            "unit": "iter/sec",
            "range": "stddev: 0.000007559683492654741",
            "extra": "mean: 500.7923727690475 usec\nrounds: 3922"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 625.7984877177927,
            "unit": "iter/sec",
            "range": "stddev: 0.00001598658586230905",
            "extra": "mean: 1.5979584796487327 msec\nrounds: 1253"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 3416.175061565057,
            "unit": "iter/sec",
            "range": "stddev: 0.00003250448169437204",
            "extra": "mean: 292.7250454026406 usec\nrounds: 6123"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 6116.092907091974,
            "unit": "iter/sec",
            "range": "stddev: 0.000003763663778631431",
            "extra": "mean: 163.5030754422387 usec\nrounds: 10286"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 582.0325640970356,
            "unit": "iter/sec",
            "range": "stddev: 0.00004461412403989863",
            "extra": "mean: 1.718116926243463 msec\nrounds: 1166"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 599.7193733587756,
            "unit": "iter/sec",
            "range": "stddev: 0.000011459839492267526",
            "extra": "mean: 1.6674465498745208 msec\nrounds: 1193"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1549.7049082640262,
            "unit": "iter/sec",
            "range": "stddev: 0.000008689329865813193",
            "extra": "mean: 645.2841406562984 usec\nrounds: 2986"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 480.84449345326703,
            "unit": "iter/sec",
            "range": "stddev: 0.00016068721754588857",
            "extra": "mean: 2.0796744344898888 msec\nrounds: 1038"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 555.57094664809,
            "unit": "iter/sec",
            "range": "stddev: 0.00013043936619259634",
            "extra": "mean: 1.7999501342416677 msec\nrounds: 1028"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 559.3266741801851,
            "unit": "iter/sec",
            "range": "stddev: 0.00008570062995924312",
            "extra": "mean: 1.7878639552918114 msec\nrounds: 1096"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 568.7389725185125,
            "unit": "iter/sec",
            "range": "stddev: 0.00002479516662181511",
            "extra": "mean: 1.7582758494142934 msec\nrounds: 1109"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 257.26709569185107,
            "unit": "iter/sec",
            "range": "stddev: 0.000041188754652684905",
            "extra": "mean: 3.8870108799213803 msec\nrounds: 508"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1442.0810903139895,
            "unit": "iter/sec",
            "range": "stddev: 0.00002148966706128125",
            "extra": "mean: 693.4422805462807 usec\nrounds: 2488"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 1392.907255482841,
            "unit": "iter/sec",
            "range": "stddev: 0.00005417337773064439",
            "extra": "mean: 717.9228883069873 usec\nrounds: 3053"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 2523.928508327916,
            "unit": "iter/sec",
            "range": "stddev: 0.00002153262187381277",
            "extra": "mean: 396.20773595623456 usec\nrounds: 5287"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 1349.3367177443206,
            "unit": "iter/sec",
            "range": "stddev: 0.000046125939131429276",
            "extra": "mean: 741.1048605211715 usec\nrounds: 2495"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 722.4031666569139,
            "unit": "iter/sec",
            "range": "stddev: 0.000028086211478703864",
            "extra": "mean: 1.3842685721156636 msec\nrounds: 1456"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 2418.4564112046546,
            "unit": "iter/sec",
            "range": "stddev: 0.00004216021596166678",
            "extra": "mean: 413.4868817014945 usec\nrounds: 4514"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 49558.08466180837,
            "unit": "iter/sec",
            "range": "stddev: 0.00043087296975359693",
            "extra": "mean: 20.17834237993955 usec\nrounds: 66692"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 3245.8247493410204,
            "unit": "iter/sec",
            "range": "stddev: 0.0019545567906348745",
            "extra": "mean: 308.0881061748709 usec\nrounds: 6753"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 73.85755255334118,
            "unit": "iter/sec",
            "range": "stddev: 0.00022079634774967505",
            "extra": "mean: 13.539576731543914 msec\nrounds: 149"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "oliverfeng",
            "username": "windoliver",
            "email": "oliverfengpet@gmail.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "f229e4bce6056f4dde4d9572f411cd53291d9a4e",
          "message": "Merge pull request #4648 from nexi-lab/fix/p12-adoption-residuals-4643-4647\n\nfix: P12 adoption residuals — stats vector_backend, score attribution, oplog snapshot_hash, CI embedder, title-arm stem fallback (#4643–#4647)",
          "timestamp": "2026-08-12T07:19:35Z",
          "url": "https://github.com/nexi-lab/nexus/commit/f229e4bce6056f4dde4d9572f411cd53291d9a4e"
        },
        "date": 1786530088776,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 235.89733576239277,
            "unit": "iter/sec",
            "range": "stddev: 0.00027067417945771013",
            "extra": "mean: 4.239132234232812 msec\nrounds: 111"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 428.1359812831886,
            "unit": "iter/sec",
            "range": "stddev: 0.000254390643220329",
            "extra": "mean: 2.335706513156983 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1395.8122053991576,
            "unit": "iter/sec",
            "range": "stddev: 0.000025703830824556103",
            "extra": "mean: 716.428754621781 usec\nrounds: 595"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 443.77651981433416,
            "unit": "iter/sec",
            "range": "stddev: 0.0004703331013532928",
            "extra": "mean: 2.2533864577115907 msec\nrounds: 201"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 173.42930312929474,
            "unit": "iter/sec",
            "range": "stddev: 0.00032639143887246575",
            "extra": "mean: 5.766038275864383 msec\nrounds: 87"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25342.315517843097,
            "unit": "iter/sec",
            "range": "stddev: 0.000002929576851373533",
            "extra": "mean: 39.45969338499936 usec\nrounds: 11066"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1910.39336299388,
            "unit": "iter/sec",
            "range": "stddev: 0.00001251207007390228",
            "extra": "mean: 523.4524048140779 usec\nrounds: 914"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 32700.446073209332,
            "unit": "iter/sec",
            "range": "stddev: 0.0000035482935970558084",
            "extra": "mean: 30.580622593380323 usec\nrounds: 9038"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 61655.714981786965,
            "unit": "iter/sec",
            "range": "stddev: 0.0000023961286489330877",
            "extra": "mean: 16.21909664489981 usec\nrounds: 10730"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 50546.40221881743,
            "unit": "iter/sec",
            "range": "stddev: 0.000002716566543836827",
            "extra": "mean: 19.783801736688584 usec\nrounds: 14627"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 41377.40497802526,
            "unit": "iter/sec",
            "range": "stddev: 0.0000030355864542607832",
            "extra": "mean: 24.16777950504824 usec\nrounds: 13252"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 140.90626376367842,
            "unit": "iter/sec",
            "range": "stddev: 0.009018433074546814",
            "extra": "mean: 7.096916583333403 msec\nrounds: 84"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 17018.151225197133,
            "unit": "iter/sec",
            "range": "stddev: 0.000009995382823178199",
            "extra": "mean: 58.760789392880504 usec\nrounds: 25417"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7792.599020182526,
            "unit": "iter/sec",
            "range": "stddev: 0.000022576364839366846",
            "extra": "mean: 128.32689034942504 usec\nrounds: 13160"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1622.2004695537726,
            "unit": "iter/sec",
            "range": "stddev: 0.000037899618101265236",
            "extra": "mean: 616.4466222075964 usec\nrounds: 2954"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7825.546753236477,
            "unit": "iter/sec",
            "range": "stddev: 0.000024225773752952595",
            "extra": "mean: 127.78659837236569 usec\nrounds: 10689"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 565.8512193608609,
            "unit": "iter/sec",
            "range": "stddev: 0.00023475758837568876",
            "extra": "mean: 1.7672489972355592 msec\nrounds: 1085"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4166.221194833136,
            "unit": "iter/sec",
            "range": "stddev: 0.0004550386414563046",
            "extra": "mean: 240.02566192121054 usec\nrounds: 7797"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 80945.96143937447,
            "unit": "iter/sec",
            "range": "stddev: 0.0000021741253118543717",
            "extra": "mean: 12.353920840744637 usec\nrounds: 54662"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 17308.359045015823,
            "unit": "iter/sec",
            "range": "stddev: 0.000011995265663513912",
            "extra": "mean: 57.77555211324112 usec\nrounds: 29340"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5151604.9449032955,
            "unit": "iter/sec",
            "range": "stddev: 1.686280347792634e-8",
            "extra": "mean: 194.11426355379658 nsec\nrounds: 110042"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4313475.115419449,
            "unit": "iter/sec",
            "range": "stddev: 1.5991173572376e-8",
            "extra": "mean: 231.8316376569054 nsec\nrounds: 99966"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2421976.039523832,
            "unit": "iter/sec",
            "range": "stddev: 4.321357058353124e-7",
            "extra": "mean: 412.8860003902446 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 21588.55784914199,
            "unit": "iter/sec",
            "range": "stddev: 0.0003805181639511656",
            "extra": "mean: 46.320833794821716 usec\nrounds: 57447"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 38812.48752117228,
            "unit": "iter/sec",
            "range": "stddev: 0.000002086145887375391",
            "extra": "mean: 25.764903613933484 usec\nrounds: 74876"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3801.7558254788023,
            "unit": "iter/sec",
            "range": "stddev: 0.000020232848745871625",
            "extra": "mean: 263.0363563325526 usec\nrounds: 7232"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7857.626532914729,
            "unit": "iter/sec",
            "range": "stddev: 0.000005146222886069719",
            "extra": "mean: 127.26489300695452 usec\nrounds: 15758"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1256.7688833367135,
            "unit": "iter/sec",
            "range": "stddev: 0.000015363307370373374",
            "extra": "mean: 795.6912470214938 usec\nrounds: 2518"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 414.4326579068578,
            "unit": "iter/sec",
            "range": "stddev: 0.000020355652383788254",
            "extra": "mean: 2.4129372551155135 msec\nrounds: 831"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1694.5269583295583,
            "unit": "iter/sec",
            "range": "stddev: 0.00004266847368157001",
            "extra": "mean: 590.1351967783306 usec\nrounds: 3166"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 3946.4386748848788,
            "unit": "iter/sec",
            "range": "stddev: 0.000007788320740665642",
            "extra": "mean: 253.39301643377772 usec\nrounds: 7302"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 383.01090309674663,
            "unit": "iter/sec",
            "range": "stddev: 0.000028432191585650517",
            "extra": "mean: 2.6108917315792572 msec\nrounds: 760"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 384.02118683294776,
            "unit": "iter/sec",
            "range": "stddev: 0.000023462500463771526",
            "extra": "mean: 2.604022992187168 msec\nrounds: 768"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 878.2896249342492,
            "unit": "iter/sec",
            "range": "stddev: 0.00001679953837423534",
            "extra": "mean: 1.1385765829521923 msec\nrounds: 1748"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 345.9928508105809,
            "unit": "iter/sec",
            "range": "stddev: 0.00015202545607850433",
            "extra": "mean: 2.890233129549447 msec\nrounds: 687"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 344.626624794452,
            "unit": "iter/sec",
            "range": "stddev: 0.000025491567665121954",
            "extra": "mean: 2.9016910710147155 msec\nrounds: 690"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 320.2298953825475,
            "unit": "iter/sec",
            "range": "stddev: 0.00006190079117868969",
            "extra": "mean: 3.1227565396584764 msec\nrounds: 643"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 320.7532007831435,
            "unit": "iter/sec",
            "range": "stddev: 0.00003399786615965861",
            "extra": "mean: 3.1176617959179316 msec\nrounds: 637"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 164.62574946108907,
            "unit": "iter/sec",
            "range": "stddev: 0.00004247781053213299",
            "extra": "mean: 6.074383887536135 msec\nrounds: 329"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 775.5672619601298,
            "unit": "iter/sec",
            "range": "stddev: 0.000041079940279033885",
            "extra": "mean: 1.2893788186374062 msec\nrounds: 1395"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 889.6196883307848,
            "unit": "iter/sec",
            "range": "stddev: 0.00006631409713449549",
            "extra": "mean: 1.1240758417524734 msec\nrounds: 1643"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1531.4130064624183,
            "unit": "iter/sec",
            "range": "stddev: 0.000019904829247706822",
            "extra": "mean: 652.9917114325752 usec\nrounds: 2904"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 794.2350800768146,
            "unit": "iter/sec",
            "range": "stddev: 0.000018912093373380816",
            "extra": "mean: 1.2590730692772785 msec\nrounds: 1660"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 454.2883359703784,
            "unit": "iter/sec",
            "range": "stddev: 0.00008618614125910087",
            "extra": "mean: 2.201245158240656 msec\nrounds: 910"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1481.3042258483872,
            "unit": "iter/sec",
            "range": "stddev: 0.00003500134895815594",
            "extra": "mean: 675.0807717619722 usec\nrounds: 2734"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28661.01207366167,
            "unit": "iter/sec",
            "range": "stddev: 0.0005128198492634795",
            "extra": "mean: 34.89060321491439 usec\nrounds: 43797"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2092.1008069346967,
            "unit": "iter/sec",
            "range": "stddev: 0.000032745742574116535",
            "extra": "mean: 477.9884395079315 usec\nrounds: 3984"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 42.603579462624076,
            "unit": "iter/sec",
            "range": "stddev: 0.0002872953304408475",
            "extra": "mean: 23.472206152942977 msec\nrounds: 85"
          }
        ]
      }
    ]
  }
}