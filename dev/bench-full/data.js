window.BENCHMARK_DATA = {
  "lastUpdate": 1788528804635,
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
          "id": "0209ba42868b8f3b21bb75a26c0a296b397700dc",
          "message": "Merge pull request #4651 from nexi-lab/ci/fuse-winfsp-vendor-msi\n\nci(fuse-windows-e2e): install WinFsp from cached vendor .msi, drop live Chocolatey",
          "timestamp": "2026-08-13T08:21:27Z",
          "url": "https://github.com/nexi-lab/nexus/commit/0209ba42868b8f3b21bb75a26c0a296b397700dc"
        },
        "date": 1786616567526,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 243.43890749201762,
            "unit": "iter/sec",
            "range": "stddev: 0.00022947701014055705",
            "extra": "mean: 4.1078068017241245 msec\nrounds: 116"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 424.3292878452951,
            "unit": "iter/sec",
            "range": "stddev: 0.00025114148715056606",
            "extra": "mean: 2.356660331126111 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1368.630060396114,
            "unit": "iter/sec",
            "range": "stddev: 0.000020754736718649686",
            "extra": "mean: 730.6576327211286 usec\nrounds: 599"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 440.4166928549593,
            "unit": "iter/sec",
            "range": "stddev: 0.00024810289163998514",
            "extra": "mean: 2.2705769699999223 msec\nrounds: 200"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 177.7255518428862,
            "unit": "iter/sec",
            "range": "stddev: 0.00036114415303222186",
            "extra": "mean: 5.626652946808823 msec\nrounds: 94"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25493.93727756009,
            "unit": "iter/sec",
            "range": "stddev: 0.000003014859410620255",
            "extra": "mean: 39.225012171039026 usec\nrounds: 11256"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1900.520616164973,
            "unit": "iter/sec",
            "range": "stddev: 0.00001831064314743423",
            "extra": "mean: 526.1716139748498 usec\nrounds: 873"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 32635.64460205778,
            "unit": "iter/sec",
            "range": "stddev: 0.000003356734996918406",
            "extra": "mean: 30.64134360431621 usec\nrounds: 9022"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 60220.82462598691,
            "unit": "iter/sec",
            "range": "stddev: 0.0000023736482521387645",
            "extra": "mean: 16.605551421965632 usec\nrounds: 10161"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 51138.77922372264,
            "unit": "iter/sec",
            "range": "stddev: 0.000002731164560891701",
            "extra": "mean: 19.554631830869212 usec\nrounds: 15042"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 40706.344090725666,
            "unit": "iter/sec",
            "range": "stddev: 0.0000032925076992793615",
            "extra": "mean: 24.566195327470716 usec\nrounds: 13055"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 138.52476560877633,
            "unit": "iter/sec",
            "range": "stddev: 0.00943624331706329",
            "extra": "mean: 7.218925768293408 msec\nrounds: 82"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 16738.173351958896,
            "unit": "iter/sec",
            "range": "stddev: 0.000010575065431623284",
            "extra": "mean: 59.743675667152075 usec\nrounds: 25369"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7575.077589475735,
            "unit": "iter/sec",
            "range": "stddev: 0.000022488828561597868",
            "extra": "mean: 132.01184914453253 usec\nrounds: 13092"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1576.8341557966448,
            "unit": "iter/sec",
            "range": "stddev: 0.000038687788557384305",
            "extra": "mean: 634.1821023624276 usec\nrounds: 2921"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7504.0251810561485,
            "unit": "iter/sec",
            "range": "stddev: 0.000025115124719603578",
            "extra": "mean: 133.26181294333767 usec\nrounds: 11419"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 547.7466917642682,
            "unit": "iter/sec",
            "range": "stddev: 0.0002462809541605844",
            "extra": "mean: 1.825661414364811 msec\nrounds: 1086"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4021.74993987271,
            "unit": "iter/sec",
            "range": "stddev: 0.0004828157158716287",
            "extra": "mean: 248.64798034450902 usec\nrounds: 7835"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 80617.3036987261,
            "unit": "iter/sec",
            "range": "stddev: 0.0000020761280911760468",
            "extra": "mean: 12.404284863421967 usec\nrounds: 32742"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 16615.90930701413,
            "unit": "iter/sec",
            "range": "stddev: 0.000014639244204086111",
            "extra": "mean: 60.18328467752689 usec\nrounds: 22875"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5218913.718395475,
            "unit": "iter/sec",
            "range": "stddev: 1.7640818722091512e-8",
            "extra": "mean: 191.61075540973764 nsec\nrounds: 109927"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4404471.935481839,
            "unit": "iter/sec",
            "range": "stddev: 1.646943398242433e-8",
            "extra": "mean: 227.04197339620518 nsec\nrounds: 102691"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2341657.1428042003,
            "unit": "iter/sec",
            "range": "stddev: 9.207113419292941e-7",
            "extra": "mean: 427.0480002048771 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 21239.79476110147,
            "unit": "iter/sec",
            "range": "stddev: 0.0003929720712118799",
            "extra": "mean: 47.0814342251272 usec\nrounds: 59202"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 38922.31165907923,
            "unit": "iter/sec",
            "range": "stddev: 0.0000021365903602488292",
            "extra": "mean: 25.692204737452553 usec\nrounds: 69997"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3835.0975865939254,
            "unit": "iter/sec",
            "range": "stddev: 0.000008624495114018632",
            "extra": "mean: 260.7495578458363 usec\nrounds: 7762"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8161.094059693384,
            "unit": "iter/sec",
            "range": "stddev: 0.000004913580678776962",
            "extra": "mean: 122.53259093518773 usec\nrounds: 15643"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1265.4599053641664,
            "unit": "iter/sec",
            "range": "stddev: 0.000013375698117730157",
            "extra": "mean: 790.2265380049524 usec\nrounds: 2526"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 412.87604840326225,
            "unit": "iter/sec",
            "range": "stddev: 0.00011177746474925234",
            "extra": "mean: 2.4220344189675176 msec\nrounds: 833"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1743.0337454629703,
            "unit": "iter/sec",
            "range": "stddev: 0.00003915211187199112",
            "extra": "mean: 573.7123578949346 usec\nrounds: 3230"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 3974.4159797852676,
            "unit": "iter/sec",
            "range": "stddev: 0.000008392772681019206",
            "extra": "mean: 251.60929431801162 usec\nrounds: 7339"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 384.73159829754195,
            "unit": "iter/sec",
            "range": "stddev: 0.00003234474620720965",
            "extra": "mean: 2.5992146328116896 msec\nrounds: 768"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 385.9445965969234,
            "unit": "iter/sec",
            "range": "stddev: 0.00003510464138270354",
            "extra": "mean: 2.591045473411278 msec\nrounds: 771"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 814.1816668689016,
            "unit": "iter/sec",
            "range": "stddev: 0.000029695052867332277",
            "extra": "mean: 1.2282271152649504 msec\nrounds: 1605"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 317.2934643890376,
            "unit": "iter/sec",
            "range": "stddev: 0.00003470173118387836",
            "extra": "mean: 3.151656470219277 msec\nrounds: 638"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 346.6549381376013,
            "unit": "iter/sec",
            "range": "stddev: 0.00003258255767288232",
            "extra": "mean: 2.8847129810770493 msec\nrounds: 687"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 322.39792267863373,
            "unit": "iter/sec",
            "range": "stddev: 0.00003170231466627443",
            "extra": "mean: 3.101756958269238 msec\nrounds: 647"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 319.80317355208365,
            "unit": "iter/sec",
            "range": "stddev: 0.00003926988660895132",
            "extra": "mean: 3.1269233162789063 msec\nrounds: 645"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 149.7059590691454,
            "unit": "iter/sec",
            "range": "stddev: 0.00008220500386954445",
            "extra": "mean: 6.679760820597163 msec\nrounds: 301"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 713.1103149137339,
            "unit": "iter/sec",
            "range": "stddev: 0.000026126905288513966",
            "extra": "mean: 1.4023075800284444 msec\nrounds: 1412"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 786.4580236701369,
            "unit": "iter/sec",
            "range": "stddev: 0.00003842422229083992",
            "extra": "mean: 1.27152367946268 msec\nrounds: 1563"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1421.1623113950516,
            "unit": "iter/sec",
            "range": "stddev: 0.000040205927171765076",
            "extra": "mean: 703.6493945708233 usec\nrounds: 2689"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 728.2019490773763,
            "unit": "iter/sec",
            "range": "stddev: 0.00002288677469813933",
            "extra": "mean: 1.3732454318022478 msec\nrounds: 1459"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 432.15510824416674,
            "unit": "iter/sec",
            "range": "stddev: 0.000036230349303854156",
            "extra": "mean: 2.3139839861270417 msec\nrounds: 865"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1339.223342880262,
            "unit": "iter/sec",
            "range": "stddev: 0.00001526001520536343",
            "extra": "mean: 746.7014410376871 usec\nrounds: 2544"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28560.107558264837,
            "unit": "iter/sec",
            "range": "stddev: 0.0005261688949068859",
            "extra": "mean: 35.0138737383927 usec\nrounds: 42903"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2084.737965984687,
            "unit": "iter/sec",
            "range": "stddev: 0.000021562026138906995",
            "extra": "mean: 479.6765906873427 usec\nrounds: 4102"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 43.56994584038274,
            "unit": "iter/sec",
            "range": "stddev: 0.00010665155244980562",
            "extra": "mean: 22.95160071264425 msec\nrounds: 87"
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
          "id": "8f52eeee85ae3873ba77c45413024367fd6de659",
          "message": "Merge pull request #4654 from nexi-lab/docs/runbook-arch-mermaid\n\ndocs(runbook): render cross-org architecture as a mermaid diagram",
          "timestamp": "2026-08-14T09:56:54Z",
          "url": "https://github.com/nexi-lab/nexus/commit/8f52eeee85ae3873ba77c45413024367fd6de659"
        },
        "date": 1786702672588,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 177.3957092672236,
            "unit": "iter/sec",
            "range": "stddev: 0.004777288736190762",
            "extra": "mean: 5.637114923076465 msec\nrounds: 117"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 513.8993662291592,
            "unit": "iter/sec",
            "range": "stddev: 0.0002909702909742049",
            "extra": "mean: 1.945906272151497 msec\nrounds: 158"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1931.1843256116913,
            "unit": "iter/sec",
            "range": "stddev: 0.00003966764230501306",
            "extra": "mean: 517.8169617150635 usec\nrounds: 653"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 161.04962430610036,
            "unit": "iter/sec",
            "range": "stddev: 0.028262469015507892",
            "extra": "mean: 6.209266269999745 msec\nrounds: 200"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 132.50412687664974,
            "unit": "iter/sec",
            "range": "stddev: 0.006078083534329708",
            "extra": "mean: 7.546934752688243 msec\nrounds: 93"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 33646.746232723126,
            "unit": "iter/sec",
            "range": "stddev: 0.0000038265404527761646",
            "extra": "mean: 29.720555832749454 usec\nrounds: 12824"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 2463.768248044122,
            "unit": "iter/sec",
            "range": "stddev: 0.00004195675132259899",
            "extra": "mean: 405.8823311786148 usec\nrounds: 1238"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 42502.83216715192,
            "unit": "iter/sec",
            "range": "stddev: 0.0000029130225813733852",
            "extra": "mean: 23.527843887373802 usec\nrounds: 10691"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 93818.52224211839,
            "unit": "iter/sec",
            "range": "stddev: 0.0000024518482113804513",
            "extra": "mean: 10.658876052420547 usec\nrounds: 7600"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 75882.59349030555,
            "unit": "iter/sec",
            "range": "stddev: 0.000001929959935141579",
            "extra": "mean: 13.178252798222507 usec\nrounds: 17690"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 57679.25016859247,
            "unit": "iter/sec",
            "range": "stddev: 0.000003478382106783988",
            "extra": "mean: 17.337257281900666 usec\nrounds: 16857"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 182.21542598346963,
            "unit": "iter/sec",
            "range": "stddev: 0.007305438914532102",
            "extra": "mean: 5.488009561225177 msec\nrounds: 98"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 32028.118582160554,
            "unit": "iter/sec",
            "range": "stddev: 0.000006334369702008926",
            "extra": "mean: 31.222564554790715 usec\nrounds: 38905"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 13824.553857477791,
            "unit": "iter/sec",
            "range": "stddev: 0.00001346669280049201",
            "extra": "mean: 72.33506486425192 usec\nrounds: 16496"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 2858.022036591868,
            "unit": "iter/sec",
            "range": "stddev: 0.00003224794373477514",
            "extra": "mean: 349.8923336478116 usec\nrounds: 5299"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 13861.413805777589,
            "unit": "iter/sec",
            "range": "stddev: 0.00001359761631392277",
            "extra": "mean: 72.14271314685007 usec\nrounds: 20948"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 935.7560223569462,
            "unit": "iter/sec",
            "range": "stddev: 0.0001546001032460969",
            "extra": "mean: 1.0686546237567764 msec\nrounds: 1709"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 5760.692339921493,
            "unit": "iter/sec",
            "range": "stddev: 0.0003995727222781019",
            "extra": "mean: 173.59024592756296 usec\nrounds: 9393"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 126156.29657920767,
            "unit": "iter/sec",
            "range": "stddev: 0.000001232588995641147",
            "extra": "mean: 7.9266752997314445 usec\nrounds: 52627"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 30710.597105627523,
            "unit": "iter/sec",
            "range": "stddev: 0.00001066814929188041",
            "extra": "mean: 32.5620500493869 usec\nrounds: 50550"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 8220916.619096096,
            "unit": "iter/sec",
            "range": "stddev: 1.141499226877366e-8",
            "extra": "mean: 121.64093693361795 nsec\nrounds: 179938"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 6715597.680919842,
            "unit": "iter/sec",
            "range": "stddev: 2.00938011398447e-8",
            "extra": "mean: 148.90707387685993 nsec\nrounds: 164002"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2908194.124403679,
            "unit": "iter/sec",
            "range": "stddev: 7.792981523857632e-7",
            "extra": "mean: 343.8560003985458 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 33867.715141518755,
            "unit": "iter/sec",
            "range": "stddev: 0.0003151079669688862",
            "extra": "mean: 29.526644942578088 usec\nrounds: 75523"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 41762.932212914886,
            "unit": "iter/sec",
            "range": "stddev: 0.000001988945857223571",
            "extra": "mean: 23.944678857840284 usec\nrounds: 81917"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 4090.586421028476,
            "unit": "iter/sec",
            "range": "stddev: 0.000019909102115278332",
            "extra": "mean: 244.4637264865743 usec\nrounds: 8442"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 9181.11855824233,
            "unit": "iter/sec",
            "range": "stddev: 0.000006235125811593476",
            "extra": "mean: 108.91919036403817 usec\nrounds: 18265"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1693.681002864936,
            "unit": "iter/sec",
            "range": "stddev: 0.000038871498384569014",
            "extra": "mean: 590.4299560002479 usec\nrounds: 3500"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 557.1766991930359,
            "unit": "iter/sec",
            "range": "stddev: 0.00011722256427067252",
            "extra": "mean: 1.7947627771375028 msec\nrounds: 1041"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 2648.5880499878035,
            "unit": "iter/sec",
            "range": "stddev: 0.00003857898529194387",
            "extra": "mean: 377.5596586281528 usec\nrounds: 5059"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 5048.344535242628,
            "unit": "iter/sec",
            "range": "stddev: 0.000013339581604256445",
            "extra": "mean: 198.08473708935145 usec\nrounds: 10863"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 518.9281469310228,
            "unit": "iter/sec",
            "range": "stddev: 0.00009239771798319425",
            "extra": "mean: 1.9270490643339926 msec\nrounds: 886"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 518.8894910559101,
            "unit": "iter/sec",
            "range": "stddev: 0.00011760363637653317",
            "extra": "mean: 1.9271926243197908 msec\nrounds: 1102"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1261.1684546629963,
            "unit": "iter/sec",
            "range": "stddev: 0.00004781834170467915",
            "extra": "mean: 792.9154874613602 usec\nrounds: 2273"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 475.88148518100803,
            "unit": "iter/sec",
            "range": "stddev: 0.00010779529306013682",
            "extra": "mean: 2.101363535123953 msec\nrounds: 968"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 475.5597964773055,
            "unit": "iter/sec",
            "range": "stddev: 0.00015532976628569386",
            "extra": "mean: 2.1027849860469057 msec\nrounds: 860"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 463.78875347444995,
            "unit": "iter/sec",
            "range": "stddev: 0.00011888023367145872",
            "extra": "mean: 2.1561540518362095 msec\nrounds: 926"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 456.8467292026964,
            "unit": "iter/sec",
            "range": "stddev: 0.00017196614750122446",
            "extra": "mean: 2.1889179369746876 msec\nrounds: 952"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 212.24021160417539,
            "unit": "iter/sec",
            "range": "stddev: 0.0004911435032256762",
            "extra": "mean: 4.71164249433083 msec\nrounds: 441"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1140.6682919011625,
            "unit": "iter/sec",
            "range": "stddev: 0.00008016843355379114",
            "extra": "mean: 876.679054813815 usec\nrounds: 2098"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 1138.0223551200268,
            "unit": "iter/sec",
            "range": "stddev: 0.0000682040739993302",
            "extra": "mean: 878.717360428768 usec\nrounds: 2239"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 2043.0163596773762,
            "unit": "iter/sec",
            "range": "stddev: 0.000033273514478877386",
            "extra": "mean: 489.47234086657807 usec\nrounds: 4248"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 1137.266762819938,
            "unit": "iter/sec",
            "range": "stddev: 0.00005437736672317869",
            "extra": "mean: 879.3011742648885 usec\nrounds: 2347"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 663.7557246949374,
            "unit": "iter/sec",
            "range": "stddev: 0.00010655178285765086",
            "extra": "mean: 1.5065783431993762 msec\nrounds: 1419"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 2092.7854964418234,
            "unit": "iter/sec",
            "range": "stddev: 0.000030686117562108225",
            "extra": "mean: 477.832057657228 usec\nrounds: 3833"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 35734.69321451881,
            "unit": "iter/sec",
            "range": "stddev: 0.0006321742038561279",
            "extra": "mean: 27.984009656859328 usec\nrounds: 60682"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 3000.596981716517,
            "unit": "iter/sec",
            "range": "stddev: 0.000026053901753600013",
            "extra": "mean: 333.2670152283969 usec\nrounds: 4925"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 55.27723110548462,
            "unit": "iter/sec",
            "range": "stddev: 0.0011826083924262177",
            "extra": "mean: 18.09063116949032 msec\nrounds: 118"
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
          "id": "1d5fc8c9f85603b129fcb9e47d3ab398fcfdabcf",
          "message": "Merge pull request #4655 from nexi-lab/docs/runbook-review-pass\n\ndocs(runbook): apply review comments — mermaid topology, prereqs table, trim to doc principles",
          "timestamp": "2026-08-14T11:01:41Z",
          "url": "https://github.com/nexi-lab/nexus/commit/1d5fc8c9f85603b129fcb9e47d3ab398fcfdabcf"
        },
        "date": 1786786808340,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 148.42249651013003,
            "unit": "iter/sec",
            "range": "stddev: 0.018696969424197515",
            "extra": "mean: 6.73752310810746 msec\nrounds: 111"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 478.63766805356823,
            "unit": "iter/sec",
            "range": "stddev: 0.0002718713706717012",
            "extra": "mean: 2.0892630621125328 msec\nrounds: 161"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1742.049655125004,
            "unit": "iter/sec",
            "range": "stddev: 0.00003447719839767042",
            "extra": "mean: 574.0364501425439 usec\nrounds: 702"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 355.37656183958825,
            "unit": "iter/sec",
            "range": "stddev: 0.002697282028675196",
            "extra": "mean: 2.8139165813962297 msec\nrounds: 86"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 126.80952936190046,
            "unit": "iter/sec",
            "range": "stddev: 0.007064146459577251",
            "extra": "mean: 7.885842688889017 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 30435.256797315637,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018593726503246436",
            "extra": "mean: 32.856630934955646 usec\nrounds: 12342"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 2236.015845850621,
            "unit": "iter/sec",
            "range": "stddev: 0.000011124415802714461",
            "extra": "mean: 447.22402207287655 usec\nrounds: 1042"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 39778.79426725252,
            "unit": "iter/sec",
            "range": "stddev: 0.000004083649228124452",
            "extra": "mean: 25.139022396745688 usec\nrounds: 10180"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 87061.3594874776,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014738231811451602",
            "extra": "mean: 11.486151903518508 usec\nrounds: 10691"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 67822.17372029882,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016942778634926937",
            "extra": "mean: 14.744440426283555 usec\nrounds: 17642"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 56243.854540694425,
            "unit": "iter/sec",
            "range": "stddev: 0.000001963904306579881",
            "extra": "mean: 17.779720258618912 usec\nrounds: 15618"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 160.46007567534528,
            "unit": "iter/sec",
            "range": "stddev: 0.00970080710588381",
            "extra": "mean: 6.2320798229166625 msec\nrounds: 96"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 29357.45907267903,
            "unit": "iter/sec",
            "range": "stddev: 0.000006327067389783322",
            "extra": "mean: 34.06289343789399 usec\nrounds: 38006"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 12560.594843709692,
            "unit": "iter/sec",
            "range": "stddev: 0.000013695028390460496",
            "extra": "mean: 79.61406385946736 usec\nrounds: 18572"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 2590.167086843876,
            "unit": "iter/sec",
            "range": "stddev: 0.00002476843902783581",
            "extra": "mean: 386.07547948518726 usec\nrounds: 4972"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 12462.505443339887,
            "unit": "iter/sec",
            "range": "stddev: 0.000014717783368288586",
            "extra": "mean: 80.24068711917089 usec\nrounds: 18710"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 904.7017983179837,
            "unit": "iter/sec",
            "range": "stddev: 0.00013737383757669945",
            "extra": "mean: 1.1053365892045248 msec\nrounds: 1760"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 5371.344944882712,
            "unit": "iter/sec",
            "range": "stddev: 0.00036175962892500145",
            "extra": "mean: 186.17311125264843 usec\nrounds: 9411"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 114590.56708760132,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013308196659692724",
            "extra": "mean: 8.72672180106699 usec\nrounds: 50525"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 29711.431532759507,
            "unit": "iter/sec",
            "range": "stddev: 0.000008210998097823563",
            "extra": "mean: 33.65707905717739 usec\nrounds: 46460"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 7163370.320250107,
            "unit": "iter/sec",
            "range": "stddev: 1.1737488800235994e-8",
            "extra": "mean: 139.59909306560678 nsec\nrounds: 152161"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 6038949.045411013,
            "unit": "iter/sec",
            "range": "stddev: 1.538463069868334e-8",
            "extra": "mean: 165.59172671938643 nsec\nrounds: 134382"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 3200798.9111805824,
            "unit": "iter/sec",
            "range": "stddev: 1.9290292284049253e-7",
            "extra": "mean: 312.42200080328075 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 30110.385548326663,
            "unit": "iter/sec",
            "range": "stddev: 0.0004129216501542057",
            "extra": "mean: 33.21113236477882 usec\nrounds: 66007"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 37481.99871797579,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013504950756557944",
            "extra": "mean: 26.679473726154722 usec\nrounds: 70450"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3783.18075374933,
            "unit": "iter/sec",
            "range": "stddev: 0.000010952316505342586",
            "extra": "mean: 264.32784080140704 usec\nrounds: 7588"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8114.23122551257,
            "unit": "iter/sec",
            "range": "stddev: 0.000004151802985808609",
            "extra": "mean: 123.24026419851386 usec\nrounds: 16234"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1513.3048548639647,
            "unit": "iter/sec",
            "range": "stddev: 0.000044013090053957604",
            "extra": "mean: 660.8053868233264 usec\nrounds: 3066"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 503.01199477775884,
            "unit": "iter/sec",
            "range": "stddev: 0.00001748177088394489",
            "extra": "mean: 1.988024163204738 msec\nrounds: 1011"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 2390.2015152998183,
            "unit": "iter/sec",
            "range": "stddev: 0.00004176347289813834",
            "extra": "mean: 418.3747661437507 usec\nrounds: 4336"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4844.5082943924135,
            "unit": "iter/sec",
            "range": "stddev: 0.000007040860184195799",
            "extra": "mean: 206.41929773502793 usec\nrounds: 9448"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 462.71297969014176,
            "unit": "iter/sec",
            "range": "stddev: 0.00002560590041700657",
            "extra": "mean: 2.161166952069629 msec\nrounds: 918"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 472.1868095538462,
            "unit": "iter/sec",
            "range": "stddev: 0.000030102779787502674",
            "extra": "mean: 2.117805876332859 msec\nrounds: 938"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1137.2308025854134,
            "unit": "iter/sec",
            "range": "stddev: 0.00000991160724040876",
            "extra": "mean: 879.3289785385438 usec\nrounds: 2190"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 432.9195717007389,
            "unit": "iter/sec",
            "range": "stddev: 0.00003051698740734778",
            "extra": "mean: 2.3098978779625665 msec\nrounds: 844"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 444.26656748439393,
            "unit": "iter/sec",
            "range": "stddev: 0.000023013262745954498",
            "extra": "mean: 2.250900862656355 msec\nrounds: 881"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 408.0311968392623,
            "unit": "iter/sec",
            "range": "stddev: 0.000033783838176561555",
            "extra": "mean: 2.45079299756076 msec\nrounds: 820"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 407.7862115390053,
            "unit": "iter/sec",
            "range": "stddev: 0.00004355940417007511",
            "extra": "mean: 2.4522653579333897 msec\nrounds: 813"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 200.22855332946946,
            "unit": "iter/sec",
            "range": "stddev: 0.000053755733492451885",
            "extra": "mean: 4.994292688888049 msec\nrounds: 405"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1032.4786792830528,
            "unit": "iter/sec",
            "range": "stddev: 0.000013047158318483984",
            "extra": "mean: 968.5430024515317 usec\nrounds: 2040"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 1055.0546126676552,
            "unit": "iter/sec",
            "range": "stddev: 0.00001858165913054813",
            "extra": "mean: 947.818234234859 usec\nrounds: 2109"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1832.1519521906926,
            "unit": "iter/sec",
            "range": "stddev: 0.000013099738542669176",
            "extra": "mean: 545.8062573927377 usec\nrounds: 3652"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 996.9315755367514,
            "unit": "iter/sec",
            "range": "stddev: 0.000018107347153396204",
            "extra": "mean: 1.0030778686707724 msec\nrounds: 1896"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 593.9235454179666,
            "unit": "iter/sec",
            "range": "stddev: 0.00002818626806347563",
            "extra": "mean: 1.6837183972833778 msec\nrounds: 1178"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1857.3698704263657,
            "unit": "iter/sec",
            "range": "stddev: 0.000011361239720139059",
            "extra": "mean: 538.3957260868276 usec\nrounds: 3680"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 32779.94673227282,
            "unit": "iter/sec",
            "range": "stddev: 0.0007595062788277315",
            "extra": "mean: 30.50645591853481 usec\nrounds: 56940"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2636.4119227729234,
            "unit": "iter/sec",
            "range": "stddev: 0.00002588398362101301",
            "extra": "mean: 379.3033976830983 usec\nrounds: 5180"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 52.73149651649206,
            "unit": "iter/sec",
            "range": "stddev: 0.00010767340711014213",
            "extra": "mean: 18.963998104761632 msec\nrounds: 105"
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
          "id": "c68b17bdcf69ed2faa89900e2bae523686d704c4",
          "message": "Merge pull request #4657 from nexi-lab/docs/runbook-planes-layers\n\ndocs(runbook): distinguish overlay (WireGuard/machine) vs broker (mTLS/agent) trust layers",
          "timestamp": "2026-08-16T06:21:56Z",
          "url": "https://github.com/nexi-lab/nexus/commit/c68b17bdcf69ed2faa89900e2bae523686d704c4"
        },
        "date": 1786873313102,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 188.1388607778582,
            "unit": "iter/sec",
            "range": "stddev: 0.0042283237500834545",
            "extra": "mean: 5.315222999998565 msec\nrounds: 89"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 477.5044396052451,
            "unit": "iter/sec",
            "range": "stddev: 0.00029781773070659015",
            "extra": "mean: 2.0942213664582976 msec\nrounds: 161"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1762.3745751812717,
            "unit": "iter/sec",
            "range": "stddev: 0.000033826276406379147",
            "extra": "mean: 567.4162655785836 usec\nrounds: 674"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 369.93819475503204,
            "unit": "iter/sec",
            "range": "stddev: 0.002492106382794269",
            "extra": "mean: 2.703154240837949 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 151.07480002434855,
            "unit": "iter/sec",
            "range": "stddev: 0.0026542793724549244",
            "extra": "mean: 6.619237621620755 msec\nrounds: 74"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 30479.732223821677,
            "unit": "iter/sec",
            "range": "stddev: 0.0000021857416532454467",
            "extra": "mean: 32.80868718454298 usec\nrounds: 12883"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 2278.8492335103565,
            "unit": "iter/sec",
            "range": "stddev: 0.00001490876032765736",
            "extra": "mean: 438.8179723761683 usec\nrounds: 1086"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 40382.936242346485,
            "unit": "iter/sec",
            "range": "stddev: 0.0000020308174339091875",
            "extra": "mean: 24.762934374033378 usec\nrounds: 9737"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 86305.29078472962,
            "unit": "iter/sec",
            "range": "stddev: 0.000001641969334367165",
            "extra": "mean: 11.586775166476055 usec\nrounds: 10808"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 66900.50581745678,
            "unit": "iter/sec",
            "range": "stddev: 0.000001845157150639191",
            "extra": "mean: 14.947570093544249 usec\nrounds: 17762"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 55376.527031426245,
            "unit": "iter/sec",
            "range": "stddev: 0.000002099618863839533",
            "extra": "mean: 18.058192768797127 usec\nrounds: 15931"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 160.51651117864725,
            "unit": "iter/sec",
            "range": "stddev: 0.00923940678078385",
            "extra": "mean: 6.229888705262522 msec\nrounds: 95"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 29680.208258053353,
            "unit": "iter/sec",
            "range": "stddev: 0.000006028180673823301",
            "extra": "mean: 33.69248595917997 usec\nrounds: 38744"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 12723.536346473218,
            "unit": "iter/sec",
            "range": "stddev: 0.000013441400564284508",
            "extra": "mean: 78.59450177758055 usec\nrounds: 19126"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 2592.6956234734166,
            "unit": "iter/sec",
            "range": "stddev: 0.000023815701936922278",
            "extra": "mean: 385.69895785156103 usec\nrounds: 5101"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 12622.50869073668,
            "unit": "iter/sec",
            "range": "stddev: 0.00001393771768987736",
            "extra": "mean: 79.22355408904357 usec\nrounds: 18867"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 907.7060046019839,
            "unit": "iter/sec",
            "range": "stddev: 0.0001358961008421762",
            "extra": "mean: 1.101678291131814 msec\nrounds: 1793"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 5388.585864388296,
            "unit": "iter/sec",
            "range": "stddev: 0.0003634734227793808",
            "extra": "mean: 185.5774455796889 usec\nrounds: 9004"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 114144.41991870239,
            "unit": "iter/sec",
            "range": "stddev: 0.0000012310796511789683",
            "extra": "mean: 8.76083124091598 usec\nrounds: 53686"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 29799.114085254543,
            "unit": "iter/sec",
            "range": "stddev: 0.00000935341809599775",
            "extra": "mean: 33.55804461632732 usec\nrounds: 45006"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 7341342.547470054,
            "unit": "iter/sec",
            "range": "stddev: 1.148043191267522e-8",
            "extra": "mean: 136.2148671763881 nsec\nrounds: 159008"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 6009491.141795393,
            "unit": "iter/sec",
            "range": "stddev: 2.2403878549690694e-8",
            "extra": "mean: 166.40344022559628 nsec\nrounds: 145816"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 3187819.9751294577,
            "unit": "iter/sec",
            "range": "stddev: 4.791767575513896e-7",
            "extra": "mean: 313.6940002264055 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 30415.422325236235,
            "unit": "iter/sec",
            "range": "stddev: 0.0004090192074665018",
            "extra": "mean: 32.87805736533475 usec\nrounds: 63697"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 37280.13559074195,
            "unit": "iter/sec",
            "range": "stddev: 0.000002242052976898913",
            "extra": "mean: 26.8239367736725 usec\nrounds: 72454"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3792.769071804327,
            "unit": "iter/sec",
            "range": "stddev: 0.0000096252243106724",
            "extra": "mean: 263.6596062317793 usec\nrounds: 7606"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8108.627806482205,
            "unit": "iter/sec",
            "range": "stddev: 0.000003558623324765283",
            "extra": "mean: 123.32542865028029 usec\nrounds: 16307"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1522.1253916143326,
            "unit": "iter/sec",
            "range": "stddev: 0.000013121083657779819",
            "extra": "mean: 656.9760977046852 usec\nrounds: 3050"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 506.27932955920033,
            "unit": "iter/sec",
            "range": "stddev: 0.000018993864565784974",
            "extra": "mean: 1.9751942092335961 msec\nrounds: 1018"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 2416.158292356001,
            "unit": "iter/sec",
            "range": "stddev: 0.00002066645066440736",
            "extra": "mean: 413.88016801866814 usec\nrounds: 4315"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4805.50771129388,
            "unit": "iter/sec",
            "range": "stddev: 0.000011389923897304604",
            "extra": "mean: 208.09455734506577 usec\nrounds: 8754"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 464.1159172516808,
            "unit": "iter/sec",
            "range": "stddev: 0.000039429030826770835",
            "extra": "mean: 2.154634139508988 msec\nrounds: 896"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 472.7518204222658,
            "unit": "iter/sec",
            "range": "stddev: 0.000028723558800634494",
            "extra": "mean: 2.1152747737846718 msec\nrounds: 946"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1131.4611441530371,
            "unit": "iter/sec",
            "range": "stddev: 0.000025331338550325515",
            "extra": "mean: 883.8129397263189 usec\nrounds: 2190"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 430.9523222622672,
            "unit": "iter/sec",
            "range": "stddev: 0.000043144272779420565",
            "extra": "mean: 2.320442304964362 msec\nrounds: 846"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 442.6862970815983,
            "unit": "iter/sec",
            "range": "stddev: 0.00008926594621145442",
            "extra": "mean: 2.2589359702174714 msec\nrounds: 873"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 407.5848504661315,
            "unit": "iter/sec",
            "range": "stddev: 0.00004171898943299185",
            "extra": "mean: 2.4534768621953367 msec\nrounds: 820"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 404.7580461086778,
            "unit": "iter/sec",
            "range": "stddev: 0.00015439775446027468",
            "extra": "mean: 2.470611787990249 msec\nrounds: 816"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 201.98273024858722,
            "unit": "iter/sec",
            "range": "stddev: 0.00007888499798195357",
            "extra": "mean: 4.950918322419273 msec\nrounds: 397"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1024.641733942596,
            "unit": "iter/sec",
            "range": "stddev: 0.00004252818842819346",
            "extra": "mean: 975.9508781203164 usec\nrounds: 2043"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 1063.124794389851,
            "unit": "iter/sec",
            "range": "stddev: 0.000028909219586868366",
            "extra": "mean: 940.6233447635094 usec\nrounds: 2158"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1825.9309055925535,
            "unit": "iter/sec",
            "range": "stddev: 0.000015006944987487577",
            "extra": "mean: 547.665849204452 usec\nrounds: 3707"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 1011.7106619785616,
            "unit": "iter/sec",
            "range": "stddev: 0.000021758359489040313",
            "extra": "mean: 988.4248902194432 usec\nrounds: 2004"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 603.7432755435565,
            "unit": "iter/sec",
            "range": "stddev: 0.00003246365583560361",
            "extra": "mean: 1.6563331477268868 msec\nrounds: 1232"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1881.7055644371344,
            "unit": "iter/sec",
            "range": "stddev: 0.000015111268589483004",
            "extra": "mean: 531.4327697697621 usec\nrounds: 3566"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 31612.379230812872,
            "unit": "iter/sec",
            "range": "stddev: 0.0007971174996496527",
            "extra": "mean: 31.633177392269513 usec\nrounds: 54185"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2637.4197606139014,
            "unit": "iter/sec",
            "range": "stddev: 0.000021801410099480704",
            "extra": "mean: 379.1584543854461 usec\nrounds: 5119"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 52.39336512468883,
            "unit": "iter/sec",
            "range": "stddev: 0.000257558966166625",
            "extra": "mean: 19.086386179245043 msec\nrounds: 106"
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
          "id": "9be69e80f5acc219c8f37d676e604c90c7eeb61e",
          "message": "Merge pull request #4660 from nexi-lab/docs/runbook-customer-connectivity\n\ndocs(runbook): document the customer-side connectivity plane (per-customer overlay)",
          "timestamp": "2026-08-17T09:45:20Z",
          "url": "https://github.com/nexi-lab/nexus/commit/9be69e80f5acc219c8f37d676e604c90c7eeb61e"
        },
        "date": 1786960675578,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 235.01318037092523,
            "unit": "iter/sec",
            "range": "stddev: 0.000376362719006139",
            "extra": "mean: 4.255080495577666 msec\nrounds: 113"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 425.5625747155766,
            "unit": "iter/sec",
            "range": "stddev: 0.00030638388408288836",
            "extra": "mean: 2.3498306933318722 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1396.2379754385786,
            "unit": "iter/sec",
            "range": "stddev: 0.000023463379918044895",
            "extra": "mean: 716.2102862056058 usec\nrounds: 580"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 435.59674848829843,
            "unit": "iter/sec",
            "range": "stddev: 0.0006402364520252202",
            "extra": "mean: 2.295701249998801 msec\nrounds: 220"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 173.20378826502653,
            "unit": "iter/sec",
            "range": "stddev: 0.0004010808553533268",
            "extra": "mean: 5.7735457752797945 msec\nrounds: 89"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 24417.343789087496,
            "unit": "iter/sec",
            "range": "stddev: 0.0000029328412478750913",
            "extra": "mean: 40.954495650215485 usec\nrounds: 10344"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1872.3711654104459,
            "unit": "iter/sec",
            "range": "stddev: 0.00005250944338428122",
            "extra": "mean: 534.0821405892501 usec\nrounds: 882"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31608.03801356519,
            "unit": "iter/sec",
            "range": "stddev: 0.000003697889148111435",
            "extra": "mean: 31.63752206229412 usec\nrounds: 9156"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 59177.20925557096,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025822640081177928",
            "extra": "mean: 16.89839741649966 usec\nrounds: 10450"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 47629.95875758176,
            "unit": "iter/sec",
            "range": "stddev: 0.000003134175184308795",
            "extra": "mean: 20.995189290203186 usec\nrounds: 14491"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 37014.73395587802,
            "unit": "iter/sec",
            "range": "stddev: 0.0000032080711582730246",
            "extra": "mean: 27.016268742928453 usec\nrounds: 13165"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 135.92476456898575,
            "unit": "iter/sec",
            "range": "stddev: 0.008713256178330308",
            "extra": "mean: 7.357011087501064 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 16974.89081731119,
            "unit": "iter/sec",
            "range": "stddev: 0.000010595992243749373",
            "extra": "mean: 58.91054091377062 usec\nrounds: 22303"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7717.642587906426,
            "unit": "iter/sec",
            "range": "stddev: 0.00002137828940961218",
            "extra": "mean: 129.57324579490162 usec\nrounds: 13080"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1616.3707755130813,
            "unit": "iter/sec",
            "range": "stddev: 0.00006150842241797673",
            "extra": "mean: 618.6699333774901 usec\nrounds: 2972"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7687.854256650394,
            "unit": "iter/sec",
            "range": "stddev: 0.000026079378513591647",
            "extra": "mean: 130.07530666114644 usec\nrounds: 12160"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 561.9484000453069,
            "unit": "iter/sec",
            "range": "stddev: 0.00026086490489956194",
            "extra": "mean: 1.7795228172539956 msec\nrounds: 1078"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4212.291695110904,
            "unit": "iter/sec",
            "range": "stddev: 0.00038372397006008167",
            "extra": "mean: 237.4004633061556 usec\nrounds: 7876"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 79043.26230177136,
            "unit": "iter/sec",
            "range": "stddev: 0.000002329016123518012",
            "extra": "mean: 12.65129969183458 usec\nrounds: 51623"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 17046.650538329577,
            "unit": "iter/sec",
            "range": "stddev: 0.000012906385512765775",
            "extra": "mean: 58.6625506137695 usec\nrounds: 28757"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5336710.526669652,
            "unit": "iter/sec",
            "range": "stddev: 1.669475178005348e-8",
            "extra": "mean: 187.3813456815026 nsec\nrounds: 113489"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 3804600.9264518246,
            "unit": "iter/sec",
            "range": "stddev: 1.735361944192268e-8",
            "extra": "mean: 262.8396563348896 nsec\nrounds: 97566"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2373509.7340738038,
            "unit": "iter/sec",
            "range": "stddev: 4.1844965152044303e-7",
            "extra": "mean: 421.31699973424475 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 20968.677239574463,
            "unit": "iter/sec",
            "range": "stddev: 0.00037894239020179216",
            "extra": "mean: 47.690180385469745 usec\nrounds: 57998"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 40045.03232477753,
            "unit": "iter/sec",
            "range": "stddev: 0.000001954030911579483",
            "extra": "mean: 24.971886447479736 usec\nrounds: 69994"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3961.8312723081863,
            "unit": "iter/sec",
            "range": "stddev: 0.000013089275070233458",
            "extra": "mean: 252.40852809397765 usec\nrounds: 7635"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7678.608010212602,
            "unit": "iter/sec",
            "range": "stddev: 0.000005134565957811306",
            "extra": "mean: 130.23193769886328 usec\nrounds: 13515"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1242.1792526559727,
            "unit": "iter/sec",
            "range": "stddev: 0.000019018773583497705",
            "extra": "mean: 805.0367914790432 usec\nrounds: 2441"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 425.7506060006152,
            "unit": "iter/sec",
            "range": "stddev: 0.00019553375492316327",
            "extra": "mean: 2.348792898720043 msec\nrounds: 859"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1873.9923593430112,
            "unit": "iter/sec",
            "range": "stddev: 0.000030024944378463677",
            "extra": "mean: 533.6201052338241 usec\nrounds: 3516"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4040.5722133757054,
            "unit": "iter/sec",
            "range": "stddev: 0.000018272433088874656",
            "extra": "mean: 247.48969878317004 usec\nrounds: 7971"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 394.4951264793579,
            "unit": "iter/sec",
            "range": "stddev: 0.000026314534418957942",
            "extra": "mean: 2.534885561006608 msec\nrounds: 795"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 397.32109707653234,
            "unit": "iter/sec",
            "range": "stddev: 0.00005297746523599894",
            "extra": "mean: 2.5168560324582487 msec\nrounds: 801"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 918.3668770487656,
            "unit": "iter/sec",
            "range": "stddev: 0.00002126688605973958",
            "extra": "mean: 1.0888894460278968 msec\nrounds: 1825"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 337.58341697630755,
            "unit": "iter/sec",
            "range": "stddev: 0.00005009571635710556",
            "extra": "mean: 2.9622308138144793 msec\nrounds: 666"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 350.8473066439664,
            "unit": "iter/sec",
            "range": "stddev: 0.000031138789092948494",
            "extra": "mean: 2.850242772462786 msec\nrounds: 690"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 326.7010252884403,
            "unit": "iter/sec",
            "range": "stddev: 0.00003788278964451929",
            "extra": "mean: 3.0609025457361585 msec\nrounds: 645"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 327.10398638614953,
            "unit": "iter/sec",
            "range": "stddev: 0.000032612019960081804",
            "extra": "mean: 3.0571318040113704 msec\nrounds: 648"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 160.5293374588076,
            "unit": "iter/sec",
            "range": "stddev: 0.0000747673912346897",
            "extra": "mean: 6.229390937694511 msec\nrounds: 321"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 795.6984553682952,
            "unit": "iter/sec",
            "range": "stddev: 0.00004794583854701107",
            "extra": "mean: 1.2567574980865612 msec\nrounds: 1568"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 835.2170238647304,
            "unit": "iter/sec",
            "range": "stddev: 0.00001699257096220102",
            "extra": "mean: 1.1972936032515034 msec\nrounds: 1661"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1348.4382849334675,
            "unit": "iter/sec",
            "range": "stddev: 0.000012719314213485622",
            "extra": "mean: 741.5986413121906 usec\nrounds: 2682"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 744.2064482785604,
            "unit": "iter/sec",
            "range": "stddev: 0.000018624729907430368",
            "extra": "mean: 1.34371316227254 msec\nrounds: 1479"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 460.28547078648313,
            "unit": "iter/sec",
            "range": "stddev: 0.00004389420737937925",
            "extra": "mean: 2.172564774402534 msec\nrounds: 922"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1352.4075106219238,
            "unit": "iter/sec",
            "range": "stddev: 0.00002961798156184193",
            "extra": "mean: 739.4220988466234 usec\nrounds: 2691"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28513.084880375867,
            "unit": "iter/sec",
            "range": "stddev: 0.0004994253107350901",
            "extra": "mean: 35.071617266087195 usec\nrounds: 44689"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2099.7487382395693,
            "unit": "iter/sec",
            "range": "stddev: 0.00002776823954420801",
            "extra": "mean: 476.24745846419734 usec\nrounds: 4129"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 42.51298321049263,
            "unit": "iter/sec",
            "range": "stddev: 0.00021705220812636398",
            "extra": "mean: 23.522226023253765 msec\nrounds: 86"
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
          "id": "32e3292e41f3ac0ebd9c6c581e4a4dce31b3fc6a",
          "message": "refactor(search): promote collect_plugin_documents off private surface (#4671)\n\nrefactor(search): promote collect_plugin_documents off private surface",
          "timestamp": "2026-08-18T07:53:20Z",
          "url": "https://github.com/nexi-lab/nexus/commit/32e3292e41f3ac0ebd9c6c581e4a4dce31b3fc6a"
        },
        "date": 1787046590102,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 257.0920284748121,
            "unit": "iter/sec",
            "range": "stddev: 0.0006215776026006693",
            "extra": "mean: 3.8896577460314847 msec\nrounds: 126"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 449.0313805925775,
            "unit": "iter/sec",
            "range": "stddev: 0.00030174370105855955",
            "extra": "mean: 2.2270158461538268 msec\nrounds: 156"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1659.2521464844292,
            "unit": "iter/sec",
            "range": "stddev: 0.00001909795530924368",
            "extra": "mean: 602.6811549521079 usec\nrounds: 626"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 494.22781262177904,
            "unit": "iter/sec",
            "range": "stddev: 0.00014727732927973075",
            "extra": "mean: 2.0233584077253792 msec\nrounds: 233"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 194.31691991747408,
            "unit": "iter/sec",
            "range": "stddev: 0.0003604013938380598",
            "extra": "mean: 5.146232250000142 msec\nrounds: 96"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 26084.910769355592,
            "unit": "iter/sec",
            "range": "stddev: 0.0000034075739706611863",
            "extra": "mean: 38.33633968856794 usec\nrounds: 11172"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1974.858699744744,
            "unit": "iter/sec",
            "range": "stddev: 0.000009200982427208998",
            "extra": "mean: 506.36534154532313 usec\nrounds: 893"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 35257.319841123775,
            "unit": "iter/sec",
            "range": "stddev: 0.000001882385809688829",
            "extra": "mean: 28.36290462537116 usec\nrounds: 9426"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 69092.00916098968,
            "unit": "iter/sec",
            "range": "stddev: 0.0000012994668489161295",
            "extra": "mean: 14.473453763226415 usec\nrounds: 10111"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 54656.93528077629,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016759287372702462",
            "extra": "mean: 18.295939844832752 usec\nrounds: 15992"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 45715.968738782234,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018370565878651827",
            "extra": "mean: 21.87419467613009 usec\nrounds: 13073"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 134.10044194847453,
            "unit": "iter/sec",
            "range": "stddev: 0.012203444492553703",
            "extra": "mean: 7.457096974999011 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 31353.891575922804,
            "unit": "iter/sec",
            "range": "stddev: 0.00000737943897537832",
            "extra": "mean: 31.89396753441341 usec\nrounds: 33574"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 13118.239845672217,
            "unit": "iter/sec",
            "range": "stddev: 0.000014815212220193402",
            "extra": "mean: 76.22973903239814 usec\nrounds: 17734"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 2370.726050797826,
            "unit": "iter/sec",
            "range": "stddev: 0.00005813913304015281",
            "extra": "mean: 421.8117060229155 usec\nrounds: 4184"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 13152.274100784167,
            "unit": "iter/sec",
            "range": "stddev: 0.000017709998599392853",
            "extra": "mean: 76.03247866772924 usec\nrounds: 15071"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 821.5575064011548,
            "unit": "iter/sec",
            "range": "stddev: 0.00018404323732803887",
            "extra": "mean: 1.2172002473454542 msec\nrounds: 1601"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4407.282318160508,
            "unit": "iter/sec",
            "range": "stddev: 0.00047056877872006975",
            "extra": "mean: 226.89719600658017 usec\nrounds: 7362"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 91540.81383889641,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017833234821076073",
            "extra": "mean: 10.924089027216972 usec\nrounds: 40965"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 31103.725681161875,
            "unit": "iter/sec",
            "range": "stddev: 0.000009615469210325154",
            "extra": "mean: 32.15048930957023 usec\nrounds: 38352"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5328086.3307992825,
            "unit": "iter/sec",
            "range": "stddev: 1.5420439800259225e-8",
            "extra": "mean: 187.68464659054933 nsec\nrounds: 112702"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4535743.396059183,
            "unit": "iter/sec",
            "range": "stddev: 1.9742888494931377e-8",
            "extra": "mean: 220.47102595548853 nsec\nrounds: 102675"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2606392.959894396,
            "unit": "iter/sec",
            "range": "stddev: 1.7702509523862621e-7",
            "extra": "mean: 383.672000111801 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 30709.70013545423,
            "unit": "iter/sec",
            "range": "stddev: 0.00048201180164984933",
            "extra": "mean: 32.563001123071984 usec\nrounds: 62329"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 35905.216153056914,
            "unit": "iter/sec",
            "range": "stddev: 0.000002240902620224024",
            "extra": "mean: 27.851106528288135 usec\nrounds: 69728"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3588.589292115105,
            "unit": "iter/sec",
            "range": "stddev: 0.00001215424203930545",
            "extra": "mean: 278.66103323587714 usec\nrounds: 7191"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7168.829179300793,
            "unit": "iter/sec",
            "range": "stddev: 0.000005278529257824048",
            "extra": "mean: 139.49279233593543 usec\nrounds: 12265"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1304.7025769717682,
            "unit": "iter/sec",
            "range": "stddev: 0.00001956432858070985",
            "extra": "mean: 766.458208675431 usec\nrounds: 2559"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 433.70392997582013,
            "unit": "iter/sec",
            "range": "stddev: 0.00003158661372091543",
            "extra": "mean: 2.305720402523795 msec\nrounds: 872"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1875.0525945884995,
            "unit": "iter/sec",
            "range": "stddev: 0.00005547205344066575",
            "extra": "mean: 533.3183735144564 usec\nrounds: 3451"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4287.764881361174,
            "unit": "iter/sec",
            "range": "stddev: 0.00001978604706372553",
            "extra": "mean: 233.22174318535502 usec\nrounds: 7924"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 407.296573698789,
            "unit": "iter/sec",
            "range": "stddev: 0.0002556675669683987",
            "extra": "mean: 2.4552133864488073 msec\nrounds: 797"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 421.9283145981101,
            "unit": "iter/sec",
            "range": "stddev: 0.000027434126183097464",
            "extra": "mean: 2.3700708518519495 msec\nrounds: 837"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 983.5585441191454,
            "unit": "iter/sec",
            "range": "stddev: 0.000048083890802526165",
            "extra": "mean: 1.0167162961261031 msec\nrounds: 1962"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 348.8663936658415,
            "unit": "iter/sec",
            "range": "stddev: 0.000029468032729168188",
            "extra": "mean: 2.8664268561157 msec\nrounds: 695"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 383.8051171049716,
            "unit": "iter/sec",
            "range": "stddev: 0.000032549069291579476",
            "extra": "mean: 2.605488971963075 msec\nrounds: 749"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 359.9067194168907,
            "unit": "iter/sec",
            "range": "stddev: 0.00005628215057847621",
            "extra": "mean: 2.778497721910188 msec\nrounds: 712"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 360.0325914316852,
            "unit": "iter/sec",
            "range": "stddev: 0.00004190148786010198",
            "extra": "mean: 2.777526323446043 msec\nrounds: 708"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 166.26462348870183,
            "unit": "iter/sec",
            "range": "stddev: 0.00007380022189812542",
            "extra": "mean: 6.014508552794774 msec\nrounds: 322"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 866.916264094506,
            "unit": "iter/sec",
            "range": "stddev: 0.0000853507017042336",
            "extra": "mean: 1.1535139452534091 msec\nrounds: 1717"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 951.9944572118524,
            "unit": "iter/sec",
            "range": "stddev: 0.000013915749248023619",
            "extra": "mean: 1.0504262839184417 msec\nrounds: 1909"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1623.736823963443,
            "unit": "iter/sec",
            "range": "stddev: 0.00002176942500103788",
            "extra": "mean: 615.8633500465061 usec\nrounds: 3231"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 887.5609552396216,
            "unit": "iter/sec",
            "range": "stddev: 0.000013980310390116919",
            "extra": "mean: 1.1266831805709867 msec\nrounds: 1750"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 537.1309107554442,
            "unit": "iter/sec",
            "range": "stddev: 0.00003331323514275",
            "extra": "mean: 1.86174353398049 msec\nrounds: 1030"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1571.4969296107793,
            "unit": "iter/sec",
            "range": "stddev: 0.000023107599494293052",
            "extra": "mean: 636.3359553287038 usec\nrounds: 3134"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28498.38875364806,
            "unit": "iter/sec",
            "range": "stddev: 0.000636206686145719",
            "extra": "mean: 35.08970309319647 usec\nrounds: 45324"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2046.1035477950936,
            "unit": "iter/sec",
            "range": "stddev: 0.000023538945918693645",
            "extra": "mean: 488.733818519406 usec\nrounds: 4039"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 40.86882283730943,
            "unit": "iter/sec",
            "range": "stddev: 0.00037219534271750235",
            "extra": "mean: 24.468529567900674 msec\nrounds: 81"
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
          "id": "529d06048d11010aff6a5bb494077a38b84d8a3e",
          "message": "feat(http-api): GET /v2/search/glob with cached gRPC backend (#4678)\n\nfeat(http-api): GET /v2/search/glob with cached gRPC backend (R10 step 1)",
          "timestamp": "2026-08-19T02:00:54Z",
          "url": "https://github.com/nexi-lab/nexus/commit/529d06048d11010aff6a5bb494077a38b84d8a3e"
        },
        "date": 1787133017722,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 234.66086304007635,
            "unit": "iter/sec",
            "range": "stddev: 0.0003998719980474402",
            "extra": "mean: 4.261469028302414 msec\nrounds: 106"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 425.9070925246873,
            "unit": "iter/sec",
            "range": "stddev: 0.0003074514580595338",
            "extra": "mean: 2.3479299066662898 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1349.7903452069295,
            "unit": "iter/sec",
            "range": "stddev: 0.000021608736808637646",
            "extra": "mean: 740.8557955322277 usec\nrounds: 582"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 423.68746242127304,
            "unit": "iter/sec",
            "range": "stddev: 0.0009678584480512547",
            "extra": "mean: 2.3602303317762527 msec\nrounds: 214"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 163.50651789075113,
            "unit": "iter/sec",
            "range": "stddev: 0.0012538692170710995",
            "extra": "mean: 6.115964139534561 msec\nrounds: 86"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25261.652571704344,
            "unit": "iter/sec",
            "range": "stddev: 0.000002693511207477551",
            "extra": "mean: 39.58569207463898 usec\nrounds: 10561"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1866.7656922053482,
            "unit": "iter/sec",
            "range": "stddev: 0.00005730761340469001",
            "extra": "mean: 535.6858679026965 usec\nrounds: 863"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31737.556248646593,
            "unit": "iter/sec",
            "range": "stddev: 0.0000031986836173449484",
            "extra": "mean: 31.508412058116285 usec\nrounds: 9023"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 64461.20986556293,
            "unit": "iter/sec",
            "range": "stddev: 0.000002227447216987838",
            "extra": "mean: 15.513205571002311 usec\nrounds: 10483"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 50829.33139827985,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026314664500760705",
            "extra": "mean: 19.673679989303217 usec\nrounds: 15037"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 40001.80977890232,
            "unit": "iter/sec",
            "range": "stddev: 0.000003051285449664803",
            "extra": "mean: 24.998868939360293 usec\nrounds: 13406"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 136.15322481634269,
            "unit": "iter/sec",
            "range": "stddev: 0.011132993252967838",
            "extra": "mean: 7.344666285715242 msec\nrounds: 84"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 17063.728756128527,
            "unit": "iter/sec",
            "range": "stddev: 0.000010284594847516017",
            "extra": "mean: 58.60383825199079 usec\nrounds: 25515"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7761.86255116612,
            "unit": "iter/sec",
            "range": "stddev: 0.000025399704353300533",
            "extra": "mean: 128.83505645816453 usec\nrounds: 11141"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1665.8714694399453,
            "unit": "iter/sec",
            "range": "stddev: 0.00003750672383679495",
            "extra": "mean: 600.286407651962 usec\nrounds: 3032"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7767.8232755198005,
            "unit": "iter/sec",
            "range": "stddev: 0.000024061562549724884",
            "extra": "mean: 128.73619346509693 usec\nrounds: 12364"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 578.5503705929029,
            "unit": "iter/sec",
            "range": "stddev: 0.0002437616477142913",
            "extra": "mean: 1.7284579715594897 msec\nrounds: 1090"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4301.36216923615,
            "unit": "iter/sec",
            "range": "stddev: 0.00040316623428104784",
            "extra": "mean: 232.4844922736611 usec\nrounds: 7636"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 82005.84851420115,
            "unit": "iter/sec",
            "range": "stddev: 0.0000022758844773926493",
            "extra": "mean: 12.19425221637488 usec\nrounds: 53577"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 17071.3029388942,
            "unit": "iter/sec",
            "range": "stddev: 0.000012624617509383682",
            "extra": "mean: 58.57783694539577 usec\nrounds: 28193"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5417294.175650243,
            "unit": "iter/sec",
            "range": "stddev: 1.675846480263409e-8",
            "extra": "mean: 184.5939998043339 nsec\nrounds: 112278"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4234037.578533474,
            "unit": "iter/sec",
            "range": "stddev: 1.6408432141743022e-8",
            "extra": "mean: 236.1811820164255 nsec\nrounds: 99271"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2414508.296700032,
            "unit": "iter/sec",
            "range": "stddev: 4.277111329857543e-7",
            "extra": "mean: 414.16300012997453 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 20920.85817150583,
            "unit": "iter/sec",
            "range": "stddev: 0.000403965181689647",
            "extra": "mean: 47.79918642926407 usec\nrounds: 57963"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39951.5526287063,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025511431179167226",
            "extra": "mean: 25.03031632571577 usec\nrounds: 65069"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3948.836282448364,
            "unit": "iter/sec",
            "range": "stddev: 0.000009619246131084415",
            "extra": "mean: 253.23916426841035 usec\nrounds: 7506"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7733.437840823224,
            "unit": "iter/sec",
            "range": "stddev: 0.000005418367114410517",
            "extra": "mean: 129.30859736418984 usec\nrounds: 13506"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1229.3038039491205,
            "unit": "iter/sec",
            "range": "stddev: 0.000015989107902980776",
            "extra": "mean: 813.4685639038248 usec\nrounds: 2449"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 430.0491990602789,
            "unit": "iter/sec",
            "range": "stddev: 0.000028254658122173936",
            "extra": "mean: 2.3253153410938747 msec\nrounds: 859"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1835.5556393604577,
            "unit": "iter/sec",
            "range": "stddev: 0.00004246965623186981",
            "extra": "mean: 544.7941639886322 usec\nrounds: 3049"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4113.735275177195,
            "unit": "iter/sec",
            "range": "stddev: 0.000007745916394401105",
            "extra": "mean: 243.0880776490719 usec\nrounds: 7946"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 390.3031354997782,
            "unit": "iter/sec",
            "range": "stddev: 0.000026628998081448045",
            "extra": "mean: 2.562111110687114 msec\nrounds: 786"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 402.1727714297106,
            "unit": "iter/sec",
            "range": "stddev: 0.00003005136742075004",
            "extra": "mean: 2.48649354466498 msec\nrounds: 806"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 927.8780847761541,
            "unit": "iter/sec",
            "range": "stddev: 0.000014529224510847899",
            "extra": "mean: 1.077727792483907 msec\nrounds: 1836"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 345.2946173974736,
            "unit": "iter/sec",
            "range": "stddev: 0.00012209719869126363",
            "extra": "mean: 2.896077580175209 msec\nrounds: 686"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 361.5012767003709,
            "unit": "iter/sec",
            "range": "stddev: 0.00006615873502321174",
            "extra": "mean: 2.766241959440842 msec\nrounds: 715"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 334.7007479668841,
            "unit": "iter/sec",
            "range": "stddev: 0.000034505793425473",
            "extra": "mean: 2.9877435472565534 msec\nrounds: 656"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 335.38700536999215,
            "unit": "iter/sec",
            "range": "stddev: 0.000033675098204338574",
            "extra": "mean: 2.981630128742824 msec\nrounds: 668"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 165.40710937177423,
            "unit": "iter/sec",
            "range": "stddev: 0.00004133172833361791",
            "extra": "mean: 6.0456893527615465 msec\nrounds: 326"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 804.7428805230147,
            "unit": "iter/sec",
            "range": "stddev: 0.000024258203796513052",
            "extra": "mean: 1.242632925624747 msec\nrounds: 1600"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 866.5476873673313,
            "unit": "iter/sec",
            "range": "stddev: 0.00001560468364765181",
            "extra": "mean: 1.154004579988104 msec\nrounds: 1719"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1454.061422047909,
            "unit": "iter/sec",
            "range": "stddev: 0.000011918105095462465",
            "extra": "mean: 687.7288571424953 usec\nrounds: 2933"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 801.9509766947957,
            "unit": "iter/sec",
            "range": "stddev: 0.000016606509638579825",
            "extra": "mean: 1.2469590150278937 msec\nrounds: 1597"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 475.15520783103085,
            "unit": "iter/sec",
            "range": "stddev: 0.00004408638670139746",
            "extra": "mean: 2.1045754808513184 msec\nrounds: 940"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1465.864096814828,
            "unit": "iter/sec",
            "range": "stddev: 0.000027093964146438254",
            "extra": "mean: 682.1914815792932 usec\nrounds: 3040"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28554.249594996043,
            "unit": "iter/sec",
            "range": "stddev: 0.000579851544866889",
            "extra": "mean: 35.0210569068936 usec\nrounds: 44810"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2162.3823208015665,
            "unit": "iter/sec",
            "range": "stddev: 0.000020403157824421085",
            "extra": "mean: 462.45291148575114 usec\nrounds: 4214"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 43.813932350328166,
            "unit": "iter/sec",
            "range": "stddev: 0.0002447776803168779",
            "extra": "mean: 22.823790204544604 msec\nrounds: 88"
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
          "id": "e80723f361502f6bd17e5181a0dcfb102168a0f0",
          "message": "feat(http-api): serve()/bind_and_serve() helpers + 3 edge-case glob E2Es (#4680)\n\nfeat(http-api): serve()/bind_and_serve() helpers + 3 edge-case glob E2Es",
          "timestamp": "2026-08-19T15:40:51Z",
          "url": "https://github.com/nexi-lab/nexus/commit/e80723f361502f6bd17e5181a0dcfb102168a0f0"
        },
        "date": 1787219469280,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 243.83335400873563,
            "unit": "iter/sec",
            "range": "stddev: 0.00020836262840706455",
            "extra": "mean: 4.101161648148325 msec\nrounds: 108"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 423.9486727333546,
            "unit": "iter/sec",
            "range": "stddev: 0.00029625530347136483",
            "extra": "mean: 2.358776107382595 msec\nrounds: 149"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1388.1098491775208,
            "unit": "iter/sec",
            "range": "stddev: 0.00002162888677119741",
            "extra": "mean: 720.4040808387876 usec\nrounds: 334"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 444.055125280964,
            "unit": "iter/sec",
            "range": "stddev: 0.00038787401388467213",
            "extra": "mean: 2.2519726562491016 msec\nrounds: 160"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 173.55344481025728,
            "unit": "iter/sec",
            "range": "stddev: 0.0008937743170905529",
            "extra": "mean: 5.76191386516863 msec\nrounds: 89"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 24163.663186880163,
            "unit": "iter/sec",
            "range": "stddev: 0.000004684242340448625",
            "extra": "mean: 41.38445368428067 usec\nrounds: 10450"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1852.1262486755315,
            "unit": "iter/sec",
            "range": "stddev: 0.000014632531916952885",
            "extra": "mean: 539.9199977404926 usec\nrounds: 885"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31067.294931131695,
            "unit": "iter/sec",
            "range": "stddev: 0.0000033297886429070554",
            "extra": "mean: 32.18819025656228 usec\nrounds: 8888"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 52118.49065495286,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027416835348295647",
            "extra": "mean: 19.187048347590036 usec\nrounds: 10590"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 42288.072615224235,
            "unit": "iter/sec",
            "range": "stddev: 0.000002942847396191905",
            "extra": "mean: 23.64732980618245 usec\nrounds: 13514"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 34837.7261522191,
            "unit": "iter/sec",
            "range": "stddev: 0.000003117109807693792",
            "extra": "mean: 28.704514055556466 usec\nrounds: 12415"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 136.64159612208846,
            "unit": "iter/sec",
            "range": "stddev: 0.009707195734866761",
            "extra": "mean: 7.318415682926492 msec\nrounds: 82"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 16469.285539801996,
            "unit": "iter/sec",
            "range": "stddev: 0.000011618006160511897",
            "extra": "mean: 60.71908812214465 usec\nrounds: 25215"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7527.569137916964,
            "unit": "iter/sec",
            "range": "stddev: 0.00002271582613270609",
            "extra": "mean: 132.8450103450954 usec\nrounds: 12953"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1582.7220984790376,
            "unit": "iter/sec",
            "range": "stddev: 0.000043052065758155926",
            "extra": "mean: 631.8228582016886 usec\nrounds: 3103"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7481.179490311483,
            "unit": "iter/sec",
            "range": "stddev: 0.000024566442365638574",
            "extra": "mean: 133.66876189711155 usec\nrounds: 12503"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 550.6458441930175,
            "unit": "iter/sec",
            "range": "stddev: 0.00025774828827166123",
            "extra": "mean: 1.8160493001913416 msec\nrounds: 1046"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4205.402481722847,
            "unit": "iter/sec",
            "range": "stddev: 0.0005071679607737023",
            "extra": "mean: 237.78936840079223 usec\nrounds: 7785"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 80965.96494377017,
            "unit": "iter/sec",
            "range": "stddev: 0.0000021124056171819734",
            "extra": "mean: 12.350868672960141 usec\nrounds: 47850"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 16693.42294811675,
            "unit": "iter/sec",
            "range": "stddev: 0.000013167001799628834",
            "extra": "mean: 59.90383177302854 usec\nrounds: 27665"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5424183.852474939,
            "unit": "iter/sec",
            "range": "stddev: 1.532035934870913e-8",
            "extra": "mean: 184.3595326407901 nsec\nrounds: 113553"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4268778.486856309,
            "unit": "iter/sec",
            "range": "stddev: 2.0029614704873175e-8",
            "extra": "mean: 234.25905164182882 nsec\nrounds: 99667"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2411660.8633266175,
            "unit": "iter/sec",
            "range": "stddev: 1.3299226284153258e-7",
            "extra": "mean: 414.65199987555934 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 20976.449273233713,
            "unit": "iter/sec",
            "range": "stddev: 0.00039173384767185763",
            "extra": "mean: 47.67251058433498 usec\nrounds: 57679"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39878.52162017071,
            "unit": "iter/sec",
            "range": "stddev: 0.0000023130369817208038",
            "extra": "mean: 25.076155267857175 usec\nrounds: 76017"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3929.252923325276,
            "unit": "iter/sec",
            "range": "stddev: 0.00001790655109633587",
            "extra": "mean: 254.50130584969142 usec\nrounds: 7932"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7646.221054112281,
            "unit": "iter/sec",
            "range": "stddev: 0.000007435767148043776",
            "extra": "mean: 130.7835586916731 usec\nrounds: 14951"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1244.978638806832,
            "unit": "iter/sec",
            "range": "stddev: 0.000021098043772202965",
            "extra": "mean: 803.2266328347483 usec\nrounds: 2473"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 424.5447004910519,
            "unit": "iter/sec",
            "range": "stddev: 0.00017232020211235404",
            "extra": "mean: 2.3554645690862346 msec\nrounds: 854"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1830.3446112087267,
            "unit": "iter/sec",
            "range": "stddev: 0.000041102349264181264",
            "extra": "mean: 546.3452039993813 usec\nrounds: 3451"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4075.0706387428577,
            "unit": "iter/sec",
            "range": "stddev: 0.00000995261461331299",
            "extra": "mean: 245.394519175377 usec\nrounds: 8005"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 395.9421570574221,
            "unit": "iter/sec",
            "range": "stddev: 0.00002877173727623635",
            "extra": "mean: 2.5256214378176796 msec\nrounds: 788"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 396.5177792604535,
            "unit": "iter/sec",
            "range": "stddev: 0.000046069815906507276",
            "extra": "mean: 2.521955010100942 msec\nrounds: 792"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 923.1610659023638,
            "unit": "iter/sec",
            "range": "stddev: 0.00003156030998705196",
            "extra": "mean: 1.0832345913792718 msec\nrounds: 1740"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 335.4437623501169,
            "unit": "iter/sec",
            "range": "stddev: 0.000025347597883398978",
            "extra": "mean: 2.981125637853589 msec\nrounds: 671"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 355.4658390405265,
            "unit": "iter/sec",
            "range": "stddev: 0.00020190958511189137",
            "extra": "mean: 2.813209850767096 msec\nrounds: 717"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 330.55168174310757,
            "unit": "iter/sec",
            "range": "stddev: 0.00006543998661681664",
            "extra": "mean: 3.025245537177943 msec\nrounds: 659"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 331.27839490623495,
            "unit": "iter/sec",
            "range": "stddev: 0.00003101856884692371",
            "extra": "mean: 3.0186091679266918 msec\nrounds: 661"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 158.40065918821313,
            "unit": "iter/sec",
            "range": "stddev: 0.00004486828747730541",
            "extra": "mean: 6.313105040881116 msec\nrounds: 318"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 809.4668753910943,
            "unit": "iter/sec",
            "range": "stddev: 0.000028450695511019512",
            "extra": "mean: 1.2353810024861727 msec\nrounds: 1609"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 834.0669572183366,
            "unit": "iter/sec",
            "range": "stddev: 0.00001793165482997376",
            "extra": "mean: 1.1989445108040966 msec\nrounds: 1666"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1400.5521702486362,
            "unit": "iter/sec",
            "range": "stddev: 0.000011354322327147239",
            "extra": "mean: 714.0041058395367 usec\nrounds: 2740"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 750.174920766978,
            "unit": "iter/sec",
            "range": "stddev: 0.00003248727892227914",
            "extra": "mean: 1.3330224355908904 msec\nrounds: 1506"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 456.35309303182396,
            "unit": "iter/sec",
            "range": "stddev: 0.000035803723901302274",
            "extra": "mean: 2.191285684855136 msec\nrounds: 898"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1409.2641457024288,
            "unit": "iter/sec",
            "range": "stddev: 0.000014222982898738598",
            "extra": "mean: 709.5901808397768 usec\nrounds: 2787"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28235.538174242753,
            "unit": "iter/sec",
            "range": "stddev: 0.0005186247568266859",
            "extra": "mean: 35.41636053929469 usec\nrounds: 43826"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2061.7383224177397,
            "unit": "iter/sec",
            "range": "stddev: 0.000021552629678033056",
            "extra": "mean: 485.0276046803697 usec\nrounds: 3974"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 42.00841506269241,
            "unit": "iter/sec",
            "range": "stddev: 0.00013174778104243283",
            "extra": "mean: 23.80475432142876 msec\nrounds: 84"
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
          "id": "e80723f361502f6bd17e5181a0dcfb102168a0f0",
          "message": "feat(http-api): serve()/bind_and_serve() helpers + 3 edge-case glob E2Es (#4680)\n\nfeat(http-api): serve()/bind_and_serve() helpers + 3 edge-case glob E2Es",
          "timestamp": "2026-08-19T15:40:51Z",
          "url": "https://github.com/nexi-lab/nexus/commit/e80723f361502f6bd17e5181a0dcfb102168a0f0"
        },
        "date": 1787306027664,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 226.05125625487648,
            "unit": "iter/sec",
            "range": "stddev: 0.0005189674653899685",
            "extra": "mean: 4.423775459458113 msec\nrounds: 111"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 426.5984456415548,
            "unit": "iter/sec",
            "range": "stddev: 0.0002591956788264481",
            "extra": "mean: 2.3441248092128313 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1354.101451328035,
            "unit": "iter/sec",
            "range": "stddev: 0.00005001577218645897",
            "extra": "mean: 738.4971037578092 usec\nrounds: 559"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 424.64782413124385,
            "unit": "iter/sec",
            "range": "stddev: 0.00037560490639527885",
            "extra": "mean: 2.3548925560747365 msec\nrounds: 214"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 161.3743342483976,
            "unit": "iter/sec",
            "range": "stddev: 0.0011722256815609963",
            "extra": "mean: 6.196772272725456 msec\nrounds: 88"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 24832.535645319833,
            "unit": "iter/sec",
            "range": "stddev: 0.0000028201050061270563",
            "extra": "mean: 40.269749907254 usec\nrounds: 10768"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1876.1830650827465,
            "unit": "iter/sec",
            "range": "stddev: 0.000016152124680345638",
            "extra": "mean: 532.9970292402658 usec\nrounds: 855"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31364.26495476057,
            "unit": "iter/sec",
            "range": "stddev: 0.0000033740880413937277",
            "extra": "mean: 31.88341896238881 usec\nrounds: 8965"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 60061.41437596744,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025223172972869107",
            "extra": "mean: 16.64962456162426 usec\nrounds: 11123"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 50623.02260585918,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026352518469257935",
            "extra": "mean: 19.753857998282754 usec\nrounds: 15028"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 39074.4223239375,
            "unit": "iter/sec",
            "range": "stddev: 0.000003169854442186259",
            "extra": "mean: 25.592188969800507 usec\nrounds: 13309"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 137.6250707027071,
            "unit": "iter/sec",
            "range": "stddev: 0.009300633568554853",
            "extra": "mean: 7.2661179746833 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 17334.326283805414,
            "unit": "iter/sec",
            "range": "stddev: 0.000010421575519779226",
            "extra": "mean: 57.68900294292081 usec\nrounds: 26164"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7956.46195893332,
            "unit": "iter/sec",
            "range": "stddev: 0.000023272731468128055",
            "extra": "mean: 125.68400441822318 usec\nrounds: 13580"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1660.5808970575033,
            "unit": "iter/sec",
            "range": "stddev: 0.000040831662843175315",
            "extra": "mean: 602.1989062815118 usec\nrounds: 3009"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7859.2745164278085,
            "unit": "iter/sec",
            "range": "stddev: 0.00002383951499354464",
            "extra": "mean: 127.23820728106075 usec\nrounds: 12553"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 577.1226810288864,
            "unit": "iter/sec",
            "range": "stddev: 0.0002520553906419397",
            "extra": "mean: 1.7327338412990694 msec\nrounds: 1109"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4214.64261764207,
            "unit": "iter/sec",
            "range": "stddev: 0.00047016330783607543",
            "extra": "mean: 237.26804161617417 usec\nrounds: 7449"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 81140.77012459186,
            "unit": "iter/sec",
            "range": "stddev: 0.0000020465971137170973",
            "extra": "mean: 12.324260645597738 usec\nrounds: 54365"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 17167.237797428512,
            "unit": "iter/sec",
            "range": "stddev: 0.000013869080939997964",
            "extra": "mean: 58.25048920507121 usec\nrounds: 29088"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5230116.516123984,
            "unit": "iter/sec",
            "range": "stddev: 1.4590260633933813e-8",
            "extra": "mean: 191.20032926935548 nsec\nrounds: 112461"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4219667.205418728,
            "unit": "iter/sec",
            "range": "stddev: 1.841172092204929e-8",
            "extra": "mean: 236.98551362435407 nsec\nrounds: 97805"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2438524.7877912656,
            "unit": "iter/sec",
            "range": "stddev: 2.607584788240555e-7",
            "extra": "mean: 410.0840003786743 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 20954.79713955659,
            "unit": "iter/sec",
            "range": "stddev: 0.0003858263767304693",
            "extra": "mean: 47.72176954709285 usec\nrounds: 60789"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 40010.99130921263,
            "unit": "iter/sec",
            "range": "stddev: 0.00000175721270424971",
            "extra": "mean: 24.99313231886228 usec\nrounds: 77283"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3961.224678845859,
            "unit": "iter/sec",
            "range": "stddev: 0.00000800073945700274",
            "extra": "mean: 252.44718012091138 usec\nrounds: 7606"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8026.291555863469,
            "unit": "iter/sec",
            "range": "stddev: 0.0000053606753182140476",
            "extra": "mean: 124.59054010684017 usec\nrounds: 13838"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1237.019749425424,
            "unit": "iter/sec",
            "range": "stddev: 0.00003508719380779091",
            "extra": "mean: 808.3945308589326 usec\nrounds: 2479"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 428.25947896826284,
            "unit": "iter/sec",
            "range": "stddev: 0.00003046851779694291",
            "extra": "mean: 2.3350329627475856 msec\nrounds: 859"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1866.7097751623755,
            "unit": "iter/sec",
            "range": "stddev: 0.00003131952435089596",
            "extra": "mean: 535.7019143016032 usec\nrounds: 3594"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4067.2780162793556,
            "unit": "iter/sec",
            "range": "stddev: 0.00000754166728009769",
            "extra": "mean: 245.86467804695954 usec\nrounds: 7967"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 397.2695699622711,
            "unit": "iter/sec",
            "range": "stddev: 0.0000338836965766845",
            "extra": "mean: 2.5171824766114623 msec\nrounds: 791"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 400.9530186230728,
            "unit": "iter/sec",
            "range": "stddev: 0.00002881679455042977",
            "extra": "mean: 2.4940577911949284 msec\nrounds: 795"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 925.6934122433718,
            "unit": "iter/sec",
            "range": "stddev: 0.00001813581175533604",
            "extra": "mean: 1.0802712720797591 msec\nrounds: 1823"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 350.0807960041753,
            "unit": "iter/sec",
            "range": "stddev: 0.00003289574307473964",
            "extra": "mean: 2.8564834501463867 msec\nrounds: 682"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 358.5524043513114,
            "unit": "iter/sec",
            "range": "stddev: 0.00003248817842936677",
            "extra": "mean: 2.7889925931725035 msec\nrounds: 703"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 328.8581702316371,
            "unit": "iter/sec",
            "range": "stddev: 0.00006638698073349887",
            "extra": "mean: 3.0408245575763933 msec\nrounds: 660"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 329.73931454191404,
            "unit": "iter/sec",
            "range": "stddev: 0.00003801926119617849",
            "extra": "mean: 3.032698728658536 msec\nrounds: 656"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 165.97760895664496,
            "unit": "iter/sec",
            "range": "stddev: 0.0001095582935981515",
            "extra": "mean: 6.024909060240832 msec\nrounds: 332"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 809.3314827436501,
            "unit": "iter/sec",
            "range": "stddev: 0.00002197694342454287",
            "extra": "mean: 1.2355876687386234 msec\nrounds: 1609"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 868.4026245742924,
            "unit": "iter/sec",
            "range": "stddev: 0.00003152993118730195",
            "extra": "mean: 1.1515395874007395 msec\nrounds: 1762"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1484.4184053540514,
            "unit": "iter/sec",
            "range": "stddev: 0.000013813927728296269",
            "extra": "mean: 673.6645115643713 usec\nrounds: 2940"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 799.3440816544485,
            "unit": "iter/sec",
            "range": "stddev: 0.000020925314800041012",
            "extra": "mean: 1.2510257133952158 msec\nrounds: 1605"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 484.28197387886934,
            "unit": "iter/sec",
            "range": "stddev: 0.000021209112885309868",
            "extra": "mean: 2.064912703626925 msec\nrounds: 965"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1465.583063832405,
            "unit": "iter/sec",
            "range": "stddev: 0.00005424581183821094",
            "extra": "mean: 682.32229525433 usec\nrounds: 2950"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28739.882836807432,
            "unit": "iter/sec",
            "range": "stddev: 0.0005031229956084558",
            "extra": "mean: 34.79485305066348 usec\nrounds: 45104"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2087.0871175627553,
            "unit": "iter/sec",
            "range": "stddev: 0.00002036924462705874",
            "extra": "mean: 479.13668365112295 usec\nrounds: 4141"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 42.69459094671713,
            "unit": "iter/sec",
            "range": "stddev: 0.0001527575077949019",
            "extra": "mean: 23.42217076743985 msec\nrounds: 86"
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
          "id": "ac467476497c7d45f2ec4e6eb50d210868a8e545",
          "message": "Merge pull request #4688 from nexi-lab/refactor/search-plugin-tests-shared-mock-kernel\n\nrefactor(search-plugin/tests): extract shared MockKernel + poison_handle to tests/common",
          "timestamp": "2026-08-22T00:27:43Z",
          "url": "https://github.com/nexi-lab/nexus/commit/ac467476497c7d45f2ec4e6eb50d210868a8e545"
        },
        "date": 1787391723052,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 228.18628489372648,
            "unit": "iter/sec",
            "range": "stddev: 0.002719752670577994",
            "extra": "mean: 4.382384333334194 msec\nrounds: 57"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 553.2980341744077,
            "unit": "iter/sec",
            "range": "stddev: 0.00024614250911637886",
            "extra": "mean: 1.8073442127661443 msec\nrounds: 188"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1996.0182375236216,
            "unit": "iter/sec",
            "range": "stddev: 0.000021946438805716433",
            "extra": "mean: 500.9974263765542 usec\nrounds: 781"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 444.9925305738153,
            "unit": "iter/sec",
            "range": "stddev: 0.000572665273833455",
            "extra": "mean: 2.2472287314811905 msec\nrounds: 216"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 164.5952350437011,
            "unit": "iter/sec",
            "range": "stddev: 0.003288280707495745",
            "extra": "mean: 6.0755100215051385 msec\nrounds: 93"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 34760.50059811494,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017027808327945965",
            "extra": "mean: 28.768285346679672 usec\nrounds: 14379"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 2621.3984575870513,
            "unit": "iter/sec",
            "range": "stddev: 0.00004482757965738378",
            "extra": "mean: 381.47577187501724 usec\nrounds: 1280"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 47028.98251323521,
            "unit": "iter/sec",
            "range": "stddev: 0.000002611899329990427",
            "extra": "mean: 21.26348363413079 usec\nrounds: 11579"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 101256.28467697193,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014415847363312712",
            "extra": "mean: 9.875930202161799 usec\nrounds: 13754"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 76986.51162932813,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014534969934831935",
            "extra": "mean: 12.989288368003526 usec\nrounds: 20349"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 64890.21247383352,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017904598130013037",
            "extra": "mean: 15.41064456220177 usec\nrounds: 17930"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 194.4457014625661,
            "unit": "iter/sec",
            "range": "stddev: 0.006146885562517105",
            "extra": "mean: 5.142823896225425 msec\nrounds: 106"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 35077.8880355674,
            "unit": "iter/sec",
            "range": "stddev: 0.000005042626083114802",
            "extra": "mean: 28.507987681186652 usec\nrounds: 45134"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 15029.960845126245,
            "unit": "iter/sec",
            "range": "stddev: 0.00001076279157985771",
            "extra": "mean: 66.53377279583994 usec\nrounds: 22548"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 3091.907832363756,
            "unit": "iter/sec",
            "range": "stddev: 0.000019224692395770047",
            "extra": "mean: 323.4249059861213 usec\nrounds: 5446"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 15059.077230266657,
            "unit": "iter/sec",
            "range": "stddev: 0.00001209605052784262",
            "extra": "mean: 66.40513125134511 usec\nrounds: 19436"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 1090.497684534406,
            "unit": "iter/sec",
            "range": "stddev: 0.00011225213485045858",
            "extra": "mean: 917.0124927197397 usec\nrounds: 2129"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 6102.965972377401,
            "unit": "iter/sec",
            "range": "stddev: 0.0003219404717305938",
            "extra": "mean: 163.85475595408758 usec\nrounds: 10203"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 132191.0410901643,
            "unit": "iter/sec",
            "range": "stddev: 0.0000010810938872581234",
            "extra": "mean: 7.564809171280558 usec\nrounds: 58487"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 35015.1491697861,
            "unit": "iter/sec",
            "range": "stddev: 0.000006408056794192266",
            "extra": "mean: 28.55906725260736 usec\nrounds: 53931"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 8421327.965055875,
            "unit": "iter/sec",
            "range": "stddev: 2.2451001849451247e-8",
            "extra": "mean: 118.74611749470859 nsec\nrounds: 184417"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 7024760.042693601,
            "unit": "iter/sec",
            "range": "stddev: 1.762499418053015e-8",
            "extra": "mean: 142.35361690967542 nsec\nrounds: 167715"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 3757293.841893335,
            "unit": "iter/sec",
            "range": "stddev: 1.6933867990412925e-7",
            "extra": "mean: 266.1490003390554 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 35669.83462280574,
            "unit": "iter/sec",
            "range": "stddev: 0.0002790697240133577",
            "extra": "mean: 28.034893084719926 usec\nrounds: 75658"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 43549.26145036668,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014572791412927668",
            "extra": "mean: 22.962501927609157 usec\nrounds: 84564"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 4443.699041663018,
            "unit": "iter/sec",
            "range": "stddev: 0.0000044114942658936035",
            "extra": "mean: 225.03774234579086 usec\nrounds: 8884"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 9270.09705920293,
            "unit": "iter/sec",
            "range": "stddev: 0.0000032834381166898672",
            "extra": "mean: 107.87373569160698 usec\nrounds: 18346"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1768.0080815739152,
            "unit": "iter/sec",
            "range": "stddev: 0.00001200159938081803",
            "extra": "mean: 565.608274318396 usec\nrounds: 3485"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 585.4267058172594,
            "unit": "iter/sec",
            "range": "stddev: 0.000020280342008082603",
            "extra": "mean: 1.7081557606839846 msec\nrounds: 1170"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 2842.498014493471,
            "unit": "iter/sec",
            "range": "stddev: 0.00003233154641696388",
            "extra": "mean: 351.803236062488 usec\nrounds: 5130"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 5637.503171850054,
            "unit": "iter/sec",
            "range": "stddev: 0.000007614564486322679",
            "extra": "mean: 177.38349221572693 usec\nrounds: 10341"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 539.1906648707528,
            "unit": "iter/sec",
            "range": "stddev: 0.000025793206400546477",
            "extra": "mean: 1.8546315156248225 msec\nrounds: 1088"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 551.0166229492015,
            "unit": "iter/sec",
            "range": "stddev: 0.000018736559059605574",
            "extra": "mean: 1.8148272817028794 msec\nrounds: 1104"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1325.0586995986607,
            "unit": "iter/sec",
            "range": "stddev: 0.000017998493335357035",
            "extra": "mean: 754.6835474555838 usec\nrounds: 2634"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 485.46553140967455,
            "unit": "iter/sec",
            "range": "stddev: 0.00002774290599413774",
            "extra": "mean: 2.059878478079055 msec\nrounds: 958"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 514.1782502961727,
            "unit": "iter/sec",
            "range": "stddev: 0.00003255433562178823",
            "extra": "mean: 1.944850836113718 msec\nrounds: 1019"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 483.2733232689301,
            "unit": "iter/sec",
            "range": "stddev: 0.00003836231942717135",
            "extra": "mean: 2.0692224293198236 msec\nrounds: 955"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 484.3736528672036,
            "unit": "iter/sec",
            "range": "stddev: 0.000017937360510957727",
            "extra": "mean: 2.0645218708337985 msec\nrounds: 960"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 230.1575491854764,
            "unit": "iter/sec",
            "range": "stddev: 0.0000549948844872115",
            "extra": "mean: 4.344849880175484 msec\nrounds: 459"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1187.6342223114148,
            "unit": "iter/sec",
            "range": "stddev: 0.000013397198320100282",
            "extra": "mean: 842.0100913341528 usec\nrounds: 2354"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 1236.1078151287331,
            "unit": "iter/sec",
            "range": "stddev: 0.00002912329325862747",
            "extra": "mean: 808.9909211486186 usec\nrounds: 2473"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 2108.780844288759,
            "unit": "iter/sec",
            "range": "stddev: 0.000010112961113546204",
            "extra": "mean: 474.20764595254843 usec\nrounds: 4200"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 1148.0195481186208,
            "unit": "iter/sec",
            "range": "stddev: 0.00011599740881712963",
            "extra": "mean: 871.0653068920335 usec\nrounds: 2307"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 683.3192318841539,
            "unit": "iter/sec",
            "range": "stddev: 0.00010654867482526106",
            "extra": "mean: 1.463444834184814 msec\nrounds: 1369"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 2124.6101209254416,
            "unit": "iter/sec",
            "range": "stddev: 0.00001973570479451941",
            "extra": "mean: 470.6745911407116 usec\nrounds: 3928"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 39139.09343496161,
            "unit": "iter/sec",
            "range": "stddev: 0.0005547410171900184",
            "extra": "mean: 25.549901958299685 usec\nrounds: 62606"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 3054.514501697302,
            "unit": "iter/sec",
            "range": "stddev: 0.000021833408831431605",
            "extra": "mean: 327.38426988784306 usec\nrounds: 5443"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 60.546368834330906,
            "unit": "iter/sec",
            "range": "stddev: 0.0003247966618970871",
            "extra": "mean: 16.516267106558196 msec\nrounds: 122"
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
          "id": "a89e0aec2e59c6450a187b25bae34f12ce2798cc",
          "message": "docs(architecture): mark sudocode FR-B + FR-C as landed (#4693)\n\ndocs(architecture): mark sudocode FR-B + FR-C as landed in search doc",
          "timestamp": "2026-08-22T16:45:43Z",
          "url": "https://github.com/nexi-lab/nexus/commit/a89e0aec2e59c6450a187b25bae34f12ce2798cc"
        },
        "date": 1787478206163,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 231.90070509265772,
            "unit": "iter/sec",
            "range": "stddev: 0.0006669706990519062",
            "extra": "mean: 4.312190424778753 msec\nrounds: 113"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 424.0029131453721,
            "unit": "iter/sec",
            "range": "stddev: 0.0003186646348029322",
            "extra": "mean: 2.3584743618427537 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1380.30158596367,
            "unit": "iter/sec",
            "range": "stddev: 0.000023438545552047048",
            "extra": "mean: 724.4793530406914 usec\nrounds: 592"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 427.16064143537653,
            "unit": "iter/sec",
            "range": "stddev: 0.0009244647336956668",
            "extra": "mean: 2.3410396534655593 msec\nrounds: 202"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 173.29560419326793,
            "unit": "iter/sec",
            "range": "stddev: 0.0005742696420093405",
            "extra": "mean: 5.770486820223955 msec\nrounds: 89"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 24636.388467256864,
            "unit": "iter/sec",
            "range": "stddev: 0.000002984423996372233",
            "extra": "mean: 40.5903649931911 usec\nrounds: 10318"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1855.1234028828817,
            "unit": "iter/sec",
            "range": "stddev: 0.000020098892083297264",
            "extra": "mean: 539.0476980916683 usec\nrounds: 891"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31799.051883359756,
            "unit": "iter/sec",
            "range": "stddev: 0.0000041040525527841535",
            "extra": "mean: 31.44747848671846 usec\nrounds: 8855"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 52822.875381994425,
            "unit": "iter/sec",
            "range": "stddev: 0.0000028013819500077643",
            "extra": "mean: 18.931192078590765 usec\nrounds: 9897"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 42915.64443080617,
            "unit": "iter/sec",
            "range": "stddev: 0.0000031971132108003704",
            "extra": "mean: 23.301525894882506 usec\nrounds: 13632"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 34603.86348920668,
            "unit": "iter/sec",
            "range": "stddev: 0.0000036965585777215995",
            "extra": "mean: 28.89850725228733 usec\nrounds: 12272"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 135.3075764470117,
            "unit": "iter/sec",
            "range": "stddev: 0.009728349176743693",
            "extra": "mean: 7.390569148148283 msec\nrounds: 81"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 15649.379806659153,
            "unit": "iter/sec",
            "range": "stddev: 0.000012419905793500748",
            "extra": "mean: 63.900295881021314 usec\nrounds: 18281"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7595.898328921464,
            "unit": "iter/sec",
            "range": "stddev: 0.000023300333203841923",
            "extra": "mean: 131.6499980249195 usec\nrounds: 12657"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1571.5618233526786,
            "unit": "iter/sec",
            "range": "stddev: 0.00004546403002478571",
            "extra": "mean: 636.3096794160208 usec\nrounds: 2876"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7595.427323839139,
            "unit": "iter/sec",
            "range": "stddev: 0.000025873198583336062",
            "extra": "mean: 131.65816186027914 usec\nrounds: 10299"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 552.8248134029193,
            "unit": "iter/sec",
            "range": "stddev: 0.00025325191840833996",
            "extra": "mean: 1.8088913083414053 msec\nrounds: 1067"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4183.561872686199,
            "unit": "iter/sec",
            "range": "stddev: 0.0005239627499621699",
            "extra": "mean: 239.03076623028778 usec\nrounds: 7409"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 81342.35424140787,
            "unit": "iter/sec",
            "range": "stddev: 0.000002270791741445004",
            "extra": "mean: 12.293718436427346 usec\nrounds: 50731"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 16581.650140639154,
            "unit": "iter/sec",
            "range": "stddev: 0.00001371147291842839",
            "extra": "mean: 60.307628705128025 usec\nrounds: 27528"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5411098.730046173,
            "unit": "iter/sec",
            "range": "stddev: 1.5051854615378682e-8",
            "extra": "mean: 184.80535098117994 nsec\nrounds: 113553"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4365052.331695467,
            "unit": "iter/sec",
            "range": "stddev: 1.961316047622317e-8",
            "extra": "mean: 229.09232788317598 nsec\nrounds: 99816"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2421342.684853349,
            "unit": "iter/sec",
            "range": "stddev: 1.48122051259933e-7",
            "extra": "mean: 412.99399967442696 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 20746.29519162856,
            "unit": "iter/sec",
            "range": "stddev: 0.00044998514011019707",
            "extra": "mean: 48.201377198349846 usec\nrounds: 59645"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39896.090069643724,
            "unit": "iter/sec",
            "range": "stddev: 0.0000020533468098501485",
            "extra": "mean: 25.0651128532739 usec\nrounds: 67034"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3911.055625276177,
            "unit": "iter/sec",
            "range": "stddev: 0.000010632302144973631",
            "extra": "mean: 255.68544551942688 usec\nrounds: 7131"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7753.100513629414,
            "unit": "iter/sec",
            "range": "stddev: 0.000004869243040270443",
            "extra": "mean: 128.98065725345225 usec\nrounds: 13704"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1246.0626005792808,
            "unit": "iter/sec",
            "range": "stddev: 0.000023633728107344083",
            "extra": "mean: 802.5278983055193 usec\nrounds: 2478"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 427.44394224960797,
            "unit": "iter/sec",
            "range": "stddev: 0.00004440932953606666",
            "extra": "mean: 2.3394880618428444 msec\nrounds: 857"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1836.7372647287411,
            "unit": "iter/sec",
            "range": "stddev: 0.0000432799327315095",
            "extra": "mean: 544.4436823944361 usec\nrounds: 3391"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4078.9401510794637,
            "unit": "iter/sec",
            "range": "stddev: 0.000009517712033845742",
            "extra": "mean: 245.16172411486767 usec\nrounds: 7398"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 398.1171602699224,
            "unit": "iter/sec",
            "range": "stddev: 0.000029217742144713087",
            "extra": "mean: 2.5118234022416988 msec\nrounds: 803"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 398.35439729029895,
            "unit": "iter/sec",
            "range": "stddev: 0.000026547060800797355",
            "extra": "mean: 2.5103275043585236 msec\nrounds: 803"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 912.9366311059492,
            "unit": "iter/sec",
            "range": "stddev: 0.000045325620737125184",
            "extra": "mean: 1.0953662783676241 msec\nrounds: 1789"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 339.34477880548764,
            "unit": "iter/sec",
            "range": "stddev: 0.000030070258638368296",
            "extra": "mean: 2.946855418020738 msec\nrounds: 677"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 358.2384383576171,
            "unit": "iter/sec",
            "range": "stddev: 0.00002442619227388661",
            "extra": "mean: 2.7914369116407727 msec\nrounds: 713"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 333.10848359048487,
            "unit": "iter/sec",
            "range": "stddev: 0.00003040471411901841",
            "extra": "mean: 3.0020250136570366 msec\nrounds: 659"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 321.3359509589317,
            "unit": "iter/sec",
            "range": "stddev: 0.00003711447129593778",
            "extra": "mean: 3.112007844176156 msec\nrounds: 661"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 159.62261747725864,
            "unit": "iter/sec",
            "range": "stddev: 0.0001308517350103032",
            "extra": "mean: 6.264776356912387 msec\nrounds: 311"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 801.2184092473559,
            "unit": "iter/sec",
            "range": "stddev: 0.000018899213263660054",
            "extra": "mean: 1.2480991305970797 msec\nrounds: 1608"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 822.8866253882966,
            "unit": "iter/sec",
            "range": "stddev: 0.00001928041208038859",
            "extra": "mean: 1.2152342365852997 msec\nrounds: 1640"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1398.4023699462903,
            "unit": "iter/sec",
            "range": "stddev: 0.000013071785613720768",
            "extra": "mean: 715.1017629056278 usec\nrounds: 2712"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 754.0228372598716,
            "unit": "iter/sec",
            "range": "stddev: 0.0000189910566509843",
            "extra": "mean: 1.3262197782152227 msec\nrounds: 1524"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 447.466061417128,
            "unit": "iter/sec",
            "range": "stddev: 0.00006214867450970372",
            "extra": "mean: 2.234806360135992 msec\nrounds: 883"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1402.7662311428353,
            "unit": "iter/sec",
            "range": "stddev: 0.000011434201688130372",
            "extra": "mean: 712.8771550091413 usec\nrounds: 2845"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28160.4719991672,
            "unit": "iter/sec",
            "range": "stddev: 0.0005541485051720614",
            "extra": "mean: 35.510768428511184 usec\nrounds: 43913"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2042.5433507286248,
            "unit": "iter/sec",
            "range": "stddev: 0.000020030896273839146",
            "extra": "mean: 489.5856920947483 usec\nrounds: 3972"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 41.36025072035071,
            "unit": "iter/sec",
            "range": "stddev: 0.00023337006443679032",
            "extra": "mean: 24.177803146342256 msec\nrounds: 82"
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
          "id": "f93fb36bdec6e965651eb600dce8cbffb3876af2",
          "message": "Merge pull request #4701 from nexi-lab/refactor/spawn-agentstate-ssot\n\nrefactor(nexusd): delete cohost.rs; inject sudocode's SudoCodeSpawnAdapter (spawn AgentState collapse)",
          "timestamp": "2026-08-24T08:46:21Z",
          "url": "https://github.com/nexi-lab/nexus/commit/f93fb36bdec6e965651eb600dce8cbffb3876af2"
        },
        "date": 1787565858776,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 261.5809507327997,
            "unit": "iter/sec",
            "range": "stddev: 0.00027119179769923875",
            "extra": "mean: 3.8229083471046876 msec\nrounds: 121"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 445.9080965926452,
            "unit": "iter/sec",
            "range": "stddev: 0.0003615537645483514",
            "extra": "mean: 2.242614582783725 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1680.8351632835252,
            "unit": "iter/sec",
            "range": "stddev: 0.000020431634405674076",
            "extra": "mean: 594.9423369073811 usec\nrounds: 653"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 489.4739764242425,
            "unit": "iter/sec",
            "range": "stddev: 0.00013332334980500318",
            "extra": "mean: 2.0430095330201348 msec\nrounds: 212"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 188.15236303688354,
            "unit": "iter/sec",
            "range": "stddev: 0.0006664084987380107",
            "extra": "mean: 5.314841567012208 msec\nrounds: 97"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25834.193764958378,
            "unit": "iter/sec",
            "range": "stddev: 0.000002616073938954411",
            "extra": "mean: 38.70838815788417 usec\nrounds: 7144"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1952.2439573718686,
            "unit": "iter/sec",
            "range": "stddev: 0.00001619248600795467",
            "extra": "mean: 512.2310642703745 usec\nrounds: 918"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 34880.061957060476,
            "unit": "iter/sec",
            "range": "stddev: 0.000002034785784118954",
            "extra": "mean: 28.669673844933598 usec\nrounds: 9149"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 67953.83087156227,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014456062939564853",
            "extra": "mean: 14.715873809823519 usec\nrounds: 10294"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 54335.76526345931,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018484667850020685",
            "extra": "mean: 18.404084218769583 usec\nrounds: 16184"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 45728.69031537745,
            "unit": "iter/sec",
            "range": "stddev: 0.000001683640968914461",
            "extra": "mean: 21.868109344555716 usec\nrounds: 14971"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 131.26298712199454,
            "unit": "iter/sec",
            "range": "stddev: 0.012708764047337227",
            "extra": "mean: 7.618293792679041 msec\nrounds: 82"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 30635.80163564243,
            "unit": "iter/sec",
            "range": "stddev: 0.000007232855050412052",
            "extra": "mean: 32.64154833920115 usec\nrounds: 31579"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 12893.910514483388,
            "unit": "iter/sec",
            "range": "stddev: 0.000016253259494160214",
            "extra": "mean: 77.55599039381624 usec\nrounds: 15927"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 2440.866978023502,
            "unit": "iter/sec",
            "range": "stddev: 0.00003751190640400713",
            "extra": "mean: 409.690494813344 usec\nrounds: 4242"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 12703.96914329736,
            "unit": "iter/sec",
            "range": "stddev: 0.000018859632794527583",
            "extra": "mean: 78.71555643124356 usec\nrounds: 16117"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 830.938857251938,
            "unit": "iter/sec",
            "range": "stddev: 0.00015851254041835742",
            "extra": "mean: 1.203457981622351 msec\nrounds: 1578"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4374.953255643859,
            "unit": "iter/sec",
            "range": "stddev: 0.000535797629479803",
            "extra": "mean: 228.57387075163862 usec\nrounds: 7768"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 90210.79111358697,
            "unit": "iter/sec",
            "range": "stddev: 0.0000020993911582866915",
            "extra": "mean: 11.085148324892437 usec\nrounds: 49432"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 30165.43159755472,
            "unit": "iter/sec",
            "range": "stddev: 0.000009670292807181622",
            "extra": "mean: 33.15052850366186 usec\nrounds: 38346"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5346478.372470977,
            "unit": "iter/sec",
            "range": "stddev: 3.30000090763396e-8",
            "extra": "mean: 187.0390059275281 nsec\nrounds: 107487"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4550743.088723649,
            "unit": "iter/sec",
            "range": "stddev: 1.782815875074799e-8",
            "extra": "mean: 219.74433197029165 nsec\nrounds: 104232"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2568957.2376478836,
            "unit": "iter/sec",
            "range": "stddev: 1.727910903811236e-7",
            "extra": "mean: 389.26299953345733 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 30033.785641896462,
            "unit": "iter/sec",
            "range": "stddev: 0.0005307598266125041",
            "extra": "mean: 33.29583596032004 usec\nrounds: 62174"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 36176.11736124632,
            "unit": "iter/sec",
            "range": "stddev: 0.0000021880113665304603",
            "extra": "mean: 27.642546324533175 usec\nrounds: 69585"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3590.9598723998324,
            "unit": "iter/sec",
            "range": "stddev: 0.000021681258726327032",
            "extra": "mean: 278.4770745242836 usec\nrounds: 5931"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7319.647843868582,
            "unit": "iter/sec",
            "range": "stddev: 0.000007050370503689432",
            "extra": "mean: 136.61859440924687 usec\nrounds: 14739"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1322.1286925170791,
            "unit": "iter/sec",
            "range": "stddev: 0.000013185060881217448",
            "extra": "mean: 756.3560231766788 usec\nrounds: 2632"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 435.86291455854024,
            "unit": "iter/sec",
            "range": "stddev: 0.000028457559833489864",
            "extra": "mean: 2.2942993464191392 msec\nrounds: 866"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1897.328147329635,
            "unit": "iter/sec",
            "range": "stddev: 0.00004191875407585693",
            "extra": "mean: 527.0569571254368 usec\nrounds: 3242"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4374.7268769574985,
            "unit": "iter/sec",
            "range": "stddev: 0.000007184683622912645",
            "extra": "mean: 228.58569874777467 usec\nrounds: 8219"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 417.70083606224944,
            "unit": "iter/sec",
            "range": "stddev: 0.000036027086519900354",
            "extra": "mean: 2.3940579325318163 msec\nrounds: 830"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 428.31828290113776,
            "unit": "iter/sec",
            "range": "stddev: 0.00002783102086979936",
            "extra": "mean: 2.3347123854407466 msec\nrounds: 838"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1009.7636098413202,
            "unit": "iter/sec",
            "range": "stddev: 0.00002315329520382152",
            "extra": "mean: 990.3307964892352 usec\nrounds: 1936"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 349.3180396223824,
            "unit": "iter/sec",
            "range": "stddev: 0.000027235613905494483",
            "extra": "mean: 2.8627207489227118 msec\nrounds: 697"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 387.9460532723731,
            "unit": "iter/sec",
            "range": "stddev: 0.00002663083772600255",
            "extra": "mean: 2.5776779827114518 msec\nrounds: 752"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 357.9629540435655,
            "unit": "iter/sec",
            "range": "stddev: 0.0000487544058644421",
            "extra": "mean: 2.793585170487491 msec\nrounds: 698"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 357.4850282464435,
            "unit": "iter/sec",
            "range": "stddev: 0.00004074122488059941",
            "extra": "mean: 2.79731994625134 msec\nrounds: 707"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 166.73353279215902,
            "unit": "iter/sec",
            "range": "stddev: 0.00005298311826457873",
            "extra": "mean: 5.997593784847981 msec\nrounds: 330"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 883.5946500352472,
            "unit": "iter/sec",
            "range": "stddev: 0.000017977553647589804",
            "extra": "mean: 1.1317406685974265 msec\nrounds: 1726"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 979.4952543253249,
            "unit": "iter/sec",
            "range": "stddev: 0.000012821323426786864",
            "extra": "mean: 1.0209339918535885 msec\nrounds: 1964"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1659.0786268741888,
            "unit": "iter/sec",
            "range": "stddev: 0.00005103831177564938",
            "extra": "mean: 602.7441881305316 usec\nrounds: 3370"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 919.4721987747118,
            "unit": "iter/sec",
            "range": "stddev: 0.00001666636335342915",
            "extra": "mean: 1.0875804633708333 msec\nrounds: 1720"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 559.7565045802543,
            "unit": "iter/sec",
            "range": "stddev: 0.000025624005629025584",
            "extra": "mean: 1.7864910757041976 msec\nrounds: 1136"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1625.5725995643566,
            "unit": "iter/sec",
            "range": "stddev: 0.000012904380799624718",
            "extra": "mean: 615.1678493276731 usec\nrounds: 3345"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28699.2297108045,
            "unit": "iter/sec",
            "range": "stddev: 0.0006645599866514679",
            "extra": "mean: 34.84414076882093 usec\nrounds: 46303"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2089.182681773148,
            "unit": "iter/sec",
            "range": "stddev: 0.000026179532829795105",
            "extra": "mean: 478.6560834169236 usec\nrounds: 3992"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 42.05400392249492,
            "unit": "iter/sec",
            "range": "stddev: 0.00012161000562011394",
            "extra": "mean: 23.77894865475804 msec\nrounds: 84"
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
          "id": "07f04ab5b07f4e28e0263e4c4712d0dcc8ea9238",
          "message": "Merge pull request #4708 from nexi-lab/fix/ann-index-hnsw-flaky-recall-assertions\n\nfix(search-plugin): de-flake ann_index tests — assert invariants, not HNSW exhaustive recall",
          "timestamp": "2026-08-25T08:07:23Z",
          "url": "https://github.com/nexi-lab/nexus/commit/07f04ab5b07f4e28e0263e4c4712d0dcc8ea9238"
        },
        "date": 1787651560514,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 229.33314828214594,
            "unit": "iter/sec",
            "range": "stddev: 0.0011268188593510464",
            "extra": "mean: 4.360468634781534 msec\nrounds: 115"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 424.4246610453937,
            "unit": "iter/sec",
            "range": "stddev: 0.0002892706938444881",
            "extra": "mean: 2.3561307619046348 msec\nrounds: 147"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1385.7416234863742,
            "unit": "iter/sec",
            "range": "stddev: 0.000034833572219605694",
            "extra": "mean: 721.635247907261 usec\nrounds: 597"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 439.17474337391826,
            "unit": "iter/sec",
            "range": "stddev: 0.0003547247430813847",
            "extra": "mean: 2.2769979719634943 msec\nrounds: 214"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 175.5015470307201,
            "unit": "iter/sec",
            "range": "stddev: 0.00047464458258698053",
            "extra": "mean: 5.697955470586013 msec\nrounds: 85"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 23919.3749689263,
            "unit": "iter/sec",
            "range": "stddev: 0.0000063852986402894945",
            "extra": "mean: 41.8071124893147 usec\nrounds: 10481"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1867.0443299284605,
            "unit": "iter/sec",
            "range": "stddev: 0.000011862842041502008",
            "extra": "mean: 535.605922135934 usec\nrounds: 899"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 30539.98969539095,
            "unit": "iter/sec",
            "range": "stddev: 0.0000037377868196403554",
            "extra": "mean: 32.743953418914174 usec\nrounds: 8716"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 62995.084550463434,
            "unit": "iter/sec",
            "range": "stddev: 0.000002460057620921138",
            "extra": "mean: 15.874254430104472 usec\nrounds: 10722"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 49786.774767025316,
            "unit": "iter/sec",
            "range": "stddev: 0.000002917279711979388",
            "extra": "mean: 20.085655370918264 usec\nrounds: 15286"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 38896.14842062583,
            "unit": "iter/sec",
            "range": "stddev: 0.000003448318520250574",
            "extra": "mean: 25.709486430016817 usec\nrounds: 12933"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 138.11399541795222,
            "unit": "iter/sec",
            "range": "stddev: 0.009356196255932743",
            "extra": "mean: 7.240395855422619 msec\nrounds: 83"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 17064.195622126503,
            "unit": "iter/sec",
            "range": "stddev: 0.000010411682244403459",
            "extra": "mean: 58.60223488667332 usec\nrounds: 25408"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7753.661760756136,
            "unit": "iter/sec",
            "range": "stddev: 0.000023706415342539075",
            "extra": "mean: 128.97132101652062 usec\nrounds: 13180"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1632.5444896457275,
            "unit": "iter/sec",
            "range": "stddev: 0.000040319102725578",
            "extra": "mean: 612.5407340151608 usec\nrounds: 3128"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7722.665340138011,
            "unit": "iter/sec",
            "range": "stddev: 0.00002572854184018315",
            "extra": "mean: 129.4889725186679 usec\nrounds: 11899"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 567.9826193797512,
            "unit": "iter/sec",
            "range": "stddev: 0.0002417043602678163",
            "extra": "mean: 1.7606172546125103 msec\nrounds: 1084"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4224.32839902082,
            "unit": "iter/sec",
            "range": "stddev: 0.0005602447717421807",
            "extra": "mean: 236.72401990143462 usec\nrounds: 7085"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 81933.0734332939,
            "unit": "iter/sec",
            "range": "stddev: 0.0000021061910787467462",
            "extra": "mean: 12.205083467473651 usec\nrounds: 50978"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 16973.45338442928,
            "unit": "iter/sec",
            "range": "stddev: 0.000012981332628960884",
            "extra": "mean: 58.91552987781245 usec\nrounds: 27897"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5254954.512204849,
            "unit": "iter/sec",
            "range": "stddev: 1.5299634758948218e-8",
            "extra": "mean: 190.2966044096973 nsec\nrounds: 113883"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4249926.177912844,
            "unit": "iter/sec",
            "range": "stddev: 1.8243847122628522e-8",
            "extra": "mean: 235.29820475401857 nsec\nrounds: 102855"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2293904.178834416,
            "unit": "iter/sec",
            "range": "stddev: 7.227738627287246e-7",
            "extra": "mean: 435.9380000380497 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 21144.07706549152,
            "unit": "iter/sec",
            "range": "stddev: 0.00037456059602027954",
            "extra": "mean: 47.294568445934374 usec\nrounds: 61786"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 40030.93128520467,
            "unit": "iter/sec",
            "range": "stddev: 0.000001998818897948845",
            "extra": "mean: 24.980682884327432 usec\nrounds: 67786"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3928.105306551488,
            "unit": "iter/sec",
            "range": "stddev: 0.000013469220403644065",
            "extra": "mean: 254.57565975437336 usec\nrounds: 7089"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7706.920704871722,
            "unit": "iter/sec",
            "range": "stddev: 0.0000064868967014389785",
            "extra": "mean: 129.75350834580107 usec\nrounds: 14199"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1243.3532097500333,
            "unit": "iter/sec",
            "range": "stddev: 0.000019288338797404264",
            "extra": "mean: 804.276686751822 usec\nrounds: 2506"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 428.2109562712609,
            "unit": "iter/sec",
            "range": "stddev: 0.00003873011812271458",
            "extra": "mean: 2.3352975568577587 msec\nrounds: 853"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1860.277724521453,
            "unit": "iter/sec",
            "range": "stddev: 0.00003573182385341467",
            "extra": "mean: 537.5541441035343 usec\nrounds: 3553"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 3990.484879726508,
            "unit": "iter/sec",
            "range": "stddev: 0.00000809372175893635",
            "extra": "mean: 250.5961130389087 usec\nrounds: 7723"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 386.82809841507157,
            "unit": "iter/sec",
            "range": "stddev: 0.0002077880804608566",
            "extra": "mean: 2.5851276163682066 msec\nrounds: 782"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 391.52468935112324,
            "unit": "iter/sec",
            "range": "stddev: 0.000032853458805049256",
            "extra": "mean: 2.5541173448277488 msec\nrounds: 783"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 897.49880242717,
            "unit": "iter/sec",
            "range": "stddev: 0.00002303959370924569",
            "extra": "mean: 1.1142076148688207 msec\nrounds: 1789"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 333.19340188490094,
            "unit": "iter/sec",
            "range": "stddev: 0.000027580660278149413",
            "extra": "mean: 3.0012599119398 msec\nrounds: 670"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 349.3697663875692,
            "unit": "iter/sec",
            "range": "stddev: 0.000030790373655704246",
            "extra": "mean: 2.862296902046933 msec\nrounds: 684"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 324.734478607512,
            "unit": "iter/sec",
            "range": "stddev: 0.00008913465258496833",
            "extra": "mean: 3.079438944358732 msec\nrounds: 647"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 326.3890446969161,
            "unit": "iter/sec",
            "range": "stddev: 0.000035424770811076735",
            "extra": "mean: 3.063828324656537 msec\nrounds: 653"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 157.9668880543007,
            "unit": "iter/sec",
            "range": "stddev: 0.00005758592052423147",
            "extra": "mean: 6.3304405899054785 msec\nrounds: 317"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 796.651028797458,
            "unit": "iter/sec",
            "range": "stddev: 0.000024978301631132344",
            "extra": "mean: 1.2552547650751125 msec\nrounds: 1592"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 835.3683886279399,
            "unit": "iter/sec",
            "range": "stddev: 0.000028776695295435488",
            "extra": "mean: 1.197076659367565 msec\nrounds: 1644"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1426.0689024470157,
            "unit": "iter/sec",
            "range": "stddev: 0.000014810739796901474",
            "extra": "mean: 701.2283896550041 usec\nrounds: 2900"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 769.0759968140324,
            "unit": "iter/sec",
            "range": "stddev: 0.000023183051697378737",
            "extra": "mean: 1.3002616180229152 msec\nrounds: 1487"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 456.64904867543777,
            "unit": "iter/sec",
            "range": "stddev: 0.0001340120773920068",
            "extra": "mean: 2.1898655059079024 msec\nrounds: 931"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1429.8083421854828,
            "unit": "iter/sec",
            "range": "stddev: 0.000014108375473152615",
            "extra": "mean: 699.3944366497999 usec\nrounds: 2794"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28781.519512774925,
            "unit": "iter/sec",
            "range": "stddev: 0.0005486136583404967",
            "extra": "mean: 34.74451720855605 usec\nrounds: 45094"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2080.0976519118994,
            "unit": "iter/sec",
            "range": "stddev: 0.00003729843068307374",
            "extra": "mean: 480.7466606583882 usec\nrounds: 3828"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 42.78707674863385,
            "unit": "iter/sec",
            "range": "stddev: 0.00013856112554978288",
            "extra": "mean: 23.37154290476105 msec\nrounds: 84"
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
          "id": "3d1383a7f0f4cc7f09f458bf21899d400d5bc3e5",
          "message": "Merge pull request #4712 from nexi-lab/fix/runtime-bridge-pins\n\nchore(deps): bump nexus-vfs 08460a79 + sudocode 470939a5 (runtime-flavor bridge)",
          "timestamp": "2026-08-26T08:06:45Z",
          "url": "https://github.com/nexi-lab/nexus/commit/3d1383a7f0f4cc7f09f458bf21899d400d5bc3e5"
        },
        "date": 1787738266602,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 86.91169220828317,
            "unit": "iter/sec",
            "range": "stddev: 0.02837394404982757",
            "extra": "mean: 11.505931763513567 msec\nrounds: 148"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 863.3743544218769,
            "unit": "iter/sec",
            "range": "stddev: 0.00020623970001650886",
            "extra": "mean: 1.1582461244978823 msec\nrounds: 249"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 3359.3422154784957,
            "unit": "iter/sec",
            "range": "stddev: 0.00001076093681516437",
            "extra": "mean: 297.6773236714029 usec\nrounds: 1035"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 308.10308860577464,
            "unit": "iter/sec",
            "range": "stddev: 0.004911500721683464",
            "extra": "mean: 3.2456669114392565 msec\nrounds: 271"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 79.30747450561928,
            "unit": "iter/sec",
            "range": "stddev: 0.03972958273640501",
            "extra": "mean: 12.609151990196656 msec\nrounds: 102"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 44852.27791412758,
            "unit": "iter/sec",
            "range": "stddev: 0.0000011371582121899754",
            "extra": "mean: 22.295411660352254 usec\nrounds: 18044"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 3651.396613502906,
            "unit": "iter/sec",
            "range": "stddev: 0.0000048662518593701545",
            "extra": "mean: 273.8678116482851 usec\nrounds: 1614"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 65453.75769164592,
            "unit": "iter/sec",
            "range": "stddev: 0.0000011973195789110488",
            "extra": "mean: 15.277961652118153 usec\nrounds: 14890"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 134699.22320594633,
            "unit": "iter/sec",
            "range": "stddev: 9.799277250018114e-7",
            "extra": "mean: 7.42394778677428 usec\nrounds: 12832"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 102649.76391741022,
            "unit": "iter/sec",
            "range": "stddev: 9.403070897422746e-7",
            "extra": "mean: 9.741863613096845 usec\nrounds: 27312"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 84636.553423257,
            "unit": "iter/sec",
            "range": "stddev: 8.503632425190337e-7",
            "extra": "mean: 11.815225922529274 usec\nrounds: 24473"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 233.31484803719877,
            "unit": "iter/sec",
            "range": "stddev: 0.01082891905975087",
            "extra": "mean: 4.286053838461941 msec\nrounds: 130"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 60510.115900736884,
            "unit": "iter/sec",
            "range": "stddev: 0.00000474918449772492",
            "extra": "mean: 16.52616236333836 usec\nrounds: 64214"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 24247.12391821753,
            "unit": "iter/sec",
            "range": "stddev: 0.000007013911981249402",
            "extra": "mean: 41.242004757878625 usec\nrounds: 32998"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 4609.04251876096,
            "unit": "iter/sec",
            "range": "stddev: 0.000014072651988136941",
            "extra": "mean: 216.96480254402778 usec\nrounds: 8255"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 24049.909490984413,
            "unit": "iter/sec",
            "range": "stddev: 0.000007768756179687383",
            "extra": "mean: 41.580198061654656 usec\nrounds: 30026"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 1579.5586703140025,
            "unit": "iter/sec",
            "range": "stddev: 0.00007049058329307008",
            "extra": "mean: 633.0882282461903 usec\nrounds: 2988"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 7785.506464575794,
            "unit": "iter/sec",
            "range": "stddev: 0.00017229994157063624",
            "extra": "mean: 128.4437954743239 usec\nrounds: 12639"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 170805.79516651083,
            "unit": "iter/sec",
            "range": "stddev: 0.0000010100483916151924",
            "extra": "mean: 5.854602292768494 usec\nrounds: 80949"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 59610.41400172088,
            "unit": "iter/sec",
            "range": "stddev: 0.000004457276971100225",
            "extra": "mean: 16.775592264316955 usec\nrounds: 75934"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 10876295.593375893,
            "unit": "iter/sec",
            "range": "stddev: 9.13084688730188e-9",
            "extra": "mean: 91.9430693488177 nsec\nrounds: 237728"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 9375021.523716977,
            "unit": "iter/sec",
            "range": "stddev: 9.067613233550468e-9",
            "extra": "mean: 106.66642177516019 nsec\nrounds: 218723"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 4077521.8451672434,
            "unit": "iter/sec",
            "range": "stddev: 5.650961527443694e-7",
            "extra": "mean: 245.24700000938537 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 48422.45782843772,
            "unit": "iter/sec",
            "range": "stddev: 0.00041505239768267825",
            "extra": "mean: 20.65157459670947 usec\nrounds: 95279"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 49177.14492777913,
            "unit": "iter/sec",
            "range": "stddev: 0.000004556613265697197",
            "extra": "mean: 20.334649387811876 usec\nrounds: 96708"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 5153.235727898077,
            "unit": "iter/sec",
            "range": "stddev: 0.000013957941001082164",
            "extra": "mean: 194.05283453002139 usec\nrounds: 9748"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 12449.712189493459,
            "unit": "iter/sec",
            "range": "stddev: 0.0000033589530488231664",
            "extra": "mean: 80.32314199551685 usec\nrounds: 24867"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 2388.469589728114,
            "unit": "iter/sec",
            "range": "stddev: 0.000018106868402399136",
            "extra": "mean: 418.67813779192085 usec\nrounds: 4964"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 704.3457326740964,
            "unit": "iter/sec",
            "range": "stddev: 0.00013058546801918784",
            "extra": "mean: 1.4197573061221398 msec\nrounds: 1421"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 3481.5194838173525,
            "unit": "iter/sec",
            "range": "stddev: 0.000018509711003032178",
            "extra": "mean: 287.23090726568 usec\nrounds: 4583"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 7418.106268774304,
            "unit": "iter/sec",
            "range": "stddev: 0.000005374667980408094",
            "extra": "mean: 134.80529447379166 usec\nrounds: 14205"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 716.9441389283819,
            "unit": "iter/sec",
            "range": "stddev: 0.00003252822487951",
            "extra": "mean: 1.3948088082492762 msec\nrounds: 1382"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 721.2756827345493,
            "unit": "iter/sec",
            "range": "stddev: 0.000026163279386772665",
            "extra": "mean: 1.3864324334472669 msec\nrounds: 1465"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1715.656011523233,
            "unit": "iter/sec",
            "range": "stddev: 0.00004898852780135014",
            "extra": "mean: 582.8674240544042 usec\nrounds: 3384"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 624.2567198784551,
            "unit": "iter/sec",
            "range": "stddev: 0.000044059159233186335",
            "extra": "mean: 1.6019050627035356 msec\nrounds: 1228"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 725.2399392399024,
            "unit": "iter/sec",
            "range": "stddev: 0.000025777732844017164",
            "extra": "mean: 1.3788540121605322 msec\nrounds: 1398"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 651.9331947832923,
            "unit": "iter/sec",
            "range": "stddev: 0.00004124790521920205",
            "extra": "mean: 1.5338994976815192 msec\nrounds: 1294"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 654.1773165430353,
            "unit": "iter/sec",
            "range": "stddev: 0.00010439675000952211",
            "extra": "mean: 1.5286375340625475 msec\nrounds: 1189"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 308.29088342056787,
            "unit": "iter/sec",
            "range": "stddev: 0.00010374745639992996",
            "extra": "mean: 3.243689819513113 msec\nrounds: 615"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1590.7174137269521,
            "unit": "iter/sec",
            "range": "stddev: 0.00007683108480487025",
            "extra": "mean: 628.6471697427779 usec\nrounds: 3034"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 1837.968830816197,
            "unit": "iter/sec",
            "range": "stddev: 0.00001569708579607827",
            "extra": "mean: 544.0788675159004 usec\nrounds: 3691"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 3083.20356044791,
            "unit": "iter/sec",
            "range": "stddev: 0.000007185619228769964",
            "extra": "mean: 324.337975224291 usec\nrounds: 6135"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 1695.8316905370548,
            "unit": "iter/sec",
            "range": "stddev: 0.000014326178062490831",
            "extra": "mean: 589.6811609195183 usec\nrounds: 3393"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 996.2626311277402,
            "unit": "iter/sec",
            "range": "stddev: 0.000028130966709245036",
            "extra": "mean: 1.003751389197474 msec\nrounds: 1981"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 3060.24992946983,
            "unit": "iter/sec",
            "range": "stddev: 0.000012846486484016715",
            "extra": "mean: 326.7706962003734 usec\nrounds: 5948"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 44420.558111259306,
            "unit": "iter/sec",
            "range": "stddev: 0.0009185115039732174",
            "extra": "mean: 22.51209895866953 usec\nrounds: 79124"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 3747.310474887791,
            "unit": "iter/sec",
            "range": "stddev: 0.000016899616780888602",
            "extra": "mean: 266.85805905365874 usec\nrounds: 7417"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 73.46975668895305,
            "unit": "iter/sec",
            "range": "stddev: 0.0009657370314349638",
            "extra": "mean: 13.611042761903693 msec\nrounds: 147"
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
          "id": "ad5c498d5ca5d88e670a0df21241a7bc15e27800",
          "message": "Merge pull request #4716 from nexi-lab/feat/runbook-helpers-mtls-driver\n\ntest(e2e): CA-pinned mTLS client channel for auth-on drivers",
          "timestamp": "2026-08-27T16:12:06Z",
          "url": "https://github.com/nexi-lab/nexus/commit/ad5c498d5ca5d88e670a0df21241a7bc15e27800"
        },
        "date": 1787860331364,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 258.79188692521944,
            "unit": "iter/sec",
            "range": "stddev: 0.0006236284606604982",
            "extra": "mean: 3.864108770492331 msec\nrounds: 122"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 445.9326587124343,
            "unit": "iter/sec",
            "range": "stddev: 0.00031295074213828945",
            "extra": "mean: 2.2424910588234432 msec\nrounds: 153"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1657.8138356915754,
            "unit": "iter/sec",
            "range": "stddev: 0.000023675358936769307",
            "extra": "mean: 603.2040380353316 usec\nrounds: 631"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 503.71506763354364,
            "unit": "iter/sec",
            "range": "stddev: 0.00009029567499596768",
            "extra": "mean: 1.9852493289470294 msec\nrounds: 228"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 198.13117580184993,
            "unit": "iter/sec",
            "range": "stddev: 0.00017194559901319688",
            "extra": "mean: 5.047161285713538 msec\nrounds: 98"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25405.446677116473,
            "unit": "iter/sec",
            "range": "stddev: 0.000002405975506847785",
            "extra": "mean: 39.36163818370228 usec\nrounds: 10439"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1942.7528549624692,
            "unit": "iter/sec",
            "range": "stddev: 0.000013397379538694842",
            "extra": "mean: 514.7335120088234 usec\nrounds: 916"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 34629.27518170215,
            "unit": "iter/sec",
            "range": "stddev: 0.0000019433619074589626",
            "extra": "mean: 28.87730091816627 usec\nrounds: 9039"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 65746.73200592125,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016002186760958116",
            "extra": "mean: 15.209881457072854 usec\nrounds: 9718"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 52964.21491940847,
            "unit": "iter/sec",
            "range": "stddev: 0.000001537200696742772",
            "extra": "mean: 18.88067257338983 usec\nrounds: 15011"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 45197.92218516914,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017051619464246267",
            "extra": "mean: 22.124910873184593 usec\nrounds: 13823"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 129.82434929443562,
            "unit": "iter/sec",
            "range": "stddev: 0.012920122832654318",
            "extra": "mean: 7.702715287500084 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 30637.00749511407,
            "unit": "iter/sec",
            "range": "stddev: 0.000006973721805216849",
            "extra": "mean: 32.640263581861845 usec\nrounds: 33519"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 13015.342121207235,
            "unit": "iter/sec",
            "range": "stddev: 0.000016887611828676494",
            "extra": "mean: 76.83240215181108 usec\nrounds: 14778"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 2425.8038174165563,
            "unit": "iter/sec",
            "range": "stddev: 0.00003691198157809437",
            "extra": "mean: 412.2344902008541 usec\nrounds: 4133"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 13016.95509581964,
            "unit": "iter/sec",
            "range": "stddev: 0.000016781673190987927",
            "extra": "mean: 76.82288159088351 usec\nrounds: 16443"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 819.186273077941,
            "unit": "iter/sec",
            "range": "stddev: 0.00017005585602880964",
            "extra": "mean: 1.2207235800505845 msec\nrounds: 1574"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4327.738441528616,
            "unit": "iter/sec",
            "range": "stddev: 0.00038725932463342435",
            "extra": "mean: 231.06756878005464 usec\nrounds: 7335"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 92732.04990187864,
            "unit": "iter/sec",
            "range": "stddev: 0.0000011170591321752294",
            "extra": "mean: 10.783758161909686 usec\nrounds: 50141"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 30944.381344921767,
            "unit": "iter/sec",
            "range": "stddev: 0.000010074738491733037",
            "extra": "mean: 32.316044352397704 usec\nrounds: 37946"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5191194.942099586,
            "unit": "iter/sec",
            "range": "stddev: 2.7053181024686513e-8",
            "extra": "mean: 192.63387546674343 nsec\nrounds: 111633"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 3745200.0229770723,
            "unit": "iter/sec",
            "range": "stddev: 3.4785951285404415e-8",
            "extra": "mean: 267.0084358285079 nsec\nrounds: 387072"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2031867.8173296787,
            "unit": "iter/sec",
            "range": "stddev: 0.0000024537486145628105",
            "extra": "mean: 492.1579993890646 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 29966.274513404547,
            "unit": "iter/sec",
            "range": "stddev: 0.0005263728050913595",
            "extra": "mean: 33.370848269866805 usec\nrounds: 61036"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 35930.09725353793,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026074319266399116",
            "extra": "mean: 27.83182001828656 usec\nrounds: 70146"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3624.4999627112607,
            "unit": "iter/sec",
            "range": "stddev: 0.000009159928182897722",
            "extra": "mean: 275.9001269935075 usec\nrounds: 6772"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7307.9686684386825,
            "unit": "iter/sec",
            "range": "stddev: 0.0000058946667679734966",
            "extra": "mean: 136.83693039336003 usec\nrounds: 14467"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1302.8262408044043,
            "unit": "iter/sec",
            "range": "stddev: 0.00005092837728281313",
            "extra": "mean: 767.5620652087647 usec\nrounds: 2561"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 429.35623874536526,
            "unit": "iter/sec",
            "range": "stddev: 0.0001190568543238492",
            "extra": "mean: 2.3290682881938083 msec\nrounds: 864"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1877.3186575124628,
            "unit": "iter/sec",
            "range": "stddev: 0.00006667204578002945",
            "extra": "mean: 532.674618663327 usec\nrounds: 3472"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4217.803654500189,
            "unit": "iter/sec",
            "range": "stddev: 0.000010181719764237506",
            "extra": "mean: 237.09022086247407 usec\nrounds: 8091"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 406.13756705524145,
            "unit": "iter/sec",
            "range": "stddev: 0.000030449466372426947",
            "extra": "mean: 2.4622199006377152 msec\nrounds: 785"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 416.41928009484525,
            "unit": "iter/sec",
            "range": "stddev: 0.00002171159006601149",
            "extra": "mean: 2.401425793186704 msec\nrounds: 822"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 956.7644032011142,
            "unit": "iter/sec",
            "range": "stddev: 0.000030015891098353302",
            "extra": "mean: 1.0451893869109568 msec\nrounds: 1910"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 335.2301057184923,
            "unit": "iter/sec",
            "range": "stddev: 0.00024752493669392603",
            "extra": "mean: 2.9830256380366524 msec\nrounds: 652"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 388.05525493450097,
            "unit": "iter/sec",
            "range": "stddev: 0.000030412483248072065",
            "extra": "mean: 2.576952604774771 msec\nrounds: 754"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 356.2697923516586,
            "unit": "iter/sec",
            "range": "stddev: 0.00008846168246421956",
            "extra": "mean: 2.8068616017070087 msec\nrounds: 703"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 355.2945716167265,
            "unit": "iter/sec",
            "range": "stddev: 0.00008261163095127851",
            "extra": "mean: 2.814565940170762 msec\nrounds: 702"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 165.26657953762503,
            "unit": "iter/sec",
            "range": "stddev: 0.00006052509596857005",
            "extra": "mean: 6.050830136363639 msec\nrounds: 330"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 875.0041122375885,
            "unit": "iter/sec",
            "range": "stddev: 0.000030470154679399817",
            "extra": "mean: 1.1428517717965554 msec\nrounds: 1709"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 954.6614125270996,
            "unit": "iter/sec",
            "range": "stddev: 0.00006669147664338553",
            "extra": "mean: 1.047491798535026 msec\nrounds: 1911"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1645.984651833775,
            "unit": "iter/sec",
            "range": "stddev: 0.00001017346678892747",
            "extra": "mean: 607.539079350412 usec\nrounds: 3264"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 875.9000688934019,
            "unit": "iter/sec",
            "range": "stddev: 0.00011155098586417518",
            "extra": "mean: 1.1416827507085185 msec\nrounds: 1765"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 540.5271804811,
            "unit": "iter/sec",
            "range": "stddev: 0.00003131904618402383",
            "extra": "mean: 1.8500457259336025 msec\nrounds: 1018"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1558.4917532350867,
            "unit": "iter/sec",
            "range": "stddev: 0.000020313687475669454",
            "extra": "mean: 641.6460003232097 usec\nrounds: 3091"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 26748.715007221625,
            "unit": "iter/sec",
            "range": "stddev: 0.0006868195509154016",
            "extra": "mean: 37.38497343629478 usec\nrounds: 44459"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 1966.3113676092335,
            "unit": "iter/sec",
            "range": "stddev: 0.000022191629483154134",
            "extra": "mean: 508.56645416024 usec\nrounds: 3774"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 39.910256575124734,
            "unit": "iter/sec",
            "range": "stddev: 0.00013521969756887016",
            "extra": "mean: 25.056215765430082 msec\nrounds: 81"
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
          "id": "b8be9b4cd3d4547907ed68503cd8864f2ef789e0",
          "message": "test(http-api): pin service_decl install under a runtime + fix stale docstring (#4717 audit) (#4718)\n\ntest(http-api): pin service_decl install under a runtime + fix stale docstring (#4717 audit)",
          "timestamp": "2026-08-28T08:57:59Z",
          "url": "https://github.com/nexi-lab/nexus/commit/b8be9b4cd3d4547907ed68503cd8864f2ef789e0"
        },
        "date": 1787950330884,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 226.56591538924403,
            "unit": "iter/sec",
            "range": "stddev: 0.001696823368674961",
            "extra": "mean: 4.4137265673081645 msec\nrounds: 104"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 426.75112888892215,
            "unit": "iter/sec",
            "range": "stddev: 0.0004185853401765283",
            "extra": "mean: 2.3432861269835965 msec\nrounds: 126"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1653.593860670766,
            "unit": "iter/sec",
            "range": "stddev: 0.000046979657906699455",
            "extra": "mean: 604.7434160129009 usec\nrounds: 637"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 446.3040477755962,
            "unit": "iter/sec",
            "range": "stddev: 0.00012224941871797766",
            "extra": "mean: 2.240624984209878 msec\nrounds: 190"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 175.25675019620033,
            "unit": "iter/sec",
            "range": "stddev: 0.000340195232036641",
            "extra": "mean: 5.705914316455702 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25763.370142437783,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025263212647690694",
            "extra": "mean: 38.81479769421882 usec\nrounds: 10148"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1932.2570291975849,
            "unit": "iter/sec",
            "range": "stddev: 0.00002970199827341078",
            "extra": "mean: 517.5294926551637 usec\nrounds: 885"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 35291.06230719374,
            "unit": "iter/sec",
            "range": "stddev: 0.00000212259334617548",
            "extra": "mean: 28.335786304629313 usec\nrounds: 9317"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 69003.1415022845,
            "unit": "iter/sec",
            "range": "stddev: 0.0000019978574619334398",
            "extra": "mean: 14.492093812379435 usec\nrounds: 10020"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 54542.29995329214,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016282624431824288",
            "extra": "mean: 18.334393688134902 usec\nrounds: 15558"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 45573.9555958178,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018770821958288413",
            "extra": "mean: 21.94235692132388 usec\nrounds: 14188"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 122.52599713798868,
            "unit": "iter/sec",
            "range": "stddev: 0.01743366328541382",
            "extra": "mean: 8.161533253010795 msec\nrounds: 83"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 31410.89202185118,
            "unit": "iter/sec",
            "range": "stddev: 0.000007110771150206305",
            "extra": "mean: 31.83609046519099 usec\nrounds: 33383"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 13140.393367929266,
            "unit": "iter/sec",
            "range": "stddev: 0.000015050600505527304",
            "extra": "mean: 76.10122254335414 usec\nrounds: 16954"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 2438.3703299218114,
            "unit": "iter/sec",
            "range": "stddev: 0.000026092694738935814",
            "extra": "mean: 410.1099770321049 usec\nrounds: 4441"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 13067.45189328721,
            "unit": "iter/sec",
            "range": "stddev: 0.000017549967845049507",
            "extra": "mean: 76.52601350028333 usec\nrounds: 16296"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 834.8723770610306,
            "unit": "iter/sec",
            "range": "stddev: 0.00016429880793479129",
            "extra": "mean: 1.197787862523685 msec\nrounds: 1593"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4399.872636998367,
            "unit": "iter/sec",
            "range": "stddev: 0.00041007378336094274",
            "extra": "mean: 227.27930613059948 usec\nrounds: 7634"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 92398.32365002019,
            "unit": "iter/sec",
            "range": "stddev: 0.000001221626287075055",
            "extra": "mean: 10.822707171482126 usec\nrounds: 50101"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 30951.654670234828,
            "unit": "iter/sec",
            "range": "stddev: 0.000010596201197629119",
            "extra": "mean: 32.30845040933035 usec\nrounds: 39080"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5274133.447192925,
            "unit": "iter/sec",
            "range": "stddev: 2.4366966739483184e-8",
            "extra": "mean: 189.60460709090216 nsec\nrounds: 112316"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4391724.588452955,
            "unit": "iter/sec",
            "range": "stddev: 1.6024037338812097e-8",
            "extra": "mean: 227.70098166658119 nsec\nrounds: 103365"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2549856.061997646,
            "unit": "iter/sec",
            "range": "stddev: 2.0511181320480838e-7",
            "extra": "mean: 392.1789997889391 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 30483.341369776674,
            "unit": "iter/sec",
            "range": "stddev: 0.0005826448901214979",
            "extra": "mean: 32.80480272387299 usec\nrounds: 50442"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 36053.234047565435,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026271488256981514",
            "extra": "mean: 27.73676277364435 usec\nrounds: 70144"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3609.9718240770558,
            "unit": "iter/sec",
            "range": "stddev: 0.000011528112080356193",
            "extra": "mean: 277.01047230629433 usec\nrounds: 6951"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7228.646490929573,
            "unit": "iter/sec",
            "range": "stddev: 0.000005403079418275743",
            "extra": "mean: 138.338484425098 usec\nrounds: 13034"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1314.9493472086374,
            "unit": "iter/sec",
            "range": "stddev: 0.00005928480395283634",
            "extra": "mean: 760.4855670849914 usec\nrounds: 2631"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 433.29954304063216,
            "unit": "iter/sec",
            "range": "stddev: 0.00013633455337004878",
            "extra": "mean: 2.307872270029664 msec\nrounds: 674"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1935.5963661070953,
            "unit": "iter/sec",
            "range": "stddev: 0.00003966753759451256",
            "extra": "mean: 516.6366384595035 usec\nrounds: 3557"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4289.563406905429,
            "unit": "iter/sec",
            "range": "stddev: 0.000024559961065845606",
            "extra": "mean: 233.12395811428715 usec\nrounds: 7616"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 411.3398834439406,
            "unit": "iter/sec",
            "range": "stddev: 0.0002042171340250847",
            "extra": "mean: 2.43107960168488 msec\nrounds: 831"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 422.44988892978546,
            "unit": "iter/sec",
            "range": "stddev: 0.00010652858400999753",
            "extra": "mean: 2.3671446630826503 msec\nrounds: 837"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 992.0694804321327,
            "unit": "iter/sec",
            "range": "stddev: 0.000042343352977802075",
            "extra": "mean: 1.0079939154709334 msec\nrounds: 1952"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 346.00523270077036,
            "unit": "iter/sec",
            "range": "stddev: 0.000029152247433530213",
            "extra": "mean: 2.890129701780587 msec\nrounds: 674"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 386.9013359595889,
            "unit": "iter/sec",
            "range": "stddev: 0.00005321439151740769",
            "extra": "mean: 2.584638270942668 msec\nrounds: 764"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 351.952967208495,
            "unit": "iter/sec",
            "range": "stddev: 0.00018494199395895336",
            "extra": "mean: 2.8412887322174654 msec\nrounds: 717"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 356.0917284286104,
            "unit": "iter/sec",
            "range": "stddev: 0.0000918993552629735",
            "extra": "mean: 2.808265174855026 msec\nrounds: 692"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 163.57837273656918,
            "unit": "iter/sec",
            "range": "stddev: 0.00016048408085735216",
            "extra": "mean: 6.1132775884158335 msec\nrounds: 328"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 879.4234630075932,
            "unit": "iter/sec",
            "range": "stddev: 0.000017052180099458064",
            "extra": "mean: 1.137108619526752 msec\nrounds: 1690"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 964.2225134355323,
            "unit": "iter/sec",
            "range": "stddev: 0.0000153592495991902",
            "extra": "mean: 1.037105010582041 msec\nrounds: 1890"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1548.8937407362225,
            "unit": "iter/sec",
            "range": "stddev: 0.000009313317518324828",
            "extra": "mean: 645.6220809083253 usec\nrounds: 3127"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 888.7280980778203,
            "unit": "iter/sec",
            "range": "stddev: 0.000050976286901491714",
            "extra": "mean: 1.1252035376881224 msec\nrounds: 1791"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 538.6631837963048,
            "unit": "iter/sec",
            "range": "stddev: 0.000036423551176242614",
            "extra": "mean: 1.856447646843727 msec\nrounds: 1093"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1507.8831315577645,
            "unit": "iter/sec",
            "range": "stddev: 0.000010941491538001336",
            "extra": "mean: 663.1813693458588 usec\nrounds: 3073"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28819.175399948632,
            "unit": "iter/sec",
            "range": "stddev: 0.0006575762569752155",
            "extra": "mean: 34.69911911503833 usec\nrounds: 48365"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2111.737037463159,
            "unit": "iter/sec",
            "range": "stddev: 0.0000238124033745104",
            "extra": "mean: 473.54380884530264 usec\nrounds: 4002"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 42.497703115275435,
            "unit": "iter/sec",
            "range": "stddev: 0.00021302204947379374",
            "extra": "mean: 23.53068346511552 msec\nrounds: 86"
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
          "id": "3dbad3077b7ba081b6e89d4f0aeb982b8890dc8e",
          "message": "feat(rebac): crate skeleton + ReBACTupleStore trait + inmem impl (R10 — PR 1/5) (#4721)\n\nfeat(rebac): crate skeleton + ReBACTupleStore trait + inmem impl (R10 — PR 1/5)",
          "timestamp": "2026-08-29T10:31:29Z",
          "url": "https://github.com/nexi-lab/nexus/commit/3dbad3077b7ba081b6e89d4f0aeb982b8890dc8e"
        },
        "date": 1788013788013,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 172.6764450267984,
            "unit": "iter/sec",
            "range": "stddev: 0.012559420919233809",
            "extra": "mean: 5.7911778288278155 msec\nrounds: 111"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 497.7204136819248,
            "unit": "iter/sec",
            "range": "stddev: 0.0002893862041926399",
            "extra": "mean: 2.0091601077850587 msec\nrounds: 167"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1780.2110719980597,
            "unit": "iter/sec",
            "range": "stddev: 0.000031440365381843587",
            "extra": "mean: 561.7311428569128 usec\nrounds: 357"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 68.12382921725442,
            "unit": "iter/sec",
            "range": "stddev: 0.036340096456830194",
            "extra": "mean: 14.679151355554154 msec\nrounds: 90"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 164.9656375692784,
            "unit": "iter/sec",
            "range": "stddev: 0.0017034229935551657",
            "extra": "mean: 6.061868488096761 msec\nrounds: 84"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 31300.70483322916,
            "unit": "iter/sec",
            "range": "stddev: 0.00000296139548670843",
            "extra": "mean: 31.94816236017757 usec\nrounds: 13236"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 2366.3437869329196,
            "unit": "iter/sec",
            "range": "stddev: 0.000014250645761712306",
            "extra": "mean: 422.5928647908452 usec\nrounds: 1102"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 41981.52653660963,
            "unit": "iter/sec",
            "range": "stddev: 0.0000019826219456956277",
            "extra": "mean: 23.820000902729408 usec\nrounds: 9969"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 89179.5777651934,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014910250265992268",
            "extra": "mean: 11.213329610429012 usec\nrounds: 11550"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 69467.79685393497,
            "unit": "iter/sec",
            "range": "stddev: 0.0000019223290549922884",
            "extra": "mean: 14.395159272182324 usec\nrounds: 18685"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 58805.001095925516,
            "unit": "iter/sec",
            "range": "stddev: 0.00000194999576851636",
            "extra": "mean: 17.00535637043442 usec\nrounds: 17754"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 166.38255045127684,
            "unit": "iter/sec",
            "range": "stddev: 0.00892790720211898",
            "extra": "mean: 6.010245649484968 msec\nrounds: 97"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 30585.032077543754,
            "unit": "iter/sec",
            "range": "stddev: 0.0000061820836115366475",
            "extra": "mean: 32.6957316070374 usec\nrounds: 38466"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 13137.471409333482,
            "unit": "iter/sec",
            "range": "stddev: 0.000012926592210449392",
            "extra": "mean: 76.11814852662991 usec\nrounds: 19579"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 2694.997983249978,
            "unit": "iter/sec",
            "range": "stddev: 0.00002533800867334909",
            "extra": "mean: 371.0577915884265 usec\nrounds: 4803"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 13141.079424573036,
            "unit": "iter/sec",
            "range": "stddev: 0.000014625460520144213",
            "extra": "mean: 76.09724952503214 usec\nrounds: 18423"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 954.6215227067376,
            "unit": "iter/sec",
            "range": "stddev: 0.00012896732840863906",
            "extra": "mean: 1.0475355690332604 msec\nrounds: 1789"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 5580.152871220827,
            "unit": "iter/sec",
            "range": "stddev: 0.0002645148339787152",
            "extra": "mean: 179.20655994164898 usec\nrounds: 9576"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 113916.21854044516,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013562187370711352",
            "extra": "mean: 8.778381277157273 usec\nrounds: 47215"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 30630.969483926536,
            "unit": "iter/sec",
            "range": "stddev: 0.000007232313977881616",
            "extra": "mean: 32.646697667363924 usec\nrounds: 45569"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 7661536.136259191,
            "unit": "iter/sec",
            "range": "stddev: 1.0611943253168279e-8",
            "extra": "mean: 130.52212796691947 nsec\nrounds: 160026"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 6312568.101618543,
            "unit": "iter/sec",
            "range": "stddev: 1.3915859024973067e-8",
            "extra": "mean: 158.4141325530571 nsec\nrounds: 162154"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 3403444.285334118,
            "unit": "iter/sec",
            "range": "stddev: 2.1137674866753244e-7",
            "extra": "mean: 293.8200000244251 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 31387.53255347659,
            "unit": "iter/sec",
            "range": "stddev: 0.0003933174438548712",
            "extra": "mean: 31.859783762748705 usec\nrounds: 70677"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 38827.83040341737,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016029534139875128",
            "extra": "mean: 25.754722569097925 usec\nrounds: 73831"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3968.7841552102927,
            "unit": "iter/sec",
            "range": "stddev: 0.000008489670097812652",
            "extra": "mean: 251.9663355053415 usec\nrounds: 7827"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8464.042112495647,
            "unit": "iter/sec",
            "range": "stddev: 0.000005006330580822889",
            "extra": "mean: 118.14686017732338 usec\nrounds: 18266"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1594.6827047098668,
            "unit": "iter/sec",
            "range": "stddev: 0.000020767993480305732",
            "extra": "mean: 627.0839942306503 usec\nrounds: 3120"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 528.2525792303621,
            "unit": "iter/sec",
            "range": "stddev: 0.00005602648740328141",
            "extra": "mean: 1.8930338238138857 msec\nrounds: 1033"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 2510.3540442764515,
            "unit": "iter/sec",
            "range": "stddev: 0.000043628480461007",
            "extra": "mean: 398.35018581541385 usec\nrounds: 4526"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 5034.255668264913,
            "unit": "iter/sec",
            "range": "stddev: 0.000007424990003814886",
            "extra": "mean: 198.63909699776056 usec\nrounds: 9660"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 480.51918551168205,
            "unit": "iter/sec",
            "range": "stddev: 0.00006681313219951303",
            "extra": "mean: 2.0810823587306873 msec\nrounds: 945"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 489.8885680621432,
            "unit": "iter/sec",
            "range": "stddev: 0.0000567296935797188",
            "extra": "mean: 2.041280538461449 msec\nrounds: 975"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1179.997151225962,
            "unit": "iter/sec",
            "range": "stddev: 0.00002327795901468339",
            "extra": "mean: 847.4596730687414 usec\nrounds: 2343"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 446.75834169883854,
            "unit": "iter/sec",
            "range": "stddev: 0.000053190854364740986",
            "extra": "mean: 2.2383465660594286 msec\nrounds: 878"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 462.3293787700764,
            "unit": "iter/sec",
            "range": "stddev: 0.00005730951712572814",
            "extra": "mean: 2.1629601014330424 msec\nrounds: 907"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 432.23799758789113,
            "unit": "iter/sec",
            "range": "stddev: 0.0000634553869040464",
            "extra": "mean: 2.3135402384346375 msec\nrounds: 843"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 433.03340044798114,
            "unit": "iter/sec",
            "range": "stddev: 0.00006493525034220499",
            "extra": "mean: 2.309290689737746 msec\nrounds: 838"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 212.77417939198935,
            "unit": "iter/sec",
            "range": "stddev: 0.00010399233747925677",
            "extra": "mean: 4.699818384249159 msec\nrounds: 419"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1066.855752311706,
            "unit": "iter/sec",
            "range": "stddev: 0.000018712130326521737",
            "extra": "mean: 937.3338408994466 usec\nrounds: 2313"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 1098.6013218306973,
            "unit": "iter/sec",
            "range": "stddev: 0.00009790270007737748",
            "extra": "mean: 910.24831313111 usec\nrounds: 2178"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1904.4501758506478,
            "unit": "iter/sec",
            "range": "stddev: 0.00003213951890174069",
            "extra": "mean: 525.0859343449806 usec\nrounds: 3823"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 1059.2869362608392,
            "unit": "iter/sec",
            "range": "stddev: 0.000027159109409903235",
            "extra": "mean: 944.0312778045623 usec\nrounds: 2059"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 614.2737140134954,
            "unit": "iter/sec",
            "range": "stddev: 0.0000363495406503133",
            "extra": "mean: 1.627938779060356 msec\nrounds: 1213"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1941.0229982713552,
            "unit": "iter/sec",
            "range": "stddev: 0.000012652438619671598",
            "extra": "mean: 515.1922470215883 usec\nrounds: 3777"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 34039.70191461399,
            "unit": "iter/sec",
            "range": "stddev: 0.000756636179945946",
            "extra": "mean: 29.377460546171182 usec\nrounds: 52948"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2749.535555690462,
            "unit": "iter/sec",
            "range": "stddev: 0.000027919007275843348",
            "extra": "mean: 363.6977881338511 usec\nrounds: 5225"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 54.42116301323296,
            "unit": "iter/sec",
            "range": "stddev: 0.0001930030156079775",
            "extra": "mean: 18.375204509261252 msec\nrounds: 108"
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
          "id": "01315c151904a9258ece82ceb324e361d19c90ec",
          "message": "Merge pull request #4726 from nexi-lab/chore/bump-sudocode-cohost-aad70dc8\n\nchore(nexusd): bump co-host sudocode pin to aad70dc8 (A2A reply-contract prompt)",
          "timestamp": "2026-08-30T09:52:29Z",
          "url": "https://github.com/nexi-lab/nexus/commit/01315c151904a9258ece82ceb324e361d19c90ec"
        },
        "date": 1788100031357,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 234.30656064436934,
            "unit": "iter/sec",
            "range": "stddev: 0.0005706013946929531",
            "extra": "mean: 4.267912931033121 msec\nrounds: 116"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 425.87108210497627,
            "unit": "iter/sec",
            "range": "stddev: 0.00029709118921127965",
            "extra": "mean: 2.348128440788338 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1412.3524642414088,
            "unit": "iter/sec",
            "range": "stddev: 0.000021474187046428115",
            "extra": "mean: 708.0385564640989 usec\nrounds: 611"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 428.5208404175542,
            "unit": "iter/sec",
            "range": "stddev: 0.0004083340662653409",
            "extra": "mean: 2.333608790241314 msec\nrounds: 205"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 142.05446410770634,
            "unit": "iter/sec",
            "range": "stddev: 0.0054983907496252375",
            "extra": "mean: 7.039553499999799 msec\nrounds: 86"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 24603.8754920895,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026760355090204976",
            "extra": "mean: 40.64400343439855 usec\nrounds: 10483"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1821.044992740077,
            "unit": "iter/sec",
            "range": "stddev: 0.000073901499310403",
            "extra": "mean: 549.1352514554443 usec\nrounds: 859"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31115.540555010644,
            "unit": "iter/sec",
            "range": "stddev: 0.000003360111447793457",
            "extra": "mean: 32.138281455597806 usec\nrounds: 8957"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 62036.33430106713,
            "unit": "iter/sec",
            "range": "stddev: 0.0000024204140578983494",
            "extra": "mean: 16.11958558265101 usec\nrounds: 10335"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 48216.47102583411,
            "unit": "iter/sec",
            "range": "stddev: 0.000003119438043417268",
            "extra": "mean: 20.739800709682918 usec\nrounds: 14933"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 39281.99977378161,
            "unit": "iter/sec",
            "range": "stddev: 0.0000038119762448220166",
            "extra": "mean: 25.456952440273685 usec\nrounds: 12784"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 134.13589302739885,
            "unit": "iter/sec",
            "range": "stddev: 0.010985546044582089",
            "extra": "mean: 7.455126121952595 msec\nrounds: 82"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 17033.958575901703,
            "unit": "iter/sec",
            "range": "stddev: 0.000010331403433027948",
            "extra": "mean: 58.70625994210887 usec\nrounds: 25171"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7708.83949768207,
            "unit": "iter/sec",
            "range": "stddev: 0.00002257711934772335",
            "extra": "mean: 129.72121164290482 usec\nrounds: 12918"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1634.238459257127,
            "unit": "iter/sec",
            "range": "stddev: 0.000040464125759393666",
            "extra": "mean: 611.905805016098 usec\nrounds: 2990"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7646.184018890768,
            "unit": "iter/sec",
            "range": "stddev: 0.000027501216472957045",
            "extra": "mean: 130.78419215773332 usec\nrounds: 8977"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 563.0361962998021,
            "unit": "iter/sec",
            "range": "stddev: 0.00025784484626006096",
            "extra": "mean: 1.776084746543588 msec\nrounds: 1085"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4159.641934916593,
            "unit": "iter/sec",
            "range": "stddev: 0.000573419694008512",
            "extra": "mean: 240.40530787178238 usec\nrounds: 7292"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 82702.48060864337,
            "unit": "iter/sec",
            "range": "stddev: 0.000002074108219711635",
            "extra": "mean: 12.091535739201134 usec\nrounds: 47609"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 16624.021895917824,
            "unit": "iter/sec",
            "range": "stddev: 0.00001478995862351298",
            "extra": "mean: 60.15391499487612 usec\nrounds: 27516"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5170672.775983614,
            "unit": "iter/sec",
            "range": "stddev: 3.4826725341428095e-8",
            "extra": "mean: 193.39843059586588 nsec\nrounds: 109207"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4133749.8550436897,
            "unit": "iter/sec",
            "range": "stddev: 3.606825106087606e-8",
            "extra": "mean: 241.9111061545912 nsec\nrounds: 98829"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2461599.047059265,
            "unit": "iter/sec",
            "range": "stddev: 1.4576068869837322e-7",
            "extra": "mean: 406.240001268543 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 20572.108798219608,
            "unit": "iter/sec",
            "range": "stddev: 0.00046831495885655763",
            "extra": "mean: 48.60950376106042 usec\nrounds: 57829"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 38731.31307353737,
            "unit": "iter/sec",
            "range": "stddev: 0.00000248192321548349",
            "extra": "mean: 25.81890260475667 usec\nrounds: 67375"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3876.6680929481,
            "unit": "iter/sec",
            "range": "stddev: 0.000009702230591337569",
            "extra": "mean: 257.9534734529021 usec\nrounds: 7063"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7279.259307625671,
            "unit": "iter/sec",
            "range": "stddev: 0.000007729867393430189",
            "extra": "mean: 137.3766145344501 usec\nrounds: 14476"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1238.7437920982597,
            "unit": "iter/sec",
            "range": "stddev: 0.000016891662076592614",
            "extra": "mean: 807.2694340660541 usec\nrounds: 2548"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 268.22261406625046,
            "unit": "iter/sec",
            "range": "stddev: 0.0000892310694844145",
            "extra": "mean: 3.7282464175559857 msec\nrounds: 843"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1834.8567779019156,
            "unit": "iter/sec",
            "range": "stddev: 0.00005651593789989761",
            "extra": "mean: 545.0016655487734 usec\nrounds: 3280"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4098.671160432375,
            "unit": "iter/sec",
            "range": "stddev: 0.000013657990193178692",
            "extra": "mean: 243.9815151929653 usec\nrounds: 7964"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 397.919074962347,
            "unit": "iter/sec",
            "range": "stddev: 0.0000275254284365634",
            "extra": "mean: 2.513073795456086 msec\nrounds: 792"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 399.1333272281918,
            "unit": "iter/sec",
            "range": "stddev: 0.000019734965926447047",
            "extra": "mean: 2.505428466584255 msec\nrounds: 793"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 918.7573202049407,
            "unit": "iter/sec",
            "range": "stddev: 0.000015065247464743032",
            "extra": "mean: 1.0884267020337177 msec\nrounds: 1819"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 348.30856527234494,
            "unit": "iter/sec",
            "range": "stddev: 0.00003048211423027572",
            "extra": "mean: 2.871017539342143 msec\nrounds: 699"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 355.0003720492245,
            "unit": "iter/sec",
            "range": "stddev: 0.00011188230907758042",
            "extra": "mean: 2.8168984562679262 msec\nrounds: 686"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 330.98216552240365,
            "unit": "iter/sec",
            "range": "stddev: 0.00003516826172232176",
            "extra": "mean: 3.021310826284722 msec\nrounds: 662"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 329.79158540442864,
            "unit": "iter/sec",
            "range": "stddev: 0.00003318862540052324",
            "extra": "mean: 3.032218056060115 msec\nrounds: 660"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 165.88404624368908,
            "unit": "iter/sec",
            "range": "stddev: 0.00004579439789463816",
            "extra": "mean: 6.028307258257779 msec\nrounds: 333"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 812.0885546822726,
            "unit": "iter/sec",
            "range": "stddev: 0.000018587066312273486",
            "extra": "mean: 1.2313928009873838 msec\nrounds: 1623"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 866.9487938293034,
            "unit": "iter/sec",
            "range": "stddev: 0.000042635753578118236",
            "extra": "mean: 1.153470662993844 msec\nrounds: 1724"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1473.1391227770719,
            "unit": "iter/sec",
            "range": "stddev: 0.000013188957739733955",
            "extra": "mean: 678.8225121024966 usec\nrounds: 2892"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 795.4308722160705,
            "unit": "iter/sec",
            "range": "stddev: 0.00001636537831803042",
            "extra": "mean: 1.2571802716356733 msec\nrounds: 1583"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 478.6576874662535,
            "unit": "iter/sec",
            "range": "stddev: 0.000017669460970861563",
            "extra": "mean: 2.0891756806277186 msec\nrounds: 955"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1463.7414183171495,
            "unit": "iter/sec",
            "range": "stddev: 0.000012288312327084515",
            "extra": "mean: 683.1807773463779 usec\nrounds: 2834"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 27660.73506491008,
            "unit": "iter/sec",
            "range": "stddev: 0.0006082635302497738",
            "extra": "mean: 36.152329200700905 usec\nrounds: 44362"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2040.7121738211565,
            "unit": "iter/sec",
            "range": "stddev: 0.000023979154059605966",
            "extra": "mean: 490.0250083418367 usec\nrounds: 4076"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 41.26392415885141,
            "unit": "iter/sec",
            "range": "stddev: 0.00036157526911255347",
            "extra": "mean: 24.23424384337166 msec\nrounds: 83"
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
          "id": "0572448806cdb877bed727b16e3bca72b1a8c1d7",
          "message": "Merge pull request #4729 from nexi-lab/chore/bump-nexus-vfs-sys-write-wake\n\nchore(deps): bump nexus-vfs pin → sys_write wakes DT_STREAM tail (#258)",
          "timestamp": "2026-08-31T11:11:18Z",
          "url": "https://github.com/nexi-lab/nexus/commit/0572448806cdb877bed727b16e3bca72b1a8c1d7"
        },
        "date": 1788195921613,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 257.06224040454254,
            "unit": "iter/sec",
            "range": "stddev: 0.0004018686144592159",
            "extra": "mean: 3.8901084749992285 msec\nrounds: 120"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 448.0564883552301,
            "unit": "iter/sec",
            "range": "stddev: 0.0002881918139094892",
            "extra": "mean: 2.2318614415581806 msec\nrounds: 154"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1673.568162857193,
            "unit": "iter/sec",
            "range": "stddev: 0.000019850232691313664",
            "extra": "mean: 597.5257071649556 usec\nrounds: 642"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 477.9368997610041,
            "unit": "iter/sec",
            "range": "stddev: 0.00014901469627664216",
            "extra": "mean: 2.0923264148469336 msec\nrounds: 229"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 195.05307750212273,
            "unit": "iter/sec",
            "range": "stddev: 0.00027820944508073536",
            "extra": "mean: 5.126809649999586 msec\nrounds: 100"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25308.145082795814,
            "unit": "iter/sec",
            "range": "stddev: 0.0000021840656019241363",
            "extra": "mean: 39.51297089251272 usec\nrounds: 7352"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1964.9454740735953,
            "unit": "iter/sec",
            "range": "stddev: 0.000012571999285212392",
            "extra": "mean: 508.91997421529766 usec\nrounds: 892"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 35058.59414033487,
            "unit": "iter/sec",
            "range": "stddev: 0.0000021454070944503923",
            "extra": "mean: 28.523676562646337 usec\nrounds: 9263"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 68862.04656264003,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013994045216822826",
            "extra": "mean: 14.521787398379963 usec\nrounds: 9840"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 54202.3391610281,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014803732307046094",
            "extra": "mean: 18.44938826402178 usec\nrounds: 16445"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 46174.16092920907,
            "unit": "iter/sec",
            "range": "stddev: 0.0000021876877774966555",
            "extra": "mean: 21.657134203978902 usec\nrounds: 13934"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 133.46799385315325,
            "unit": "iter/sec",
            "range": "stddev: 0.012029771453806834",
            "extra": "mean: 7.492432988093307 msec\nrounds: 84"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 30901.938677603113,
            "unit": "iter/sec",
            "range": "stddev: 0.000007075481175566347",
            "extra": "mean: 32.36042924144345 usec\nrounds: 33473"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 13083.64971631019,
            "unit": "iter/sec",
            "range": "stddev: 0.000014615559745671754",
            "extra": "mean: 76.43127274749578 usec\nrounds: 17991"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 2417.7494862619496,
            "unit": "iter/sec",
            "range": "stddev: 0.00002849340567732958",
            "extra": "mean: 413.6077809889588 usec\nrounds: 4187"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 12918.568436751902,
            "unit": "iter/sec",
            "range": "stddev: 0.000016645994672531305",
            "extra": "mean: 77.40795776992677 usec\nrounds: 16339"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 824.1676331022948,
            "unit": "iter/sec",
            "range": "stddev: 0.000162207031318112",
            "extra": "mean: 1.2133453921696062 msec\nrounds: 1558"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4385.019717077369,
            "unit": "iter/sec",
            "range": "stddev: 0.000530806429588413",
            "extra": "mean: 228.04914561855233 usec\nrounds: 7760"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 89962.0912773764,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013017410616504594",
            "extra": "mean: 11.115793172445727 usec\nrounds: 44467"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 30430.77264486097,
            "unit": "iter/sec",
            "range": "stddev: 0.000011234373513342676",
            "extra": "mean: 32.86147255182744 usec\nrounds: 36906"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5422463.666537259,
            "unit": "iter/sec",
            "range": "stddev: 1.685455515969432e-8",
            "extra": "mean: 184.41801762013316 nsec\nrounds: 108062"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4668391.959076447,
            "unit": "iter/sec",
            "range": "stddev: 1.702763158842151e-8",
            "extra": "mean: 214.20652095327296 nsec\nrounds: 109004"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2517604.3463886534,
            "unit": "iter/sec",
            "range": "stddev: 1.6838369558627381e-7",
            "extra": "mean: 397.20300031831357 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 30222.171261165648,
            "unit": "iter/sec",
            "range": "stddev: 0.0004860549917316886",
            "extra": "mean: 33.08829108797231 usec\nrounds: 61277"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 35768.16083976143,
            "unit": "iter/sec",
            "range": "stddev: 0.000002773530730633009",
            "extra": "mean: 27.957825521975305 usec\nrounds: 59188"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3595.183085026463,
            "unit": "iter/sec",
            "range": "stddev: 0.000014023939054813923",
            "extra": "mean: 278.14995129591273 usec\nrounds: 5749"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7114.505232073323,
            "unit": "iter/sec",
            "range": "stddev: 0.00000796806533114798",
            "extra": "mean: 140.5579119531518 usec\nrounds: 14356"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1306.63644030948,
            "unit": "iter/sec",
            "range": "stddev: 0.000012679915956241242",
            "extra": "mean: 765.3238262382669 usec\nrounds: 2584"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 432.84258936912,
            "unit": "iter/sec",
            "range": "stddev: 0.00005069003928250836",
            "extra": "mean: 2.310308700115503 msec\nrounds: 867"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1877.152345138296,
            "unit": "iter/sec",
            "range": "stddev: 0.00007429168139782544",
            "extra": "mean: 532.7218126914076 usec\nrounds: 2285"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4302.893148558984,
            "unit": "iter/sec",
            "range": "stddev: 0.000012802597864026466",
            "extra": "mean: 232.40177375422272 usec\nrounds: 7125"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 411.37680208207684,
            "unit": "iter/sec",
            "range": "stddev: 0.00008584739687969532",
            "extra": "mean: 2.4308614266501167 msec\nrounds: 818"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 422.7114018495624,
            "unit": "iter/sec",
            "range": "stddev: 0.00002705297055987077",
            "extra": "mean: 2.36568021497534 msec\nrounds: 828"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 996.2427225450924,
            "unit": "iter/sec",
            "range": "stddev: 0.000016984147627296837",
            "extra": "mean: 1.0037714478308146 msec\nrounds: 1936"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 332.8275080797642,
            "unit": "iter/sec",
            "range": "stddev: 0.000030836344008244926",
            "extra": "mean: 3.00455934597913 msec\nrounds: 659"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 385.66783476409836,
            "unit": "iter/sec",
            "range": "stddev: 0.000027474778745851748",
            "extra": "mean: 2.592904851947714 msec\nrounds: 770"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 354.97816767630644,
            "unit": "iter/sec",
            "range": "stddev: 0.00004030234012267796",
            "extra": "mean: 2.8170746571430527 msec\nrounds: 700"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 353.0428773539648,
            "unit": "iter/sec",
            "range": "stddev: 0.000040337448405533716",
            "extra": "mean: 2.832517136430963 msec\nrounds: 667"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 158.86150449346877,
            "unit": "iter/sec",
            "range": "stddev: 0.00019902670132915446",
            "extra": "mean: 6.294791196825867 msec\nrounds: 315"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 850.1219822779613,
            "unit": "iter/sec",
            "range": "stddev: 0.000018833495884918954",
            "extra": "mean: 1.1763017788581707 msec\nrounds: 1646"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 945.1774471679888,
            "unit": "iter/sec",
            "range": "stddev: 0.000012872219244571663",
            "extra": "mean: 1.0580023920336596 msec\nrounds: 1908"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1602.971590508207,
            "unit": "iter/sec",
            "range": "stddev: 0.00001115052274961042",
            "extra": "mean: 623.8413743084238 usec\nrounds: 3254"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 857.3397312795348,
            "unit": "iter/sec",
            "range": "stddev: 0.000016690114994379553",
            "extra": "mean: 1.1663987606261432 msec\nrounds: 1788"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 513.2386941601669,
            "unit": "iter/sec",
            "range": "stddev: 0.00012583538240508235",
            "extra": "mean: 1.9484111610804016 msec\nrounds: 1074"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1559.6530726276812,
            "unit": "iter/sec",
            "range": "stddev: 0.000011857457694675842",
            "extra": "mean: 641.1682300059296 usec\nrounds: 3326"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28440.09644468703,
            "unit": "iter/sec",
            "range": "stddev: 0.0006356538426667026",
            "extra": "mean: 35.161624783688545 usec\nrounds: 46813"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2042.8241801572394,
            "unit": "iter/sec",
            "range": "stddev: 0.00002293269831654757",
            "extra": "mean: 489.5183881772089 usec\nrounds: 4060"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 40.7626517101096,
            "unit": "iter/sec",
            "range": "stddev: 0.00011801797652384877",
            "extra": "mean: 24.532260734941065 msec\nrounds: 83"
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
          "id": "1e82cfe5d8f42464d62b51b8effed9827d60bddd",
          "message": "feat(nexusd): wire nexus-rebac PermissionProvider behind --features {rebac,full} (#4731)\n\nfeat(nexusd): wire nexus-rebac PermissionProvider behind --features {rebac,full}",
          "timestamp": "2026-08-31T18:26:49Z",
          "url": "https://github.com/nexi-lab/nexus/commit/1e82cfe5d8f42464d62b51b8effed9827d60bddd"
        },
        "date": 1788271831934,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 235.7862134165564,
            "unit": "iter/sec",
            "range": "stddev: 0.0004225153606346324",
            "extra": "mean: 4.241130070795658 msec\nrounds: 113"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 415.41521501670644,
            "unit": "iter/sec",
            "range": "stddev: 0.00033074430542624853",
            "extra": "mean: 2.4072300769238404 msec\nrounds: 143"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1380.33604622598,
            "unit": "iter/sec",
            "range": "stddev: 0.00003972260527591846",
            "extra": "mean: 724.4612663228867 usec\nrounds: 582"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 451.4397499001511,
            "unit": "iter/sec",
            "range": "stddev: 0.0002526895717049933",
            "extra": "mean: 2.215135021276214 msec\nrounds: 188"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 175.33563846339584,
            "unit": "iter/sec",
            "range": "stddev: 0.0006537361184468899",
            "extra": "mean: 5.703347070588654 msec\nrounds: 85"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 24298.847019431905,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026757396392289127",
            "extra": "mean: 41.154216049851875 usec\nrounds: 10081"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1844.6169294568426,
            "unit": "iter/sec",
            "range": "stddev: 0.00007825600614119796",
            "extra": "mean: 542.1179780099142 usec\nrounds: 864"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31253.470310580775,
            "unit": "iter/sec",
            "range": "stddev: 0.000003715934765531389",
            "extra": "mean: 31.99644679654831 usec\nrounds: 5432"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 62562.30694885157,
            "unit": "iter/sec",
            "range": "stddev: 0.000002302557758550778",
            "extra": "mean: 15.984065306568057 usec\nrounds: 10244"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 50778.73178726853,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025727413863331045",
            "extra": "mean: 19.693284270851453 usec\nrounds: 14845"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 39738.44714927094,
            "unit": "iter/sec",
            "range": "stddev: 0.0000032090494980761583",
            "extra": "mean: 25.164546471674257 usec\nrounds: 13051"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 132.61416160504106,
            "unit": "iter/sec",
            "range": "stddev: 0.012108333172842267",
            "extra": "mean: 7.540672790122191 msec\nrounds: 81"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 16689.194187830606,
            "unit": "iter/sec",
            "range": "stddev: 0.000011862874685902556",
            "extra": "mean: 59.91901039351427 usec\nrounds: 18858"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7529.056683627998,
            "unit": "iter/sec",
            "range": "stddev: 0.000025070720120037998",
            "extra": "mean: 132.81876362738896 usec\nrounds: 11943"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1608.6663081220934,
            "unit": "iter/sec",
            "range": "stddev: 0.00004395977361609453",
            "extra": "mean: 621.6329607644786 usec\nrounds: 2931"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7584.26117906673,
            "unit": "iter/sec",
            "range": "stddev: 0.000025673437378173267",
            "extra": "mean: 131.85199934307295 usec\nrounds: 12178"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 564.3246939648251,
            "unit": "iter/sec",
            "range": "stddev: 0.0002562124486128218",
            "extra": "mean: 1.7720294906363445 msec\nrounds: 1068"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4187.778514632476,
            "unit": "iter/sec",
            "range": "stddev: 0.0006175065190729711",
            "extra": "mean: 238.79008799197706 usec\nrounds: 7012"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 80420.98530803448,
            "unit": "iter/sec",
            "range": "stddev: 0.000002655093140097653",
            "extra": "mean: 12.4345653832731 usec\nrounds: 50334"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 16531.453300480996,
            "unit": "iter/sec",
            "range": "stddev: 0.00001408143386411288",
            "extra": "mean: 60.49074947154853 usec\nrounds: 27909"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5271268.918203912,
            "unit": "iter/sec",
            "range": "stddev: 2.2036692161137975e-8",
            "extra": "mean: 189.70764260320294 nsec\nrounds: 110902"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4233640.473334943,
            "unit": "iter/sec",
            "range": "stddev: 1.910394531086336e-8",
            "extra": "mean: 236.20333523792948 nsec\nrounds: 101492"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2423302.47797997,
            "unit": "iter/sec",
            "range": "stddev: 1.715405567589825e-7",
            "extra": "mean: 412.65999976758394 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 21257.11119491377,
            "unit": "iter/sec",
            "range": "stddev: 0.0006377274417459634",
            "extra": "mean: 47.0430808227259 usec\nrounds: 39036"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39888.088418283005,
            "unit": "iter/sec",
            "range": "stddev: 0.0000022272738653993597",
            "extra": "mean: 25.070140978268654 usec\nrounds: 63336"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3901.8990796338235,
            "unit": "iter/sec",
            "range": "stddev: 0.000012449248070610077",
            "extra": "mean: 256.28545987249 usec\nrounds: 6741"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7863.222946571398,
            "unit": "iter/sec",
            "range": "stddev: 0.000010093810101137735",
            "extra": "mean: 127.17431602725064 usec\nrounds: 13271"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1228.8831426933812,
            "unit": "iter/sec",
            "range": "stddev: 0.00005039012874917541",
            "extra": "mean: 813.7470238287011 usec\nrounds: 2476"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 431.24007934644044,
            "unit": "iter/sec",
            "range": "stddev: 0.00003827489327949525",
            "extra": "mean: 2.318893924506125 msec\nrounds: 861"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1828.0878588055728,
            "unit": "iter/sec",
            "range": "stddev: 0.00004483819704436694",
            "extra": "mean: 547.0196605612682 usec\nrounds: 3385"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4012.2715927667605,
            "unit": "iter/sec",
            "range": "stddev: 0.0000121814298051777",
            "extra": "mean: 249.23537125522088 usec\nrounds: 7243"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 394.6383972613274,
            "unit": "iter/sec",
            "range": "stddev: 0.000034781717607400834",
            "extra": "mean: 2.5339652880705508 msec\nrounds: 788"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 392.85473901382943,
            "unit": "iter/sec",
            "range": "stddev: 0.00005824704974901626",
            "extra": "mean: 2.5454701208652026 msec\nrounds: 786"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 920.8050123915747,
            "unit": "iter/sec",
            "range": "stddev: 0.000013687195807192166",
            "extra": "mean: 1.0860062516414142 msec\nrounds: 1828"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 348.77111216685324,
            "unit": "iter/sec",
            "range": "stddev: 0.000034296104010906446",
            "extra": "mean: 2.8672099411765406 msec\nrounds: 697"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 349.2995642261724,
            "unit": "iter/sec",
            "range": "stddev: 0.0000811577755029715",
            "extra": "mean: 2.862872165945496 msec\nrounds: 693"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 322.0713682763712,
            "unit": "iter/sec",
            "range": "stddev: 0.000053348632562270696",
            "extra": "mean: 3.104901889763435 msec\nrounds: 635"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 324.77378913357006,
            "unit": "iter/sec",
            "range": "stddev: 0.00004530986695910364",
            "extra": "mean: 3.0790662099543047 msec\nrounds: 643"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 162.8470170082872,
            "unit": "iter/sec",
            "range": "stddev: 0.00005509241191205276",
            "extra": "mean: 6.140732685015105 msec\nrounds: 327"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 794.2929258980654,
            "unit": "iter/sec",
            "range": "stddev: 0.000026826755078992523",
            "extra": "mean: 1.2589813750001013 msec\nrounds: 1504"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 874.2166098947815,
            "unit": "iter/sec",
            "range": "stddev: 0.000019251342273223418",
            "extra": "mean: 1.1438812631578317 msec\nrounds: 1710"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1463.2100532917805,
            "unit": "iter/sec",
            "range": "stddev: 0.000020604926895008457",
            "extra": "mean: 683.4288745831825 usec\nrounds: 2998"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 799.3742147453877,
            "unit": "iter/sec",
            "range": "stddev: 0.000019537339305807422",
            "extra": "mean: 1.250978554916879 msec\nrounds: 1566"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 477.8692227105321,
            "unit": "iter/sec",
            "range": "stddev: 0.000025231517556484387",
            "extra": "mean: 2.0926227354167715 msec\nrounds: 960"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1453.0908183294212,
            "unit": "iter/sec",
            "range": "stddev: 0.000029998727898233357",
            "extra": "mean: 688.1882311731022 usec\nrounds: 2855"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28194.70525744336,
            "unit": "iter/sec",
            "range": "stddev: 0.0006418357095254226",
            "extra": "mean: 35.467652201684274 usec\nrounds: 43807"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2095.0125801409067,
            "unit": "iter/sec",
            "range": "stddev: 0.00002186558755685467",
            "extra": "mean: 477.32410271862994 usec\nrounds: 3972"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 41.96667912823735,
            "unit": "iter/sec",
            "range": "stddev: 0.00020269468334170452",
            "extra": "mean: 23.828428190477155 msec\nrounds: 84"
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
          "id": "9e7d6af22ce3f86e995175001a509232042250e4",
          "message": "feat(http-api): /v2/rebac/tuples router behind --features rebac (R10 — PR 5/5) (#4735)\n\nfeat(http-api): /v2/rebac/tuples router behind --features rebac (R10 — PR 5/5)",
          "timestamp": "2026-09-02T12:02:53Z",
          "url": "https://github.com/nexi-lab/nexus/commit/9e7d6af22ce3f86e995175001a509232042250e4"
        },
        "date": 1788356399209,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 260.59636731614694,
            "unit": "iter/sec",
            "range": "stddev: 0.0005546168145336138",
            "extra": "mean: 3.8373520333337297 msec\nrounds: 120"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 442.5152586440783,
            "unit": "iter/sec",
            "range": "stddev: 0.0003375429112679243",
            "extra": "mean: 2.2598090810792018 msec\nrounds: 148"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1671.782514771444,
            "unit": "iter/sec",
            "range": "stddev: 0.00002101932086031758",
            "extra": "mean: 598.1639305138407 usec\nrounds: 331"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 487.8742201800807,
            "unit": "iter/sec",
            "range": "stddev: 0.00009582125539645338",
            "extra": "mean: 2.049708631111697 msec\nrounds: 225"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 186.97717600369828,
            "unit": "iter/sec",
            "range": "stddev: 0.00040363513656128903",
            "extra": "mean: 5.348246354839698 msec\nrounds: 93"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25746.2297885296,
            "unit": "iter/sec",
            "range": "stddev: 0.000002294047917380018",
            "extra": "mean: 38.84063834641598 usec\nrounds: 10546"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1993.9790605871144,
            "unit": "iter/sec",
            "range": "stddev: 0.000012970379461337507",
            "extra": "mean: 501.5097800001753 usec\nrounds: 900"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 34573.42377056918,
            "unit": "iter/sec",
            "range": "stddev: 0.000002123660801245478",
            "extra": "mean: 28.923950564920784 usec\nrounds: 5664"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 67664.10708287761,
            "unit": "iter/sec",
            "range": "stddev: 0.0000015071799503815103",
            "extra": "mean: 14.778884154566043 usec\nrounds: 9599"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 54452.27787120238,
            "unit": "iter/sec",
            "range": "stddev: 0.0000015207127618688791",
            "extra": "mean: 18.36470463853377 usec\nrounds: 16126"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 45147.99720912356,
            "unit": "iter/sec",
            "range": "stddev: 0.000001997936682690824",
            "extra": "mean: 22.149376756803708 usec\nrounds: 14301"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 136.388690204158,
            "unit": "iter/sec",
            "range": "stddev: 0.012209888831827007",
            "extra": "mean: 7.3319862409640875 msec\nrounds: 83"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 29804.733727395997,
            "unit": "iter/sec",
            "range": "stddev: 0.000007583801179163747",
            "extra": "mean: 33.5517172925057 usec\nrounds: 31881"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 12481.520583933572,
            "unit": "iter/sec",
            "range": "stddev: 0.000015710848093379687",
            "extra": "mean: 80.11844336396139 usec\nrounds: 17277"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 2332.673773815937,
            "unit": "iter/sec",
            "range": "stddev: 0.0000269517232290741",
            "extra": "mean: 428.69260640939785 usec\nrounds: 3994"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 12510.099835270694,
            "unit": "iter/sec",
            "range": "stddev: 0.00001703057552403772",
            "extra": "mean: 79.93541323951888 usec\nrounds: 15756"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 792.5580459683348,
            "unit": "iter/sec",
            "range": "stddev: 0.0001636569640707836",
            "extra": "mean: 1.2617372381580152 msec\nrounds: 1520"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4158.596260279033,
            "unit": "iter/sec",
            "range": "stddev: 0.0004548847828271447",
            "extra": "mean: 240.46575753254345 usec\nrounds: 7003"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 87991.87368059948,
            "unit": "iter/sec",
            "range": "stddev: 0.0000012346575183600632",
            "extra": "mean: 11.364685830306177 usec\nrounds: 51469"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 29910.690201212376,
            "unit": "iter/sec",
            "range": "stddev: 0.000009814877981671997",
            "extra": "mean: 33.432862741477855 usec\nrounds: 38198"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5213933.732503458,
            "unit": "iter/sec",
            "range": "stddev: 2.3239325227366845e-8",
            "extra": "mean: 191.7937686407557 nsec\nrounds: 113598"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4460923.767079346,
            "unit": "iter/sec",
            "range": "stddev: 1.4951848191768628e-8",
            "extra": "mean: 224.16881619448063 nsec\nrounds: 102728"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2567090.919861999,
            "unit": "iter/sec",
            "range": "stddev: 1.813065464360115e-7",
            "extra": "mean: 389.54600020701946 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 29666.467686194068,
            "unit": "iter/sec",
            "range": "stddev: 0.0004494136218649655",
            "extra": "mean: 33.70809125568298 usec\nrounds: 62681"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 35420.65734428023,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026904771085686694",
            "extra": "mean: 28.232112980858645 usec\nrounds: 69826"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3504.0265999315525,
            "unit": "iter/sec",
            "range": "stddev: 0.00001026803249657172",
            "extra": "mean: 285.38596140210063 usec\nrounds: 5933"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7153.420379305085,
            "unit": "iter/sec",
            "range": "stddev: 0.000004912224985808039",
            "extra": "mean: 139.79326629440231 usec\nrounds: 13962"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1292.0787754567325,
            "unit": "iter/sec",
            "range": "stddev: 0.00002517235087204986",
            "extra": "mean: 773.9466191962742 usec\nrounds: 2563"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 434.3931579620515,
            "unit": "iter/sec",
            "range": "stddev: 0.00002057761835897295",
            "extra": "mean: 2.3020620414269044 msec\nrounds: 869"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1903.1743289388905,
            "unit": "iter/sec",
            "range": "stddev: 0.000060159135528374254",
            "extra": "mean: 525.4379405997701 usec\nrounds: 3367"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4230.9564513681125,
            "unit": "iter/sec",
            "range": "stddev: 0.000010409757356859208",
            "extra": "mean: 236.3531772293809 usec\nrounds: 7905"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 404.8349263821687,
            "unit": "iter/sec",
            "range": "stddev: 0.00002077472190945449",
            "extra": "mean: 2.4701426058679257 msec\nrounds: 784"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 414.1804022086347,
            "unit": "iter/sec",
            "range": "stddev: 0.000022232186471121994",
            "extra": "mean: 2.4144068494488327 msec\nrounds: 817"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 962.0830815617553,
            "unit": "iter/sec",
            "range": "stddev: 0.000012074624887117196",
            "extra": "mean: 1.039411272440935 msec\nrounds: 1905"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 337.94221039306905,
            "unit": "iter/sec",
            "range": "stddev: 0.0000947242186625389",
            "extra": "mean: 2.9590858118518995 msec\nrounds: 675"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 376.6536895505613,
            "unit": "iter/sec",
            "range": "stddev: 0.000022035787859291274",
            "extra": "mean: 2.65495872665748 msec\nrounds: 739"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 353.89412678227086,
            "unit": "iter/sec",
            "range": "stddev: 0.0000339414176004836",
            "extra": "mean: 2.8257038597739657 msec\nrounds: 706"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 358.6340354473079,
            "unit": "iter/sec",
            "range": "stddev: 0.00007433718001791886",
            "extra": "mean: 2.788357771879475 msec\nrounds: 697"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 163.45143856499791,
            "unit": "iter/sec",
            "range": "stddev: 0.00004503467002884196",
            "extra": "mean: 6.118025076924246 msec\nrounds: 325"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 876.2981569518572,
            "unit": "iter/sec",
            "range": "stddev: 0.000020358646528809927",
            "extra": "mean: 1.1411641027278103 msec\nrounds: 1723"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 895.867849793163,
            "unit": "iter/sec",
            "range": "stddev: 0.000019381704550438705",
            "extra": "mean: 1.1162360611901396 msec\nrounds: 1765"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1573.5067180659378,
            "unit": "iter/sec",
            "range": "stddev: 0.000012923320760339015",
            "extra": "mean: 635.52318431099 usec\nrounds: 3136"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 859.8121348576948,
            "unit": "iter/sec",
            "range": "stddev: 0.00001811104791223268",
            "extra": "mean: 1.163044762290436 msec\nrounds: 1729"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 508.0607849488761,
            "unit": "iter/sec",
            "range": "stddev: 0.000019942168502338943",
            "extra": "mean: 1.9682684230404153 msec\nrounds: 1033"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1534.803577015514,
            "unit": "iter/sec",
            "range": "stddev: 0.000012194121006795803",
            "extra": "mean: 651.549172138717 usec\nrounds: 3137"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 27799.875366207085,
            "unit": "iter/sec",
            "range": "stddev: 0.0006150353466519675",
            "extra": "mean: 35.971384289570516 usec\nrounds: 47649"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2040.3826861857356,
            "unit": "iter/sec",
            "range": "stddev: 0.000022192920570988562",
            "extra": "mean: 490.10413917468924 usec\nrounds: 3880"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 40.95664485210868,
            "unit": "iter/sec",
            "range": "stddev: 0.00010521470642319191",
            "extra": "mean: 24.41606248780689 msec\nrounds: 82"
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
          "id": "9e7d6af22ce3f86e995175001a509232042250e4",
          "message": "feat(http-api): /v2/rebac/tuples router behind --features rebac (R10 — PR 5/5) (#4735)\n\nfeat(http-api): /v2/rebac/tuples router behind --features rebac (R10 — PR 5/5)",
          "timestamp": "2026-09-02T12:02:53Z",
          "url": "https://github.com/nexi-lab/nexus/commit/9e7d6af22ce3f86e995175001a509232042250e4"
        },
        "date": 1788442570315,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 266.3123462753241,
            "unit": "iter/sec",
            "range": "stddev: 0.0004636410792559084",
            "extra": "mean: 3.7549892597399928 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 444.19458402899454,
            "unit": "iter/sec",
            "range": "stddev: 0.0002925176436037281",
            "extra": "mean: 2.251265629872528 msec\nrounds: 154"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1662.9752103081764,
            "unit": "iter/sec",
            "range": "stddev: 0.0000228277333177943",
            "extra": "mean: 601.3318742223967 usec\nrounds: 644"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 488.95435559836204,
            "unit": "iter/sec",
            "range": "stddev: 0.0001622374586923699",
            "extra": "mean: 2.0451806769902716 msec\nrounds: 226"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 190.44238389926207,
            "unit": "iter/sec",
            "range": "stddev: 0.0002723930975706562",
            "extra": "mean: 5.250931959184927 msec\nrounds: 98"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 25828.0246781518,
            "unit": "iter/sec",
            "range": "stddev: 0.000003875119078314592",
            "extra": "mean: 38.71763375098176 usec\nrounds: 10856"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1974.1345869176605,
            "unit": "iter/sec",
            "range": "stddev: 0.000024762209560196",
            "extra": "mean: 506.55107641944636 usec\nrounds: 916"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 35445.42101671225,
            "unit": "iter/sec",
            "range": "stddev: 0.0000022023645478171696",
            "extra": "mean: 28.21238883094399 usec\nrounds: 8631"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 69988.33804771135,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013686506432786526",
            "extra": "mean: 14.2880946725481 usec\nrounds: 9517"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 55322.99628384369,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016591510381080886",
            "extra": "mean: 18.075665946749094 usec\nrounds: 16671"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 46426.46445850809,
            "unit": "iter/sec",
            "range": "stddev: 0.000001875378733531676",
            "extra": "mean: 21.539439017453343 usec\nrounds: 15070"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 131.9158541295498,
            "unit": "iter/sec",
            "range": "stddev: 0.013982458571913409",
            "extra": "mean: 7.580589964705349 msec\nrounds: 85"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 29468.862373400636,
            "unit": "iter/sec",
            "range": "stddev: 0.000010474176071437742",
            "extra": "mean: 33.93412298476191 usec\nrounds: 32004"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 12730.085611848535,
            "unit": "iter/sec",
            "range": "stddev: 0.000016475709849414486",
            "extra": "mean: 78.55406715169687 usec\nrounds: 17036"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 2393.5910800961306,
            "unit": "iter/sec",
            "range": "stddev: 0.000029597954317161544",
            "extra": "mean: 417.78230555565 usec\nrounds: 4212"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 12287.259612625125,
            "unit": "iter/sec",
            "range": "stddev: 0.000022938778083680165",
            "extra": "mean: 81.38511202062523 usec\nrounds: 16247"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 819.1045812206027,
            "unit": "iter/sec",
            "range": "stddev: 0.00016138952715658936",
            "extra": "mean: 1.220845326624633 msec\nrounds: 1540"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4373.119645539384,
            "unit": "iter/sec",
            "range": "stddev: 0.00037741161152366197",
            "extra": "mean: 228.66970973913507 usec\nrounds: 7855"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 91804.76108579426,
            "unit": "iter/sec",
            "range": "stddev: 0.0000011653475822308028",
            "extra": "mean: 10.892681252832524 usec\nrounds: 50984"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 30635.114259347374,
            "unit": "iter/sec",
            "range": "stddev: 0.000009230001510109544",
            "extra": "mean: 32.64228073492105 usec\nrounds: 38531"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5356271.70669108,
            "unit": "iter/sec",
            "range": "stddev: 1.4577330181725855e-8",
            "extra": "mean: 186.6970263571199 nsec\nrounds: 113857"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4486104.407421644,
            "unit": "iter/sec",
            "range": "stddev: 1.722748114364009e-8",
            "extra": "mean: 222.9105498181534 nsec\nrounds: 100106"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2430529.3891787482,
            "unit": "iter/sec",
            "range": "stddev: 7.917779034383042e-7",
            "extra": "mean: 411.4330007496392 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 30935.359450615513,
            "unit": "iter/sec",
            "range": "stddev: 0.00040862866057783995",
            "extra": "mean: 32.32546890545677 usec\nrounds: 63098"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 35806.412431800476,
            "unit": "iter/sec",
            "range": "stddev: 0.0000020628888262545094",
            "extra": "mean: 27.9279584880131 usec\nrounds: 59067"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3606.370673617738,
            "unit": "iter/sec",
            "range": "stddev: 0.00001201110592177385",
            "extra": "mean: 277.28708180649886 usec\nrounds: 6332"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7458.713770363018,
            "unit": "iter/sec",
            "range": "stddev: 0.000007582400356360687",
            "extra": "mean: 134.07137353540375 usec\nrounds: 12031"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1314.4563325215056,
            "unit": "iter/sec",
            "range": "stddev: 0.00001613635167242935",
            "extra": "mean: 760.7708033036839 usec\nrounds: 2603"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 430.97152918232587,
            "unit": "iter/sec",
            "range": "stddev: 0.000026405789676788978",
            "extra": "mean: 2.3203388908248326 msec\nrounds: 861"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1941.6363180954665,
            "unit": "iter/sec",
            "range": "stddev: 0.000036221402384303426",
            "extra": "mean: 515.0295092238956 usec\nrounds: 3415"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4303.2474309233685,
            "unit": "iter/sec",
            "range": "stddev: 0.000007431010633119842",
            "extra": "mean: 232.38264033203063 usec\nrounds: 8063"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 414.336514200564,
            "unit": "iter/sec",
            "range": "stddev: 0.000022921011083601328",
            "extra": "mean: 2.4134971592581853 msec\nrounds: 810"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 422.2817958274333,
            "unit": "iter/sec",
            "range": "stddev: 0.000028124662256959606",
            "extra": "mean: 2.3680869265050033 msec\nrounds: 830"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 977.387713387778,
            "unit": "iter/sec",
            "range": "stddev: 0.0000940032370072408",
            "extra": "mean: 1.0231354316229782 msec\nrounds: 1967"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 347.14709265898085,
            "unit": "iter/sec",
            "range": "stddev: 0.000041578249768501983",
            "extra": "mean: 2.8806232895124593 msec\nrounds: 677"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 384.25719798348223,
            "unit": "iter/sec",
            "range": "stddev: 0.00003954848053441279",
            "extra": "mean: 2.602423598693358 msec\nrounds: 765"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 355.45472284211434,
            "unit": "iter/sec",
            "range": "stddev: 0.00017568546085530056",
            "extra": "mean: 2.813297828776295 msec\nrounds: 695"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 359.20150631472217,
            "unit": "iter/sec",
            "range": "stddev: 0.000048920656939573384",
            "extra": "mean: 2.7839526906767156 msec\nrounds: 708"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 165.34248938422238,
            "unit": "iter/sec",
            "range": "stddev: 0.000142132765081813",
            "extra": "mean: 6.048052159636977 msec\nrounds: 332"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 892.3559449929279,
            "unit": "iter/sec",
            "range": "stddev: 0.00002499349752926247",
            "extra": "mean: 1.1206290557160183 msec\nrounds: 1723"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 976.7048781333966,
            "unit": "iter/sec",
            "range": "stddev: 0.00003264117917575157",
            "extra": "mean: 1.0238507274696154 msec\nrounds: 1842"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1682.7276891249285,
            "unit": "iter/sec",
            "range": "stddev: 0.00001094803375230945",
            "extra": "mean: 594.2732186929376 usec\nrounds: 3306"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 895.8486865135458,
            "unit": "iter/sec",
            "range": "stddev: 0.00006125700442291384",
            "extra": "mean: 1.1162599388204602 msec\nrounds: 1798"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 537.8170927043108,
            "unit": "iter/sec",
            "range": "stddev: 0.00005545338119935845",
            "extra": "mean: 1.8593682007607653 msec\nrounds: 1051"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1622.3120019767173,
            "unit": "iter/sec",
            "range": "stddev: 0.000012596968538764677",
            "extra": "mean: 616.4042420826223 usec\nrounds: 3189"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 28874.28149385558,
            "unit": "iter/sec",
            "range": "stddev: 0.000541194545753853",
            "extra": "mean: 34.63289641381376 usec\nrounds: 48076"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 1880.5260360013247,
            "unit": "iter/sec",
            "range": "stddev: 0.0027174910792850866",
            "extra": "mean: 531.7661020670365 usec\nrounds: 3919"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 40.903998581209464,
            "unit": "iter/sec",
            "range": "stddev: 0.00016814911532611482",
            "extra": "mean: 24.447487646339336 msec\nrounds: 82"
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
          "id": "d8ab98c5e00ab77c31e63571b432939ea3bdb0b6",
          "message": "Merge pull request #4748 from nexi-lab/fix/owner-grant-admin-bypass-projection-mode\n\nperf(write path): admin-bypass owner-grant skip, NEXUS_PROJECTION_MODE=async, NEXUS_SYNC_OWNER_GRANT; fix(search-plugin): lenient query parse",
          "timestamp": "2026-09-04T04:47:34Z",
          "url": "https://github.com/nexi-lab/nexus/commit/d8ab98c5e00ab77c31e63571b432939ea3bdb0b6"
        },
        "date": 1788528803400,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_create_key_rpc_benchmark",
            "value": 220.06617730843092,
            "unit": "iter/sec",
            "range": "stddev: 0.0004547770823197541",
            "extra": "mean: 4.5440876568617945 msec\nrounds: 102"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_list_keys_rpc_benchmark",
            "value": 404.7205342613643,
            "unit": "iter/sec",
            "range": "stddev: 0.00032179931054226877",
            "extra": "mean: 2.470840778625308 msec\nrounds: 131"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_get_key_rpc_benchmark",
            "value": 1353.3971165540597,
            "unit": "iter/sec",
            "range": "stddev: 0.00002507905940747907",
            "extra": "mean: 738.8814323368306 usec\nrounds: 569"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_update_key_rpc_benchmark",
            "value": 431.862889391713,
            "unit": "iter/sec",
            "range": "stddev: 0.00030983741127043175",
            "extra": "mean: 2.3155497371133666 msec\nrounds: 194"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_admin_revoke_key_rpc_benchmark",
            "value": 166.18408689835874,
            "unit": "iter/sec",
            "range": "stddev: 0.0005380337470268534",
            "extra": "mean: 6.017423320510937 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_list_rpc_benchmark",
            "value": 24603.85183453157,
            "unit": "iter/sec",
            "range": "stddev: 0.000003059252573276933",
            "extra": "mean: 40.644042515184445 usec\nrounds: 8938"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_audit_export_rpc_benchmark",
            "value": 1825.287187071427,
            "unit": "iter/sec",
            "range": "stddev: 0.000021954569527719958",
            "extra": "mean: 547.8589928659089 usec\nrounds: 841"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_events_replay_rpc_benchmark",
            "value": 31573.157350877354,
            "unit": "iter/sec",
            "range": "stddev: 0.0000037370658328980132",
            "extra": "mean: 31.672473832339485 usec\nrounds: 8885"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_alerts_rpc_benchmark",
            "value": 60365.89131973164,
            "unit": "iter/sec",
            "range": "stddev: 0.000003182976471357279",
            "extra": "mean: 16.565646230641054 usec\nrounds: 10227"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_rings_rpc_benchmark",
            "value": 47681.93872505412,
            "unit": "iter/sec",
            "range": "stddev: 0.00000415812459725426",
            "extra": "mean: 20.972301603889218 usec\nrounds: 14403"
          },
          {
            "name": "tests/benchmarks/test_full_control_plane_rpc_benchmark.py::test_governance_status_rpc_benchmark",
            "value": 37736.10346477785,
            "unit": "iter/sec",
            "range": "stddev: 0.000003454182680621998",
            "extra": "mean: 26.499821343064227 usec\nrounds: 12997"
          },
          {
            "name": "tests/benchmarks/test_rebac_filter_chain_latency.py::test_filter_chain_inherited_grants_stay_bulk",
            "value": 127.00491845828535,
            "unit": "iter/sec",
            "range": "stddev: 0.011945148968800279",
            "extra": "mean: 7.8737108148173744 msec\nrounds: 81"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestL1CacheHit::test_l1_cache_hit_latency",
            "value": 16179.140629353884,
            "unit": "iter/sec",
            "range": "stddev: 0.000011248583794811024",
            "extra": "mean: 61.80797997303365 usec\nrounds: 24567"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBoundaryCacheHit::test_boundary_cache_hit_latency",
            "value": 7477.342201510918,
            "unit": "iter/sec",
            "range": "stddev: 0.00002509149523330249",
            "extra": "mean: 133.73735921808338 usec\nrounds: 12633"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestLeopardIndexHit::test_leopard_group_check_latency",
            "value": 1580.9474817879998,
            "unit": "iter/sec",
            "range": "stddev: 0.00006068213818559926",
            "extra": "mean: 632.5320806160069 usec\nrounds: 2791"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDirectGrantTraversal::test_direct_grant_latency",
            "value": 7355.388525556934,
            "unit": "iter/sec",
            "range": "stddev: 0.000028199542175070046",
            "extra": "mean: 135.9547488926538 usec\nrounds: 9482"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDeepInheritanceTraversal::test_deep_inheritance_latency",
            "value": 548.0976875069954,
            "unit": "iter/sec",
            "range": "stddev: 0.000263825187493418",
            "extra": "mean: 1.8244922808349506 msec\nrounds: 1054"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestBulkPermissionCheck::test_bulk_check_latency",
            "value": 4060.8577362788883,
            "unit": "iter/sec",
            "range": "stddev: 0.0006725288827556255",
            "extra": "mean: 246.2533939729532 usec\nrounds: 7267"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestDenialLatency::test_denial_latency",
            "value": 75108.6309864982,
            "unit": "iter/sec",
            "range": "stddev: 0.000002303890775957066",
            "extra": "mean: 13.314049089508282 usec\nrounds: 43981"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCachedConsistencyLatency::test_cached_consistency_latency",
            "value": 16146.3275661889,
            "unit": "iter/sec",
            "range": "stddev: 0.000014009678757167795",
            "extra": "mean: 61.93358804970876 usec\nrounds: 26610"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_check_latency",
            "value": 5325773.678739964,
            "unit": "iter/sec",
            "range": "stddev: 1.6667291076298362e-8",
            "extra": "mean: 187.76614635201548 nsec\nrounds: 112906"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_read_fence_advance_latency",
            "value": 4135615.716061212,
            "unit": "iter/sec",
            "range": "stddev: 1.6255193456683022e-8",
            "extra": "mean: 241.8019633972198 nsec\nrounds: 97810"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_durable_stream_publish_latency",
            "value": 2399145.907690975,
            "unit": "iter/sec",
            "range": "stddev: 1.8122275474607582e-7",
            "extra": "mean: 416.814999368853 nsec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_rebac_latency.py::TestCrossZoneInvalidationLatency::test_invalidation_pipeline_with_durable_stream",
            "value": 20564.080769239,
            "unit": "iter/sec",
            "range": "stddev: 0.00046629374983210207",
            "extra": "mean: 48.62848046657455 usec\nrounds: 57696"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39469.38262524063,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026001667065683907",
            "extra": "mean: 25.336094296050657 usec\nrounds: 53894"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3903.535712350734,
            "unit": "iter/sec",
            "range": "stddev: 0.000011739940500218294",
            "extra": "mean: 256.1780072450762 usec\nrounds: 7315"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7792.913287703437,
            "unit": "iter/sec",
            "range": "stddev: 0.000007948590394152604",
            "extra": "mean: 128.3217152663454 usec\nrounds: 13697"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1232.8997181893353,
            "unit": "iter/sec",
            "range": "stddev: 0.000015829716329496527",
            "extra": "mean: 811.0959758094704 usec\nrounds: 2439"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 428.5129374113618,
            "unit": "iter/sec",
            "range": "stddev: 0.00007486100830617892",
            "extra": "mean: 2.3336518286728523 msec\nrounds: 858"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestSectionAwareGrepBenchmarks::test_section_filter_uses_cached_structure_ranges",
            "value": 1799.2146771603827,
            "unit": "iter/sec",
            "range": "stddev: 0.00004242434555027089",
            "extra": "mean: 555.7980449438382 usec\nrounds: 3382"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 4021.256712447788,
            "unit": "iter/sec",
            "range": "stddev: 0.00002134551758269317",
            "extra": "mean: 248.67847827384486 usec\nrounds: 6743"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 389.00228034533666,
            "unit": "iter/sec",
            "range": "stddev: 0.00003115292493594466",
            "extra": "mean: 2.5706790179025436 msec\nrounds: 782"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 394.33594237177607,
            "unit": "iter/sec",
            "range": "stddev: 0.000060674006553068955",
            "extra": "mean: 2.535908834445554 msec\nrounds: 749"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 904.7642848039233,
            "unit": "iter/sec",
            "range": "stddev: 0.00004407991973411906",
            "extra": "mean: 1.1052602504272324 msec\nrounds: 1753"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 336.3081812903683,
            "unit": "iter/sec",
            "range": "stddev: 0.00010118147179956222",
            "extra": "mean: 2.973463197247053 msec\nrounds: 654"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 354.83048886203005,
            "unit": "iter/sec",
            "range": "stddev: 0.00016724117030651292",
            "extra": "mean: 2.8182471106332505 msec\nrounds: 696"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 321.5755454938738,
            "unit": "iter/sec",
            "range": "stddev: 0.0003016935858366016",
            "extra": "mean: 3.1096891974923215 msec\nrounds: 638"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 327.9710179653787,
            "unit": "iter/sec",
            "range": "stddev: 0.0000905760489613097",
            "extra": "mean: 3.049049901432334 msec\nrounds: 629"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 156.7163244554661,
            "unit": "iter/sec",
            "range": "stddev: 0.00025699756102033806",
            "extra": "mean: 6.380956186119391 msec\nrounds: 317"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 797.7305316346363,
            "unit": "iter/sec",
            "range": "stddev: 0.000026215443394503617",
            "extra": "mean: 1.253556132483599 msec\nrounds: 1570"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 832.0680710699316,
            "unit": "iter/sec",
            "range": "stddev: 0.00002744587242611872",
            "extra": "mean: 1.2018247482013458 msec\nrounds: 1668"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1384.899713243026,
            "unit": "iter/sec",
            "range": "stddev: 0.00002458891830632514",
            "extra": "mean: 722.0739454543573 usec\nrounds: 2750"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 763.5118020059317,
            "unit": "iter/sec",
            "range": "stddev: 0.00001924349014879632",
            "extra": "mean: 1.309737449208717 msec\nrounds: 1516"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 454.50045238254603,
            "unit": "iter/sec",
            "range": "stddev: 0.00013740073560907255",
            "extra": "mean: 2.200217832034885 msec\nrounds: 899"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 1406.923827055855,
            "unit": "iter/sec",
            "range": "stddev: 0.000013641379881816522",
            "extra": "mean: 710.7705341039049 usec\nrounds: 2771"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_small_list",
            "value": 27352.56510086977,
            "unit": "iter/sec",
            "range": "stddev: 0.0006275821400892566",
            "extra": "mean: 36.55964244348701 usec\nrounds: 43884"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_threshold_size",
            "value": 2051.190837042483,
            "unit": "iter/sec",
            "range": "stddev: 0.00002894464017905963",
            "extra": "mean: 487.5216785981034 usec\nrounds: 3967"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestFilesFilterValidator::test_validator_at_size_cap",
            "value": 41.073942577871684,
            "unit": "iter/sec",
            "range": "stddev: 0.00027226472174094135",
            "extra": "mean: 24.346335833335445 msec\nrounds: 84"
          }
        ]
      }
    ]
  }
}