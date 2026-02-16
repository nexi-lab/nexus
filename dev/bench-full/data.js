window.BENCHMARK_DATA = {
  "lastUpdate": 1771214601318,
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
        "date": 1771209386623,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 416.7242133073333,
            "unit": "iter/sec",
            "range": "stddev: 0.008323358326762996",
            "extra": "mean: 2.399668577123216 msec\nrounds: 577"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 460.9490261944046,
            "unit": "iter/sec",
            "range": "stddev: 0.00028511622524902356",
            "extra": "mean: 2.1694372765162355 msec\nrounds: 528"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6137.10614863148,
            "unit": "iter/sec",
            "range": "stddev: 0.000023757138728941075",
            "extra": "mean: 162.94324650437912 usec\nrounds: 6365"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4461.347466946497,
            "unit": "iter/sec",
            "range": "stddev: 0.00003241476727555447",
            "extra": "mean: 224.14752659568907 usec\nrounds: 4136"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 400.5376926485228,
            "unit": "iter/sec",
            "range": "stddev: 0.00033146505211188905",
            "extra": "mean: 2.496643932279086 msec\nrounds: 443"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 337.32361257935156,
            "unit": "iter/sec",
            "range": "stddev: 0.0005566968904445751",
            "extra": "mean: 2.9645123042335535 msec\nrounds: 378"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 333.88271513993783,
            "unit": "iter/sec",
            "range": "stddev: 0.0008936793614823619",
            "extra": "mean: 2.9950636994816495 msec\nrounds: 386"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 335.40763629546547,
            "unit": "iter/sec",
            "range": "stddev: 0.0008428346346835112",
            "extra": "mean: 2.9814467286579176 msec\nrounds: 328"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 312.25543098091174,
            "unit": "iter/sec",
            "range": "stddev: 0.0004788970379126801",
            "extra": "mean: 3.20250634827591 msec\nrounds: 290"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 17892.045991659557,
            "unit": "iter/sec",
            "range": "stddev: 0.0000056597108873304695",
            "extra": "mean: 55.89075729327734 usec\nrounds: 14911"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 12996.043364403664,
            "unit": "iter/sec",
            "range": "stddev: 0.00001879155196247077",
            "extra": "mean: 76.94649609580507 usec\nrounds: 17033"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 14329.394147877172,
            "unit": "iter/sec",
            "range": "stddev: 0.000055877127111728004",
            "extra": "mean: 69.78662110066566 usec\nrounds: 6379"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6264.879835180315,
            "unit": "iter/sec",
            "range": "stddev: 0.00008508736972132133",
            "extra": "mean: 159.61998095869592 usec\nrounds: 5987"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 14380.380839078802,
            "unit": "iter/sec",
            "range": "stddev: 0.00003163296009720341",
            "extra": "mean: 69.53918753545747 usec\nrounds: 17666"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53713.454084616635,
            "unit": "iter/sec",
            "range": "stddev: 0.000014606820040922693",
            "extra": "mean: 18.617309518480525 usec\nrounds: 44860"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 218437.5272832133,
            "unit": "iter/sec",
            "range": "stddev: 0.000010035728978279753",
            "extra": "mean: 4.577967954670438 usec\nrounds: 196503"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 167.06637583482046,
            "unit": "iter/sec",
            "range": "stddev: 0.0009015227136121597",
            "extra": "mean: 5.985644897143792 msec\nrounds: 175"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4264.254551600042,
            "unit": "iter/sec",
            "range": "stddev: 0.0000775079846856722",
            "extra": "mean: 234.5075763886511 usec\nrounds: 3744"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 251.92105738019137,
            "unit": "iter/sec",
            "range": "stddev: 0.00020189019033012447",
            "extra": "mean: 3.969497470355689 msec\nrounds: 253"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 171.15526213443866,
            "unit": "iter/sec",
            "range": "stddev: 0.00028971534368965585",
            "extra": "mean: 5.842648292136776 msec\nrounds: 178"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.7284698389197,
            "unit": "iter/sec",
            "range": "stddev: 0.00037219100459902593",
            "extra": "mean: 5.413350745946114 msec\nrounds: 185"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 92.54770376559328,
            "unit": "iter/sec",
            "range": "stddev: 0.0006107081132124463",
            "extra": "mean: 10.80523837234061 msec\nrounds: 94"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 133.12068667712407,
            "unit": "iter/sec",
            "range": "stddev: 0.0004520612086718217",
            "extra": "mean: 7.511980481481723 msec\nrounds: 135"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 70.43758860665922,
            "unit": "iter/sec",
            "range": "stddev: 0.0013519716561228197",
            "extra": "mean: 14.196965282049124 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 3.638540666538692,
            "unit": "iter/sec",
            "range": "stddev: 0.2527689339220282",
            "extra": "mean: 274.8354606000021 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 781.8102254403051,
            "unit": "iter/sec",
            "range": "stddev: 0.00016056766631075467",
            "extra": "mean: 1.2790827843634474 msec\nrounds: 793"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1646791.4218339906,
            "unit": "iter/sec",
            "range": "stddev: 7.189218326323555e-8",
            "extra": "mean: 607.2414434162675 nsec\nrounds: 163640"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 813190.5341829684,
            "unit": "iter/sec",
            "range": "stddev: 1.5658383202154594e-7",
            "extra": "mean: 1.2297241027340824 usec\nrounds: 81887"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23718.595532838957,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017330790539852665",
            "extra": "mean: 42.1610123843748 usec\nrounds: 23982"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1506.1682624082607,
            "unit": "iter/sec",
            "range": "stddev: 0.000007244948341473382",
            "extra": "mean: 663.9364438612376 usec\nrounds: 1523"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 150.89045929809942,
            "unit": "iter/sec",
            "range": "stddev: 0.000030194750092414602",
            "extra": "mean: 6.627324250000449 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10201.427376887283,
            "unit": "iter/sec",
            "range": "stddev: 0.000002493360479114798",
            "extra": "mean: 98.02549810486673 usec\nrounds: 10287"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1439.2890982862998,
            "unit": "iter/sec",
            "range": "stddev: 0.000006784306531557498",
            "extra": "mean: 694.787448324772 usec\nrounds: 1403"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 71198.65876273316,
            "unit": "iter/sec",
            "range": "stddev: 0.000018705481957852437",
            "extra": "mean: 14.045208398271408 usec\nrounds: 63537"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1108825.482585509,
            "unit": "iter/sec",
            "range": "stddev: 0.000001063290315164705",
            "extra": "mean: 901.8551753232124 nsec\nrounds: 113935"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5499.916709090304,
            "unit": "iter/sec",
            "range": "stddev: 0.00005919300054413952",
            "extra": "mean: 181.82093527838202 usec\nrounds: 5176"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 255.95173167812865,
            "unit": "iter/sec",
            "range": "stddev: 0.00022659023051464994",
            "extra": "mean: 3.9069866550367673 msec\nrounds: 258"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 71955.52485832582,
            "unit": "iter/sec",
            "range": "stddev: 0.000018354684710660092",
            "extra": "mean: 13.897473501429017 usec\nrounds: 67400"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 2000.59078652275,
            "unit": "iter/sec",
            "range": "stddev: 0.001553909528273702",
            "extra": "mean: 499.85234698501813 usec\nrounds: 3101"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 355865.7827326286,
            "unit": "iter/sec",
            "range": "stddev: 0.0000061092979508580466",
            "extra": "mean: 2.81004819379144 usec\nrounds: 182150"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3140.579889939416,
            "unit": "iter/sec",
            "range": "stddev: 0.000024090229604772483",
            "extra": "mean: 318.41253368634756 usec\nrounds: 2642"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2511.085746046572,
            "unit": "iter/sec",
            "range": "stddev: 0.000028368693620699007",
            "extra": "mean: 398.2341111108571 usec\nrounds: 1683"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5037.1400993285015,
            "unit": "iter/sec",
            "range": "stddev: 0.000024143391369007266",
            "extra": "mean: 198.52534975815132 usec\nrounds: 4120"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1105.1565526912868,
            "unit": "iter/sec",
            "range": "stddev: 0.00003508137947189987",
            "extra": "mean: 904.849179570796 usec\nrounds: 607"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 358984.53762084746,
            "unit": "iter/sec",
            "range": "stddev: 5.057280281119651e-7",
            "extra": "mean: 2.7856352995798965 usec\nrounds: 110902"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 127084.26186000915,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013061672606451572",
            "extra": "mean: 7.868794966142693 usec\nrounds: 77436"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 292130.66764029,
            "unit": "iter/sec",
            "range": "stddev: 7.666124905139551e-7",
            "extra": "mean: 3.4231257131528983 usec\nrounds: 175439"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 43.44681718012714,
            "unit": "iter/sec",
            "range": "stddev: 0.0007968511350728458",
            "extra": "mean: 23.01664574999999 msec\nrounds: 48"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.559991959420869,
            "unit": "iter/sec",
            "range": "stddev: 0.0071023103117306505",
            "extra": "mean: 219.29863230000137 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1433.622861122924,
            "unit": "iter/sec",
            "range": "stddev: 0.000250291660303536",
            "extra": "mean: 697.5335195315754 usec\nrounds: 1536"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 388.9769715848323,
            "unit": "iter/sec",
            "range": "stddev: 0.0004461426121742503",
            "extra": "mean: 2.5708462789600106 msec\nrounds: 423"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 957224.5398527131,
            "unit": "iter/sec",
            "range": "stddev: 1.409665064368397e-7",
            "extra": "mean: 1.0446869656662465 usec\nrounds: 98532"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 479430.693983372,
            "unit": "iter/sec",
            "range": "stddev: 1.422195674880663e-7",
            "extra": "mean: 2.0858072137423114 usec\nrounds: 48172"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59558.7257592968,
            "unit": "iter/sec",
            "range": "stddev: 0.000001273729247133661",
            "extra": "mean: 16.790151019036962 usec\nrounds: 59913"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3986.8915525625603,
            "unit": "iter/sec",
            "range": "stddev: 0.000005800592450482458",
            "extra": "mean: 250.8219716579082 usec\nrounds: 3987"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 395.4492022949596,
            "unit": "iter/sec",
            "range": "stddev: 0.000017635804723504385",
            "extra": "mean: 2.528769799500354 msec\nrounds: 399"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18493.103400722892,
            "unit": "iter/sec",
            "range": "stddev: 0.0000031013766993085414",
            "extra": "mean: 54.07421233371302 usec\nrounds: 17821"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18529.8734782739,
            "unit": "iter/sec",
            "range": "stddev: 0.0000029512948805949425",
            "extra": "mean: 53.96690922755033 usec\nrounds: 18640"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18482.206936474588,
            "unit": "iter/sec",
            "range": "stddev: 0.000002623862982519152",
            "extra": "mean: 54.1060926023127 usec\nrounds: 18682"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1506.4242366040114,
            "unit": "iter/sec",
            "range": "stddev: 0.000005197796050103441",
            "extra": "mean: 663.8236266394237 usec\nrounds: 1524"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 151.0016600744864,
            "unit": "iter/sec",
            "range": "stddev: 0.00001023556777399749",
            "extra": "mean: 6.622443750000616 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39577.6635210199,
            "unit": "iter/sec",
            "range": "stddev: 0.0000029756853999133597",
            "extra": "mean: 25.266777041269627 usec\nrounds: 40281"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3944.0836935783486,
            "unit": "iter/sec",
            "range": "stddev: 0.000014237902219960298",
            "extra": "mean: 253.54431540795477 usec\nrounds: 3998"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8170.164879017768,
            "unit": "iter/sec",
            "range": "stddev: 0.000005240430872312622",
            "extra": "mean: 122.39655071933161 usec\nrounds: 8271"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1250.3829469547782,
            "unit": "iter/sec",
            "range": "stddev: 0.00007143677941210624",
            "extra": "mean: 799.754989009912 usec\nrounds: 1274"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 428.77967520190697,
            "unit": "iter/sec",
            "range": "stddev: 0.00015821395224813417",
            "extra": "mean: 2.3322000967725733 msec\nrounds: 434"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11325.494123529163,
            "unit": "iter/sec",
            "range": "stddev: 0.000005607694122294441",
            "extra": "mean: 88.29636827257369 usec\nrounds: 10981"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1049.9758641324129,
            "unit": "iter/sec",
            "range": "stddev: 0.0000301788234163679",
            "extra": "mean: 952.4028448275737 usec\nrounds: 1044"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1059.1064598624957,
            "unit": "iter/sec",
            "range": "stddev: 0.000015108340745582612",
            "extra": "mean: 944.1921448858225 usec\nrounds: 1056"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1184.9455505531578,
            "unit": "iter/sec",
            "range": "stddev: 0.000023443764438547326",
            "extra": "mean: 843.920633765137 usec\nrounds: 1155"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1624.6751153073924,
            "unit": "iter/sec",
            "range": "stddev: 0.000012632353803009781",
            "extra": "mean: 615.5076732438274 usec\nrounds: 1622"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 987.1962144567087,
            "unit": "iter/sec",
            "range": "stddev: 0.000020566974903447805",
            "extra": "mean: 1.0129698487046344 msec\nrounds: 965"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1035.6761315355436,
            "unit": "iter/sec",
            "range": "stddev: 0.000023898105330469008",
            "extra": "mean: 965.5528109133418 usec\nrounds: 1063"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1028.212139058057,
            "unit": "iter/sec",
            "range": "stddev: 0.000028302147067381173",
            "extra": "mean: 972.5619471056799 usec\nrounds: 1002"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 735.0666182248556,
            "unit": "iter/sec",
            "range": "stddev: 0.000018665616706253825",
            "extra": "mean: 1.3604209131615084 msec\nrounds: 737"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1080.2779217136967,
            "unit": "iter/sec",
            "range": "stddev: 0.00001936169115997301",
            "extra": "mean: 925.6877141519767 usec\nrounds: 1067"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 946.1676897973582,
            "unit": "iter/sec",
            "range": "stddev: 0.000028451886301433113",
            "extra": "mean: 1.0568951051522073 msec\nrounds: 951"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1565.7263874278865,
            "unit": "iter/sec",
            "range": "stddev: 0.000015336983452209118",
            "extra": "mean: 638.6811948943139 usec\nrounds: 1606"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3140.8462304743643,
            "unit": "iter/sec",
            "range": "stddev: 0.00001286466951381126",
            "extra": "mean: 318.385532630475 usec\nrounds: 3126"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3168.289036185327,
            "unit": "iter/sec",
            "range": "stddev: 0.000012849176120103465",
            "extra": "mean: 315.62776898790037 usec\nrounds: 3173"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4096.834224879619,
            "unit": "iter/sec",
            "range": "stddev: 0.000012399130332341969",
            "extra": "mean: 244.09091144745648 usec\nrounds: 4167"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7616.264400006282,
            "unit": "iter/sec",
            "range": "stddev: 0.000008358180283583501",
            "extra": "mean: 131.29796281746405 usec\nrounds: 7396"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 644.2866958446722,
            "unit": "iter/sec",
            "range": "stddev: 0.000018071645481335886",
            "extra": "mean: 1.5521040655495468 msec\nrounds: 656"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4689.147325454609,
            "unit": "iter/sec",
            "range": "stddev: 0.0000070132420176349735",
            "extra": "mean: 213.25838805950735 usec\nrounds: 4489"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 439.5468314223573,
            "unit": "iter/sec",
            "range": "stddev: 0.00002752685915946217",
            "extra": "mean: 2.275070432800157 msec\nrounds: 439"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 620.4685696842074,
            "unit": "iter/sec",
            "range": "stddev: 0.00004731309343251778",
            "extra": "mean: 1.6116851825531762 msec\nrounds: 619"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 8913.652897221957,
            "unit": "iter/sec",
            "range": "stddev: 0.00001787599767178073",
            "extra": "mean: 112.18745126497596 usec\nrounds: 9090"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 632.5128482247302,
            "unit": "iter/sec",
            "range": "stddev: 0.00014992250746246276",
            "extra": "mean: 1.580995552591056 msec\nrounds: 637"
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
        "date": 1771210259422,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 383.60690581168086,
            "unit": "iter/sec",
            "range": "stddev: 0.008906240702104416",
            "extra": "mean: 2.6068352390165703 msec\nrounds: 569"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 436.16569006359737,
            "unit": "iter/sec",
            "range": "stddev: 0.0005006320526743747",
            "extra": "mean: 2.2927067001858625 msec\nrounds: 537"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6083.03954574155,
            "unit": "iter/sec",
            "range": "stddev: 0.000023998476730636734",
            "extra": "mean: 164.39150074242949 usec\nrounds: 6061"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4595.971596351122,
            "unit": "iter/sec",
            "range": "stddev: 0.00004167096522477212",
            "extra": "mean: 217.58184946006406 usec\nrounds: 4723"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 380.83287965136645,
            "unit": "iter/sec",
            "range": "stddev: 0.0005057987966277806",
            "extra": "mean: 2.625823697038581 msec\nrounds: 439"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 322.0780269806775,
            "unit": "iter/sec",
            "range": "stddev: 0.0011087124879327697",
            "extra": "mean: 3.10483769841273 msec\nrounds: 378"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 326.9138990011103,
            "unit": "iter/sec",
            "range": "stddev: 0.0007684746811216747",
            "extra": "mean: 3.0589094041443734 msec\nrounds: 386"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 326.3962129421219,
            "unit": "iter/sec",
            "range": "stddev: 0.0009699800870647153",
            "extra": "mean: 3.063761037501145 msec\nrounds: 320"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 309.8635876725347,
            "unit": "iter/sec",
            "range": "stddev: 0.0006823768761847467",
            "extra": "mean: 3.227226559633088 msec\nrounds: 327"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 17029.28943600237,
            "unit": "iter/sec",
            "range": "stddev: 0.000017656605406635412",
            "extra": "mean: 58.72235619449018 usec\nrounds: 17701"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15054.02934742862,
            "unit": "iter/sec",
            "range": "stddev: 0.00003436136900239765",
            "extra": "mean: 66.42739806873101 usec\nrounds: 17399"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 14166.904836707825,
            "unit": "iter/sec",
            "range": "stddev: 0.00005821115789376088",
            "extra": "mean: 70.58704858445176 usec\nrounds: 9077"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6835.035817510163,
            "unit": "iter/sec",
            "range": "stddev: 0.00007691900860807932",
            "extra": "mean: 146.30501239484005 usec\nrounds: 4034"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 18415.95455435938,
            "unit": "iter/sec",
            "range": "stddev: 0.000013668697734681738",
            "extra": "mean: 54.30074216616061 usec\nrounds: 16084"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54819.26378156311,
            "unit": "iter/sec",
            "range": "stddev: 0.000023318145427575594",
            "extra": "mean: 18.241762676432028 usec\nrounds: 45912"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 215667.84598291473,
            "unit": "iter/sec",
            "range": "stddev: 0.000007647352224273409",
            "extra": "mean: 4.636759807390204 usec\nrounds: 187970"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 130.50606833083472,
            "unit": "iter/sec",
            "range": "stddev: 0.017171900505073907",
            "extra": "mean: 7.6624789390251635 msec\nrounds: 164"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4337.989963484406,
            "unit": "iter/sec",
            "range": "stddev: 0.000053686471646629196",
            "extra": "mean: 230.5215107498242 usec\nrounds: 4000"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 245.62059542077589,
            "unit": "iter/sec",
            "range": "stddev: 0.00025862689148006265",
            "extra": "mean: 4.071319826771394 msec\nrounds: 254"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 169.3991184723223,
            "unit": "iter/sec",
            "range": "stddev: 0.0004920004939248411",
            "extra": "mean: 5.9032184406755785 msec\nrounds: 177"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.28669072538605,
            "unit": "iter/sec",
            "range": "stddev: 0.0003932902201711675",
            "extra": "mean: 5.426327837695807 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 90.23656810661271,
            "unit": "iter/sec",
            "range": "stddev: 0.0008168859776572126",
            "extra": "mean: 11.081981739581673 msec\nrounds: 96"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 108.68050469632357,
            "unit": "iter/sec",
            "range": "stddev: 0.019010993408149487",
            "extra": "mean: 9.201282261194983 msec\nrounds: 134"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 72.21631515617459,
            "unit": "iter/sec",
            "range": "stddev: 0.0013288246087014605",
            "extra": "mean: 13.847286417721614 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 3.8298294118722023,
            "unit": "iter/sec",
            "range": "stddev: 0.24684233454588778",
            "extra": "mean: 261.10823550001214 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 784.0422092557013,
            "unit": "iter/sec",
            "range": "stddev: 0.00017178086923432093",
            "extra": "mean: 1.2754415364311948 msec\nrounds: 796"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1635821.6067175549,
            "unit": "iter/sec",
            "range": "stddev: 6.982048955082095e-8",
            "extra": "mean: 611.3136028363161 nsec\nrounds: 163613"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 812903.4235188048,
            "unit": "iter/sec",
            "range": "stddev: 1.0080226720371831e-7",
            "extra": "mean: 1.2301584309625768 usec\nrounds: 81948"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23713.610562171856,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018617378070386568",
            "extra": "mean: 42.169875286524615 usec\nrounds: 23983"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1506.1934531577956,
            "unit": "iter/sec",
            "range": "stddev: 0.000006113831586782368",
            "extra": "mean: 663.9253396723107 usec\nrounds: 1525"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 151.0924464491454,
            "unit": "iter/sec",
            "range": "stddev: 0.000007912993252424236",
            "extra": "mean: 6.618464546052469 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10202.459093932312,
            "unit": "iter/sec",
            "range": "stddev: 0.0000024729814903691525",
            "extra": "mean: 98.01558534008022 usec\nrounds: 10259"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1442.1674167421306,
            "unit": "iter/sec",
            "range": "stddev: 0.00000572625208007094",
            "extra": "mean: 693.4007719152393 usec\nrounds: 1403"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 72736.82329163025,
            "unit": "iter/sec",
            "range": "stddev: 0.000014788333405087204",
            "extra": "mean: 13.748194583513918 usec\nrounds: 66059"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1076755.8904434093,
            "unit": "iter/sec",
            "range": "stddev: 0.000001416732783602339",
            "extra": "mean: 928.715606643395 nsec\nrounds: 113676"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5526.188773549786,
            "unit": "iter/sec",
            "range": "stddev: 0.00006336413732450513",
            "extra": "mean: 180.95654002743072 usec\nrounds: 5259"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 258.11829076255003,
            "unit": "iter/sec",
            "range": "stddev: 0.00027876945200069035",
            "extra": "mean: 3.8741927084893297 msec\nrounds: 271"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 71855.08097302432,
            "unit": "iter/sec",
            "range": "stddev: 0.000013866613409413608",
            "extra": "mean: 13.916900328529556 usec\nrounds: 64562"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1987.760413119796,
            "unit": "iter/sec",
            "range": "stddev: 0.0013920084737617377",
            "extra": "mean: 503.078737960425 usec\nrounds: 2824"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 362070.2841056217,
            "unit": "iter/sec",
            "range": "stddev: 0.0000035674178233279516",
            "extra": "mean: 2.761894703593748 usec\nrounds: 175101"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3163.9348293121166,
            "unit": "iter/sec",
            "range": "stddev: 0.00002537803109038001",
            "extra": "mean: 316.0621358997505 usec\nrounds: 2649"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2687.407248834299,
            "unit": "iter/sec",
            "range": "stddev: 0.00002824365910615423",
            "extra": "mean: 372.10586539638314 usec\nrounds: 1679"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5030.083907837919,
            "unit": "iter/sec",
            "range": "stddev: 0.000021610678477670163",
            "extra": "mean: 198.8038407156174 usec\nrounds: 4696"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1005.602737511783,
            "unit": "iter/sec",
            "range": "stddev: 0.000019067793932123937",
            "extra": "mean: 994.4284782620559 usec\nrounds: 1012"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 352983.8224822486,
            "unit": "iter/sec",
            "range": "stddev: 7.42028270426386e-7",
            "extra": "mean: 2.8329910219902206 usec\nrounds: 186568"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 124606.28356680558,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013758279968934533",
            "extra": "mean: 8.025277468963807 usec\nrounds: 80946"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 278633.40894770925,
            "unit": "iter/sec",
            "range": "stddev: 8.491528032500283e-7",
            "extra": "mean: 3.588945072224518 usec\nrounds: 162049"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.20548039193781,
            "unit": "iter/sec",
            "range": "stddev: 0.0011552363987363244",
            "extra": "mean: 23.693605444448924 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.5149422170695495,
            "unit": "iter/sec",
            "range": "stddev: 0.0083688463145453",
            "extra": "mean: 221.4867770000069 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1403.082394381035,
            "unit": "iter/sec",
            "range": "stddev: 0.000407519670892146",
            "extra": "mean: 712.7165190046779 usec\nrounds: 1526"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 388.57594511591265,
            "unit": "iter/sec",
            "range": "stddev: 0.0005091714759948744",
            "extra": "mean: 2.5734994987960436 msec\nrounds: 415"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 939656.1820690325,
            "unit": "iter/sec",
            "range": "stddev: 1.0911030236904188e-7",
            "extra": "mean: 1.064219039987686 usec\nrounds: 99811"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 484449.6162719209,
            "unit": "iter/sec",
            "range": "stddev: 1.3786292472992978e-7",
            "extra": "mean: 2.0641981465389403 usec\nrounds: 49097"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59535.22041738664,
            "unit": "iter/sec",
            "range": "stddev: 0.000001443873344969356",
            "extra": "mean: 16.796780006679214 usec\nrounds: 59770"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3985.6000496057736,
            "unit": "iter/sec",
            "range": "stddev: 0.000005902805376875882",
            "extra": "mean: 250.9032485833376 usec\nrounds: 3882"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 395.86103891119024,
            "unit": "iter/sec",
            "range": "stddev: 0.00002186429616175773",
            "extra": "mean: 2.5261389773302385 msec\nrounds: 397"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 17750.53850193648,
            "unit": "iter/sec",
            "range": "stddev: 0.00001267952746791241",
            "extra": "mean: 56.33631903003426 usec\nrounds: 17362"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18385.47663443411,
            "unit": "iter/sec",
            "range": "stddev: 0.000003366472144913417",
            "extra": "mean: 54.39075743769964 usec\nrounds: 18622"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18390.358927349822,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026845085014836902",
            "extra": "mean: 54.37631771899881 usec\nrounds: 18488"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1506.5696417281627,
            "unit": "iter/sec",
            "range": "stddev: 0.0000058219326802560285",
            "extra": "mean: 663.7595583387141 usec\nrounds: 1517"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 150.96073843313036,
            "unit": "iter/sec",
            "range": "stddev: 0.000024437957952081632",
            "extra": "mean: 6.62423892714966 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39501.37037011569,
            "unit": "iter/sec",
            "range": "stddev: 0.000004765945629219304",
            "extra": "mean: 25.31557742504393 usec\nrounds: 40168"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3928.730003500219,
            "unit": "iter/sec",
            "range": "stddev: 0.00001024754489187615",
            "extra": "mean: 254.5351803532111 usec\nrounds: 3970"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8010.718879896724,
            "unit": "iter/sec",
            "range": "stddev: 0.0000062985895442383274",
            "extra": "mean: 124.8327416044454 usec\nrounds: 8278"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1261.8495045625205,
            "unit": "iter/sec",
            "range": "stddev: 0.000020067107645487565",
            "extra": "mean: 792.487532296252 usec\nrounds: 1285"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 430.8414765769373,
            "unit": "iter/sec",
            "range": "stddev: 0.00009084926940441877",
            "extra": "mean: 2.321039301844992 msec\nrounds: 434"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11422.632296272655,
            "unit": "iter/sec",
            "range": "stddev: 0.000005719627305964848",
            "extra": "mean: 87.54549512429918 usec\nrounds: 10870"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1036.549587646397,
            "unit": "iter/sec",
            "range": "stddev: 0.000020767053531356796",
            "extra": "mean: 964.7391807570086 usec\nrounds: 1029"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1041.5174326851618,
            "unit": "iter/sec",
            "range": "stddev: 0.00002104902697822592",
            "extra": "mean: 960.13755374394 usec\nrounds: 1042"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1164.4906465118231,
            "unit": "iter/sec",
            "range": "stddev: 0.000028662458264928586",
            "extra": "mean: 858.744553247768 usec\nrounds: 1155"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1605.7530156724083,
            "unit": "iter/sec",
            "range": "stddev: 0.00001964528114861812",
            "extra": "mean: 622.760779671493 usec\nrounds: 1584"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 955.2681473486572,
            "unit": "iter/sec",
            "range": "stddev: 0.000028612584141104875",
            "extra": "mean: 1.0468264882227005 msec\nrounds: 934"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1020.5283789535614,
            "unit": "iter/sec",
            "range": "stddev: 0.00003289616749529907",
            "extra": "mean: 979.8845584533269 usec\nrounds: 1035"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1005.0649620586366,
            "unit": "iter/sec",
            "range": "stddev: 0.00003585431733152674",
            "extra": "mean: 994.9605625010922 usec\nrounds: 992"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 739.5578438382036,
            "unit": "iter/sec",
            "range": "stddev: 0.000050492195011077026",
            "extra": "mean: 1.352159277778919 msec\nrounds: 738"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1048.5754031821743,
            "unit": "iter/sec",
            "range": "stddev: 0.0001246119343189346",
            "extra": "mean: 953.6748592092093 usec\nrounds: 1037"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 958.9205309538066,
            "unit": "iter/sec",
            "range": "stddev: 0.00002232541694937711",
            "extra": "mean: 1.0428392840909695 msec\nrounds: 968"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1633.0914301024088,
            "unit": "iter/sec",
            "range": "stddev: 0.00001738799738793879",
            "extra": "mean: 612.3355873206018 usec\nrounds: 1672"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3041.1056744848147,
            "unit": "iter/sec",
            "range": "stddev: 0.000013610669450800044",
            "extra": "mean: 328.827770895994 usec\nrounds: 2955"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3005.206738717265,
            "unit": "iter/sec",
            "range": "stddev: 0.000018006294421252335",
            "extra": "mean: 332.75580914837076 usec\nrounds: 3039"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 3943.111438622203,
            "unit": "iter/sec",
            "range": "stddev: 0.000013126879244884724",
            "extra": "mean: 253.606831956395 usec\nrounds: 3993"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7611.897469255496,
            "unit": "iter/sec",
            "range": "stddev: 0.00000627143509922984",
            "extra": "mean: 131.37328820297523 usec\nrounds: 7519"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 648.8630783863756,
            "unit": "iter/sec",
            "range": "stddev: 0.00009154881878790569",
            "extra": "mean: 1.5411571921873701 msec\nrounds: 640"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4685.194050122187,
            "unit": "iter/sec",
            "range": "stddev: 0.000008201114116653117",
            "extra": "mean: 213.43833132672924 usec\nrounds: 4651"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 439.4992342476753,
            "unit": "iter/sec",
            "range": "stddev: 0.00004184905076529815",
            "extra": "mean: 2.275316819861534 msec\nrounds: 433"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 626.6982783834733,
            "unit": "iter/sec",
            "range": "stddev: 0.000030998722536504806",
            "extra": "mean: 1.5956641888014018 msec\nrounds: 625"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 9217.602322800016,
            "unit": "iter/sec",
            "range": "stddev: 0.000004844855679277102",
            "extra": "mean: 108.4880823645939 usec\nrounds: 9288"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 645.2053734499814,
            "unit": "iter/sec",
            "range": "stddev: 0.0000291406955705547",
            "extra": "mean: 1.549894097522614 msec\nrounds: 646"
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
        "date": 1771211418132,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 394.89438569804776,
            "unit": "iter/sec",
            "range": "stddev: 0.0071697945011079695",
            "extra": "mean: 2.5323226569360258 msec\nrounds: 548"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 444.66976912140717,
            "unit": "iter/sec",
            "range": "stddev: 0.00037304297556300703",
            "extra": "mean: 2.248859871845644 msec\nrounds: 515"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6198.930392199334,
            "unit": "iter/sec",
            "range": "stddev: 0.0000236135899780177",
            "extra": "mean: 161.31815276686908 usec\nrounds: 6559"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4304.289028571565,
            "unit": "iter/sec",
            "range": "stddev: 0.00005622184561399947",
            "extra": "mean: 232.32640590863463 usec\nrounds: 4671"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 396.09401501377465,
            "unit": "iter/sec",
            "range": "stddev: 0.0004306201988955774",
            "extra": "mean: 2.524653143181736 msec\nrounds: 440"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 325.57430053642753,
            "unit": "iter/sec",
            "range": "stddev: 0.0006425441990616867",
            "extra": "mean: 3.0714955030306914 msec\nrounds: 330"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 325.9255959899553,
            "unit": "iter/sec",
            "range": "stddev: 0.000957190610242731",
            "extra": "mean: 3.0681849241162973 msec\nrounds: 369"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 323.7990314706547,
            "unit": "iter/sec",
            "range": "stddev: 0.0010083847869067106",
            "extra": "mean: 3.0883353648654386 msec\nrounds: 370"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 315.33712044608626,
            "unit": "iter/sec",
            "range": "stddev: 0.0005740775523284862",
            "extra": "mean: 3.1712092714786233 msec\nrounds: 291"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 17423.35340308081,
            "unit": "iter/sec",
            "range": "stddev: 0.00001552924798760679",
            "extra": "mean: 57.394232721192424 usec\nrounds: 16711"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16115.051698612268,
            "unit": "iter/sec",
            "range": "stddev: 0.00001826415744869813",
            "extra": "mean: 62.05378789359478 usec\nrounds: 16157"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 12818.406411132637,
            "unit": "iter/sec",
            "range": "stddev: 0.00008283988270349684",
            "extra": "mean: 78.0128175005835 usec\nrounds: 13885"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6325.958186251347,
            "unit": "iter/sec",
            "range": "stddev: 0.00010393731596600045",
            "extra": "mean: 158.0788191381617 usec\nrounds: 5894"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 17901.90777572565,
            "unit": "iter/sec",
            "range": "stddev: 0.000014114666909611268",
            "extra": "mean: 55.85996825187338 usec\nrounds: 16694"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 52628.09522795097,
            "unit": "iter/sec",
            "range": "stddev: 0.000023961396551623488",
            "extra": "mean: 19.0012577059581 usec\nrounds: 44186"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 208786.37484304496,
            "unit": "iter/sec",
            "range": "stddev: 0.000007518021364473974",
            "extra": "mean: 4.7895845729959605 usec\nrounds: 195695"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 158.60897937981395,
            "unit": "iter/sec",
            "range": "stddev: 0.001008673882526335",
            "extra": "mean: 6.304813283019393 msec\nrounds: 159"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4237.178821149275,
            "unit": "iter/sec",
            "range": "stddev: 0.0000829349006595916",
            "extra": "mean: 236.00608853434326 usec\nrounds: 3637"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 241.3294642160721,
            "unit": "iter/sec",
            "range": "stddev: 0.0005274325358165766",
            "extra": "mean: 4.14371284189592 msec\nrounds: 253"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 163.89593122282312,
            "unit": "iter/sec",
            "range": "stddev: 0.0006184703761236923",
            "extra": "mean: 6.101432735633075 msec\nrounds: 174"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 175.53005377367697,
            "unit": "iter/sec",
            "range": "stddev: 0.0008476390167864365",
            "extra": "mean: 5.6970301011208555 msec\nrounds: 178"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 91.83960584067796,
            "unit": "iter/sec",
            "range": "stddev: 0.0006224846735682224",
            "extra": "mean: 10.888548473681233 msec\nrounds: 95"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 106.1928050093789,
            "unit": "iter/sec",
            "range": "stddev: 0.019743503438642895",
            "extra": "mean: 9.416833842101454 msec\nrounds: 133"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 72.27801939495816,
            "unit": "iter/sec",
            "range": "stddev: 0.0011617870713226772",
            "extra": "mean: 13.835464894736118 msec\nrounds: 76"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 5.110116403673805,
            "unit": "iter/sec",
            "range": "stddev: 0.014001085139772019",
            "extra": "mean: 195.69025849999662 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 781.144412689707,
            "unit": "iter/sec",
            "range": "stddev: 0.0001600116359018493",
            "extra": "mean: 1.2801730176328214 msec\nrounds: 794"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1635249.7450777101,
            "unit": "iter/sec",
            "range": "stddev: 7.71069892464846e-8",
            "extra": "mean: 611.5273847374782 nsec\nrounds: 161265"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 817081.2218749127,
            "unit": "iter/sec",
            "range": "stddev: 1.0314431514879493e-7",
            "extra": "mean: 1.2238685374574554 usec\nrounds: 82015"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23728.998835542272,
            "unit": "iter/sec",
            "range": "stddev: 0.000001737105031470664",
            "extra": "mean: 42.142528091078105 usec\nrounds: 23833"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1506.855541260549,
            "unit": "iter/sec",
            "range": "stddev: 0.000005587243376379091",
            "extra": "mean: 663.6336215503826 usec\nrounds: 1522"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 151.0515791573751,
            "unit": "iter/sec",
            "range": "stddev: 0.000013045648210423817",
            "extra": "mean: 6.620255184211855 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10203.5909527564,
            "unit": "iter/sec",
            "range": "stddev: 0.000002501824370533694",
            "extra": "mean: 98.00471271634618 usec\nrounds: 10286"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1438.8280521082231,
            "unit": "iter/sec",
            "range": "stddev: 0.000005926084736905804",
            "extra": "mean: 695.0100802766277 usec\nrounds: 1445"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 69561.19150508253,
            "unit": "iter/sec",
            "range": "stddev: 0.000014881647089858607",
            "extra": "mean: 14.375831959792327 usec\nrounds: 62854"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1093028.2376696032,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013756783153766968",
            "extra": "mean: 914.8894470760018 nsec\nrounds: 111770"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5430.834018072941,
            "unit": "iter/sec",
            "range": "stddev: 0.000054418016599950005",
            "extra": "mean: 184.13378068122887 usec\nrounds: 5280"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 256.73165414489296,
            "unit": "iter/sec",
            "range": "stddev: 0.0003323472901326403",
            "extra": "mean: 3.8951176602306505 msec\nrounds: 259"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 71632.44675973764,
            "unit": "iter/sec",
            "range": "stddev: 0.000012853078247206285",
            "extra": "mean: 13.960154165249996 usec\nrounds: 66059"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1983.1184763362992,
            "unit": "iter/sec",
            "range": "stddev: 0.0013817588090962371",
            "extra": "mean: 504.25630739291194 usec\nrounds: 3084"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 342735.7236396887,
            "unit": "iter/sec",
            "range": "stddev: 0.000007079082461860822",
            "extra": "mean: 2.9176999391265097 usec\nrounds: 172385"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3054.4514993358234,
            "unit": "iter/sec",
            "range": "stddev: 0.000024703444130126082",
            "extra": "mean: 327.3910226492205 usec\nrounds: 2605"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2616.6489613494655,
            "unit": "iter/sec",
            "range": "stddev: 0.000029828450009202363",
            "extra": "mean: 382.16819098434866 usec\nrounds: 1686"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5056.548534570132,
            "unit": "iter/sec",
            "range": "stddev: 0.00004915830649419615",
            "extra": "mean: 197.76335442313956 usec\nrounds: 5042"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1095.5371470460707,
            "unit": "iter/sec",
            "range": "stddev: 0.000057215979817891705",
            "extra": "mean: 912.794242254888 usec\nrounds: 710"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 351804.46390508435,
            "unit": "iter/sec",
            "range": "stddev: 7.093172374475744e-7",
            "extra": "mean: 2.842488093811671 usec\nrounds: 193051"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 126422.81359154568,
            "unit": "iter/sec",
            "range": "stddev: 0.000001345745546933104",
            "extra": "mean: 7.909964757079836 usec\nrounds: 73660"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 278181.43238263595,
            "unit": "iter/sec",
            "range": "stddev: 7.867106780166555e-7",
            "extra": "mean: 3.5947762272807244 usec\nrounds: 162312"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.94384155273556,
            "unit": "iter/sec",
            "range": "stddev: 0.0012518826181239615",
            "extra": "mean: 23.841402288884534 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.5213387337293,
            "unit": "iter/sec",
            "range": "stddev: 0.007690926450586772",
            "extra": "mean: 221.17343090000645 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1397.759635116412,
            "unit": "iter/sec",
            "range": "stddev: 0.00047515243798212643",
            "extra": "mean: 715.4305896927086 usec\nrounds: 1533"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 390.4455480751214,
            "unit": "iter/sec",
            "range": "stddev: 0.0006223537078821865",
            "extra": "mean: 2.561176596659775 msec\nrounds: 419"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 963829.342641017,
            "unit": "iter/sec",
            "range": "stddev: 1.0065286447658819e-7",
            "extra": "mean: 1.037528072407373 usec\nrounds: 94787"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 479117.6670218877,
            "unit": "iter/sec",
            "range": "stddev: 1.4003496574060175e-7",
            "extra": "mean: 2.0871699560064787 usec\nrounds: 48196"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59486.21646621649,
            "unit": "iter/sec",
            "range": "stddev: 0.000001224742190706732",
            "extra": "mean: 16.8106169698643 usec\nrounds: 59588"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3968.1551319076966,
            "unit": "iter/sec",
            "range": "stddev: 0.000006225845494950303",
            "extra": "mean: 252.00627665966488 usec\nrounds: 3976"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 395.99202147681336,
            "unit": "iter/sec",
            "range": "stddev: 0.000026347445360594337",
            "extra": "mean: 2.525303404524662 msec\nrounds: 398"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 17805.620971876495,
            "unit": "iter/sec",
            "range": "stddev: 0.0000035414030367613",
            "extra": "mean: 56.162040154593505 usec\nrounds: 18379"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 17724.705017494038,
            "unit": "iter/sec",
            "range": "stddev: 0.000003235337097141724",
            "extra": "mean: 56.418428346932366 usec\nrounds: 18471"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 17874.36095563873,
            "unit": "iter/sec",
            "range": "stddev: 0.000002983347150075842",
            "extra": "mean: 55.946056056596255 usec\nrounds: 18410"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1506.8103899036498,
            "unit": "iter/sec",
            "range": "stddev: 0.0000056302112716001565",
            "extra": "mean: 663.6535072365298 usec\nrounds: 1520"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 151.07995074753939,
            "unit": "iter/sec",
            "range": "stddev: 0.000012654030978917527",
            "extra": "mean: 6.619011953949071 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39995.947767867474,
            "unit": "iter/sec",
            "range": "stddev: 0.0000019467649161590473",
            "extra": "mean: 25.002532901680468 usec\nrounds: 40150"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3875.3076370954727,
            "unit": "iter/sec",
            "range": "stddev: 0.00001107152440370517",
            "extra": "mean: 258.04402995719227 usec\nrounds: 3939"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8093.150505604149,
            "unit": "iter/sec",
            "range": "stddev: 0.000005020565275465227",
            "extra": "mean: 123.56127558823282 usec\nrounds: 8197"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1243.2101543592335,
            "unit": "iter/sec",
            "range": "stddev: 0.00009640183201169375",
            "extra": "mean: 804.3692343515428 usec\nrounds: 1310"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 432.7384108330071,
            "unit": "iter/sec",
            "range": "stddev: 0.00002371291349526669",
            "extra": "mean: 2.3108648896570867 msec\nrounds: 435"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11157.25593873162,
            "unit": "iter/sec",
            "range": "stddev: 0.000006670970895155957",
            "extra": "mean: 89.62777276880162 usec\nrounds: 10870"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1012.2398990572932,
            "unit": "iter/sec",
            "range": "stddev: 0.000017417709554510687",
            "extra": "mean: 987.9081045227596 usec\nrounds: 995"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1015.8328937021791,
            "unit": "iter/sec",
            "range": "stddev: 0.000055035439166312946",
            "extra": "mean: 984.4138796840133 usec\nrounds: 1014"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1168.6531064274363,
            "unit": "iter/sec",
            "range": "stddev: 0.000016477547476950884",
            "extra": "mean: 855.6859126973892 usec\nrounds: 1134"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1567.684307496349,
            "unit": "iter/sec",
            "range": "stddev: 0.000014915839283822811",
            "extra": "mean: 637.8835300055008 usec\nrounds: 1583"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 967.9029158062203,
            "unit": "iter/sec",
            "range": "stddev: 0.000021668660788820034",
            "extra": "mean: 1.0331614707111862 msec\nrounds: 956"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1037.9262604992164,
            "unit": "iter/sec",
            "range": "stddev: 0.000027769370177115443",
            "extra": "mean: 963.4595809523358 usec\nrounds: 1050"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1033.3046993775663,
            "unit": "iter/sec",
            "range": "stddev: 0.000027594893392828168",
            "extra": "mean: 967.7687526267633 usec\nrounds: 1047"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 755.8308762665303,
            "unit": "iter/sec",
            "range": "stddev: 0.0000177297169161201",
            "extra": "mean: 1.3230472998662837 msec\nrounds: 747"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1094.131817621326,
            "unit": "iter/sec",
            "range": "stddev: 0.000016284955456164757",
            "extra": "mean: 913.9666573028 usec\nrounds: 1068"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 943.601956336402,
            "unit": "iter/sec",
            "range": "stddev: 0.0000224764581260384",
            "extra": "mean: 1.0597688922589428 msec\nrounds: 956"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1638.6773298950236,
            "unit": "iter/sec",
            "range": "stddev: 0.000022050637503839452",
            "extra": "mean: 610.2482665480346 usec\nrounds: 1692"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3107.3890526152877,
            "unit": "iter/sec",
            "range": "stddev: 0.00001393136152028197",
            "extra": "mean: 321.81358145622767 usec\nrounds: 3063"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3052.6561948871586,
            "unit": "iter/sec",
            "range": "stddev: 0.000013733890626204963",
            "extra": "mean: 327.5835653143262 usec\nrounds: 3154"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4012.640747932151,
            "unit": "iter/sec",
            "range": "stddev: 0.000014196206120539742",
            "extra": "mean: 249.2124420845135 usec\nrounds: 4049"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7567.442199608615,
            "unit": "iter/sec",
            "range": "stddev: 0.000010237613750895345",
            "extra": "mean: 132.14504632116248 usec\nrounds: 7448"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 642.1118519632287,
            "unit": "iter/sec",
            "range": "stddev: 0.00001837409987195254",
            "extra": "mean: 1.5573610687025075 msec\nrounds: 655"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4517.326799008497,
            "unit": "iter/sec",
            "range": "stddev: 0.000014915572558055123",
            "extra": "mean: 221.3698597629662 usec\nrounds: 4471"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 431.6558729357655,
            "unit": "iter/sec",
            "range": "stddev: 0.00002702571702036547",
            "extra": "mean: 2.3166602441867146 msec\nrounds: 430"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 618.3520496662735,
            "unit": "iter/sec",
            "range": "stddev: 0.00002745796310222488",
            "extra": "mean: 1.6172017227721702 msec\nrounds: 606"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 9279.718167623243,
            "unit": "iter/sec",
            "range": "stddev: 0.000004590565439648981",
            "extra": "mean: 107.76189340415321 usec\nrounds: 9278"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 645.0929146484754,
            "unit": "iter/sec",
            "range": "stddev: 0.00003615641419305384",
            "extra": "mean: 1.550164289968866 msec\nrounds: 638"
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
        "date": 1771212731773,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 402.7238234202208,
            "unit": "iter/sec",
            "range": "stddev: 0.008113051360139017",
            "extra": "mean: 2.4830912447822917 msec\nrounds: 527"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 436.84599981975924,
            "unit": "iter/sec",
            "range": "stddev: 0.0008422019651148651",
            "extra": "mean: 2.2891362182842365 msec\nrounds: 536"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6199.965109768327,
            "unit": "iter/sec",
            "range": "stddev: 0.000018498392826610062",
            "extra": "mean: 161.29123024006287 usec\nrounds: 5238"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4640.79130896034,
            "unit": "iter/sec",
            "range": "stddev: 0.00004206542295757289",
            "extra": "mean: 215.48049317995006 usec\nrounds: 4692"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 385.60791508215027,
            "unit": "iter/sec",
            "range": "stddev: 0.0003691257526953242",
            "extra": "mean: 2.5933077638900617 msec\nrounds: 432"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 322.9654565837355,
            "unit": "iter/sec",
            "range": "stddev: 0.001153456498671653",
            "extra": "mean: 3.096306368420331 msec\nrounds: 380"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 329.2406369257409,
            "unit": "iter/sec",
            "range": "stddev: 0.0010172820249497235",
            "extra": "mean: 3.0372921439389224 msec\nrounds: 396"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 319.4110964725707,
            "unit": "iter/sec",
            "range": "stddev: 0.0014379364883733172",
            "extra": "mean: 3.130761614244277 msec\nrounds: 337"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 304.2469736541417,
            "unit": "iter/sec",
            "range": "stddev: 0.000544756408611779",
            "extra": "mean: 3.2868034412620593 msec\nrounds: 349"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 15816.35095932602,
            "unit": "iter/sec",
            "range": "stddev: 0.00003879261954567908",
            "extra": "mean: 63.225708797916866 usec\nrounds: 16686"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16250.331414703323,
            "unit": "iter/sec",
            "range": "stddev: 0.000017995795041540635",
            "extra": "mean: 61.5372065024593 usec\nrounds: 14794"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 12681.576828225509,
            "unit": "iter/sec",
            "range": "stddev: 0.00007668280369755306",
            "extra": "mean: 78.85454731262521 usec\nrounds: 14288"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6393.441503930706,
            "unit": "iter/sec",
            "range": "stddev: 0.00009025488743033993",
            "extra": "mean: 156.4102837861576 usec\nrounds: 5187"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15384.380220201652,
            "unit": "iter/sec",
            "range": "stddev: 0.000018691536065724428",
            "extra": "mean: 65.00099358483564 usec\nrounds: 17770"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53431.47994408264,
            "unit": "iter/sec",
            "range": "stddev: 0.000016509555735603993",
            "extra": "mean: 18.71555871270129 usec\nrounds: 28716"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 166367.1672611002,
            "unit": "iter/sec",
            "range": "stddev: 0.0004945279179285313",
            "extra": "mean: 6.010801388657286 usec\nrounds: 180181"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 159.19850345979,
            "unit": "iter/sec",
            "range": "stddev: 0.0015814154670899258",
            "extra": "mean: 6.281466083332735 msec\nrounds: 168"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4265.1036487706215,
            "unit": "iter/sec",
            "range": "stddev: 0.00008551447536717128",
            "extra": "mean: 234.4608906018594 usec\nrounds: 3958"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 248.12636859936126,
            "unit": "iter/sec",
            "range": "stddev: 0.00030571938400329626",
            "extra": "mean: 4.030204470588356 msec\nrounds: 255"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 169.64110692723884,
            "unit": "iter/sec",
            "range": "stddev: 0.0004318936732408223",
            "extra": "mean: 5.894797659089271 msec\nrounds: 176"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 183.54255624967325,
            "unit": "iter/sec",
            "range": "stddev: 0.0005130896222478779",
            "extra": "mean: 5.4483277362645985 msec\nrounds: 182"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 93.10580161562211,
            "unit": "iter/sec",
            "range": "stddev: 0.0005729679197575556",
            "extra": "mean: 10.740469258064055 msec\nrounds: 93"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 105.92293277524007,
            "unit": "iter/sec",
            "range": "stddev: 0.02078328349563392",
            "extra": "mean: 9.440826210145818 msec\nrounds: 138"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 70.50932301231227,
            "unit": "iter/sec",
            "range": "stddev: 0.0014726658460742883",
            "extra": "mean: 14.182521647887345 msec\nrounds: 71"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 5.261416089094611,
            "unit": "iter/sec",
            "range": "stddev: 0.01632833024566924",
            "extra": "mean: 190.06290000000376 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 770.6692496370651,
            "unit": "iter/sec",
            "range": "stddev: 0.00017815270960799443",
            "extra": "mean: 1.2975735057171862 msec\nrounds: 787"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1647437.856092722,
            "unit": "iter/sec",
            "range": "stddev: 7.774555706567758e-8",
            "extra": "mean: 607.0031693770411 nsec\nrounds: 166058"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 809259.1402204467,
            "unit": "iter/sec",
            "range": "stddev: 1.0540629187196444e-7",
            "extra": "mean: 1.2356981222697025 usec\nrounds: 81215"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23615.835399507694,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018609759043214296",
            "extra": "mean: 42.344468577251625 usec\nrounds: 23868"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1505.893207616366,
            "unit": "iter/sec",
            "range": "stddev: 0.000005701242713169561",
            "extra": "mean: 664.0577133506503 usec\nrounds: 1528"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 150.86850708261036,
            "unit": "iter/sec",
            "range": "stddev: 0.000011401882116047912",
            "extra": "mean: 6.62828856291681 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10189.678850936116,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027068479561386703",
            "extra": "mean: 98.1385198325589 usec\nrounds: 10286"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1425.9954773620557,
            "unit": "iter/sec",
            "range": "stddev: 0.000006249032921745688",
            "extra": "mean: 701.2644961889337 usec\nrounds: 1443"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 65741.8991556875,
            "unit": "iter/sec",
            "range": "stddev: 0.00001694225924989367",
            "extra": "mean: 15.210999573222512 usec\nrounds: 63252"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1081256.1174929307,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018809549006334662",
            "extra": "mean: 924.8502587145251 nsec\nrounds: 108614"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5426.862128656562,
            "unit": "iter/sec",
            "range": "stddev: 0.00006156925034930118",
            "extra": "mean: 184.2685471443059 usec\nrounds: 5112"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 256.57326317350885,
            "unit": "iter/sec",
            "range": "stddev: 0.0002580502584395539",
            "extra": "mean: 3.897522242306851 msec\nrounds: 260"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 70406.00092971716,
            "unit": "iter/sec",
            "range": "stddev: 0.00002681661299217282",
            "extra": "mean: 14.203334755488394 usec\nrounds: 64692"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1864.4426965030066,
            "unit": "iter/sec",
            "range": "stddev: 0.0020422026601570153",
            "extra": "mean: 536.3533037918644 usec\nrounds: 2584"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 343440.3737806136,
            "unit": "iter/sec",
            "range": "stddev: 0.000010147839464540844",
            "extra": "mean: 2.9117135792508493 usec\nrounds: 156937"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3089.3511604006417,
            "unit": "iter/sec",
            "range": "stddev: 0.00002573573570431254",
            "extra": "mean: 323.6925645805559 usec\nrounds: 2733"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2535.0767103332123,
            "unit": "iter/sec",
            "range": "stddev: 0.00002790218316319606",
            "extra": "mean: 394.4653808399191 usec\nrounds: 1691"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5105.014628996281,
            "unit": "iter/sec",
            "range": "stddev: 0.00005432741047877929",
            "extra": "mean: 195.88582456160643 usec\nrounds: 4617"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1112.0803109070894,
            "unit": "iter/sec",
            "range": "stddev: 0.00002547520160029753",
            "extra": "mean: 899.2156323533244 usec\nrounds: 612"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 365748.25780045404,
            "unit": "iter/sec",
            "range": "stddev: 5.125422338466806e-7",
            "extra": "mean: 2.7341210208732774 usec\nrounds: 111770"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 126752.50410039787,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013789792564944766",
            "extra": "mean: 7.889390486581014 usec\nrounds: 79593"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 282359.1785124004,
            "unit": "iter/sec",
            "range": "stddev: 8.325279704721541e-7",
            "extra": "mean: 3.541588430978109 usec\nrounds: 167758"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.80883954292311,
            "unit": "iter/sec",
            "range": "stddev: 0.0007856987153915348",
            "extra": "mean: 23.35966147826387 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.501026081136789,
            "unit": "iter/sec",
            "range": "stddev: 0.006044665421944948",
            "extra": "mean: 222.17156309999382 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1457.814917610228,
            "unit": "iter/sec",
            "range": "stddev: 0.00020445101429919388",
            "extra": "mean: 685.95813358755 usec\nrounds: 1572"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 386.89396546651164,
            "unit": "iter/sec",
            "range": "stddev: 0.0006068003092334041",
            "extra": "mean: 2.5846875093908825 msec\nrounds: 426"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 990153.9185880446,
            "unit": "iter/sec",
            "range": "stddev: 1.0766395671555942e-7",
            "extra": "mean: 1.0099439907544838 usec\nrounds: 103008"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 485439.40124782955,
            "unit": "iter/sec",
            "range": "stddev: 1.3359336428470346e-7",
            "extra": "mean: 2.0599893569196985 usec\nrounds: 48905"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59682.09083089128,
            "unit": "iter/sec",
            "range": "stddev: 0.000001236402486866095",
            "extra": "mean: 16.75544516081871 usec\nrounds: 60021"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3911.58014793439,
            "unit": "iter/sec",
            "range": "stddev: 0.000005841384209069864",
            "extra": "mean: 255.65115942417174 usec\nrounds: 3889"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 399.55989889726493,
            "unit": "iter/sec",
            "range": "stddev: 0.000016542518831465287",
            "extra": "mean: 2.5027536616158783 msec\nrounds: 396"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18390.40245051969,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026375916567296615",
            "extra": "mean: 54.37618903069418 usec\nrounds: 18288"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18379.426037531943,
            "unit": "iter/sec",
            "range": "stddev: 0.0000031562242236538966",
            "extra": "mean: 54.4086631409456 usec\nrounds: 14276"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18415.66475068043,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025848027746736537",
            "extra": "mean: 54.30159668621528 usec\nrounds: 18348"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1506.5587431088873,
            "unit": "iter/sec",
            "range": "stddev: 0.000005139856901094306",
            "extra": "mean: 663.764360051724 usec\nrounds: 1522"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 150.91054613258007,
            "unit": "iter/sec",
            "range": "stddev: 0.000009153794980909487",
            "extra": "mean: 6.626442125002092 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 38660.37165498802,
            "unit": "iter/sec",
            "range": "stddev: 0.0000024403194852300656",
            "extra": "mean: 25.86628004831864 usec\nrounds: 38854"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3821.2602408546936,
            "unit": "iter/sec",
            "range": "stddev: 0.000015292924991126438",
            "extra": "mean: 261.6937703715076 usec\nrounds: 3976"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7984.781426402783,
            "unit": "iter/sec",
            "range": "stddev: 0.000005225139544847909",
            "extra": "mean: 125.2382434280996 usec\nrounds: 8027"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1245.4374597888204,
            "unit": "iter/sec",
            "range": "stddev: 0.000018446835973257313",
            "extra": "mean: 802.9307229682672 usec\nrounds: 1267"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 432.1236561715516,
            "unit": "iter/sec",
            "range": "stddev: 0.00003793998159012531",
            "extra": "mean: 2.314152409196046 msec\nrounds: 435"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11651.527525289963,
            "unit": "iter/sec",
            "range": "stddev: 0.000004513287534855847",
            "extra": "mean: 85.82565657845912 usec\nrounds: 11234"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1042.956529030697,
            "unit": "iter/sec",
            "range": "stddev: 0.000021911650358018938",
            "extra": "mean: 958.8127329998883 usec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1022.8491788433244,
            "unit": "iter/sec",
            "range": "stddev: 0.000017251388057992046",
            "extra": "mean: 977.661243401336 usec\nrounds: 1023"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1167.2183171298352,
            "unit": "iter/sec",
            "range": "stddev: 0.000021217137498506494",
            "extra": "mean: 856.7377544750827 usec\nrounds: 1173"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1600.522931127729,
            "unit": "iter/sec",
            "range": "stddev: 0.000016487043145383753",
            "extra": "mean: 624.7957967683723 usec\nrounds: 1609"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 969.9757832859949,
            "unit": "iter/sec",
            "range": "stddev: 0.00007545442549765179",
            "extra": "mean: 1.0309535735132396 msec\nrounds: 959"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1008.6349168210093,
            "unit": "iter/sec",
            "range": "stddev: 0.0000917511396473581",
            "extra": "mean: 991.4390066445204 usec\nrounds: 903"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1004.0801567980574,
            "unit": "iter/sec",
            "range": "stddev: 0.00003751456092294203",
            "extra": "mean: 995.9364232323157 usec\nrounds: 990"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 739.5864508462847,
            "unit": "iter/sec",
            "range": "stddev: 0.000027146251882157547",
            "extra": "mean: 1.3521069766160974 msec\nrounds: 727"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1065.4266702783962,
            "unit": "iter/sec",
            "range": "stddev: 0.000020167066198297822",
            "extra": "mean: 938.5911089861303 usec\nrounds: 1046"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 929.616526187205,
            "unit": "iter/sec",
            "range": "stddev: 0.000019510453503809658",
            "extra": "mean: 1.0757123736832337 msec\nrounds: 950"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1609.2660507693372,
            "unit": "iter/sec",
            "range": "stddev: 0.00002621414928967147",
            "extra": "mean: 621.4012900613499 usec\nrounds: 1610"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3014.9860949163944,
            "unit": "iter/sec",
            "range": "stddev: 0.000013805911151017683",
            "extra": "mean: 331.67648822198964 usec\nrounds: 3099"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3004.6197862334416,
            "unit": "iter/sec",
            "range": "stddev: 0.000012745729261148001",
            "extra": "mean: 332.82081299663844 usec\nrounds: 3016"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4007.805705903768,
            "unit": "iter/sec",
            "range": "stddev: 0.00002398229569379313",
            "extra": "mean: 249.51309354316567 usec\nrounds: 4073"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7533.819798429706,
            "unit": "iter/sec",
            "range": "stddev: 0.000008092685331039982",
            "extra": "mean: 132.73479148100046 usec\nrounds: 7630"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 620.5676085233183,
            "unit": "iter/sec",
            "range": "stddev: 0.000026465339131956317",
            "extra": "mean: 1.6114279673403615 msec\nrounds: 643"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4637.026968047508,
            "unit": "iter/sec",
            "range": "stddev: 0.00001018347237121231",
            "extra": "mean: 215.6554203567778 usec\nrounds: 4539"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 428.91299242589207,
            "unit": "iter/sec",
            "range": "stddev: 0.000035488506075569",
            "extra": "mean: 2.331475188811821 msec\nrounds: 429"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 606.8409180664491,
            "unit": "iter/sec",
            "range": "stddev: 0.000025701702879243673",
            "extra": "mean: 1.6478783322427506 msec\nrounds: 611"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 9071.487198337603,
            "unit": "iter/sec",
            "range": "stddev: 0.000004498406041982391",
            "extra": "mean: 110.23550804142181 usec\nrounds: 9141"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 626.8589424579249,
            "unit": "iter/sec",
            "range": "stddev: 0.00004851456751736524",
            "extra": "mean: 1.595255219745263 msec\nrounds: 628"
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
        "date": 1771212948662,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 377.52442068013835,
            "unit": "iter/sec",
            "range": "stddev: 0.00825215898535255",
            "extra": "mean: 2.6488352679236633 msec\nrounds: 530"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 413.41637759076684,
            "unit": "iter/sec",
            "range": "stddev: 0.0008299448117828047",
            "extra": "mean: 2.418868855238922 msec\nrounds: 525"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6824.185020154762,
            "unit": "iter/sec",
            "range": "stddev: 0.000012436050972516626",
            "extra": "mean: 146.53764472190724 usec\nrounds: 6167"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4315.9667046959485,
            "unit": "iter/sec",
            "range": "stddev: 0.00003612252750895395",
            "extra": "mean: 231.69780223558237 usec\nrounds: 4920"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 377.23693046703505,
            "unit": "iter/sec",
            "range": "stddev: 0.0005420102305998749",
            "extra": "mean: 2.650853930875639 msec\nrounds: 434"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 313.37885488463274,
            "unit": "iter/sec",
            "range": "stddev: 0.0011643704992987537",
            "extra": "mean: 3.1910257645434945 msec\nrounds: 361"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 320.4773727300448,
            "unit": "iter/sec",
            "range": "stddev: 0.0008357580217227022",
            "extra": "mean: 3.120345101063822 msec\nrounds: 376"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 315.4038553390011,
            "unit": "iter/sec",
            "range": "stddev: 0.0007529254850586278",
            "extra": "mean: 3.170538289473932 msec\nrounds: 342"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 294.7463885239888,
            "unit": "iter/sec",
            "range": "stddev: 0.0009128787891340874",
            "extra": "mean: 3.3927472530120997 msec\nrounds: 332"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 15772.477346271511,
            "unit": "iter/sec",
            "range": "stddev: 0.00002037227983491485",
            "extra": "mean: 63.40158099744503 usec\nrounds: 17124"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14603.02959307749,
            "unit": "iter/sec",
            "range": "stddev: 0.00002084094075109358",
            "extra": "mean: 68.47894086813645 usec\nrounds: 16979"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 13823.564884717243,
            "unit": "iter/sec",
            "range": "stddev: 0.00007279407624565849",
            "extra": "mean: 72.34023989756494 usec\nrounds: 14056"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6438.6495543904975,
            "unit": "iter/sec",
            "range": "stddev: 0.00008948944877532369",
            "extra": "mean: 155.31207150699836 usec\nrounds: 5454"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16969.643650164046,
            "unit": "iter/sec",
            "range": "stddev: 0.00001837250939816167",
            "extra": "mean: 58.92875658531185 usec\nrounds: 10402"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54222.15234346552,
            "unit": "iter/sec",
            "range": "stddev: 0.000015220450915155609",
            "extra": "mean: 18.44264671873567 usec\nrounds: 43931"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 169924.3840368306,
            "unit": "iter/sec",
            "range": "stddev: 0.00046532058859490553",
            "extra": "mean: 5.884970574813166 usec\nrounds: 184468"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 163.6465745193554,
            "unit": "iter/sec",
            "range": "stddev: 0.0007464710399600931",
            "extra": "mean: 6.110729802546063 msec\nrounds: 157"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4173.782864942915,
            "unit": "iter/sec",
            "range": "stddev: 0.00007167716012632423",
            "extra": "mean: 239.5908058369196 usec\nrounds: 4146"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 239.69553789735622,
            "unit": "iter/sec",
            "range": "stddev: 0.0003784397395343224",
            "extra": "mean: 4.17195918110176 msec\nrounds: 254"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 167.2498290491945,
            "unit": "iter/sec",
            "range": "stddev: 0.0003688122143682404",
            "extra": "mean: 5.979079355027994 msec\nrounds: 169"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 182.1994088890273,
            "unit": "iter/sec",
            "range": "stddev: 0.00039592952893226354",
            "extra": "mean: 5.488492010471191 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 89.79710864516113,
            "unit": "iter/sec",
            "range": "stddev: 0.0006749597418798927",
            "extra": "mean: 11.136216021738099 msec\nrounds: 92"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 122.37594244031926,
            "unit": "iter/sec",
            "range": "stddev: 0.0006766889227939901",
            "extra": "mean: 8.171540746153465 msec\nrounds: 130"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 53.0088473702575,
            "unit": "iter/sec",
            "range": "stddev: 0.027115833497334742",
            "extra": "mean: 18.864775402777113 msec\nrounds: 72"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 5.129341200998693,
            "unit": "iter/sec",
            "range": "stddev: 0.01485638706793808",
            "extra": "mean: 194.95681039999795 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 781.1622344403738,
            "unit": "iter/sec",
            "range": "stddev: 0.00015417670786185904",
            "extra": "mean: 1.2801438112486352 msec\nrounds: 800"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1665398.2353155513,
            "unit": "iter/sec",
            "range": "stddev: 7.113631907323063e-8",
            "extra": "mean: 600.456983077399 nsec\nrounds: 167197"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 816855.3756508898,
            "unit": "iter/sec",
            "range": "stddev: 1.7016386718236543e-7",
            "extra": "mean: 1.2242069157017865 usec\nrounds: 82291"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23715.59505117588,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017048458468283472",
            "extra": "mean: 42.16634656824339 usec\nrounds: 23851"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1505.9895721855355,
            "unit": "iter/sec",
            "range": "stddev: 0.00000703770748972155",
            "extra": "mean: 664.0152219306347 usec\nrounds: 1523"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 150.97260845162046,
            "unit": "iter/sec",
            "range": "stddev: 0.000015584969805165243",
            "extra": "mean: 6.623718105264456 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10196.08860192071,
            "unit": "iter/sec",
            "range": "stddev: 0.000002679457050829939",
            "extra": "mean: 98.07682524567537 usec\nrounds: 10283"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1435.5144719589034,
            "unit": "iter/sec",
            "range": "stddev: 0.00000648404418762751",
            "extra": "mean: 696.6143633755219 usec\nrounds: 1398"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 69831.80203508954,
            "unit": "iter/sec",
            "range": "stddev: 0.00002614083492346835",
            "extra": "mean: 14.320123079417506 usec\nrounds: 63658"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1143625.1378718147,
            "unit": "iter/sec",
            "range": "stddev: 0.0000011966506863183876",
            "extra": "mean: 874.412398682414 nsec\nrounds: 70443"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5358.917937653533,
            "unit": "iter/sec",
            "range": "stddev: 0.000059223957318346244",
            "extra": "mean: 186.60483546009704 usec\nrounds: 5172"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 256.11040256559767,
            "unit": "iter/sec",
            "range": "stddev: 0.0003056534741080769",
            "extra": "mean: 3.9045661167311216 msec\nrounds: 257"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 72187.73861241041,
            "unit": "iter/sec",
            "range": "stddev: 0.00001327716385598207",
            "extra": "mean: 13.852768063135883 usec\nrounds: 68786"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1985.7918043078769,
            "unit": "iter/sec",
            "range": "stddev: 0.0015222120325168685",
            "extra": "mean: 503.57746357430335 usec\nrounds: 2759"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 349860.53145219845,
            "unit": "iter/sec",
            "range": "stddev: 0.000004772575239405672",
            "extra": "mean: 2.858281829760012 usec\nrounds: 161005"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3057.1522452790737,
            "unit": "iter/sec",
            "range": "stddev: 0.000025866418985491443",
            "extra": "mean: 327.10179924608707 usec\nrounds: 2650"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2523.796013610112,
            "unit": "iter/sec",
            "range": "stddev: 0.00003632640995232282",
            "extra": "mean: 396.22853614447655 usec\nrounds: 1660"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5009.398267230549,
            "unit": "iter/sec",
            "range": "stddev: 0.000057480450898139354",
            "extra": "mean: 199.62477460448576 usec\nrounds: 2906"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1103.394094407487,
            "unit": "iter/sec",
            "range": "stddev: 0.00006450866276338549",
            "extra": "mean: 906.2945008211153 usec\nrounds: 609"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 355545.26582177693,
            "unit": "iter/sec",
            "range": "stddev: 6.916123936192182e-7",
            "extra": "mean: 2.812581395757543 usec\nrounds: 110534"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 125324.61174308414,
            "unit": "iter/sec",
            "range": "stddev: 0.000001533317912915269",
            "extra": "mean: 7.979278659566113 usec\nrounds: 77496"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 274947.43707526143,
            "unit": "iter/sec",
            "range": "stddev: 8.313310374262497e-7",
            "extra": "mean: 3.637058816177544 usec\nrounds: 160721"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 43.29980832551684,
            "unit": "iter/sec",
            "range": "stddev: 0.00121729446286432",
            "extra": "mean: 23.094790454550207 msec\nrounds: 44"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.553295315849185,
            "unit": "iter/sec",
            "range": "stddev: 0.007637145407751513",
            "extra": "mean: 219.62116019999485 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1412.8320098028635,
            "unit": "iter/sec",
            "range": "stddev: 0.00033443997587232106",
            "extra": "mean: 707.7982329544848 usec\nrounds: 1584"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 381.4418380152413,
            "unit": "iter/sec",
            "range": "stddev: 0.0005237592060933011",
            "extra": "mean: 2.621631662649557 msec\nrounds: 415"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 984193.8470071523,
            "unit": "iter/sec",
            "range": "stddev: 1.105250606652102e-7",
            "extra": "mean: 1.0160599998068602 usec\nrounds: 100322"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 482113.838289002,
            "unit": "iter/sec",
            "range": "stddev: 1.5938932540149223e-7",
            "extra": "mean: 2.074198914407747 usec\nrounds: 48361"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59155.4155767541,
            "unit": "iter/sec",
            "range": "stddev: 0.000001938675852938155",
            "extra": "mean: 16.904623021412146 usec\nrounds: 60526"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3969.2533174219566,
            "unit": "iter/sec",
            "range": "stddev: 0.000008624128518964636",
            "extra": "mean: 251.93655330859644 usec\nrounds: 4005"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 398.8561459855824,
            "unit": "iter/sec",
            "range": "stddev: 0.00001996828332090735",
            "extra": "mean: 2.5071695900008706 msec\nrounds: 400"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18419.185326399573,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027657954072356783",
            "extra": "mean: 54.2912176776209 usec\nrounds: 17751"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18415.82698877499,
            "unit": "iter/sec",
            "range": "stddev: 0.000002798599685333604",
            "extra": "mean: 54.30111830489778 usec\nrounds: 18427"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18435.069695121496,
            "unit": "iter/sec",
            "range": "stddev: 0.0000033591114122698025",
            "extra": "mean: 54.244438265651446 usec\nrounds: 13631"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1507.0607195822622,
            "unit": "iter/sec",
            "range": "stddev: 0.000005930507359391485",
            "extra": "mean: 663.5432713535172 usec\nrounds: 1522"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 150.99841708062587,
            "unit": "iter/sec",
            "range": "stddev: 0.000016401107728433858",
            "extra": "mean: 6.622585980262616 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39883.844451986086,
            "unit": "iter/sec",
            "range": "stddev: 0.0000020060254725305213",
            "extra": "mean: 25.072808645712268 usec\nrounds: 39973"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3933.9766509997053,
            "unit": "iter/sec",
            "range": "stddev: 0.000009694625490364661",
            "extra": "mean: 254.19571306959315 usec\nrounds: 3994"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8025.695573638393,
            "unit": "iter/sec",
            "range": "stddev: 0.000005995445488475496",
            "extra": "mean: 124.59979210831902 usec\nrounds: 8110"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1213.7274422254297,
            "unit": "iter/sec",
            "range": "stddev: 0.00011846103451084563",
            "extra": "mean: 823.9082064144897 usec\nrounds: 1216"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 431.62361444744664,
            "unit": "iter/sec",
            "range": "stddev: 0.000023550117031528338",
            "extra": "mean: 2.316833385680656 msec\nrounds: 433"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11400.125781150471,
            "unit": "iter/sec",
            "range": "stddev: 0.000005016895568357189",
            "extra": "mean: 87.71833041118276 usec\nrounds: 9467"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1031.9236363289137,
            "unit": "iter/sec",
            "range": "stddev: 0.000022851441965571175",
            "extra": "mean: 969.0639547297484 usec\nrounds: 994"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1037.017566882883,
            "unit": "iter/sec",
            "range": "stddev: 0.000022302200939592437",
            "extra": "mean: 964.3038188888619 usec\nrounds: 1027"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1178.6253224557015,
            "unit": "iter/sec",
            "range": "stddev: 0.000026897335572169905",
            "extra": "mean: 848.4460506214731 usec\nrounds: 1126"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1609.781927194133,
            "unit": "iter/sec",
            "range": "stddev: 0.00001511154603588479",
            "extra": "mean: 621.2021536004014 usec\nrounds: 1569"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 962.5472044390493,
            "unit": "iter/sec",
            "range": "stddev: 0.000029455585874897613",
            "extra": "mean: 1.0389100870983021 msec\nrounds: 930"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1008.2165392999293,
            "unit": "iter/sec",
            "range": "stddev: 0.000030196755945773186",
            "extra": "mean: 991.8504220277574 usec\nrounds: 1026"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1005.2808959617911,
            "unit": "iter/sec",
            "range": "stddev: 0.00003753713111428541",
            "extra": "mean: 994.7468454011168 usec\nrounds: 1022"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 755.2242805977398,
            "unit": "iter/sec",
            "range": "stddev: 0.000023432482954162575",
            "extra": "mean: 1.3241099706282307 msec\nrounds: 715"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1092.4487816014314,
            "unit": "iter/sec",
            "range": "stddev: 0.00002043861218953793",
            "extra": "mean: 915.3747222218419 usec\nrounds: 1026"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 942.1789797797547,
            "unit": "iter/sec",
            "range": "stddev: 0.000015306568677232602",
            "extra": "mean: 1.0613694653151375 msec\nrounds: 937"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1624.3818334958805,
            "unit": "iter/sec",
            "range": "stddev: 0.00001560369530521701",
            "extra": "mean: 615.6188030297471 usec\nrounds: 1650"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 2967.570031856435,
            "unit": "iter/sec",
            "range": "stddev: 0.000015744910183272366",
            "extra": "mean: 336.97604075561645 usec\nrounds: 2699"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 2973.1909715075562,
            "unit": "iter/sec",
            "range": "stddev: 0.00001704192376062506",
            "extra": "mean: 336.33897370976814 usec\nrounds: 3005"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 3971.2611757847258,
            "unit": "iter/sec",
            "range": "stddev: 0.000015206877918130805",
            "extra": "mean: 251.80917490333505 usec\nrounds: 4128"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7633.648070885984,
            "unit": "iter/sec",
            "range": "stddev: 0.000006051084305835582",
            "extra": "mean: 130.99896546369564 usec\nrounds: 7702"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 637.6994566363668,
            "unit": "iter/sec",
            "range": "stddev: 0.000026225158264594383",
            "extra": "mean: 1.5681368230649546 msec\nrounds: 633"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4651.705905314474,
            "unit": "iter/sec",
            "range": "stddev: 0.00001046232839307428",
            "extra": "mean: 214.9748974580533 usec\nrounds: 4525"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 422.6204207619741,
            "unit": "iter/sec",
            "range": "stddev: 0.000039849023930061665",
            "extra": "mean: 2.3661894950485944 msec\nrounds: 404"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 609.5544412976631,
            "unit": "iter/sec",
            "range": "stddev: 0.0000557159322558544",
            "extra": "mean: 1.6405425541172802 msec\nrounds: 619"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 9126.25105978185,
            "unit": "iter/sec",
            "range": "stddev: 0.00000444951155231944",
            "extra": "mean: 109.57401823042808 usec\nrounds: 8941"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 635.2503809590094,
            "unit": "iter/sec",
            "range": "stddev: 0.00003519826794817053",
            "extra": "mean: 1.574182448329026 msec\nrounds: 629"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.376817499281235,
            "unit": "iter/sec",
            "range": "stddev: 0.0004675998732402494",
            "extra": "mean: 135.55981289999863 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 549.3522086097366,
            "unit": "iter/sec",
            "range": "stddev: 0.00005404333737086286",
            "extra": "mean: 1.8203258025133497 msec\nrounds: 557"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 358.79978175695936,
            "unit": "iter/sec",
            "range": "stddev: 0.00007216274532079116",
            "extra": "mean: 2.787069699717296 msec\nrounds: 353"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 737884.0311110262,
            "unit": "iter/sec",
            "range": "stddev: 1.1732497769489947e-7",
            "extra": "mean: 1.3552265096377107 usec\nrounds: 71706"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 552.0606987344627,
            "unit": "iter/sec",
            "range": "stddev: 0.0000568712977935403",
            "extra": "mean: 1.8113950192295667 msec\nrounds: 572"
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
        "date": 1771213289713,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 395.0610223903865,
            "unit": "iter/sec",
            "range": "stddev: 0.008051894646284393",
            "extra": "mean: 2.5312545235399924 msec\nrounds: 531"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 412.91993906493263,
            "unit": "iter/sec",
            "range": "stddev: 0.0006493115037229225",
            "extra": "mean: 2.421776972709346 msec\nrounds: 513"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 5997.400163645359,
            "unit": "iter/sec",
            "range": "stddev: 0.00002265493568049642",
            "extra": "mean: 166.73891564910633 usec\nrounds: 5323"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4495.6513990476915,
            "unit": "iter/sec",
            "range": "stddev: 0.00003960564619855149",
            "extra": "mean: 222.43717566976588 usec\nrounds: 4554"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 378.4940893439133,
            "unit": "iter/sec",
            "range": "stddev: 0.0006519925887145222",
            "extra": "mean: 2.6420491842644447 msec\nrounds: 483"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 323.0917979314536,
            "unit": "iter/sec",
            "range": "stddev: 0.0007824319818430065",
            "extra": "mean: 3.0950955932720947 msec\nrounds: 327"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 313.875537936965,
            "unit": "iter/sec",
            "range": "stddev: 0.000936492139772213",
            "extra": "mean: 3.1859762202966837 msec\nrounds: 404"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 322.8372050342687,
            "unit": "iter/sec",
            "range": "stddev: 0.0009242445073887449",
            "extra": "mean: 3.09753641899437 msec\nrounds: 358"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 280.87701474732427,
            "unit": "iter/sec",
            "range": "stddev: 0.0016528535693591093",
            "extra": "mean: 3.5602770874633354 msec\nrounds: 343"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 15384.201184370859,
            "unit": "iter/sec",
            "range": "stddev: 0.000020073646219832812",
            "extra": "mean: 65.00175004314956 usec\nrounds: 17287"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16174.122448488182,
            "unit": "iter/sec",
            "range": "stddev: 0.00001777995631625337",
            "extra": "mean: 61.82715650786182 usec\nrounds: 16734"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 14498.143457723865,
            "unit": "iter/sec",
            "range": "stddev: 0.00005243059886234528",
            "extra": "mean: 68.97434853751923 usec\nrounds: 14119"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6460.606719102123,
            "unit": "iter/sec",
            "range": "stddev: 0.0000840356898684682",
            "extra": "mean: 154.78422437374073 usec\nrounds: 4234"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16846.018673119142,
            "unit": "iter/sec",
            "range": "stddev: 0.000020294286689870448",
            "extra": "mean: 59.36120690615642 usec\nrounds: 18158"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54145.52059711769,
            "unit": "iter/sec",
            "range": "stddev: 0.000019344161234318692",
            "extra": "mean: 18.46874845734206 usec\nrounds: 41973"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 217358.12676593545,
            "unit": "iter/sec",
            "range": "stddev: 0.000007616337638878944",
            "extra": "mean: 4.600702144791951 usec\nrounds: 187618"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 134.26233685672918,
            "unit": "iter/sec",
            "range": "stddev: 0.016784422687160374",
            "extra": "mean: 7.4481051306823005 msec\nrounds: 176"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4264.212156457268,
            "unit": "iter/sec",
            "range": "stddev: 0.000051238618447370395",
            "extra": "mean: 234.50990788197691 usec\nrounds: 4060"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 244.4758194932264,
            "unit": "iter/sec",
            "range": "stddev: 0.00022952719844893793",
            "extra": "mean: 4.090384079999808 msec\nrounds: 250"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 171.70223041602952,
            "unit": "iter/sec",
            "range": "stddev: 0.0003723536910231644",
            "extra": "mean: 5.8240361675968275 msec\nrounds: 179"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.33205351784744,
            "unit": "iter/sec",
            "range": "stddev: 0.00036736678817517236",
            "extra": "mean: 5.424992457446788 msec\nrounds: 188"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 90.26962221483869,
            "unit": "iter/sec",
            "range": "stddev: 0.0008657042489305386",
            "extra": "mean: 11.077923840425889 msec\nrounds: 94"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 107.10396726733885,
            "unit": "iter/sec",
            "range": "stddev: 0.018949490446737185",
            "extra": "mean: 9.33672230370264 msec\nrounds: 135"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 56.07336068507224,
            "unit": "iter/sec",
            "range": "stddev: 0.023391245318180116",
            "extra": "mean: 17.833780386667968 msec\nrounds: 75"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 3.6695716990036806,
            "unit": "iter/sec",
            "range": "stddev: 0.2552347472524232",
            "extra": "mean: 272.5113669999985 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 779.503672436307,
            "unit": "iter/sec",
            "range": "stddev: 0.00015098988432803558",
            "extra": "mean: 1.282867593008947 msec\nrounds: 801"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1672835.8588588692,
            "unit": "iter/sec",
            "range": "stddev: 8.411232062547643e-8",
            "extra": "mean: 597.7872812232478 nsec\nrounds: 165810"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 821023.7986476396,
            "unit": "iter/sec",
            "range": "stddev: 1.0979137530163304e-7",
            "extra": "mean: 1.2179914901945152 usec\nrounds: 82082"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23720.122210082525,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017955543729671089",
            "extra": "mean: 42.1582988124293 usec\nrounds: 23995"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1506.6808050917225,
            "unit": "iter/sec",
            "range": "stddev: 0.0000054162823216764836",
            "extra": "mean: 663.7105859585985 usec\nrounds: 1524"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 150.8851180108753,
            "unit": "iter/sec",
            "range": "stddev: 0.000017084712663212763",
            "extra": "mean: 6.627558855260486 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10199.43411840934,
            "unit": "iter/sec",
            "range": "stddev: 0.000002623097205890931",
            "extra": "mean: 98.04465506523178 usec\nrounds: 10286"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1426.9455991620598,
            "unit": "iter/sec",
            "range": "stddev: 0.000007473268095167757",
            "extra": "mean: 700.7975641028127 usec\nrounds: 1404"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 71286.31342113692,
            "unit": "iter/sec",
            "range": "stddev: 0.000012629818117334007",
            "extra": "mean: 14.02793821153743 usec\nrounds: 64025"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1159986.3264811086,
            "unit": "iter/sec",
            "range": "stddev: 0.0000010760327945478189",
            "extra": "mean: 862.0791272889938 nsec\nrounds: 117565"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5472.122201319584,
            "unit": "iter/sec",
            "range": "stddev: 0.00004322429550549238",
            "extra": "mean: 182.74445694192528 usec\nrounds: 5272"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 254.15219048474614,
            "unit": "iter/sec",
            "range": "stddev: 0.0003699732477653899",
            "extra": "mean: 3.934650329366406 msec\nrounds: 252"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 69183.75126869025,
            "unit": "iter/sec",
            "range": "stddev: 0.000023075817825658288",
            "extra": "mean: 14.454261031846642 usec\nrounds: 66103"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1976.491198900176,
            "unit": "iter/sec",
            "range": "stddev: 0.0015470591369463973",
            "extra": "mean: 505.94710492839675 usec\nrounds: 3307"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 366240.92498613236,
            "unit": "iter/sec",
            "range": "stddev: 0.000003083924897394965",
            "extra": "mean: 2.7304430820719032 usec\nrounds: 182482"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3150.929912676397,
            "unit": "iter/sec",
            "range": "stddev: 0.00002575850536544231",
            "extra": "mean: 317.3666275396779 usec\nrounds: 2658"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2604.8429595073994,
            "unit": "iter/sec",
            "range": "stddev: 0.000031281925452873603",
            "extra": "mean: 383.90030245397577 usec\nrounds: 1630"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5123.873321233392,
            "unit": "iter/sec",
            "range": "stddev: 0.000025943907144451118",
            "extra": "mean: 195.16485621453367 usec\nrounds: 3846"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1011.5609308845079,
            "unit": "iter/sec",
            "range": "stddev: 0.000021012414459520904",
            "extra": "mean: 988.5711967203013 usec\nrounds: 976"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 368606.4179339456,
            "unit": "iter/sec",
            "range": "stddev: 5.413836469544561e-7",
            "extra": "mean: 2.712920750552966 usec\nrounds: 112537"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 127347.39447501014,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014079613437021312",
            "extra": "mean: 7.852536002974398 usec\nrounds: 79410"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 276738.1504020789,
            "unit": "iter/sec",
            "range": "stddev: 8.245277854286201e-7",
            "extra": "mean: 3.6135241872039616 usec\nrounds: 172385"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 40.452364821354614,
            "unit": "iter/sec",
            "range": "stddev: 0.0018543003133246808",
            "extra": "mean: 24.72043363635702 msec\nrounds: 44"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.420307174412785,
            "unit": "iter/sec",
            "range": "stddev: 0.008484369221883994",
            "extra": "mean: 226.2286217999872 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1443.5290439942494,
            "unit": "iter/sec",
            "range": "stddev: 0.0003098337053937074",
            "extra": "mean: 692.7467127595831 usec\nrounds: 1591"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 391.3406281745532,
            "unit": "iter/sec",
            "range": "stddev: 0.00034362794785435647",
            "extra": "mean: 2.5553186354930695 msec\nrounds: 417"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 971405.9889408326,
            "unit": "iter/sec",
            "range": "stddev: 1.3158711741333665e-7",
            "extra": "mean: 1.0294356956665922 usec\nrounds: 96628"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 482145.30758709763,
            "unit": "iter/sec",
            "range": "stddev: 1.5301948654647688e-7",
            "extra": "mean: 2.0740635328476236 usec\nrounds: 48833"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 58983.07151903464,
            "unit": "iter/sec",
            "range": "stddev: 0.0000033432185658076734",
            "extra": "mean: 16.954017046692567 usec\nrounds: 60129"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3876.2226059831287,
            "unit": "iter/sec",
            "range": "stddev: 0.000028719429615316663",
            "extra": "mean: 257.98311955986577 usec\nrounds: 3906"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 383.94646194694207,
            "unit": "iter/sec",
            "range": "stddev: 0.0002526671882833742",
            "extra": "mean: 2.604529795454115 msec\nrounds: 396"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18172.850004838157,
            "unit": "iter/sec",
            "range": "stddev: 0.000006619379825449036",
            "extra": "mean: 55.02714212320962 usec\nrounds: 18498"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18004.92606912052,
            "unit": "iter/sec",
            "range": "stddev: 0.000006733521063776918",
            "extra": "mean: 55.540355798242196 usec\nrounds: 18488"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18020.338903919295,
            "unit": "iter/sec",
            "range": "stddev: 0.000006619140024406891",
            "extra": "mean: 55.49285201192898 usec\nrounds: 18265"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1505.8277145219017,
            "unit": "iter/sec",
            "range": "stddev: 0.000006132331578175481",
            "extra": "mean: 664.0865952699633 usec\nrounds: 1522"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 148.95060311753122,
            "unit": "iter/sec",
            "range": "stddev: 0.0004289111812108649",
            "extra": "mean: 6.713635118422034 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39037.17465681137,
            "unit": "iter/sec",
            "range": "stddev: 0.000005711950918972827",
            "extra": "mean: 25.616607984346427 usec\nrounds: 40103"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3869.4255503506874,
            "unit": "iter/sec",
            "range": "stddev: 0.000024311334034080637",
            "extra": "mean: 258.4362942218567 usec\nrounds: 3946"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7803.919446900306,
            "unit": "iter/sec",
            "range": "stddev: 0.000011179756779760637",
            "extra": "mean: 128.14073835644177 usec\nrounds: 8202"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1253.3572758825655,
            "unit": "iter/sec",
            "range": "stddev: 0.00004787533750938612",
            "extra": "mean: 797.8570988833483 usec\nrounds: 1254"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 432.6041486758568,
            "unit": "iter/sec",
            "range": "stddev: 0.0000284079873703925",
            "extra": "mean: 2.31158208505597 msec\nrounds: 435"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11608.523807528061,
            "unit": "iter/sec",
            "range": "stddev: 0.000004967823045493246",
            "extra": "mean: 86.14359728939056 usec\nrounds: 11512"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1034.6910133267565,
            "unit": "iter/sec",
            "range": "stddev: 0.000016119684251137433",
            "extra": "mean: 966.4721033816487 usec\nrounds: 1035"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1037.2642744526945,
            "unit": "iter/sec",
            "range": "stddev: 0.000020558296647313517",
            "extra": "mean: 964.0744645598088 usec\nrounds: 1044"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1179.22177234385,
            "unit": "iter/sec",
            "range": "stddev: 0.000021805912602889277",
            "extra": "mean: 848.0169069575229 usec\nrounds: 1150"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1591.900857913889,
            "unit": "iter/sec",
            "range": "stddev: 0.000015870752381665744",
            "extra": "mean: 628.1798235289933 usec\nrounds: 1598"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 944.8356683765198,
            "unit": "iter/sec",
            "range": "stddev: 0.00004462003241680669",
            "extra": "mean: 1.0583851070295296 msec\nrounds: 953"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1014.4698440778972,
            "unit": "iter/sec",
            "range": "stddev: 0.000032733647665004343",
            "extra": "mean: 985.7365458792425 usec\nrounds: 1068"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1022.9598610254899,
            "unit": "iter/sec",
            "range": "stddev: 0.00003267666897176771",
            "extra": "mean: 977.5554624377213 usec\nrounds: 1025"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 763.6732196469057,
            "unit": "iter/sec",
            "range": "stddev: 0.000019048296419670186",
            "extra": "mean: 1.3094606099482748 msec\nrounds: 764"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1095.1391789965828,
            "unit": "iter/sec",
            "range": "stddev: 0.00001943009579797489",
            "extra": "mean: 913.125947074824 usec\nrounds: 1077"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 930.4505856134705,
            "unit": "iter/sec",
            "range": "stddev: 0.00016470937531267032",
            "extra": "mean: 1.074748101040394 msec\nrounds: 960"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1632.4163529688494,
            "unit": "iter/sec",
            "range": "stddev: 0.000013827392112534889",
            "extra": "mean: 612.5888154583333 usec\nrounds: 1669"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3039.236254553147,
            "unit": "iter/sec",
            "range": "stddev: 0.000024197173299273158",
            "extra": "mean: 329.0300313119383 usec\nrounds: 3002"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 2992.590431589252,
            "unit": "iter/sec",
            "range": "stddev: 0.00001598892875493599",
            "extra": "mean: 334.15865714338247 usec\nrounds: 3010"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 3964.7718623405667,
            "unit": "iter/sec",
            "range": "stddev: 0.000012469297040456302",
            "extra": "mean: 252.22132186179792 usec\nrounds: 4039"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7745.643420733564,
            "unit": "iter/sec",
            "range": "stddev: 0.000006159460146188585",
            "extra": "mean: 129.10483295980248 usec\nrounds: 7585"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 652.8409674941342,
            "unit": "iter/sec",
            "range": "stddev: 0.00007529577382080017",
            "extra": "mean: 1.5317666166668453 msec\nrounds: 660"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4532.5777244048195,
            "unit": "iter/sec",
            "range": "stddev: 0.000015795555968909786",
            "extra": "mean: 220.62500872642215 usec\nrounds: 4469"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 424.91771128601243,
            "unit": "iter/sec",
            "range": "stddev: 0.000037343603287543095",
            "extra": "mean: 2.35339684235214 msec\nrounds: 425"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 617.3400634741561,
            "unit": "iter/sec",
            "range": "stddev: 0.00004768045245306223",
            "extra": "mean: 1.619852750803793 msec\nrounds: 622"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 9262.641049602558,
            "unit": "iter/sec",
            "range": "stddev: 0.000005410788748326954",
            "extra": "mean: 107.96056919887963 usec\nrounds: 9227"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 638.1868472879185,
            "unit": "iter/sec",
            "range": "stddev: 0.000024255408235377954",
            "extra": "mean: 1.566939218897517 msec\nrounds: 635"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.405489281458281,
            "unit": "iter/sec",
            "range": "stddev: 0.0005209107760757553",
            "extra": "mean: 135.0349668999968 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 557.1795096922029,
            "unit": "iter/sec",
            "range": "stddev: 0.00006018539600282096",
            "extra": "mean: 1.7947537240779368 msec\nrounds: 569"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 362.96774881323995,
            "unit": "iter/sec",
            "range": "stddev: 0.0000622400641416211",
            "extra": "mean: 2.7550657138811974 msec\nrounds: 353"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 757747.5291167361,
            "unit": "iter/sec",
            "range": "stddev: 1.1142828002428423e-7",
            "extra": "mean: 1.3197007731132346 usec\nrounds: 73987"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 554.5470661283341,
            "unit": "iter/sec",
            "range": "stddev: 0.00006256068396846589",
            "extra": "mean: 1.803273447972004 msec\nrounds: 567"
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
        "date": 1771213610745,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 331.07750101158217,
            "unit": "iter/sec",
            "range": "stddev: 0.00889144069596798",
            "extra": "mean: 3.020440824110898 msec\nrounds: 506"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 367.95938768638695,
            "unit": "iter/sec",
            "range": "stddev: 0.0008947098131114321",
            "extra": "mean: 2.7176912275229226 msec\nrounds: 545"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6416.23243661979,
            "unit": "iter/sec",
            "range": "stddev: 0.000018494620936784896",
            "extra": "mean: 155.85470287713292 usec\nrounds: 6812"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4548.941930101655,
            "unit": "iter/sec",
            "range": "stddev: 0.00003306112827947754",
            "extra": "mean: 219.83133998319758 usec\nrounds: 4762"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 349.40234395539215,
            "unit": "iter/sec",
            "range": "stddev: 0.0011312094004965266",
            "extra": "mean: 2.8620300272732826 msec\nrounds: 440"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 316.2557316657987,
            "unit": "iter/sec",
            "range": "stddev: 0.0016380430017805263",
            "extra": "mean: 3.1619980284080476 msec\nrounds: 352"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 332.2515383468726,
            "unit": "iter/sec",
            "range": "stddev: 0.0009503972848596043",
            "extra": "mean: 3.0097678553289167 msec\nrounds: 394"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 325.66517037697554,
            "unit": "iter/sec",
            "range": "stddev: 0.0009498802861969273",
            "extra": "mean: 3.07063846846884 msec\nrounds: 333"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 301.0494253201701,
            "unit": "iter/sec",
            "range": "stddev: 0.0005211245495171701",
            "extra": "mean: 3.3217136984615956 msec\nrounds: 325"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 16923.53502248705,
            "unit": "iter/sec",
            "range": "stddev: 0.000015012352713672498",
            "extra": "mean: 59.08930957221738 usec\nrounds: 17227"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 17645.955772031582,
            "unit": "iter/sec",
            "range": "stddev: 0.000014866638952226648",
            "extra": "mean: 56.670208908999754 usec\nrounds: 17735"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 14130.630226816917,
            "unit": "iter/sec",
            "range": "stddev: 0.00005638142686784293",
            "extra": "mean: 70.7682519426638 usec\nrounds: 14027"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6700.42813122028,
            "unit": "iter/sec",
            "range": "stddev: 0.00007280217590707654",
            "extra": "mean: 149.2441946120658 usec\nrounds: 6125"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 14508.054223246529,
            "unit": "iter/sec",
            "range": "stddev: 0.00002790280304459192",
            "extra": "mean: 68.92723066871925 usec\nrounds: 18338"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54158.07759809333,
            "unit": "iter/sec",
            "range": "stddev: 0.000014478323567755086",
            "extra": "mean: 18.46446632432178 usec\nrounds: 47126"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 216664.52404593327,
            "unit": "iter/sec",
            "range": "stddev: 0.000009487357841418565",
            "extra": "mean: 4.61543025746106 usec\nrounds: 186916"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 152.39793719895266,
            "unit": "iter/sec",
            "range": "stddev: 0.0009753985155621923",
            "extra": "mean: 6.56176860645114 msec\nrounds: 155"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4322.278769625643,
            "unit": "iter/sec",
            "range": "stddev: 0.00007833395185484897",
            "extra": "mean: 231.359441003064 usec\nrounds: 4068"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 249.68319689600125,
            "unit": "iter/sec",
            "range": "stddev: 0.00020861178853662658",
            "extra": "mean: 4.005075281123235 msec\nrounds: 249"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 170.64449509415138,
            "unit": "iter/sec",
            "range": "stddev: 0.0004613857612445309",
            "extra": "mean: 5.860136299435033 msec\nrounds: 177"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 186.99826901970556,
            "unit": "iter/sec",
            "range": "stddev: 0.00041910752321240694",
            "extra": "mean: 5.347643083768983 msec\nrounds: 191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 92.79263310838037,
            "unit": "iter/sec",
            "range": "stddev: 0.0006691385645331146",
            "extra": "mean: 10.776717574465371 msec\nrounds: 94"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 131.7416108311928,
            "unit": "iter/sec",
            "range": "stddev: 0.0006533961457546683",
            "extra": "mean: 7.590616159091532 msec\nrounds: 132"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 60.01957738328289,
            "unit": "iter/sec",
            "range": "stddev: 0.02356835953687113",
            "extra": "mean: 16.661230278481227 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 3.7760748793594896,
            "unit": "iter/sec",
            "range": "stddev: 0.24397929878552538",
            "extra": "mean: 264.8252568999965 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 781.1150981861636,
            "unit": "iter/sec",
            "range": "stddev: 0.00016513450869992539",
            "extra": "mean: 1.2802210613034002 msec\nrounds: 783"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1717853.6106264626,
            "unit": "iter/sec",
            "range": "stddev: 6.954738237684467e-8",
            "extra": "mean: 582.1217790701749 nsec\nrounds: 173281"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 821529.2474964607,
            "unit": "iter/sec",
            "range": "stddev: 1.19857349703572e-7",
            "extra": "mean: 1.2172421165130927 usec\nrounds: 82768"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23731.717305549595,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017371481423633365",
            "extra": "mean: 42.13770066130667 usec\nrounds: 23896"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1506.8019599668428,
            "unit": "iter/sec",
            "range": "stddev: 0.00000570977213113153",
            "extra": "mean: 663.6572201047608 usec\nrounds: 1522"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 151.06593485215996,
            "unit": "iter/sec",
            "range": "stddev: 0.000012545594239717039",
            "extra": "mean: 6.619626065788066 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10199.963507423612,
            "unit": "iter/sec",
            "range": "stddev: 0.000002547528737382959",
            "extra": "mean: 98.03956644278112 usec\nrounds: 10287"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1442.1442806376494,
            "unit": "iter/sec",
            "range": "stddev: 0.000005509106632993235",
            "extra": "mean: 693.4118960398652 usec\nrounds: 1414"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 73320.06030431144,
            "unit": "iter/sec",
            "range": "stddev: 0.000011218350689743455",
            "extra": "mean: 13.638832208396273 usec\nrounds: 63412"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1166853.7884472434,
            "unit": "iter/sec",
            "range": "stddev: 9.88297687120009e-7",
            "extra": "mean: 857.0054019627607 nsec\nrounds: 120993"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5478.8910363273935,
            "unit": "iter/sec",
            "range": "stddev: 0.00004440473234943348",
            "extra": "mean: 182.5186873346398 usec\nrounds: 5306"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 258.4965487639452,
            "unit": "iter/sec",
            "range": "stddev: 0.00022971539649930403",
            "extra": "mean: 3.868523602275183 msec\nrounds: 264"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 72175.95368182256,
            "unit": "iter/sec",
            "range": "stddev: 0.000012316313041844817",
            "extra": "mean: 13.855029950949563 usec\nrounds: 65407"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1953.3400160232873,
            "unit": "iter/sec",
            "range": "stddev: 0.0008117696516545883",
            "extra": "mean: 511.94364104404764 usec\nrounds: 2875"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 356606.95194344444,
            "unit": "iter/sec",
            "range": "stddev: 0.000006559822620025785",
            "extra": "mean: 2.8042078107288093 usec\nrounds: 183453"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3078.5174395326094,
            "unit": "iter/sec",
            "range": "stddev: 0.000025149356219245645",
            "extra": "mean: 324.8316826659989 usec\nrounds: 2625"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2566.2781443006074,
            "unit": "iter/sec",
            "range": "stddev: 0.000027457033692226568",
            "extra": "mean: 389.66937477953377 usec\nrounds: 1697"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5138.315712150047,
            "unit": "iter/sec",
            "range": "stddev: 0.00005059082296617663",
            "extra": "mean: 194.61630153153158 usec\nrounds: 3721"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1101.826136304554,
            "unit": "iter/sec",
            "range": "stddev: 0.000055402614768665685",
            "extra": "mean: 907.5842068458537 usec\nrounds: 701"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 368871.6011702998,
            "unit": "iter/sec",
            "range": "stddev: 5.026997102230555e-7",
            "extra": "mean: 2.710970421217984 usec\nrounds: 112158"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 128263.6634054748,
            "unit": "iter/sec",
            "range": "stddev: 0.0000012787080502709037",
            "extra": "mean: 7.796440343659451 usec\nrounds: 78902"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 292736.768973646,
            "unit": "iter/sec",
            "range": "stddev: 7.351809959687462e-7",
            "extra": "mean: 3.416038250015755 usec\nrounds: 176026"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 39.96885273781366,
            "unit": "iter/sec",
            "range": "stddev: 0.001306086903499875",
            "extra": "mean: 25.01948220930349 msec\nrounds: 43"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.165151643085388,
            "unit": "iter/sec",
            "range": "stddev: 0.006786341358952931",
            "extra": "mean: 240.08729710000125 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1448.3084240385308,
            "unit": "iter/sec",
            "range": "stddev: 0.00030323222183120354",
            "extra": "mean: 690.460666666257 usec\nrounds: 1551"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 390.8079233402009,
            "unit": "iter/sec",
            "range": "stddev: 0.0005673301139387199",
            "extra": "mean: 2.5588017547164554 msec\nrounds: 424"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 940268.455743328,
            "unit": "iter/sec",
            "range": "stddev: 1.0315791330178473e-7",
            "extra": "mean: 1.06352605353484 usec\nrounds: 95703"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 466763.34829267726,
            "unit": "iter/sec",
            "range": "stddev: 1.3495766012184777e-7",
            "extra": "mean: 2.1424132885707308 usec\nrounds: 46905"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59315.990252764794,
            "unit": "iter/sec",
            "range": "stddev: 0.0000012357281702915009",
            "extra": "mean: 16.858860414176238 usec\nrounds: 59698"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3947.7063108378984,
            "unit": "iter/sec",
            "range": "stddev: 0.000011981462928388737",
            "extra": "mean: 253.31165017383236 usec\nrounds: 4022"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 398.23747680141116,
            "unit": "iter/sec",
            "range": "stddev: 0.00001682543709384719",
            "extra": "mean: 2.5110645236903943 msec\nrounds: 401"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18315.259039184664,
            "unit": "iter/sec",
            "range": "stddev: 0.000004447351893062807",
            "extra": "mean: 54.59928237217642 usec\nrounds: 18079"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 17838.04161712611,
            "unit": "iter/sec",
            "range": "stddev: 0.000015735815477385076",
            "extra": "mean: 56.059965632096684 usec\nrounds: 17691"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18275.757178911794,
            "unit": "iter/sec",
            "range": "stddev: 0.0000029407876490112823",
            "extra": "mean: 54.717295169246924 usec\nrounds: 18342"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1507.1057392955593,
            "unit": "iter/sec",
            "range": "stddev: 0.000005396126125501065",
            "extra": "mean: 663.52345023078 usec\nrounds: 1517"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 150.94709420532695,
            "unit": "iter/sec",
            "range": "stddev: 0.00001322905416742669",
            "extra": "mean: 6.624837697370592 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39714.83300541473,
            "unit": "iter/sec",
            "range": "stddev: 0.000003923215285836468",
            "extra": "mean: 25.17950912354736 usec\nrounds: 40281"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3938.9043323918017,
            "unit": "iter/sec",
            "range": "stddev: 0.00001003073982324323",
            "extra": "mean: 253.87770700000087 usec\nrounds: 4000"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8085.248010233575,
            "unit": "iter/sec",
            "range": "stddev: 0.000005418444001999836",
            "extra": "mean: 123.68204398112347 usec\nrounds: 8208"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1243.3300687744068,
            "unit": "iter/sec",
            "range": "stddev: 0.000017555031053995198",
            "extra": "mean: 804.2916560248033 usec\nrounds: 1253"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 431.5243152280072,
            "unit": "iter/sec",
            "range": "stddev: 0.00004240807206445704",
            "extra": "mean: 2.317366518435059 msec\nrounds: 434"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11230.772205476684,
            "unit": "iter/sec",
            "range": "stddev: 0.0000047336055447890075",
            "extra": "mean: 89.04107230599426 usec\nrounds: 10829"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1030.9073218283618,
            "unit": "iter/sec",
            "range": "stddev: 0.000018038393907278844",
            "extra": "mean: 970.0193012757478 usec\nrounds: 1019"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1034.3280296967098,
            "unit": "iter/sec",
            "range": "stddev: 0.00001521433943190484",
            "extra": "mean: 966.8112738791622 usec\nrounds: 1026"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1179.531855193384,
            "unit": "iter/sec",
            "range": "stddev: 0.00002478808265880023",
            "extra": "mean: 847.7939748698437 usec\nrounds: 1154"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1587.5889677041548,
            "unit": "iter/sec",
            "range": "stddev: 0.00005673091278090119",
            "extra": "mean: 629.8859593652384 usec\nrounds: 1575"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 939.2417753080983,
            "unit": "iter/sec",
            "range": "stddev: 0.00008833141448821346",
            "extra": "mean: 1.0646885884861448 msec\nrounds: 938"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1014.9151286093995,
            "unit": "iter/sec",
            "range": "stddev: 0.00002856405976597413",
            "extra": "mean: 985.3040631783313 usec\nrounds: 1013"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1010.5614281108153,
            "unit": "iter/sec",
            "range": "stddev: 0.000026322967998150074",
            "extra": "mean: 989.5489499034619 usec\nrounds: 1038"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 745.9289042476207,
            "unit": "iter/sec",
            "range": "stddev: 0.000026413022889084412",
            "extra": "mean: 1.3406103374002478 msec\nrounds: 738"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1100.4986486089422,
            "unit": "iter/sec",
            "range": "stddev: 0.000020430404453864042",
            "extra": "mean: 908.6789895326314 usec\nrounds: 1051"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 928.5449096190014,
            "unit": "iter/sec",
            "range": "stddev: 0.00004626889510874697",
            "extra": "mean: 1.076953833509591 msec\nrounds: 949"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1598.8910841119857,
            "unit": "iter/sec",
            "range": "stddev: 0.00001626726865852776",
            "extra": "mean: 625.4334706953437 usec\nrounds: 1638"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3146.1489149702593,
            "unit": "iter/sec",
            "range": "stddev: 0.00001417590515696914",
            "extra": "mean: 317.84890894443026 usec\nrounds: 3108"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3134.212997494559,
            "unit": "iter/sec",
            "range": "stddev: 0.000014996710708183867",
            "extra": "mean: 319.05936220652023 usec\nrounds: 3244"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4070.0199587930656,
            "unit": "iter/sec",
            "range": "stddev: 0.000010785553738864357",
            "extra": "mean: 245.6990408215449 usec\nrounds: 4287"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7656.183132020166,
            "unit": "iter/sec",
            "range": "stddev: 0.0000067112829208931805",
            "extra": "mean: 130.61338564613715 usec\nrounds: 7538"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 649.0682451430332,
            "unit": "iter/sec",
            "range": "stddev: 0.000028633583703449512",
            "extra": "mean: 1.540670041221371 msec\nrounds: 655"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4697.594357057896,
            "unit": "iter/sec",
            "range": "stddev: 0.000008691594509043857",
            "extra": "mean: 212.8749151142757 usec\nrounds: 4618"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 439.236792993997,
            "unit": "iter/sec",
            "range": "stddev: 0.00003263538245422585",
            "extra": "mean: 2.276676307518862 msec\nrounds: 439"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 618.5695653579894,
            "unit": "iter/sec",
            "range": "stddev: 0.000028151750119461095",
            "extra": "mean: 1.616633045017762 msec\nrounds: 622"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 9309.74818852254,
            "unit": "iter/sec",
            "range": "stddev: 0.000004206716061734355",
            "extra": "mean: 107.4142908862823 usec\nrounds: 8866"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 643.9320264281995,
            "unit": "iter/sec",
            "range": "stddev: 0.00002659682975723989",
            "extra": "mean: 1.5529589443576204 msec\nrounds: 647"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.45588632032749,
            "unit": "iter/sec",
            "range": "stddev: 0.000484187363578362",
            "extra": "mean: 134.12221660000796 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 465.67973187643724,
            "unit": "iter/sec",
            "range": "stddev: 0.00006205719861824023",
            "extra": "mean: 2.147398590809485 msec\nrounds: 457"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 326.94812227873007,
            "unit": "iter/sec",
            "range": "stddev: 0.000055744486681987144",
            "extra": "mean: 3.0585892129622914 msec\nrounds: 324"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 771118.588797958,
            "unit": "iter/sec",
            "range": "stddev: 1.0456853188777908e-7",
            "extra": "mean: 1.296817395569246 usec\nrounds: 73444"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 462.19190940915325,
            "unit": "iter/sec",
            "range": "stddev: 0.0000649486951834463",
            "extra": "mean: 2.1636034288838113 msec\nrounds: 457"
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
        "date": 1771213688836,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 441.78917753959513,
            "unit": "iter/sec",
            "range": "stddev: 0.006608018928584665",
            "extra": "mean: 2.2635230803280044 msec\nrounds: 610"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 406.93076832127696,
            "unit": "iter/sec",
            "range": "stddev: 0.002018227658811101",
            "extra": "mean: 2.457420470133847 msec\nrounds: 519"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 8201.467733654006,
            "unit": "iter/sec",
            "range": "stddev: 0.00001412470987223126",
            "extra": "mean: 121.92939513699326 usec\nrounds: 8883"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 6020.447344646039,
            "unit": "iter/sec",
            "range": "stddev: 0.00003309685668975914",
            "extra": "mean: 166.10061391688714 usec\nrounds: 6395"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 380.12180153276796,
            "unit": "iter/sec",
            "range": "stddev: 0.0004847123239091312",
            "extra": "mean: 2.6307357167299865 msec\nrounds: 526"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 347.8248340291618,
            "unit": "iter/sec",
            "range": "stddev: 0.0009786863092827592",
            "extra": "mean: 2.8750103562650144 msec\nrounds: 407"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 348.7911927734635,
            "unit": "iter/sec",
            "range": "stddev: 0.000692852955226839",
            "extra": "mean: 2.8670448701653153 msec\nrounds: 362"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 331.7608420692744,
            "unit": "iter/sec",
            "range": "stddev: 0.0006764179335769854",
            "extra": "mean: 3.0142195015022044 msec\nrounds: 333"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 274.82584025898115,
            "unit": "iter/sec",
            "range": "stddev: 0.009119775166282257",
            "extra": "mean: 3.6386680344819595 msec\nrounds: 377"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 21652.127089602374,
            "unit": "iter/sec",
            "range": "stddev: 0.000015891698720233843",
            "extra": "mean: 46.18483883184913 usec\nrounds: 22157"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 21826.402088820578,
            "unit": "iter/sec",
            "range": "stddev: 0.00001481887481870729",
            "extra": "mean: 45.816071560057864 usec\nrounds: 22848"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 15432.382689608485,
            "unit": "iter/sec",
            "range": "stddev: 0.0000715636656941928",
            "extra": "mean: 64.79880781296059 usec\nrounds: 15308"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 2880.266299133546,
            "unit": "iter/sec",
            "range": "stddev: 0.00015227087524943668",
            "extra": "mean: 347.1901192958527 usec\nrounds: 3068"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 21211.129988015757,
            "unit": "iter/sec",
            "range": "stddev: 0.000013824903760907371",
            "extra": "mean: 47.14506019080539 usec\nrounds: 23276"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 56351.142512257065,
            "unit": "iter/sec",
            "range": "stddev: 0.00003130196587257414",
            "extra": "mean: 17.745869123815684 usec\nrounds: 55457"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 212604.62646169018,
            "unit": "iter/sec",
            "range": "stddev: 0.000015738758376053725",
            "extra": "mean: 4.703566505784354 usec\nrounds: 114772"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 165.39675451308315,
            "unit": "iter/sec",
            "range": "stddev: 0.0019338487288618911",
            "extra": "mean: 6.046067850266666 msec\nrounds: 187"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4726.578844590028,
            "unit": "iter/sec",
            "range": "stddev: 0.00007642763969656877",
            "extra": "mean: 211.56951632036885 usec\nrounds: 4718"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 256.9641913483253,
            "unit": "iter/sec",
            "range": "stddev: 0.0003202710098955304",
            "extra": "mean: 3.8915928120290495 msec\nrounds: 266"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 171.65594311199487,
            "unit": "iter/sec",
            "range": "stddev: 0.0008051054672908062",
            "extra": "mean: 5.825606628414619 msec\nrounds: 183"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 186.9670216452733,
            "unit": "iter/sec",
            "range": "stddev: 0.0006717342822548712",
            "extra": "mean: 5.3485368232332915 msec\nrounds: 198"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 97.33804858371812,
            "unit": "iter/sec",
            "range": "stddev: 0.0006535435148037358",
            "extra": "mean: 10.27347491089185 msec\nrounds: 101"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 135.78069819018913,
            "unit": "iter/sec",
            "range": "stddev: 0.0007881217114930329",
            "extra": "mean: 7.364817041957553 msec\nrounds: 143"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 67.82808950981331,
            "unit": "iter/sec",
            "range": "stddev: 0.00216321222320583",
            "extra": "mean: 14.743154454546753 msec\nrounds: 66"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 5.83850651283637,
            "unit": "iter/sec",
            "range": "stddev: 0.011683435127305911",
            "extra": "mean: 171.2766780000038 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 900.8008632602342,
            "unit": "iter/sec",
            "range": "stddev: 0.00015569466885903844",
            "extra": "mean: 1.110123270065193 msec\nrounds: 922"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1719751.984725564,
            "unit": "iter/sec",
            "range": "stddev: 4.827970261365878e-8",
            "extra": "mean: 581.47919518731 nsec\nrounds: 171792"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 737637.1055191277,
            "unit": "iter/sec",
            "range": "stddev: 7.517162457072924e-8",
            "extra": "mean: 1.3556801745978178 usec\nrounds: 74450"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 19926.480293245855,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014890148608965405",
            "extra": "mean: 50.18447740311435 usec\nrounds: 20025"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1264.818345523886,
            "unit": "iter/sec",
            "range": "stddev: 0.000007737047704266492",
            "extra": "mean: 790.6273683797664 usec\nrounds: 1265"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 126.43286830082347,
            "unit": "iter/sec",
            "range": "stddev: 0.000025121381823787688",
            "extra": "mean: 7.9093357086599205 msec\nrounds: 127"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10993.17561620626,
            "unit": "iter/sec",
            "range": "stddev: 0.000001831505345221161",
            "extra": "mean: 90.96552578727015 usec\nrounds: 11052"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1217.9076441499433,
            "unit": "iter/sec",
            "range": "stddev: 0.000004837734591263708",
            "extra": "mean: 821.0803214869094 usec\nrounds: 1210"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 76188.90489986313,
            "unit": "iter/sec",
            "range": "stddev: 0.000014087792057740483",
            "extra": "mean: 13.125270684941901 usec\nrounds: 76203"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1099676.7719917411,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014535090296561576",
            "extra": "mean: 909.3581181939435 nsec\nrounds: 113689"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5744.140134119,
            "unit": "iter/sec",
            "range": "stddev: 0.00008881862937756499",
            "extra": "mean: 174.0904603040945 usec\nrounds: 5794"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 257.23951760779767,
            "unit": "iter/sec",
            "range": "stddev: 0.0005914948516551397",
            "extra": "mean: 3.8874275978260004 msec\nrounds: 276"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 74510.06770252614,
            "unit": "iter/sec",
            "range": "stddev: 0.0000280626901410679",
            "extra": "mean: 13.421005118293518 usec\nrounds: 76000"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1914.424926427773,
            "unit": "iter/sec",
            "range": "stddev: 0.0008947946530982301",
            "extra": "mean: 522.3500729620947 usec\nrounds: 2563"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 358052.9219044925,
            "unit": "iter/sec",
            "range": "stddev: 0.000004443793563210514",
            "extra": "mean: 2.7928832270966395 usec\nrounds: 189466"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3618.4127284806664,
            "unit": "iter/sec",
            "range": "stddev: 0.00003197458129132123",
            "extra": "mean: 276.36427213760373 usec\nrounds: 3546"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2816.3656991725425,
            "unit": "iter/sec",
            "range": "stddev: 0.000017481884233878962",
            "extra": "mean: 355.0675256035831 usec\nrounds: 1699"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 6650.961743481115,
            "unit": "iter/sec",
            "range": "stddev: 0.000011299654313418466",
            "extra": "mean: 150.3541951628487 usec\nrounds: 4176"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1130.1037613350552,
            "unit": "iter/sec",
            "range": "stddev: 0.000019464405097396266",
            "extra": "mean: 884.8744993279589 usec\nrounds: 745"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 375181.8906056407,
            "unit": "iter/sec",
            "range": "stddev: 4.270675043854116e-7",
            "extra": "mean: 2.6653738494300487 usec\nrounds: 190332"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 134523.73519256906,
            "unit": "iter/sec",
            "range": "stddev: 0.0000010579054312763872",
            "extra": "mean: 7.433632426043719 usec\nrounds: 133263"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 291395.0836928724,
            "unit": "iter/sec",
            "range": "stddev: 4.641620624937505e-7",
            "extra": "mean: 3.431766889567672 usec\nrounds: 146585"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 45.663360723663594,
            "unit": "iter/sec",
            "range": "stddev: 0.000796725748256541",
            "extra": "mean: 21.899395580005603 msec\nrounds: 50"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.805128845378107,
            "unit": "iter/sec",
            "range": "stddev: 0.004246960688883761",
            "extra": "mean: 208.11096480001083 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1787.7004976355083,
            "unit": "iter/sec",
            "range": "stddev: 0.0003560047321311414",
            "extra": "mean: 559.3778159835186 usec\nrounds: 1902"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 452.077983770664,
            "unit": "iter/sec",
            "range": "stddev: 0.0005178861925122721",
            "extra": "mean: 2.212007741804328 msec\nrounds: 488"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 1095222.7047392293,
            "unit": "iter/sec",
            "range": "stddev: 6.339052120982034e-8",
            "extra": "mean: 913.0563087058155 nsec\nrounds: 106884"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 591872.3747269985,
            "unit": "iter/sec",
            "range": "stddev: 8.886194588241005e-8",
            "extra": "mean: 1.6895534285769809 usec\nrounds: 59631"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 93043.73426539551,
            "unit": "iter/sec",
            "range": "stddev: 6.67999211602549e-7",
            "extra": "mean: 10.747633979818847 usec\nrounds: 94003"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 6599.371226181385,
            "unit": "iter/sec",
            "range": "stddev: 0.0000031786833718421922",
            "extra": "mean: 151.52958755112087 usec\nrounds: 6539"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 618.3318818897355,
            "unit": "iter/sec",
            "range": "stddev: 0.000009001348046986404",
            "extra": "mean: 1.6172544701137144 msec\nrounds: 619"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 26880.13925293803,
            "unit": "iter/sec",
            "range": "stddev: 0.0000011770037078071952",
            "extra": "mean: 37.202188224925166 usec\nrounds: 26904"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 26810.332657583473,
            "unit": "iter/sec",
            "range": "stddev: 0.0000015426562386445348",
            "extra": "mean: 37.29905230090995 usec\nrounds: 26921"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 28058.965896596,
            "unit": "iter/sec",
            "range": "stddev: 0.0000011477987078056818",
            "extra": "mean: 35.63923216861374 usec\nrounds: 28195"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1260.2186274950996,
            "unit": "iter/sec",
            "range": "stddev: 0.000025474470700205494",
            "extra": "mean: 793.5131081086076 usec\nrounds: 1258"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 126.35939187385544,
            "unit": "iter/sec",
            "range": "stddev: 0.000018149967026429353",
            "extra": "mean: 7.913934889765057 msec\nrounds: 127"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 42975.45053900284,
            "unit": "iter/sec",
            "range": "stddev: 0.000001321771892436272",
            "extra": "mean: 23.269098693740023 usec\nrounds: 43093"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 4326.501282870088,
            "unit": "iter/sec",
            "range": "stddev: 0.000012675757443084728",
            "extra": "mean: 231.1336423172458 usec\nrounds: 4367"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8714.814157471092,
            "unit": "iter/sec",
            "range": "stddev: 0.000003608238002524308",
            "extra": "mean: 114.74713997689939 usec\nrounds: 8730"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1367.6984708285506,
            "unit": "iter/sec",
            "range": "stddev: 0.000010678020697387782",
            "extra": "mean: 731.1553104202865 usec\nrounds: 1382"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 467.31423201668815,
            "unit": "iter/sec",
            "range": "stddev: 0.00002121037065124725",
            "extra": "mean: 2.139887748944674 msec\nrounds: 474"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 13367.482297598603,
            "unit": "iter/sec",
            "range": "stddev: 0.000003231541520064515",
            "extra": "mean: 74.80840278948001 usec\nrounds: 12977"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 984.938496031475,
            "unit": "iter/sec",
            "range": "stddev: 0.000011977384498906124",
            "extra": "mean: 1.0152918218033014 msec\nrounds: 954"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 976.2762111255986,
            "unit": "iter/sec",
            "range": "stddev: 0.000011760974783876933",
            "extra": "mean: 1.0243002836738684 msec\nrounds: 980"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1240.3820789427116,
            "unit": "iter/sec",
            "range": "stddev: 0.000021676549380945206",
            "extra": "mean: 806.2031989791318 usec\nrounds: 1176"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1583.6157821989486,
            "unit": "iter/sec",
            "range": "stddev: 0.00001508394608870539",
            "extra": "mean: 631.466300879774 usec\nrounds: 1592"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 963.1779362997087,
            "unit": "iter/sec",
            "range": "stddev: 0.000014179386163082176",
            "extra": "mean: 1.0382297624484138 msec\nrounds: 964"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1090.6692564189857,
            "unit": "iter/sec",
            "range": "stddev: 0.00004322984944159421",
            "extra": "mean: 916.8682385742845 usec\nrounds: 1094"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1054.9075453441649,
            "unit": "iter/sec",
            "range": "stddev: 0.00007697139945739329",
            "extra": "mean: 947.9503719671934 usec\nrounds: 1113"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 741.1679273777403,
            "unit": "iter/sec",
            "range": "stddev: 0.0000171680512515805",
            "extra": "mean: 1.3492219010852375 msec\nrounds: 738"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1194.4577406269373,
            "unit": "iter/sec",
            "range": "stddev: 0.000012697900423230006",
            "extra": "mean: 837.199982876856 usec\nrounds: 1168"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 1017.1733502249871,
            "unit": "iter/sec",
            "range": "stddev: 0.00002645629135460068",
            "extra": "mean: 983.1165944121634 usec\nrounds: 1038"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1728.7572783579187,
            "unit": "iter/sec",
            "range": "stddev: 0.000019780239094912706",
            "extra": "mean: 578.4502038075942 usec\nrounds: 1786"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3192.0576767247753,
            "unit": "iter/sec",
            "range": "stddev: 0.00001453725413871704",
            "extra": "mean: 313.27754736125394 usec\nrounds: 2956"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3200.8449640163185,
            "unit": "iter/sec",
            "range": "stddev: 0.000012559513557879195",
            "extra": "mean: 312.4175057654876 usec\nrounds: 3209"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4983.996691785762,
            "unit": "iter/sec",
            "range": "stddev: 0.000008171300600651342",
            "extra": "mean: 200.64218775428216 usec\nrounds: 4916"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 8391.830505419952,
            "unit": "iter/sec",
            "range": "stddev: 0.000003456151796318673",
            "extra": "mean: 119.16351257978096 usec\nrounds: 8426"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 708.6252587955149,
            "unit": "iter/sec",
            "range": "stddev: 0.00004722759494515125",
            "extra": "mean: 1.411183114894534 msec\nrounds: 705"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 5291.0870347683485,
            "unit": "iter/sec",
            "range": "stddev: 0.000004486606245450307",
            "extra": "mean: 188.99708007615138 usec\nrounds: 5270"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 486.55225608197304,
            "unit": "iter/sec",
            "range": "stddev: 0.00002429742961253888",
            "extra": "mean: 2.055277696280012 msec\nrounds: 484"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 687.7303194007703,
            "unit": "iter/sec",
            "range": "stddev: 0.0000179935891867979",
            "extra": "mean: 1.45405833040968 msec\nrounds: 684"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 11770.307851275893,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027248540446505773",
            "extra": "mean: 84.95954503786412 usec\nrounds: 11990"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 705.8788442787111,
            "unit": "iter/sec",
            "range": "stddev: 0.00002776830774814469",
            "extra": "mean: 1.4166737083923107 msec\nrounds: 703"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 8.119589002871367,
            "unit": "iter/sec",
            "range": "stddev: 0.0006379692570540108",
            "extra": "mean: 123.15894309999749 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 645.0539200569754,
            "unit": "iter/sec",
            "range": "stddev: 0.00006219468421997343",
            "extra": "mean: 1.5502580000004857 msec\nrounds: 659"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 372.0080372207654,
            "unit": "iter/sec",
            "range": "stddev: 0.00004383564272531211",
            "extra": "mean: 2.688113965146827 msec\nrounds: 373"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 812242.1865227169,
            "unit": "iter/sec",
            "range": "stddev: 7.372310116080819e-8",
            "extra": "mean: 1.231159888753244 usec\nrounds: 79277"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 652.0817826194594,
            "unit": "iter/sec",
            "range": "stddev: 0.00006852938010207526",
            "extra": "mean: 1.5335499728008473 msec\nrounds: 625"
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
        "date": 1771214029643,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 379.38407681226505,
            "unit": "iter/sec",
            "range": "stddev: 0.005989597664425314",
            "extra": "mean: 2.6358512681986954 msec\nrounds: 522"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 395.58411916222775,
            "unit": "iter/sec",
            "range": "stddev: 0.0005001536484058026",
            "extra": "mean: 2.5279073440000843 msec\nrounds: 500"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6921.056782892809,
            "unit": "iter/sec",
            "range": "stddev: 0.000018609407955184046",
            "extra": "mean: 144.4866053507551 usec\nrounds: 5382"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4647.567888405786,
            "unit": "iter/sec",
            "range": "stddev: 0.000035122870903596615",
            "extra": "mean: 215.1663028946138 usec\nrounds: 4906"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 372.55809010293683,
            "unit": "iter/sec",
            "range": "stddev: 0.0004053512840684931",
            "extra": "mean: 2.684145175115383 msec\nrounds: 434"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 319.9886906110978,
            "unit": "iter/sec",
            "range": "stddev: 0.0010398579421304766",
            "extra": "mean: 3.1251104471544036 msec\nrounds: 369"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 328.83042238357325,
            "unit": "iter/sec",
            "range": "stddev: 0.000511928267479157",
            "extra": "mean: 3.0410811528670627 msec\nrounds: 314"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 328.77726657997687,
            "unit": "iter/sec",
            "range": "stddev: 0.00044346526474753795",
            "extra": "mean: 3.0415728264981623 msec\nrounds: 317"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 289.78521852002115,
            "unit": "iter/sec",
            "range": "stddev: 0.001950885854020627",
            "extra": "mean: 3.4508316369867233 msec\nrounds: 292"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 17934.272725858096,
            "unit": "iter/sec",
            "range": "stddev: 0.00001270155776602536",
            "extra": "mean: 55.75916098109594 usec\nrounds: 17002"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15757.573842938764,
            "unit": "iter/sec",
            "range": "stddev: 0.000016093725294039512",
            "extra": "mean: 63.46154617248499 usec\nrounds: 15702"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 14185.123169828239,
            "unit": "iter/sec",
            "range": "stddev: 0.00006060763689089886",
            "extra": "mean: 70.49639174984397 usec\nrounds: 11903"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6333.133644869545,
            "unit": "iter/sec",
            "range": "stddev: 0.00009296501557549069",
            "extra": "mean: 157.89971538183113 usec\nrounds: 5474"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15317.967665578542,
            "unit": "iter/sec",
            "range": "stddev: 0.000017676165600068616",
            "extra": "mean: 65.28281178234431 usec\nrounds: 18553"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 55080.945839889326,
            "unit": "iter/sec",
            "range": "stddev: 0.000013560551013880996",
            "extra": "mean: 18.155098550900433 usec\nrounds: 46169"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 214757.17547743375,
            "unit": "iter/sec",
            "range": "stddev: 0.000010288421595391716",
            "extra": "mean: 4.656421829803205 usec\nrounds: 190840"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 160.83145743142177,
            "unit": "iter/sec",
            "range": "stddev: 0.0005613215387711367",
            "extra": "mean: 6.217689101190905 msec\nrounds: 168"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4244.796433530351,
            "unit": "iter/sec",
            "range": "stddev: 0.00007366475122882549",
            "extra": "mean: 235.58255752875073 usec\nrounds: 4111"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 243.30693641989728,
            "unit": "iter/sec",
            "range": "stddev: 0.00024117809729620081",
            "extra": "mean: 4.110034899597797 msec\nrounds: 249"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 170.22683991935503,
            "unit": "iter/sec",
            "range": "stddev: 0.0003237194238309391",
            "extra": "mean: 5.874514268570985 msec\nrounds: 175"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 185.20759855194845,
            "unit": "iter/sec",
            "range": "stddev: 0.00039574656796086897",
            "extra": "mean: 5.399346505319069 msec\nrounds: 188"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 90.89452336321801,
            "unit": "iter/sec",
            "range": "stddev: 0.0007120268024655187",
            "extra": "mean: 11.001762955551916 msec\nrounds: 90"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 130.0527233050615,
            "unit": "iter/sec",
            "range": "stddev: 0.0004088717071271923",
            "extra": "mean: 7.689189234848427 msec\nrounds: 132"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 69.80556169119845,
            "unit": "iter/sec",
            "range": "stddev: 0.0014176635378106799",
            "extra": "mean: 14.325506102561546 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 3.6656787706367835,
            "unit": "iter/sec",
            "range": "stddev: 0.2619528783801907",
            "extra": "mean: 272.8007724000008 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 771.0203690682905,
            "unit": "iter/sec",
            "range": "stddev: 0.00016796319492048256",
            "extra": "mean: 1.2969825962035362 msec\nrounds: 790"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1648986.563759349,
            "unit": "iter/sec",
            "range": "stddev: 8.417091292642528e-8",
            "extra": "mean: 606.4330795517255 nsec\nrounds: 165235"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 816313.1759335054,
            "unit": "iter/sec",
            "range": "stddev: 1.2289167266372156e-7",
            "extra": "mean: 1.2250200406926388 usec\nrounds: 81090"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23715.129287252053,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017953606873201173",
            "extra": "mean: 42.167174713129015 usec\nrounds: 23965"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1506.2840129859546,
            "unit": "iter/sec",
            "range": "stddev: 0.000005454973212779967",
            "extra": "mean: 663.8854235846719 usec\nrounds: 1518"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 150.85873795809277,
            "unit": "iter/sec",
            "range": "stddev: 0.00001286390019551598",
            "extra": "mean: 6.628717789471307 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10196.662434050646,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026207210255796197",
            "extra": "mean: 98.0713058285237 usec\nrounds: 10277"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1435.8556917907365,
            "unit": "iter/sec",
            "range": "stddev: 0.0000062987079791113355",
            "extra": "mean: 696.448818441388 usec\nrounds: 1399"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 71894.58229017392,
            "unit": "iter/sec",
            "range": "stddev: 0.000012871425836644076",
            "extra": "mean: 13.909253912400482 usec\nrounds: 61088"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1136252.5895771561,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016071723054308576",
            "extra": "mean: 880.0860030357678 nsec\nrounds: 113689"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5510.16049298412,
            "unit": "iter/sec",
            "range": "stddev: 0.00004600324835725275",
            "extra": "mean: 181.48291710799757 usec\nrounds: 5091"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 254.17744791394543,
            "unit": "iter/sec",
            "range": "stddev: 0.00025254490724106775",
            "extra": "mean: 3.9342593460083877 msec\nrounds: 263"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 72232.95797862695,
            "unit": "iter/sec",
            "range": "stddev: 0.000012589650249048194",
            "extra": "mean: 13.844095936039206 usec\nrounds: 65283"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1979.2362136467211,
            "unit": "iter/sec",
            "range": "stddev: 0.0009485387166163517",
            "extra": "mean: 505.2454038103471 usec\nrounds: 2625"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 358891.39469777775,
            "unit": "iter/sec",
            "range": "stddev: 0.000006359154515806146",
            "extra": "mean: 2.7863582542626846 usec\nrounds: 183487"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3115.9630825086697,
            "unit": "iter/sec",
            "range": "stddev: 0.000024766798348890133",
            "extra": "mean: 320.9280641396102 usec\nrounds: 2744"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2657.3695852935007,
            "unit": "iter/sec",
            "range": "stddev: 0.000029032879370298414",
            "extra": "mean: 376.31197614898275 usec\nrounds: 1719"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4988.7567868528995,
            "unit": "iter/sec",
            "range": "stddev: 0.00002521955181153253",
            "extra": "mean: 200.4507420837484 usec\nrounds: 3916"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1110.3249753914893,
            "unit": "iter/sec",
            "range": "stddev: 0.000027114313030352637",
            "extra": "mean: 900.637220780709 usec\nrounds: 616"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 366722.34090302914,
            "unit": "iter/sec",
            "range": "stddev: 5.110018523568728e-7",
            "extra": "mean: 2.7268586842502347 usec\nrounds: 112284"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 127254.0395990311,
            "unit": "iter/sec",
            "range": "stddev: 0.0000012737754437795557",
            "extra": "mean: 7.858296704379151 usec\nrounds: 77616"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 282115.1674707576,
            "unit": "iter/sec",
            "range": "stddev: 7.592570330694308e-7",
            "extra": "mean: 3.544651671745562 usec\nrounds: 169435"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.274526148938484,
            "unit": "iter/sec",
            "range": "stddev: 0.0006803951714926201",
            "extra": "mean: 24.22801890908488 msec\nrounds: 44"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.390357137245408,
            "unit": "iter/sec",
            "range": "stddev: 0.005297776008624351",
            "extra": "mean: 227.77190299999575 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1456.5149083023796,
            "unit": "iter/sec",
            "range": "stddev: 0.0002657547684365775",
            "extra": "mean: 686.5703840721658 usec\nrounds: 1557"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 397.1829391288462,
            "unit": "iter/sec",
            "range": "stddev: 0.00038166320094414036",
            "extra": "mean: 2.517731507283096 msec\nrounds: 412"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 970480.8339398287,
            "unit": "iter/sec",
            "range": "stddev: 1.0473414613943305e-7",
            "extra": "mean: 1.0304170520713256 usec\nrounds: 100111"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 480702.4907382915,
            "unit": "iter/sec",
            "range": "stddev: 1.4132989119865388e-7",
            "extra": "mean: 2.08028878415866 usec\nrounds: 48476"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59776.48038799544,
            "unit": "iter/sec",
            "range": "stddev: 0.00000126061619203992",
            "extra": "mean: 16.7289876136773 usec\nrounds: 59985"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3939.01544385327,
            "unit": "iter/sec",
            "range": "stddev: 0.000007100493544982661",
            "extra": "mean: 253.87054563608623 usec\nrounds: 4010"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 393.9091022236473,
            "unit": "iter/sec",
            "range": "stddev: 0.00006724035454801656",
            "extra": "mean: 2.5386567468355583 msec\nrounds: 395"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 17936.27858118878,
            "unit": "iter/sec",
            "range": "stddev: 0.000002937182188299757",
            "extra": "mean: 55.7529253057421 usec\nrounds: 17257"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18413.322534451872,
            "unit": "iter/sec",
            "range": "stddev: 0.000002786203511496449",
            "extra": "mean: 54.30850397200019 usec\nrounds: 18505"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18071.65106844266,
            "unit": "iter/sec",
            "range": "stddev: 0.0000031927176370149496",
            "extra": "mean: 55.335287086537114 usec\nrounds: 18430"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1505.5877548151614,
            "unit": "iter/sec",
            "range": "stddev: 0.000006193263472395885",
            "extra": "mean: 664.1924370079433 usec\nrounds: 1524"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 151.0010853888059,
            "unit": "iter/sec",
            "range": "stddev: 0.000008691086063383046",
            "extra": "mean: 6.6224689539492045 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39282.06921509123,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016043282053643448",
            "extra": "mean: 25.4569074384662 usec\nrounds: 39282"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3874.4864509905574,
            "unit": "iter/sec",
            "range": "stddev: 0.000010508864660977061",
            "extra": "mean: 258.09872163686066 usec\nrounds: 3887"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8104.696021025519,
            "unit": "iter/sec",
            "range": "stddev: 0.000005535977429995992",
            "extra": "mean: 123.38525681972044 usec\nrounds: 8212"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1254.759530698224,
            "unit": "iter/sec",
            "range": "stddev: 0.000027209576157065203",
            "extra": "mean: 796.9654547621087 usec\nrounds: 1260"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 431.3509551626111,
            "unit": "iter/sec",
            "range": "stddev: 0.00006066421528899763",
            "extra": "mean: 2.318297868664784 msec\nrounds: 434"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11547.782304680833,
            "unit": "iter/sec",
            "range": "stddev: 0.000004063434943752659",
            "extra": "mean: 86.59671386380873 usec\nrounds: 11173"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1049.627797022503,
            "unit": "iter/sec",
            "range": "stddev: 0.0000242303294539887",
            "extra": "mean: 952.7186711677388 usec\nrounds: 1037"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1059.6516812035745,
            "unit": "iter/sec",
            "range": "stddev: 0.00001516156066629649",
            "extra": "mean: 943.7063308050236 usec\nrounds: 1055"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1203.8192480218565,
            "unit": "iter/sec",
            "range": "stddev: 0.00001553561544207669",
            "extra": "mean: 830.6894923330251 usec\nrounds: 1174"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1626.0605751793814,
            "unit": "iter/sec",
            "range": "stddev: 0.000022374559707599566",
            "extra": "mean: 614.9832394095671 usec\nrounds: 1629"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 960.3476478187259,
            "unit": "iter/sec",
            "range": "stddev: 0.000018071395122625906",
            "extra": "mean: 1.0412895811962866 msec\nrounds: 936"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1029.588405371682,
            "unit": "iter/sec",
            "range": "stddev: 0.00002382104850851084",
            "extra": "mean: 971.2619089168932 usec\nrounds: 1043"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1031.8090430914638,
            "unit": "iter/sec",
            "range": "stddev: 0.00002675243482243295",
            "extra": "mean: 969.171579465752 usec\nrounds: 1013"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 746.490118480439,
            "unit": "iter/sec",
            "range": "stddev: 0.00009302988832596782",
            "extra": "mean: 1.3396024612296378 msec\nrounds: 748"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1097.198596782991,
            "unit": "iter/sec",
            "range": "stddev: 0.00001587901491101597",
            "extra": "mean: 911.4120296289301 usec\nrounds: 1080"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 933.1359271789445,
            "unit": "iter/sec",
            "range": "stddev: 0.00003011241363073725",
            "extra": "mean: 1.0716552335769547 msec\nrounds: 959"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1609.560653529996,
            "unit": "iter/sec",
            "range": "stddev: 0.000027472862933095213",
            "extra": "mean: 621.2875531014364 usec\nrounds: 1676"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3232.709783214692,
            "unit": "iter/sec",
            "range": "stddev: 0.00001481624886226884",
            "extra": "mean: 309.33800652082465 usec\nrounds: 3067"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3205.067596805434,
            "unit": "iter/sec",
            "range": "stddev: 0.000013347575344052065",
            "extra": "mean: 312.00589996813903 usec\nrounds: 3129"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4186.3210066719885,
            "unit": "iter/sec",
            "range": "stddev: 0.000016333946198724086",
            "extra": "mean: 238.87322505996093 usec\nrounds: 4190"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7644.598061758118,
            "unit": "iter/sec",
            "range": "stddev: 0.0000067100715299808",
            "extra": "mean: 130.81132479711016 usec\nrounds: 7380"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 647.7248063442181,
            "unit": "iter/sec",
            "range": "stddev: 0.00003146599127380194",
            "extra": "mean: 1.5438655277756548 msec\nrounds: 648"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4661.534365083932,
            "unit": "iter/sec",
            "range": "stddev: 0.00000793424740541223",
            "extra": "mean: 214.5216406619786 usec\nrounds: 4589"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 435.40513174104467,
            "unit": "iter/sec",
            "range": "stddev: 0.0001254215639701972",
            "extra": "mean: 2.296711561486018 msec\nrounds: 431"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 620.5187215220233,
            "unit": "iter/sec",
            "range": "stddev: 0.000020607989347729766",
            "extra": "mean: 1.6115549222224528 msec\nrounds: 630"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 8538.768808834679,
            "unit": "iter/sec",
            "range": "stddev: 0.00003501157837168969",
            "extra": "mean: 117.11290261956096 usec\nrounds: 9047"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 638.1701965393074,
            "unit": "iter/sec",
            "range": "stddev: 0.000026111209859718156",
            "extra": "mean: 1.5669801025225503 msec\nrounds: 634"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.42483503520052,
            "unit": "iter/sec",
            "range": "stddev: 0.0005813381222671832",
            "extra": "mean: 134.68312700000524 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 560.8511423732057,
            "unit": "iter/sec",
            "range": "stddev: 0.000045717837370700655",
            "extra": "mean: 1.7830043026542906 msec\nrounds: 565"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 366.89955501140435,
            "unit": "iter/sec",
            "range": "stddev: 0.00005814603082499651",
            "extra": "mean: 2.7255415994410708 msec\nrounds: 357"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 748625.3305623926,
            "unit": "iter/sec",
            "range": "stddev: 1.0534023723657356e-7",
            "extra": "mean: 1.3357816776634666 usec\nrounds: 71552"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 565.1926502676922,
            "unit": "iter/sec",
            "range": "stddev: 0.00004943847831290262",
            "extra": "mean: 1.7693082164574678 msec\nrounds: 559"
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
        "date": 1771214600867,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 401.92957765313855,
            "unit": "iter/sec",
            "range": "stddev: 0.007103485814614676",
            "extra": "mean: 2.4879980364694396 msec\nrounds: 521"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 440.05715015167607,
            "unit": "iter/sec",
            "range": "stddev: 0.00038072144572070084",
            "extra": "mean: 2.2724321140000256 msec\nrounds: 500"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6103.001621272763,
            "unit": "iter/sec",
            "range": "stddev: 0.00001672178006927208",
            "extra": "mean: 163.85379884455165 usec\nrounds: 5712"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4623.006686490661,
            "unit": "iter/sec",
            "range": "stddev: 0.00003590023908860107",
            "extra": "mean: 216.3094427101301 usec\nrounds: 4486"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 385.38844756129123,
            "unit": "iter/sec",
            "range": "stddev: 0.00038823532678017346",
            "extra": "mean: 2.5947845772957745 msec\nrounds: 414"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 312.57394275927146,
            "unit": "iter/sec",
            "range": "stddev: 0.001360955666791711",
            "extra": "mean: 3.199243005262755 msec\nrounds: 380"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 329.191387733361,
            "unit": "iter/sec",
            "range": "stddev: 0.0008759788841168758",
            "extra": "mean: 3.037746542780097 msec\nrounds: 374"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 320.81374599327654,
            "unit": "iter/sec",
            "range": "stddev: 0.0011738756712307231",
            "extra": "mean: 3.117073418733615 msec\nrounds: 363"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 285.8927802476631,
            "unit": "iter/sec",
            "range": "stddev: 0.0016440322679968156",
            "extra": "mean: 3.4978148071235675 msec\nrounds: 337"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 16982.172243107136,
            "unit": "iter/sec",
            "range": "stddev: 0.000019765023822994018",
            "extra": "mean: 58.88528191120475 usec\nrounds: 16452"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16022.840245185467,
            "unit": "iter/sec",
            "range": "stddev: 0.00001764549312319519",
            "extra": "mean: 62.41090747319154 usec\nrounds: 16406"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 12308.800869934263,
            "unit": "iter/sec",
            "range": "stddev: 0.00008455079782106902",
            "extra": "mean: 81.24268241617438 usec\nrounds: 13842"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6406.890853369341,
            "unit": "iter/sec",
            "range": "stddev: 0.00009477457542487254",
            "extra": "mean: 156.08194721689486 usec\nrounds: 5191"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 18201.60434867131,
            "unit": "iter/sec",
            "range": "stddev: 0.000015484113964653367",
            "extra": "mean: 54.94021190901221 usec\nrounds: 17248"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 52292.1646432676,
            "unit": "iter/sec",
            "range": "stddev: 0.00002520663455583486",
            "extra": "mean: 19.123323863563677 usec\nrounds: 41641"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 163977.32676373207,
            "unit": "iter/sec",
            "range": "stddev: 0.0004819030775391621",
            "extra": "mean: 6.098404088760742 usec\nrounds: 178540"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 163.52792924632288,
            "unit": "iter/sec",
            "range": "stddev: 0.000834148534540313",
            "extra": "mean: 6.1151633522717415 msec\nrounds: 176"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4083.766941997342,
            "unit": "iter/sec",
            "range": "stddev: 0.00007663576629793961",
            "extra": "mean: 244.87195626078184 usec\nrounds: 3498"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 233.22891455391553,
            "unit": "iter/sec",
            "range": "stddev: 0.0006273254525133049",
            "extra": "mean: 4.287633040322837 msec\nrounds: 248"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 155.03315516718018,
            "unit": "iter/sec",
            "range": "stddev: 0.0008946630874301453",
            "extra": "mean: 6.450233170586309 msec\nrounds: 170"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 170.39305615817034,
            "unit": "iter/sec",
            "range": "stddev: 0.0008520381997252315",
            "extra": "mean: 5.8687837553176605 msec\nrounds: 188"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 68.85912382701422,
            "unit": "iter/sec",
            "range": "stddev: 0.022784534804942073",
            "extra": "mean: 14.522403777779246 msec\nrounds: 90"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 124.81675663513825,
            "unit": "iter/sec",
            "range": "stddev: 0.0007511669054165792",
            "extra": "mean: 8.01174479259367 msec\nrounds: 135"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 53.75589942774622,
            "unit": "iter/sec",
            "range": "stddev: 0.029338754051333696",
            "extra": "mean: 18.602609399999135 msec\nrounds: 75"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 3.3071482397297167,
            "unit": "iter/sec",
            "range": "stddev: 0.2963216405900598",
            "extra": "mean: 302.37531780000495 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 769.19560448016,
            "unit": "iter/sec",
            "range": "stddev: 0.00019420427966858336",
            "extra": "mean: 1.3000594311453755 msec\nrounds: 777"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1608580.6624404723,
            "unit": "iter/sec",
            "range": "stddev: 7.566828410254285e-8",
            "extra": "mean: 621.6660583764829 nsec\nrounds: 160231"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 808575.9946857824,
            "unit": "iter/sec",
            "range": "stddev: 1.0577798183539329e-7",
            "extra": "mean: 1.2367421325544128 usec\nrounds: 81348"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23500.694015082114,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018470333813261245",
            "extra": "mean: 42.55193482193448 usec\nrounds: 23873"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1506.8808526885596,
            "unit": "iter/sec",
            "range": "stddev: 0.0000056920312127102295",
            "extra": "mean: 663.6224743421561 usec\nrounds: 1520"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 150.95541179757748,
            "unit": "iter/sec",
            "range": "stddev: 0.00002312553762266762",
            "extra": "mean: 6.624472671049001 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10170.77720146196,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027408133241439542",
            "extra": "mean: 98.32090313179398 usec\nrounds: 10282"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1413.378480808328,
            "unit": "iter/sec",
            "range": "stddev: 0.000005951985496989942",
            "extra": "mean: 707.5245686690291 usec\nrounds: 1398"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 67472.86947449263,
            "unit": "iter/sec",
            "range": "stddev: 0.000022821365666989378",
            "extra": "mean: 14.820771782620552 usec\nrounds: 60530"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1120051.9720944308,
            "unit": "iter/sec",
            "range": "stddev: 0.000001974520650225782",
            "extra": "mean: 892.8157129441586 nsec\nrounds: 114587"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5253.516937321983,
            "unit": "iter/sec",
            "range": "stddev: 0.00009386398268032888",
            "extra": "mean: 190.34867726337947 usec\nrounds: 4341"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 246.49609659188613,
            "unit": "iter/sec",
            "range": "stddev: 0.0002795081750877562",
            "extra": "mean: 4.056859373540752 msec\nrounds: 257"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 66963.08763286177,
            "unit": "iter/sec",
            "range": "stddev: 0.000025355875746143396",
            "extra": "mean: 14.93360051559593 usec\nrounds: 58956"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1982.5165784763794,
            "unit": "iter/sec",
            "range": "stddev: 0.001230000676905196",
            "extra": "mean: 504.4094010898656 usec\nrounds: 2937"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 341286.4488670077,
            "unit": "iter/sec",
            "range": "stddev: 0.000008325094350491926",
            "extra": "mean: 2.930089967884073 usec\nrounds: 174490"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3069.3767558462378,
            "unit": "iter/sec",
            "range": "stddev: 0.00002558312538490109",
            "extra": "mean: 325.79903985240696 usec\nrounds: 2710"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2586.722959721151,
            "unit": "iter/sec",
            "range": "stddev: 0.000028466468906779638",
            "extra": "mean: 386.58952488201527 usec\nrounds: 1688"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5011.161601380595,
            "unit": "iter/sec",
            "range": "stddev: 0.00004288877788599607",
            "extra": "mean: 199.554530375651 usec\nrounds: 4395"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1085.4631893742423,
            "unit": "iter/sec",
            "range": "stddev: 0.000056717915941892034",
            "extra": "mean: 921.2656954092464 usec\nrounds: 719"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 366684.844047958,
            "unit": "iter/sec",
            "range": "stddev: 5.479111038053922e-7",
            "extra": "mean: 2.7271375303125756 usec\nrounds: 112906"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 124545.18486415024,
            "unit": "iter/sec",
            "range": "stddev: 0.0000012725772139663106",
            "extra": "mean: 8.029214466145495 usec\nrounds: 80367"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 278921.1596849369,
            "unit": "iter/sec",
            "range": "stddev: 7.803441940579585e-7",
            "extra": "mean: 3.585242514872581 usec\nrounds: 159949"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 43.21676275197848,
            "unit": "iter/sec",
            "range": "stddev: 0.0006371182368245868",
            "extra": "mean: 23.13916953333622 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.545905658402268,
            "unit": "iter/sec",
            "range": "stddev: 0.004942401490975267",
            "extra": "mean: 219.97816830001398 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1379.418824414884,
            "unit": "iter/sec",
            "range": "stddev: 0.00038030725776951415",
            "extra": "mean: 724.942984901033 usec\nrounds: 1457"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 378.89699415693644,
            "unit": "iter/sec",
            "range": "stddev: 0.0004229748058611071",
            "extra": "mean: 2.639239728531093 msec\nrounds: 361"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 973012.7159937583,
            "unit": "iter/sec",
            "range": "stddev: 7.501388897234761e-8",
            "extra": "mean: 1.0277357978602355 usec\nrounds: 43078"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 478280.6443972855,
            "unit": "iter/sec",
            "range": "stddev: 1.434417578700848e-7",
            "extra": "mean: 2.090822640878912 usec\nrounds: 49097"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59713.162730884425,
            "unit": "iter/sec",
            "range": "stddev: 0.0000012467847529655668",
            "extra": "mean: 16.74672642122148 usec\nrounds: 60202"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3978.040531279982,
            "unit": "iter/sec",
            "range": "stddev: 0.000006913755415447914",
            "extra": "mean: 251.38004304803752 usec\nrounds: 4042"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 397.6533148396592,
            "unit": "iter/sec",
            "range": "stddev: 0.000021254895398003382",
            "extra": "mean: 2.5147533358378205 msec\nrounds: 399"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18404.474445341548,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026520813555646324",
            "extra": "mean: 54.33461319256064 usec\nrounds: 18389"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18469.695819163266,
            "unit": "iter/sec",
            "range": "stddev: 0.000002732688644439255",
            "extra": "mean: 54.14274332349579 usec\nrounds: 18498"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 17306.838421503366,
            "unit": "iter/sec",
            "range": "stddev: 0.000003051777330692993",
            "extra": "mean: 57.78062842243457 usec\nrounds: 18626"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1506.3510017768597,
            "unit": "iter/sec",
            "range": "stddev: 0.00000613235738065698",
            "extra": "mean: 663.8558999996822 usec\nrounds: 1520"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 150.98575905210743,
            "unit": "iter/sec",
            "range": "stddev: 0.000009926231473388123",
            "extra": "mean: 6.6231411907853195 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 37963.155071265326,
            "unit": "iter/sec",
            "range": "stddev: 0.000002694852500142448",
            "extra": "mean: 26.34133011660323 usec\nrounds: 30468"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3738.9247656370753,
            "unit": "iter/sec",
            "range": "stddev: 0.00001852090952547913",
            "extra": "mean: 267.4565717905292 usec\nrounds: 3949"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7624.798705380181,
            "unit": "iter/sec",
            "range": "stddev: 0.0000197034523501456",
            "extra": "mean: 131.15100327755326 usec\nrounds: 7628"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1224.0745919565209,
            "unit": "iter/sec",
            "range": "stddev: 0.000030180153936683603",
            "extra": "mean: 816.9436785724248 usec\nrounds: 1232"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 410.8301038232457,
            "unit": "iter/sec",
            "range": "stddev: 0.0001504813681164069",
            "extra": "mean: 2.4340962132371806 msec\nrounds: 408"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11321.180465436975,
            "unit": "iter/sec",
            "range": "stddev: 0.000006143384816466355",
            "extra": "mean: 88.33001143767228 usec\nrounds: 10754"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1021.193692710923,
            "unit": "iter/sec",
            "range": "stddev: 0.00003645211269240221",
            "extra": "mean: 979.2461578423375 usec\nrounds: 1001"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1024.6328736985138,
            "unit": "iter/sec",
            "range": "stddev: 0.00003263401397622686",
            "extra": "mean: 975.9593173996077 usec\nrounds: 1046"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1148.7704062944167,
            "unit": "iter/sec",
            "range": "stddev: 0.0000215262357294509",
            "extra": "mean: 870.4959620484091 usec\nrounds: 1054"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1561.121131071824,
            "unit": "iter/sec",
            "range": "stddev: 0.000024044571328329392",
            "extra": "mean: 640.5652835622221 usec\nrounds: 1594"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 965.8668649053794,
            "unit": "iter/sec",
            "range": "stddev: 0.00002954637408542642",
            "extra": "mean: 1.0353393788883776 msec\nrounds: 900"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 988.7546324942598,
            "unit": "iter/sec",
            "range": "stddev: 0.000036981287714432523",
            "extra": "mean: 1.0113732640396054 msec\nrounds: 1015"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 992.738478656367,
            "unit": "iter/sec",
            "range": "stddev: 0.00004074992440583691",
            "extra": "mean: 1.0073146367344008 msec\nrounds: 980"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 732.0452709271246,
            "unit": "iter/sec",
            "range": "stddev: 0.000048743727104028906",
            "extra": "mean: 1.3660357353767407 msec\nrounds: 718"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1059.6205293095811,
            "unit": "iter/sec",
            "range": "stddev: 0.000027952561380388044",
            "extra": "mean: 943.7340749254564 usec\nrounds: 1001"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 945.5949040415558,
            "unit": "iter/sec",
            "range": "stddev: 0.00001922116582987218",
            "extra": "mean: 1.0575353100211433 msec\nrounds: 958"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1541.3555155291738,
            "unit": "iter/sec",
            "range": "stddev: 0.000024839334719550434",
            "extra": "mean: 648.7795903832626 usec\nrounds: 1643"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3124.8148290800164,
            "unit": "iter/sec",
            "range": "stddev: 0.00001490617686055469",
            "extra": "mean: 320.0189626258309 usec\nrounds: 2863"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3072.0972445362913,
            "unit": "iter/sec",
            "range": "stddev: 0.000019344321524857106",
            "extra": "mean: 325.5105292576577 usec\nrounds: 3059"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4053.635579166842,
            "unit": "iter/sec",
            "range": "stddev: 0.000011913008427299258",
            "extra": "mean: 246.69213116723566 usec\nrounds: 4155"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7667.974446514133,
            "unit": "iter/sec",
            "range": "stddev: 0.000005426970282899122",
            "extra": "mean: 130.41253684075602 usec\nrounds: 7356"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 647.4563483910215,
            "unit": "iter/sec",
            "range": "stddev: 0.00003307264577981229",
            "extra": "mean: 1.5445056681969007 msec\nrounds: 654"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4617.38022485141,
            "unit": "iter/sec",
            "range": "stddev: 0.000007632103840873197",
            "extra": "mean: 216.57302437816472 usec\nrounds: 4143"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 435.7652077641676,
            "unit": "iter/sec",
            "range": "stddev: 0.00004443900358253873",
            "extra": "mean: 2.2948137716887014 msec\nrounds: 438"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 623.8346291151734,
            "unit": "iter/sec",
            "range": "stddev: 0.000051620168287173155",
            "extra": "mean: 1.6029889225905385 msec\nrounds: 633"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 8952.364834010452,
            "unit": "iter/sec",
            "range": "stddev: 0.0000038055112260379836",
            "extra": "mean: 111.70232877473373 usec\nrounds: 8848"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 636.2099519475183,
            "unit": "iter/sec",
            "range": "stddev: 0.000022865305937981863",
            "extra": "mean: 1.5718081695183717 msec\nrounds: 643"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.306003077448369,
            "unit": "iter/sec",
            "range": "stddev: 0.0007562511125080462",
            "extra": "mean: 136.87374469998872 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 538.7018754687673,
            "unit": "iter/sec",
            "range": "stddev: 0.00007511645631974667",
            "extra": "mean: 1.8563143095238357 msec\nrounds: 546"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 352.62213179537207,
            "unit": "iter/sec",
            "range": "stddev: 0.00007602704848396796",
            "extra": "mean: 2.8358968704219154 msec\nrounds: 355"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 760199.8610123991,
            "unit": "iter/sec",
            "range": "stddev: 1.1000407438362936e-7",
            "extra": "mean: 1.3154435448965305 usec\nrounds: 71912"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 539.659652503934,
            "unit": "iter/sec",
            "range": "stddev: 0.000060665206334045874",
            "extra": "mean: 1.8530197604363432 msec\nrounds: 551"
          }
        ]
      }
    ]
  }
}