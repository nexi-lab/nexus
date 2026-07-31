window.BENCHMARK_DATA = {
  "lastUpdate": 1785497853854,
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
      }
    ]
  }
}