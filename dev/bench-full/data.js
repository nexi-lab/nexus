window.BENCHMARK_DATA = {
  "lastUpdate": 1771226017015,
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
        "date": 1771216369006,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 334.50754989214204,
            "unit": "iter/sec",
            "range": "stddev: 0.00798229324910309",
            "extra": "mean: 2.9894691474749613 msec\nrounds: 495"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 389.12835175605545,
            "unit": "iter/sec",
            "range": "stddev: 0.0005474429642517043",
            "extra": "mean: 2.569846158695987 msec\nrounds: 460"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6026.2421895797825,
            "unit": "iter/sec",
            "range": "stddev: 0.00001727564482033729",
            "extra": "mean: 165.9408912786712 usec\nrounds: 5068"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4506.259854449456,
            "unit": "iter/sec",
            "range": "stddev: 0.000039626791356512685",
            "extra": "mean: 221.91352303232256 usec\nrounds: 4841"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 326.574338123374,
            "unit": "iter/sec",
            "range": "stddev: 0.0006051579913755114",
            "extra": "mean: 3.0620899539945414 msec\nrounds: 413"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 283.7929675830548,
            "unit": "iter/sec",
            "range": "stddev: 0.0022040372491072143",
            "extra": "mean: 3.5236954901193602 msec\nrounds: 253"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 313.68117998174625,
            "unit": "iter/sec",
            "range": "stddev: 0.0007035467884844283",
            "extra": "mean: 3.187950262295596 msec\nrounds: 366"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 309.3461081447134,
            "unit": "iter/sec",
            "range": "stddev: 0.0006347105123877321",
            "extra": "mean: 3.2326251201201335 msec\nrounds: 333"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 275.2637044099229,
            "unit": "iter/sec",
            "range": "stddev: 0.0018938448891997195",
            "extra": "mean: 3.632879976470851 msec\nrounds: 340"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 16022.597759451382,
            "unit": "iter/sec",
            "range": "stddev: 0.000017376500126525223",
            "extra": "mean: 62.41185199885091 usec\nrounds: 14858"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15635.113818756417,
            "unit": "iter/sec",
            "range": "stddev: 0.000016869472902725474",
            "extra": "mean: 63.958600595562395 usec\nrounds: 16790"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 13693.716089481166,
            "unit": "iter/sec",
            "range": "stddev: 0.00005838796426829093",
            "extra": "mean: 73.02619635645509 usec\nrounds: 12625"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6179.206821591641,
            "unit": "iter/sec",
            "range": "stddev: 0.00008857792591022893",
            "extra": "mean: 161.8330683649815 usec\nrounds: 6085"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 17142.28392211599,
            "unit": "iter/sec",
            "range": "stddev: 0.000014060200685646904",
            "extra": "mean: 58.33528394135729 usec\nrounds: 16278"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53844.37376982131,
            "unit": "iter/sec",
            "range": "stddev: 0.000014506617058422221",
            "extra": "mean: 18.57204253641965 usec\nrounds: 45702"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 217765.24942123666,
            "unit": "iter/sec",
            "range": "stddev: 0.000012734454031168482",
            "extra": "mean: 4.592100909845532 usec\nrounds: 190477"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 151.35799625218982,
            "unit": "iter/sec",
            "range": "stddev: 0.0008280605869788266",
            "extra": "mean: 6.606852791139089 msec\nrounds: 158"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4321.821787590942,
            "unit": "iter/sec",
            "range": "stddev: 0.0000760358884863544",
            "extra": "mean: 231.38390455415268 usec\nrounds: 4128"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 245.60961526594335,
            "unit": "iter/sec",
            "range": "stddev: 0.0002684827842483177",
            "extra": "mean: 4.071501838057974 msec\nrounds: 247"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 172.72135606024347,
            "unit": "iter/sec",
            "range": "stddev: 0.00028917152545957213",
            "extra": "mean: 5.78967200588218 msec\nrounds: 170"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 182.68693458852826,
            "unit": "iter/sec",
            "range": "stddev: 0.0004897780095371705",
            "extra": "mean: 5.473845199999292 msec\nrounds: 190"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 91.6925799558608,
            "unit": "iter/sec",
            "range": "stddev: 0.0006995289136268861",
            "extra": "mean: 10.906007885058775 msec\nrounds: 87"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 127.18341578741791,
            "unit": "iter/sec",
            "range": "stddev: 0.0007306709566805062",
            "extra": "mean: 7.8626603461528415 msec\nrounds: 130"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 56.10864534329059,
            "unit": "iter/sec",
            "range": "stddev: 0.027966532321707063",
            "extra": "mean: 17.822565379749967 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 5.1195417818427815,
            "unit": "iter/sec",
            "range": "stddev: 0.014423467731820076",
            "extra": "mean: 195.32998120000684 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 775.1596760794573,
            "unit": "iter/sec",
            "range": "stddev: 0.00016253091359501613",
            "extra": "mean: 1.2900567855357528 msec\nrounds: 802"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1657401.1369645072,
            "unit": "iter/sec",
            "range": "stddev: 8.534450027162526e-8",
            "extra": "mean: 603.354237967688 nsec\nrounds: 154036"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 815652.5206474832,
            "unit": "iter/sec",
            "range": "stddev: 1.0890453015308512e-7",
            "extra": "mean: 1.2260122719981024 usec\nrounds: 81747"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23715.04504624813,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017997379848096416",
            "extra": "mean: 42.167324500115434 usec\nrounds: 24000"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1505.9149459527007,
            "unit": "iter/sec",
            "range": "stddev: 0.000005662231515956305",
            "extra": "mean: 664.0481274773197 usec\nrounds: 1514"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 149.89913129669577,
            "unit": "iter/sec",
            "range": "stddev: 0.0002401259313227034",
            "extra": "mean: 6.671152736840731 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10198.728568751972,
            "unit": "iter/sec",
            "range": "stddev: 0.000002528950871135446",
            "extra": "mean: 98.05143780999468 usec\nrounds: 10283"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1436.7460252445633,
            "unit": "iter/sec",
            "range": "stddev: 0.000005826499518248132",
            "extra": "mean: 696.0172378620499 usec\nrounds: 1421"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 70559.50411150453,
            "unit": "iter/sec",
            "range": "stddev: 0.000013387407350515175",
            "extra": "mean: 14.172435203338578 usec\nrounds: 59949"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1157604.5591493258,
            "unit": "iter/sec",
            "range": "stddev: 0.0000010606532592715075",
            "extra": "mean: 863.8528520783101 nsec\nrounds: 114601"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5480.573268771141,
            "unit": "iter/sec",
            "range": "stddev: 0.000046329888011545946",
            "extra": "mean: 182.46266420669912 usec\nrounds: 5149"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 251.88151437986562,
            "unit": "iter/sec",
            "range": "stddev: 0.00038026097386060196",
            "extra": "mean: 3.970120643676485 msec\nrounds: 261"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 68130.14148528822,
            "unit": "iter/sec",
            "range": "stddev: 0.00002156726020995788",
            "extra": "mean: 14.677791329934879 usec\nrounds: 60162"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1615.900919545546,
            "unit": "iter/sec",
            "range": "stddev: 0.0007485001856342922",
            "extra": "mean: 618.8498242090479 usec\nrounds: 2594"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 352702.63931942586,
            "unit": "iter/sec",
            "range": "stddev: 0.000006371834710236315",
            "extra": "mean: 2.8352495516608482 usec\nrounds: 170620"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3081.578836215408,
            "unit": "iter/sec",
            "range": "stddev: 0.000027781113836019533",
            "extra": "mean: 324.5089784002197 usec\nrounds: 2639"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2592.0893329422147,
            "unit": "iter/sec",
            "range": "stddev: 0.000030990520832883495",
            "extra": "mean: 385.7891729622317 usec\nrounds: 1694"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4963.365755068173,
            "unit": "iter/sec",
            "range": "stddev: 0.00003080107964974831",
            "extra": "mean: 201.47618558613854 usec\nrounds: 2678"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1082.3580864422909,
            "unit": "iter/sec",
            "range": "stddev: 0.00006478842991988018",
            "extra": "mean: 923.9086514214517 usec\nrounds: 809"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 363918.4982893597,
            "unit": "iter/sec",
            "range": "stddev: 6.69224566958989e-7",
            "extra": "mean: 2.7478680108337823 usec\nrounds: 114195"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 127596.02924671867,
            "unit": "iter/sec",
            "range": "stddev: 0.000001483239284723646",
            "extra": "mean: 7.8372344805997685 usec\nrounds: 80109"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 277314.0431431552,
            "unit": "iter/sec",
            "range": "stddev: 8.199773955307851e-7",
            "extra": "mean: 3.6060200510068627 usec\nrounds: 156202"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 36.75141401110369,
            "unit": "iter/sec",
            "range": "stddev: 0.002157632722110379",
            "extra": "mean: 27.20983741463309 msec\nrounds: 41"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.202325388439265,
            "unit": "iter/sec",
            "range": "stddev: 0.004451529853583866",
            "extra": "mean: 237.96348629999784 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1433.8907818149673,
            "unit": "iter/sec",
            "range": "stddev: 0.00027348271456574704",
            "extra": "mean: 697.4031862693447 usec\nrounds: 1573"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 392.8991188176061,
            "unit": "iter/sec",
            "range": "stddev: 0.0003943359282539648",
            "extra": "mean: 2.545182598040455 msec\nrounds: 408"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 965417.001009852,
            "unit": "iter/sec",
            "range": "stddev: 1.0220305260374086e-7",
            "extra": "mean: 1.0358218251325313 usec\nrounds: 96256"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 482738.61088375567,
            "unit": "iter/sec",
            "range": "stddev: 1.4081175653422698e-7",
            "extra": "mean: 2.0715144333892983 usec\nrounds: 48762"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59735.67237820704,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013285779687617761",
            "extra": "mean: 16.74041590540166 usec\nrounds: 60093"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3951.8814176371534,
            "unit": "iter/sec",
            "range": "stddev: 0.000006585664884798386",
            "extra": "mean: 253.0440299997423 usec\nrounds: 4000"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 387.7310554143051,
            "unit": "iter/sec",
            "range": "stddev: 0.000059922349515336635",
            "extra": "mean: 2.5791073117201373 msec\nrounds: 401"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18420.88880951524,
            "unit": "iter/sec",
            "range": "stddev: 0.0000029342502029591937",
            "extra": "mean: 54.286197063599545 usec\nrounds: 17573"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18343.295816750295,
            "unit": "iter/sec",
            "range": "stddev: 0.0000030374067040751997",
            "extra": "mean: 54.51583019703819 usec\nrounds: 18174"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18000.281186520766,
            "unit": "iter/sec",
            "range": "stddev: 0.0000031432001727339503",
            "extra": "mean: 55.554687709480596 usec\nrounds: 18502"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1505.9565016840781,
            "unit": "iter/sec",
            "range": "stddev: 0.000005885024050371503",
            "extra": "mean: 664.0298035711669 usec\nrounds: 1512"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 150.86467587756601,
            "unit": "iter/sec",
            "range": "stddev: 0.000020110237646093975",
            "extra": "mean: 6.628456888155505 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39695.68796600332,
            "unit": "iter/sec",
            "range": "stddev: 0.0000019025964379549417",
            "extra": "mean: 25.191653079710633 usec\nrounds: 39989"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3906.2599047881704,
            "unit": "iter/sec",
            "range": "stddev: 0.000009087805418539628",
            "extra": "mean: 255.99935088144838 usec\nrounds: 3970"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8177.695943449843,
            "unit": "iter/sec",
            "range": "stddev: 0.000005097213205073804",
            "extra": "mean: 122.28383237958097 usec\nrounds: 8215"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1252.657571569982,
            "unit": "iter/sec",
            "range": "stddev: 0.00003451791597758366",
            "extra": "mean: 798.3027626190604 usec\nrounds: 1268"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 430.18849046285413,
            "unit": "iter/sec",
            "range": "stddev: 0.000026774063137662892",
            "extra": "mean: 2.3245624236112565 msec\nrounds: 432"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11355.14035647124,
            "unit": "iter/sec",
            "range": "stddev: 0.000004965802157712301",
            "extra": "mean: 88.06584230639693 usec\nrounds: 10457"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 988.4314106318504,
            "unit": "iter/sec",
            "range": "stddev: 0.000020442007302214638",
            "extra": "mean: 1.011703987999283 msec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 979.8896381437864,
            "unit": "iter/sec",
            "range": "stddev: 0.00008191437841869456",
            "extra": "mean: 1.0205230885942511 msec\nrounds: 982"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1176.2187876315443,
            "unit": "iter/sec",
            "range": "stddev: 0.000019131492133917142",
            "extra": "mean: 850.1819648822463 usec\nrounds: 1139"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1572.824878040363,
            "unit": "iter/sec",
            "range": "stddev: 0.000015611663883852796",
            "extra": "mean: 635.7986918708551 usec\nrounds: 1587"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 934.3103625783266,
            "unit": "iter/sec",
            "range": "stddev: 0.00005186707573758405",
            "extra": "mean: 1.0703081546054953 msec\nrounds: 912"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1022.9256402893817,
            "unit": "iter/sec",
            "range": "stddev: 0.000027487070438880954",
            "extra": "mean: 977.5881653695803 usec\nrounds: 1028"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1021.5600736240823,
            "unit": "iter/sec",
            "range": "stddev: 0.00002966124478481253",
            "extra": "mean: 978.8949527484998 usec\nrounds: 1037"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 720.5767756932772,
            "unit": "iter/sec",
            "range": "stddev: 0.00002631897544331964",
            "extra": "mean: 1.387777172027069 msec\nrounds: 715"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1086.0966929676129,
            "unit": "iter/sec",
            "range": "stddev: 0.000021047850490402707",
            "extra": "mean: 920.7283352163008 usec\nrounds: 1062"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 965.1666320285824,
            "unit": "iter/sec",
            "range": "stddev: 0.000016089149506266394",
            "extra": "mean: 1.0360905224191235 msec\nrounds: 959"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1647.6960437245464,
            "unit": "iter/sec",
            "range": "stddev: 0.000009470424614912415",
            "extra": "mean: 606.9080543153716 usec\nrounds: 1657"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3039.874695948344,
            "unit": "iter/sec",
            "range": "stddev: 0.000015659024756277408",
            "extra": "mean: 328.9609276766692 usec\nrounds: 2876"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3006.069582373343,
            "unit": "iter/sec",
            "range": "stddev: 0.000015834214206286628",
            "extra": "mean: 332.6602969750564 usec\nrounds: 3108"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 3989.3319982029793,
            "unit": "iter/sec",
            "range": "stddev: 0.000017698096570604695",
            "extra": "mean: 250.66853309036614 usec\nrounds: 4110"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7619.23225471468,
            "unit": "iter/sec",
            "range": "stddev: 0.000006524818933640753",
            "extra": "mean: 131.24681943921755 usec\nrounds: 7665"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 495.41831096863126,
            "unit": "iter/sec",
            "range": "stddev: 0.011426483155283135",
            "extra": "mean: 2.0184962442038556 msec\nrounds: 647"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4503.088855514989,
            "unit": "iter/sec",
            "range": "stddev: 0.000014544389240691453",
            "extra": "mean: 222.0697907782316 usec\nrounds: 4359"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 411.38503705968867,
            "unit": "iter/sec",
            "range": "stddev: 0.00004163557718275368",
            "extra": "mean: 2.4308127664227808 msec\nrounds: 411"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 590.3612556109306,
            "unit": "iter/sec",
            "range": "stddev: 0.00003786654042012313",
            "extra": "mean: 1.693878096666689 msec\nrounds: 600"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 8786.27726003561,
            "unit": "iter/sec",
            "range": "stddev: 0.000005930217212616314",
            "extra": "mean: 113.81384520478325 usec\nrounds: 8915"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 506.8743911988781,
            "unit": "iter/sec",
            "range": "stddev: 0.008080039120222066",
            "extra": "mean: 1.9728753658963967 msec\nrounds: 604"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.243485479884876,
            "unit": "iter/sec",
            "range": "stddev: 0.0007682322417984378",
            "extra": "mean: 138.05508449999593 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 552.4322951076622,
            "unit": "iter/sec",
            "range": "stddev: 0.00006041145890684131",
            "extra": "mean: 1.810176575221969 msec\nrounds: 565"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 362.23847573790107,
            "unit": "iter/sec",
            "range": "stddev: 0.000059110512268082935",
            "extra": "mean: 2.760612323036478 msec\nrounds: 356"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 754501.7564724368,
            "unit": "iter/sec",
            "range": "stddev: 1.1157774033246022e-7",
            "extra": "mean: 1.3253779615773917 usec\nrounds: 73390"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 553.7254583083444,
            "unit": "iter/sec",
            "range": "stddev: 0.00006161949079208282",
            "extra": "mean: 1.8059491124988978 msec\nrounds: 560"
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
        "date": 1771219235795,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 374.6495754177534,
            "unit": "iter/sec",
            "range": "stddev: 0.006362976320182487",
            "extra": "mean: 2.6691609055874386 msec\nrounds: 519"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 415.3319550406242,
            "unit": "iter/sec",
            "range": "stddev: 0.0004967541757745969",
            "extra": "mean: 2.407712644942498 msec\nrounds: 445"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6445.5972621266465,
            "unit": "iter/sec",
            "range": "stddev: 0.00002060395371185502",
            "extra": "mean: 155.14466066253448 usec\nrounds: 6274"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4570.431800530515,
            "unit": "iter/sec",
            "range": "stddev: 0.000035070665084719416",
            "extra": "mean: 218.7977074472317 usec\nrounds: 4700"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 359.60126807486233,
            "unit": "iter/sec",
            "range": "stddev: 0.0007269515075766786",
            "extra": "mean: 2.780857824427412 msec\nrounds: 393"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 315.40631035159186,
            "unit": "iter/sec",
            "range": "stddev: 0.0011593581868280759",
            "extra": "mean: 3.1705136111109296 msec\nrounds: 360"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 319.9929700868002,
            "unit": "iter/sec",
            "range": "stddev: 0.0006374140997375803",
            "extra": "mean: 3.125068653004294 msec\nrounds: 366"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 324.2537698329271,
            "unit": "iter/sec",
            "range": "stddev: 0.0006222264242996652",
            "extra": "mean: 3.0840042369137404 msec\nrounds: 363"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 300.7727080359959,
            "unit": "iter/sec",
            "range": "stddev: 0.0007603231814303186",
            "extra": "mean: 3.324769745665627 msec\nrounds: 346"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 13141.11907063629,
            "unit": "iter/sec",
            "range": "stddev: 0.000026942536552941885",
            "extra": "mean: 76.09701994364322 usec\nrounds: 16597"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14123.354827586982,
            "unit": "iter/sec",
            "range": "stddev: 0.000018389104601643312",
            "extra": "mean: 70.8047069699553 usec\nrounds: 17145"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 12890.052640377182,
            "unit": "iter/sec",
            "range": "stddev: 0.00006376602050855816",
            "extra": "mean: 77.57920218786154 usec\nrounds: 9966"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6414.449217377009,
            "unit": "iter/sec",
            "range": "stddev: 0.00009694875421018163",
            "extra": "mean: 155.89803054188326 usec\nrounds: 5828"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16762.840902936412,
            "unit": "iter/sec",
            "range": "stddev: 0.000013771820749734988",
            "extra": "mean: 59.65575917533322 usec\nrounds: 17847"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54596.04647429809,
            "unit": "iter/sec",
            "range": "stddev: 0.000021187358676667345",
            "extra": "mean: 18.3163445813016 usec\nrounds: 46883"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 215546.73232806602,
            "unit": "iter/sec",
            "range": "stddev: 0.000008715638341203867",
            "extra": "mean: 4.639365158540107 usec\nrounds: 184843"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 154.0815049911338,
            "unit": "iter/sec",
            "range": "stddev: 0.0012917827904580321",
            "extra": "mean: 6.490071602412906 msec\nrounds: 166"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4280.561963709486,
            "unit": "iter/sec",
            "range": "stddev: 0.00005564388750965069",
            "extra": "mean: 233.6141862862818 usec\nrounds: 4171"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 243.15955692456933,
            "unit": "iter/sec",
            "range": "stddev: 0.00030020235509445304",
            "extra": "mean: 4.112525999996828 msec\nrounds: 242"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 169.56629593027557,
            "unit": "iter/sec",
            "range": "stddev: 0.0003334061629541343",
            "extra": "mean: 5.897398386358529 msec\nrounds: 176"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.82360885463532,
            "unit": "iter/sec",
            "range": "stddev: 0.0004307256172995642",
            "extra": "mean: 5.410564192513441 msec\nrounds: 187"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 91.05771836680776,
            "unit": "iter/sec",
            "range": "stddev: 0.000631936703835269",
            "extra": "mean: 10.982045431576712 msec\nrounds: 95"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 129.15189731633362,
            "unit": "iter/sec",
            "range": "stddev: 0.0005592574700354034",
            "extra": "mean: 7.742820823999864 msec\nrounds: 125"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 57.59244453374753,
            "unit": "iter/sec",
            "range": "stddev: 0.025862239862467905",
            "extra": "mean: 17.363388689188707 msec\nrounds: 74"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 5.024312653957064,
            "unit": "iter/sec",
            "range": "stddev: 0.01321699511543871",
            "extra": "mean: 199.03219979998994 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 779.53689655306,
            "unit": "iter/sec",
            "range": "stddev: 0.00015390373027428277",
            "extra": "mean: 1.2828129167737656 msec\nrounds: 781"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1645517.5511636205,
            "unit": "iter/sec",
            "range": "stddev: 8.15855694268078e-8",
            "extra": "mean: 607.7115368917545 nsec\nrounds: 166362"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 813075.5999908294,
            "unit": "iter/sec",
            "range": "stddev: 1.0717510708933676e-7",
            "extra": "mean: 1.229897933244189 usec\nrounds: 81820"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23728.455870812955,
            "unit": "iter/sec",
            "range": "stddev: 0.000001807380726478947",
            "extra": "mean: 42.14349241452512 usec\nrounds: 23862"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1506.1490247754875,
            "unit": "iter/sec",
            "range": "stddev: 0.000005767211939819467",
            "extra": "mean: 663.9449241412641 usec\nrounds: 1516"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 150.90517660582785,
            "unit": "iter/sec",
            "range": "stddev: 0.000011498889002378337",
            "extra": "mean: 6.626677907889482 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10193.5582908514,
            "unit": "iter/sec",
            "range": "stddev: 0.000002598832502587664",
            "extra": "mean: 98.1011705105457 usec\nrounds: 10275"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1433.9254014380801,
            "unit": "iter/sec",
            "range": "stddev: 0.0000060424711216636045",
            "extra": "mean: 697.3863486880855 usec\nrounds: 1411"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 70245.47801969454,
            "unit": "iter/sec",
            "range": "stddev: 0.000017446153958077615",
            "extra": "mean: 14.235791800286885 usec\nrounds: 64025"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1118720.524001259,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017810555724057006",
            "extra": "mean: 893.8782998486176 nsec\nrounds: 116741"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5346.988850136845,
            "unit": "iter/sec",
            "range": "stddev: 0.000059099021488706015",
            "extra": "mean: 187.02114929123277 usec\nrounds: 5218"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 258.1250535797413,
            "unit": "iter/sec",
            "range": "stddev: 0.0003652175237982802",
            "extra": "mean: 3.874091205529087 msec\nrounds: 253"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 70467.9829456217,
            "unit": "iter/sec",
            "range": "stddev: 0.000015123369861214715",
            "extra": "mean: 14.190841829142093 usec\nrounds: 65151"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1960.136498259615,
            "unit": "iter/sec",
            "range": "stddev: 0.0009468551873849004",
            "extra": "mean: 510.16855249003805 usec\nrounds: 3153"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 352544.56174368033,
            "unit": "iter/sec",
            "range": "stddev: 0.000007287573752566083",
            "extra": "mean: 2.8365208501700168 usec\nrounds: 177936"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3069.189578724324,
            "unit": "iter/sec",
            "range": "stddev: 0.000027082084738470624",
            "extra": "mean: 325.8189089823638 usec\nrounds: 2505"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2596.294737311813,
            "unit": "iter/sec",
            "range": "stddev: 0.00003025306479252624",
            "extra": "mean: 385.16428263279295 usec\nrounds: 1670"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5078.052181454689,
            "unit": "iter/sec",
            "range": "stddev: 0.00003755711926506535",
            "extra": "mean: 196.9259007719637 usec\nrounds: 4797"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1105.8551812312091,
            "unit": "iter/sec",
            "range": "stddev: 0.00005535493828672849",
            "extra": "mean: 904.2775373956698 usec\nrounds: 722"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 364014.2147607691,
            "unit": "iter/sec",
            "range": "stddev: 5.550776991758046e-7",
            "extra": "mean: 2.747145466990079 usec\nrounds: 112651"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 127375.14732007777,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014335797678295653",
            "extra": "mean: 7.850825070978135 usec\nrounds: 76894"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 278937.14789283887,
            "unit": "iter/sec",
            "range": "stddev: 7.820512643768189e-7",
            "extra": "mean: 3.5850370148051294 usec\nrounds: 171175"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.44728106382604,
            "unit": "iter/sec",
            "range": "stddev: 0.0014001061079072486",
            "extra": "mean: 24.12703497872555 msec\nrounds: 47"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.443668771448617,
            "unit": "iter/sec",
            "range": "stddev: 0.0041513039795919825",
            "extra": "mean: 225.03927529999146 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1449.337971347554,
            "unit": "iter/sec",
            "range": "stddev: 0.0002256501439394957",
            "extra": "mean: 689.9701931290931 usec\nrounds: 1543"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 391.99484345863067,
            "unit": "iter/sec",
            "range": "stddev: 0.0005410024418968461",
            "extra": "mean: 2.5510539658553837 msec\nrounds: 410"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 963333.9997856686,
            "unit": "iter/sec",
            "range": "stddev: 1.1305892864607488e-7",
            "extra": "mean: 1.0380615655862757 usec\nrounds: 97561"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 474798.0373373115,
            "unit": "iter/sec",
            "range": "stddev: 1.7977199211412803e-7",
            "extra": "mean: 2.1061586640249073 usec\nrounds: 48384"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59594.268106442396,
            "unit": "iter/sec",
            "range": "stddev: 0.0000012734498672787381",
            "extra": "mean: 16.78013728122111 usec\nrounds: 59877"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3941.82783892965,
            "unit": "iter/sec",
            "range": "stddev: 0.000006600216788638616",
            "extra": "mean: 253.68941538338126 usec\nrounds: 4030"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 374.2598683500682,
            "unit": "iter/sec",
            "range": "stddev: 0.0000733243172318727",
            "extra": "mean: 2.6719402334226197 msec\nrounds: 377"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18474.431452274086,
            "unit": "iter/sec",
            "range": "stddev: 0.0000028155097443061547",
            "extra": "mean: 54.12886467350022 usec\nrounds: 17698"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18422.90063933167,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027486798361126424",
            "extra": "mean: 54.280268866297114 usec\nrounds: 18392"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18243.970340799628,
            "unit": "iter/sec",
            "range": "stddev: 0.0000034769983610610505",
            "extra": "mean: 54.812630218087186 usec\nrounds: 18419"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1505.9926231323602,
            "unit": "iter/sec",
            "range": "stddev: 0.0000053820107855965805",
            "extra": "mean: 664.0138767214339 usec\nrounds: 1525"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 150.82355380769476,
            "unit": "iter/sec",
            "range": "stddev: 0.00001551580238797795",
            "extra": "mean: 6.630264138153345 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39698.67484866163,
            "unit": "iter/sec",
            "range": "stddev: 0.000001917609661297485",
            "extra": "mean: 25.18975768869306 usec\nrounds: 39767"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3917.6878985089984,
            "unit": "iter/sec",
            "range": "stddev: 0.000008892553248379976",
            "extra": "mean: 255.25259436326766 usec\nrounds: 3264"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7954.720643278374,
            "unit": "iter/sec",
            "range": "stddev: 0.000005862233609416748",
            "extra": "mean: 125.71151707822521 usec\nrounds: 8051"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1239.0936348767882,
            "unit": "iter/sec",
            "range": "stddev: 0.00004155070258412479",
            "extra": "mean: 807.0415115153401 usec\nrounds: 1259"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 431.4050669950382,
            "unit": "iter/sec",
            "range": "stddev: 0.000019564593467863836",
            "extra": "mean: 2.318007080828982 msec\nrounds: 433"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11595.182369098457,
            "unit": "iter/sec",
            "range": "stddev: 0.000005074577544541513",
            "extra": "mean: 86.24271427287191 usec\nrounds: 10531"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 987.754356479663,
            "unit": "iter/sec",
            "range": "stddev: 0.00008220867786534695",
            "extra": "mean: 1.01239745837617 msec\nrounds: 973"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 992.7069231052785,
            "unit": "iter/sec",
            "range": "stddev: 0.000022363817036875665",
            "extra": "mean: 1.0073466566264169 msec\nrounds: 996"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1173.2791993179967,
            "unit": "iter/sec",
            "range": "stddev: 0.00002778273151881685",
            "extra": "mean: 852.3120503468225 usec\nrounds: 1152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1583.2601703143366,
            "unit": "iter/sec",
            "range": "stddev: 0.000015002557778833708",
            "extra": "mean: 631.6081328575723 usec\nrounds: 1543"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 939.7590888483644,
            "unit": "iter/sec",
            "range": "stddev: 0.0000437083128343136",
            "extra": "mean: 1.0641025044253185 msec\nrounds: 904"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1018.7996315644289,
            "unit": "iter/sec",
            "range": "stddev: 0.00002760117978365742",
            "extra": "mean: 981.5472729062919 usec\nrounds: 1015"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1020.7117405372719,
            "unit": "iter/sec",
            "range": "stddev: 0.00003565854047042116",
            "extra": "mean: 979.7085311016704 usec\nrounds: 1045"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 734.8639231552869,
            "unit": "iter/sec",
            "range": "stddev: 0.00003046739332382197",
            "extra": "mean: 1.3607961535331572 msec\nrounds: 736"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1091.3951606889193,
            "unit": "iter/sec",
            "range": "stddev: 0.000017258792857992818",
            "extra": "mean: 916.258414934488 usec\nrounds: 1058"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 909.4840707864967,
            "unit": "iter/sec",
            "range": "stddev: 0.00001557473560794183",
            "extra": "mean: 1.0995244800002133 msec\nrounds: 925"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1576.8012841562913,
            "unit": "iter/sec",
            "range": "stddev: 0.000015008715824813003",
            "extra": "mean: 634.1953231824492 usec\nrounds: 1609"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3081.53657381467,
            "unit": "iter/sec",
            "range": "stddev: 0.00001527595122655538",
            "extra": "mean: 324.5134289488858 usec\nrounds: 3026"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3041.475445445663,
            "unit": "iter/sec",
            "range": "stddev: 0.000021289402289433427",
            "extra": "mean: 328.7877932723115 usec\nrounds: 3062"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4016.0326871883544,
            "unit": "iter/sec",
            "range": "stddev: 0.000012819300665014975",
            "extra": "mean: 249.00195737702157 usec\nrounds: 3918"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7610.498166332533,
            "unit": "iter/sec",
            "range": "stddev: 0.0000075342207036721385",
            "extra": "mean: 131.39744312978343 usec\nrounds: 7517"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 643.1971191252725,
            "unit": "iter/sec",
            "range": "stddev: 0.00004783180996408882",
            "extra": "mean: 1.5547333317661125 msec\nrounds: 639"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4726.886854309788,
            "unit": "iter/sec",
            "range": "stddev: 0.000008416498200239227",
            "extra": "mean: 211.55573019232304 usec\nrounds: 4607"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 447.0876601808953,
            "unit": "iter/sec",
            "range": "stddev: 0.00003997152475561555",
            "extra": "mean: 2.236697831461937 msec\nrounds: 445"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 617.4209631597154,
            "unit": "iter/sec",
            "range": "stddev: 0.000034931703093725576",
            "extra": "mean: 1.6196405040774726 msec\nrounds: 613"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 9087.09975062299,
            "unit": "iter/sec",
            "range": "stddev: 0.000004256354195037517",
            "extra": "mean: 110.04611233979712 usec\nrounds: 9133"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 640.1468776017109,
            "unit": "iter/sec",
            "range": "stddev: 0.000032766100037109263",
            "extra": "mean: 1.5621414943808942 msec\nrounds: 623"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.370751643956445,
            "unit": "iter/sec",
            "range": "stddev: 0.00030350463804173356",
            "extra": "mean: 135.67137359999606 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 560.7290005472199,
            "unit": "iter/sec",
            "range": "stddev: 0.00006123008063229344",
            "extra": "mean: 1.7833926888462914 msec\nrounds: 556"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 361.7998033214041,
            "unit": "iter/sec",
            "range": "stddev: 0.00007395963590737236",
            "extra": "mean: 2.763959490358407 msec\nrounds: 363"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 758391.4268106265,
            "unit": "iter/sec",
            "range": "stddev: 1.1596397931373045e-7",
            "extra": "mean: 1.3185803064855903 usec\nrounds: 71912"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 554.5850600127543,
            "unit": "iter/sec",
            "range": "stddev: 0.000049444934012217845",
            "extra": "mean: 1.8031499081078781 msec\nrounds: 555"
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
        "date": 1771219630999,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 390.4857134192638,
            "unit": "iter/sec",
            "range": "stddev: 0.006354719312373985",
            "extra": "mean: 2.560913154142215 msec\nrounds: 519"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 430.29596411608696,
            "unit": "iter/sec",
            "range": "stddev: 0.0005349724563496631",
            "extra": "mean: 2.323981825054292 msec\nrounds: 463"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6047.764469289534,
            "unit": "iter/sec",
            "range": "stddev: 0.00003631642679546663",
            "extra": "mean: 165.3503546770028 usec\nrounds: 6169"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4434.371576290663,
            "unit": "iter/sec",
            "range": "stddev: 0.00003659224970929565",
            "extra": "mean: 225.51109729881875 usec\nrounds: 4368"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 384.44473975763776,
            "unit": "iter/sec",
            "range": "stddev: 0.0005446395992299665",
            "extra": "mean: 2.601154071272822 msec\nrounds: 463"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 324.99121020221065,
            "unit": "iter/sec",
            "range": "stddev: 0.0009700116965824187",
            "extra": "mean: 3.077006296194277 msec\nrounds: 368"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 319.63852943420176,
            "unit": "iter/sec",
            "range": "stddev: 0.0009938846790186475",
            "extra": "mean: 3.128533977959788 msec\nrounds: 363"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 324.2608809768847,
            "unit": "iter/sec",
            "range": "stddev: 0.0008278923378954914",
            "extra": "mean: 3.083936603722748 msec\nrounds: 376"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 304.8483566987322,
            "unit": "iter/sec",
            "range": "stddev: 0.0007492840390611646",
            "extra": "mean: 3.2803194704055914 msec\nrounds: 321"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 14638.760680474194,
            "unit": "iter/sec",
            "range": "stddev: 0.000019896114286920033",
            "extra": "mean: 68.31179372539664 usec\nrounds: 16575"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15945.820931327096,
            "unit": "iter/sec",
            "range": "stddev: 0.000030315864368414548",
            "extra": "mean: 62.71235606536908 usec\nrounds: 17039"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 13064.949498215481,
            "unit": "iter/sec",
            "range": "stddev: 0.00007171413053832246",
            "extra": "mean: 76.54067091010097 usec\nrounds: 12805"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6592.573132112957,
            "unit": "iter/sec",
            "range": "stddev: 0.00008633008455135132",
            "extra": "mean: 151.6858410153873 usec\nrounds: 5793"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16563.994890734724,
            "unit": "iter/sec",
            "range": "stddev: 0.00001722781925798816",
            "extra": "mean: 60.37190946969939 usec\nrounds: 17751"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54940.93983883833,
            "unit": "iter/sec",
            "range": "stddev: 0.000013909961359083882",
            "extra": "mean: 18.201363189879206 usec\nrounds: 44580"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 174450.47539571498,
            "unit": "iter/sec",
            "range": "stddev: 0.0004922035329773368",
            "extra": "mean: 5.732285897941226 usec\nrounds: 189036"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 159.20557353799722,
            "unit": "iter/sec",
            "range": "stddev: 0.0011707728491388671",
            "extra": "mean: 6.281187132945019 msec\nrounds: 173"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4329.064309556509,
            "unit": "iter/sec",
            "range": "stddev: 0.00004831788869745202",
            "extra": "mean: 230.99679942210074 usec\nrounds: 4153"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 248.94255175615635,
            "unit": "iter/sec",
            "range": "stddev: 0.0003912774535618062",
            "extra": "mean: 4.016991040485187 msec\nrounds: 247"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 172.29356498756914,
            "unit": "iter/sec",
            "range": "stddev: 0.0002810022263202848",
            "extra": "mean: 5.804047296091118 msec\nrounds: 179"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 188.78323847428905,
            "unit": "iter/sec",
            "range": "stddev: 0.0004569177703850892",
            "extra": "mean: 5.297080440413109 msec\nrounds: 193"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 94.16543052815837,
            "unit": "iter/sec",
            "range": "stddev: 0.0005931674265151194",
            "extra": "mean: 10.61960843157797 msec\nrounds: 95"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 109.79077046817162,
            "unit": "iter/sec",
            "range": "stddev: 0.018682800715410515",
            "extra": "mean: 9.108233740739623 msec\nrounds: 135"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 70.01689851481852,
            "unit": "iter/sec",
            "range": "stddev: 0.0014627574084099158",
            "extra": "mean: 14.282266441555649 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 5.093179899427884,
            "unit": "iter/sec",
            "range": "stddev: 0.019245478085075176",
            "extra": "mean: 196.34099320001042 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 777.9729708155038,
            "unit": "iter/sec",
            "range": "stddev: 0.00016914669536283945",
            "extra": "mean: 1.2853917006290825 msec\nrounds: 795"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1727413.0451478441,
            "unit": "iter/sec",
            "range": "stddev: 8.445117145592299e-8",
            "extra": "mean: 578.9003404882895 nsec\nrounds: 173281"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 822730.3162894493,
            "unit": "iter/sec",
            "range": "stddev: 1.6044136443463092e-7",
            "extra": "mean: 1.215465116819865 usec\nrounds: 83599"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23727.00534539088,
            "unit": "iter/sec",
            "range": "stddev: 0.000001845064500096396",
            "extra": "mean: 42.14606881244102 usec\nrounds: 23862"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1506.2878587550513,
            "unit": "iter/sec",
            "range": "stddev: 0.000006021518871081152",
            "extra": "mean: 663.8837285899 usec\nrounds: 1518"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 150.95542603988557,
            "unit": "iter/sec",
            "range": "stddev: 0.000012554160307286114",
            "extra": "mean: 6.624472046044765 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10199.035456192036,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026578933055405156",
            "extra": "mean: 98.04848745700556 usec\nrounds: 10284"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1440.4698715926033,
            "unit": "iter/sec",
            "range": "stddev: 0.000006504893518831271",
            "extra": "mean: 694.2179213331177 usec\nrounds: 1411"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 72928.45529813539,
            "unit": "iter/sec",
            "range": "stddev: 0.00001267345430869156",
            "extra": "mean: 13.712068847639058 usec\nrounds: 64025"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1140730.8711918306,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018642153948810708",
            "extra": "mean: 876.630961126882 nsec\nrounds: 118274"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5545.942290308562,
            "unit": "iter/sec",
            "range": "stddev: 0.000049074043767729836",
            "extra": "mean: 180.31200969174932 usec\nrounds: 5365"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 263.636714356246,
            "unit": "iter/sec",
            "range": "stddev: 0.00022044321844894116",
            "extra": "mean: 3.793098402253352 msec\nrounds: 266"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 72992.20230500845,
            "unit": "iter/sec",
            "range": "stddev: 0.000012896307044568591",
            "extra": "mean: 13.700093550011761 usec\nrounds: 67440"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1998.0414521159853,
            "unit": "iter/sec",
            "range": "stddev: 0.00140708220405402",
            "extra": "mean: 500.49011692974153 usec\nrounds: 3113"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 356751.336473363,
            "unit": "iter/sec",
            "range": "stddev: 0.000005549306284023375",
            "extra": "mean: 2.803072890729494 usec\nrounds: 179824"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3115.582658257037,
            "unit": "iter/sec",
            "range": "stddev: 0.0000261024406790015",
            "extra": "mean: 320.9672506520607 usec\nrounds: 2685"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2633.184682945282,
            "unit": "iter/sec",
            "range": "stddev: 0.00003433440179204766",
            "extra": "mean: 379.76827317766237 usec\nrounds: 1618"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5074.217541002445,
            "unit": "iter/sec",
            "range": "stddev: 0.000024483149513452403",
            "extra": "mean: 197.0747197808243 usec\nrounds: 4236"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1111.902265425315,
            "unit": "iter/sec",
            "range": "stddev: 0.00005591471869609375",
            "extra": "mean: 899.3596209802568 usec\nrounds: 591"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 360901.02348151855,
            "unit": "iter/sec",
            "range": "stddev: 5.6026852211088e-7",
            "extra": "mean: 2.7708427932768367 usec\nrounds: 111770"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 126942.9412509786,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014195892271204285",
            "extra": "mean: 7.877554987660971 usec\nrounds: 74935"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 279780.8147439246,
            "unit": "iter/sec",
            "range": "stddev: 8.91798916048576e-7",
            "extra": "mean: 3.5742264919604 usec\nrounds: 159211"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 40.16987108119941,
            "unit": "iter/sec",
            "range": "stddev: 0.0013543449724289784",
            "extra": "mean: 24.89427954544836 msec\nrounds: 44"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.406967217209194,
            "unit": "iter/sec",
            "range": "stddev: 0.008579494771192592",
            "extra": "mean: 226.91341930001272 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1431.3480573194022,
            "unit": "iter/sec",
            "range": "stddev: 0.00024604951980655764",
            "extra": "mean: 698.6420911995217 usec\nrounds: 1568"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 387.59264307923763,
            "unit": "iter/sec",
            "range": "stddev: 0.0004330222977947669",
            "extra": "mean: 2.5800283309184606 msec\nrounds: 414"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 933439.3530845476,
            "unit": "iter/sec",
            "range": "stddev: 1.1293602942487891e-7",
            "extra": "mean: 1.0713068789048832 usec\nrounds: 94608"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 453673.4454488582,
            "unit": "iter/sec",
            "range": "stddev: 1.7352823872918936e-7",
            "extra": "mean: 2.204228636328084 usec\nrounds: 45788"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59616.13362986424,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013385417469972439",
            "extra": "mean: 16.773982798157473 usec\nrounds: 59877"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3974.4987845087867,
            "unit": "iter/sec",
            "range": "stddev: 0.000006501935349236608",
            "extra": "mean: 251.6040522889709 usec\nrounds: 3997"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 391.91763753331117,
            "unit": "iter/sec",
            "range": "stddev: 0.000052130824511768645",
            "extra": "mean: 2.5515565114494363 msec\nrounds: 393"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18264.416500970092,
            "unit": "iter/sec",
            "range": "stddev: 0.0000035060885867367167",
            "extra": "mean: 54.751270041771456 usec\nrounds: 17227"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18492.66138963471,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027775980383743955",
            "extra": "mean: 54.075504814061446 usec\nrounds: 18488"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18356.69353556699,
            "unit": "iter/sec",
            "range": "stddev: 0.0000028327083661915426",
            "extra": "mean: 54.476041562847435 usec\nrounds: 18430"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1506.8106754247226,
            "unit": "iter/sec",
            "range": "stddev: 0.000005268438771726997",
            "extra": "mean: 663.6533814828007 usec\nrounds: 1523"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 150.945072661135,
            "unit": "iter/sec",
            "range": "stddev: 0.00002465950233813603",
            "extra": "mean: 6.624926421049568 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39869.972465672174,
            "unit": "iter/sec",
            "range": "stddev: 0.000002003906129191616",
            "extra": "mean: 25.081532244874122 usec\nrounds: 39991"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3921.6313636662967,
            "unit": "iter/sec",
            "range": "stddev: 0.000009685842391143335",
            "extra": "mean: 254.99592064285952 usec\nrounds: 3982"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8207.204381061767,
            "unit": "iter/sec",
            "range": "stddev: 0.000006333747659447883",
            "extra": "mean: 121.84416928954678 usec\nrounds: 8323"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1262.2453753960383,
            "unit": "iter/sec",
            "range": "stddev: 0.000018531289323169327",
            "extra": "mean: 792.2389889416255 usec\nrounds: 1266"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 426.04602141348005,
            "unit": "iter/sec",
            "range": "stddev: 0.00005447427394066249",
            "extra": "mean: 2.3471642727288713 msec\nrounds: 429"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11476.174289346212,
            "unit": "iter/sec",
            "range": "stddev: 0.0000058179745069023566",
            "extra": "mean: 87.1370523649453 usec\nrounds: 10637"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1036.093500164571,
            "unit": "iter/sec",
            "range": "stddev: 0.000027208299975618416",
            "extra": "mean: 965.1638581278254 usec\nrounds: 1015"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1036.2644637615035,
            "unit": "iter/sec",
            "range": "stddev: 0.000023785657140805242",
            "extra": "mean: 965.0046247558578 usec\nrounds: 1026"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1157.4133987181906,
            "unit": "iter/sec",
            "range": "stddev: 0.00009603228246524312",
            "extra": "mean: 863.9955275336173 usec\nrounds: 1126"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1602.5217436683613,
            "unit": "iter/sec",
            "range": "stddev: 0.000017216444474944923",
            "extra": "mean: 624.0164939733561 usec\nrounds: 1577"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 965.2346800422961,
            "unit": "iter/sec",
            "range": "stddev: 0.0000245255778141033",
            "extra": "mean: 1.0360174791442227 msec\nrounds: 887"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1002.228315184649,
            "unit": "iter/sec",
            "range": "stddev: 0.000031274532160827604",
            "extra": "mean: 997.7766391640628 usec\nrounds: 1006"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1007.195062398747,
            "unit": "iter/sec",
            "range": "stddev: 0.00002898643282834139",
            "extra": "mean: 992.8563367044203 usec\nrounds: 989"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 742.5952203044145,
            "unit": "iter/sec",
            "range": "stddev: 0.000051886270987521726",
            "extra": "mean: 1.3466286513264478 msec\nrounds: 717"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1074.387896147833,
            "unit": "iter/sec",
            "range": "stddev: 0.000035649476719413237",
            "extra": "mean: 930.762533332191 usec\nrounds: 1080"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 963.4156244033294,
            "unit": "iter/sec",
            "range": "stddev: 0.000017239232179347233",
            "extra": "mean: 1.0379736166509945 msec\nrounds: 973"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1625.487280812746,
            "unit": "iter/sec",
            "range": "stddev: 0.000017252007868509626",
            "extra": "mean: 615.2001383240591 usec\nrounds: 1670"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3075.9684601698705,
            "unit": "iter/sec",
            "range": "stddev: 0.000015016634309348724",
            "extra": "mean: 325.100862687251 usec\nrounds: 3066"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3054.9857596724833,
            "unit": "iter/sec",
            "range": "stddev: 0.000014881021676201438",
            "extra": "mean: 327.33376803275416 usec\nrounds: 3147"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 3969.727717382638,
            "unit": "iter/sec",
            "range": "stddev: 0.000016412997289336984",
            "extra": "mean: 251.90644577994647 usec\nrounds: 3993"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7797.605180039394,
            "unit": "iter/sec",
            "range": "stddev: 0.000008045542276626794",
            "extra": "mean: 128.24450288401854 usec\nrounds: 7453"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 649.7439668852809,
            "unit": "iter/sec",
            "range": "stddev: 0.00006551186732421617",
            "extra": "mean: 1.5390677727932802 msec\nrounds: 647"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4632.820793499859,
            "unit": "iter/sec",
            "range": "stddev: 0.000010911545433602339",
            "extra": "mean: 215.85121561426752 usec\nrounds: 4406"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 430.3898943501996,
            "unit": "iter/sec",
            "range": "stddev: 0.0001299552524475018",
            "extra": "mean: 2.3234746287660744 msec\nrounds: 431"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 627.2486176575986,
            "unit": "iter/sec",
            "range": "stddev: 0.00004132834417524013",
            "extra": "mean: 1.594264175080061 msec\nrounds: 634"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 9298.827165132481,
            "unit": "iter/sec",
            "range": "stddev: 0.000003806554139000313",
            "extra": "mean: 107.5404437830255 usec\nrounds: 9223"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 646.400247298294,
            "unit": "iter/sec",
            "range": "stddev: 0.000050578103667049814",
            "extra": "mean: 1.5470291111112935 msec\nrounds: 612"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.319721819213603,
            "unit": "iter/sec",
            "range": "stddev: 0.0006944427991252308",
            "extra": "mean: 136.61721369999213 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 484.62547964440944,
            "unit": "iter/sec",
            "range": "stddev: 0.00006476243678821271",
            "extra": "mean: 2.0634490797589575 msec\nrounds: 489"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 336.21912150140605,
            "unit": "iter/sec",
            "range": "stddev: 0.00007202801392395987",
            "extra": "mean: 2.9742508264682916 msec\nrounds: 340"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 778521.957428392,
            "unit": "iter/sec",
            "range": "stddev: 1.0750794345256996e-7",
            "extra": "mean: 1.2844852870986359 usec\nrounds: 75444"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 484.64115278865876,
            "unit": "iter/sec",
            "range": "stddev: 0.00007306378418504199",
            "extra": "mean: 2.063382348457061 msec\nrounds: 485"
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
        "date": 1771220771847,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 379.67979570307506,
            "unit": "iter/sec",
            "range": "stddev: 0.00839073361416285",
            "extra": "mean: 2.633798298769736 msec\nrounds: 569"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 438.9354301113926,
            "unit": "iter/sec",
            "range": "stddev: 0.0007106260333122252",
            "extra": "mean: 2.2782394206505976 msec\nrounds: 523"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6295.427935465477,
            "unit": "iter/sec",
            "range": "stddev: 0.000023115550286625337",
            "extra": "mean: 158.84543675998114 usec\nrounds: 6246"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4585.568655580645,
            "unit": "iter/sec",
            "range": "stddev: 0.00003310927917517286",
            "extra": "mean: 218.07546132430014 usec\nrounds: 5029"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 372.7539486996684,
            "unit": "iter/sec",
            "range": "stddev: 0.0006182859077642407",
            "extra": "mean: 2.682734826789749 msec\nrounds: 433"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 323.263736623235,
            "unit": "iter/sec",
            "range": "stddev: 0.0010028160173126512",
            "extra": "mean: 3.0934493625726525 msec\nrounds: 342"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 328.01170094541277,
            "unit": "iter/sec",
            "range": "stddev: 0.0009314989548249684",
            "extra": "mean: 3.0486717306661526 msec\nrounds: 375"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 332.4941426314413,
            "unit": "iter/sec",
            "range": "stddev: 0.000803058001731246",
            "extra": "mean: 3.0075717788161658 msec\nrounds: 321"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 314.5849770170712,
            "unit": "iter/sec",
            "range": "stddev: 0.0006863833235138837",
            "extra": "mean: 3.1787913379784003 msec\nrounds: 287"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 14184.965938246794,
            "unit": "iter/sec",
            "range": "stddev: 0.000019268193069993253",
            "extra": "mean: 70.49717315878137 usec\nrounds: 15968"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 13522.549340505433,
            "unit": "iter/sec",
            "range": "stddev: 0.00002552096436906279",
            "extra": "mean: 73.95055287427208 usec\nrounds: 16700"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 12591.310495218933,
            "unit": "iter/sec",
            "range": "stddev: 0.00008405556223546944",
            "extra": "mean: 79.41985072798511 usec\nrounds: 13941"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6277.91427555802,
            "unit": "iter/sec",
            "range": "stddev: 0.00011329814095442273",
            "extra": "mean: 159.28857198533723 usec\nrounds: 6161"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 12677.867820650423,
            "unit": "iter/sec",
            "range": "stddev: 0.000018898402763047365",
            "extra": "mean: 78.87761681590842 usec\nrounds: 17305"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 43557.966579780914,
            "unit": "iter/sec",
            "range": "stddev: 0.0008882871966344111",
            "extra": "mean: 22.95791283480593 usec\nrounds: 46039"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 219018.6341225878,
            "unit": "iter/sec",
            "range": "stddev: 0.00000774445226659236",
            "extra": "mean: 4.565821552152891 usec\nrounds: 178859"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 161.02627181229695,
            "unit": "iter/sec",
            "range": "stddev: 0.0011412509213019565",
            "extra": "mean: 6.210166755681131 msec\nrounds: 176"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4370.811356186283,
            "unit": "iter/sec",
            "range": "stddev: 0.00004978003613077533",
            "extra": "mean: 228.7904735546725 usec\nrounds: 4065"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 247.5078924824569,
            "unit": "iter/sec",
            "range": "stddev: 0.00034204295390573335",
            "extra": "mean: 4.040275200803461 msec\nrounds: 249"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 170.75181738993334,
            "unit": "iter/sec",
            "range": "stddev: 0.0003739400532258738",
            "extra": "mean: 5.856453039772769 msec\nrounds: 176"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 180.69763050733206,
            "unit": "iter/sec",
            "range": "stddev: 0.0005111316730414147",
            "extra": "mean: 5.534106878946725 msec\nrounds: 190"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 92.05701635138142,
            "unit": "iter/sec",
            "range": "stddev: 0.000581115375125384",
            "extra": "mean: 10.862833053191755 msec\nrounds: 94"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 132.9299019977305,
            "unit": "iter/sec",
            "range": "stddev: 0.0005030605569550311",
            "extra": "mean: 7.522761884057305 msec\nrounds: 138"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 59.07069944940229,
            "unit": "iter/sec",
            "range": "stddev: 0.024953967786920418",
            "extra": "mean: 16.928866753246453 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 5.275387202948928,
            "unit": "iter/sec",
            "range": "stddev: 0.014860928268517183",
            "extra": "mean: 189.55954540000448 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 773.531273121528,
            "unit": "iter/sec",
            "range": "stddev: 0.00018709480721226676",
            "extra": "mean: 1.2927725545789175 msec\nrounds: 797"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1667004.055422535,
            "unit": "iter/sec",
            "range": "stddev: 7.131167612322525e-8",
            "extra": "mean: 599.8785646304444 nsec\nrounds: 165810"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 824123.2530914303,
            "unit": "iter/sec",
            "range": "stddev: 9.840057478270985e-8",
            "extra": "mean: 1.2134107322525183 usec\nrounds: 82089"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23731.169172548158,
            "unit": "iter/sec",
            "range": "stddev: 0.000001724010163844768",
            "extra": "mean: 42.13867394096976 usec\nrounds: 23873"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1507.7548363404653,
            "unit": "iter/sec",
            "range": "stddev: 0.000005442720791169262",
            "extra": "mean: 663.2377996061626 usec\nrounds: 1522"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 151.1135587972108,
            "unit": "iter/sec",
            "range": "stddev: 0.000010246454240899164",
            "extra": "mean: 6.617539868424154 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10200.84074958973,
            "unit": "iter/sec",
            "range": "stddev: 0.000002522376385501141",
            "extra": "mean: 98.03113532972458 usec\nrounds: 10286"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1442.2800533744016,
            "unit": "iter/sec",
            "range": "stddev: 0.000006476826261896692",
            "extra": "mean: 693.3466199302763 usec\nrounds: 1455"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 70549.39511648146,
            "unit": "iter/sec",
            "range": "stddev: 0.000014214115025637581",
            "extra": "mean: 14.174465965993576 usec\nrounds: 63172"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1140173.3555438777,
            "unit": "iter/sec",
            "range": "stddev: 0.0000015859783552369847",
            "extra": "mean: 877.0596112755037 nsec\nrounds: 117565"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5552.456158714086,
            "unit": "iter/sec",
            "range": "stddev: 0.00004783556452959766",
            "extra": "mean: 180.1004765126491 usec\nrounds: 5322"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 260.27370498065835,
            "unit": "iter/sec",
            "range": "stddev: 0.00023561602788054983",
            "extra": "mean: 3.8421092137383326 msec\nrounds: 262"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 70906.43176221573,
            "unit": "iter/sec",
            "range": "stddev: 0.000014777438224563783",
            "extra": "mean: 14.103092979681923 usec\nrounds: 64982"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1959.779116952119,
            "unit": "iter/sec",
            "range": "stddev: 0.0015075806906754902",
            "extra": "mean: 510.2615857827981 usec\nrounds: 3334"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 352344.14238636557,
            "unit": "iter/sec",
            "range": "stddev: 0.000008313372449322753",
            "extra": "mean: 2.8381343116056197 usec\nrounds: 175439"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3087.7130869362186,
            "unit": "iter/sec",
            "range": "stddev: 0.00002594579496265659",
            "extra": "mean: 323.8642878546236 usec\nrounds: 2536"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2625.5912901551756,
            "unit": "iter/sec",
            "range": "stddev: 0.000033772225468152294",
            "extra": "mean: 380.86658946103483 usec\nrounds: 1632"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4948.474364155467,
            "unit": "iter/sec",
            "range": "stddev: 0.00005443497080342575",
            "extra": "mean: 202.08248571389038 usec\nrounds: 4060"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1101.1058355444752,
            "unit": "iter/sec",
            "range": "stddev: 0.00005668593337936403",
            "extra": "mean: 908.1779132571025 usec\nrounds: 611"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 360946.4171074168,
            "unit": "iter/sec",
            "range": "stddev: 5.404158004740716e-7",
            "extra": "mean: 2.770494324376137 usec\nrounds: 112146"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 127908.57823905256,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014349330192075902",
            "extra": "mean: 7.8180839296881794 usec\nrounds: 78101"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 290693.94982886675,
            "unit": "iter/sec",
            "range": "stddev: 7.673664731994787e-7",
            "extra": "mean: 3.440044075869848 usec\nrounds: 172385"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.07265946375707,
            "unit": "iter/sec",
            "range": "stddev: 0.0021593823990948653",
            "extra": "mean: 23.768404772734574 msec\nrounds: 44"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.420484997286603,
            "unit": "iter/sec",
            "range": "stddev: 0.008741896860615725",
            "extra": "mean: 226.2195212999984 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1403.4664095249889,
            "unit": "iter/sec",
            "range": "stddev: 0.00030528546868499373",
            "extra": "mean: 712.5215061887058 usec\nrounds: 1535"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 385.51464064254657,
            "unit": "iter/sec",
            "range": "stddev: 0.0005188008336066601",
            "extra": "mean: 2.593935209135705 msec\nrounds: 416"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 973719.819004765,
            "unit": "iter/sec",
            "range": "stddev: 1.1130278005303554e-7",
            "extra": "mean: 1.0269894691289079 usec\nrounds: 97371"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 471375.270350407,
            "unit": "iter/sec",
            "range": "stddev: 1.5129815211635462e-7",
            "extra": "mean: 2.1214519787103563 usec\nrounds: 47329"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59435.85778778036,
            "unit": "iter/sec",
            "range": "stddev: 0.000001459895737861782",
            "extra": "mean: 16.824860231185117 usec\nrounds: 59949"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3971.259762043568,
            "unit": "iter/sec",
            "range": "stddev: 0.0000059954274300372305",
            "extra": "mean: 251.80926454566915 usec\nrounds: 4022"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 398.1660026229663,
            "unit": "iter/sec",
            "range": "stddev: 0.00002596215949194573",
            "extra": "mean: 2.5115152810947694 msec\nrounds: 402"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18450.456342420894,
            "unit": "iter/sec",
            "range": "stddev: 0.0000031711635900219573",
            "extra": "mean: 54.199201442016445 usec\nrounds: 18030"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18137.75755731249,
            "unit": "iter/sec",
            "range": "stddev: 0.00000363287431265486",
            "extra": "mean: 55.133607163959255 usec\nrounds: 18369"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18319.79845265234,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026648594817997723",
            "extra": "mean: 54.58575336319925 usec\nrounds: 18359"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1506.9695200230922,
            "unit": "iter/sec",
            "range": "stddev: 0.000005573060059426132",
            "extra": "mean: 663.5834280076722 usec\nrounds: 1521"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 151.04026913533454,
            "unit": "iter/sec",
            "range": "stddev: 0.00001909174298197548",
            "extra": "mean: 6.620750914472906 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39969.91817319707,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018977103343884127",
            "extra": "mean: 25.018815291710496 usec\nrounds: 40166"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3896.1279086394607,
            "unit": "iter/sec",
            "range": "stddev: 0.000017286859123226926",
            "extra": "mean: 256.66508478393433 usec\nrounds: 3963"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8104.391890075863,
            "unit": "iter/sec",
            "range": "stddev: 0.000004947928075820163",
            "extra": "mean: 123.38988705920528 usec\nrounds: 8199"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1234.438321778883,
            "unit": "iter/sec",
            "range": "stddev: 0.000030237242393689927",
            "extra": "mean: 810.08502600515 usec\nrounds: 1269"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 432.6599947613203,
            "unit": "iter/sec",
            "range": "stddev: 0.00003258240788644198",
            "extra": "mean: 2.3112837149449335 msec\nrounds: 435"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11437.18931316928,
            "unit": "iter/sec",
            "range": "stddev: 0.000004563119991945053",
            "extra": "mean: 87.43406903727268 usec\nrounds: 11023"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1032.5924564530246,
            "unit": "iter/sec",
            "range": "stddev: 0.000023940363756282475",
            "extra": "mean: 968.4362826308258 usec\nrounds: 1019"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1038.093545756835,
            "unit": "iter/sec",
            "range": "stddev: 0.000020785604096897703",
            "extra": "mean: 963.3043227052699 usec\nrounds: 1035"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1188.850521723781,
            "unit": "iter/sec",
            "range": "stddev: 0.000013339508815473249",
            "extra": "mean: 841.1486404111124 usec\nrounds: 1168"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1612.0030346226913,
            "unit": "iter/sec",
            "range": "stddev: 0.000014895916903614477",
            "extra": "mean: 620.3462267265905 usec\nrounds: 1579"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 965.5586325426414,
            "unit": "iter/sec",
            "range": "stddev: 0.000024032931046930877",
            "extra": "mean: 1.0356698871477779 msec\nrounds: 957"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 970.311456150611,
            "unit": "iter/sec",
            "range": "stddev: 0.000041038686353743116",
            "extra": "mean: 1.0305969219070839 msec\nrounds: 986"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 972.2490899425982,
            "unit": "iter/sec",
            "range": "stddev: 0.0000395268170813741",
            "extra": "mean: 1.028543004405425 msec\nrounds: 908"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 756.608156940976,
            "unit": "iter/sec",
            "range": "stddev: 0.000022690075862692533",
            "extra": "mean: 1.3216881034472 msec\nrounds: 754"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1084.5755585290435,
            "unit": "iter/sec",
            "range": "stddev: 0.00002228732132627016",
            "extra": "mean: 922.0196713230849 usec\nrounds: 1074"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 941.9139494039667,
            "unit": "iter/sec",
            "range": "stddev: 0.000022287684111362474",
            "extra": "mean: 1.0616681074027934 msec\nrounds: 959"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1585.673982847468,
            "unit": "iter/sec",
            "range": "stddev: 0.000020194764390086807",
            "extra": "mean: 630.6466592863267 usec\nrounds: 1626"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3122.5148155506013,
            "unit": "iter/sec",
            "range": "stddev: 0.000014832438806741158",
            "extra": "mean: 320.2546854285037 usec\nrounds: 3198"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3115.3074342185864,
            "unit": "iter/sec",
            "range": "stddev: 0.000013943347096050256",
            "extra": "mean: 320.99560673081066 usec\nrounds: 3209"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4161.101849730949,
            "unit": "iter/sec",
            "range": "stddev: 0.000011739598157969591",
            "extra": "mean: 240.32096211840107 usec\nrounds: 4171"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7684.532581871056,
            "unit": "iter/sec",
            "range": "stddev: 0.000006045064171894426",
            "extra": "mean: 130.13153231455445 usec\nrounds: 7721"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 640.3444349876963,
            "unit": "iter/sec",
            "range": "stddev: 0.0000536344161311442",
            "extra": "mean: 1.5616595465832606 msec\nrounds: 644"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4685.2832942316945,
            "unit": "iter/sec",
            "range": "stddev: 0.000007977683268806976",
            "extra": "mean: 213.43426580654238 usec\nrounds: 4571"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 436.5245054644854,
            "unit": "iter/sec",
            "range": "stddev: 0.000037547048757794905",
            "extra": "mean: 2.2908221359438836 msec\nrounds: 434"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 616.8300282610611,
            "unit": "iter/sec",
            "range": "stddev: 0.000027705570495738446",
            "extra": "mean: 1.6211921504845577 msec\nrounds: 618"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 9245.787638216145,
            "unit": "iter/sec",
            "range": "stddev: 0.000004919518933547421",
            "extra": "mean: 108.15736193925142 usec\nrounds: 9159"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 628.4855711264305,
            "unit": "iter/sec",
            "range": "stddev: 0.00003924525184173444",
            "extra": "mean: 1.5911264250787915 msec\nrounds: 614"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.443000730057856,
            "unit": "iter/sec",
            "range": "stddev: 0.00040401743408060263",
            "extra": "mean: 134.35441380001407 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 527.3262354346522,
            "unit": "iter/sec",
            "range": "stddev: 0.0000755071165692355",
            "extra": "mean: 1.8963592797080222 msec\nrounds: 547"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 357.22539883460934,
            "unit": "iter/sec",
            "range": "stddev: 0.00006765189024565582",
            "extra": "mean: 2.799353022663954 msec\nrounds: 353"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 757030.512691305,
            "unit": "iter/sec",
            "range": "stddev: 1.035926624069838e-7",
            "extra": "mean: 1.3209507189411942 usec\nrounds: 73720"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 523.7129894575932,
            "unit": "iter/sec",
            "range": "stddev: 0.00007975965777175292",
            "extra": "mean: 1.9094428057545312 msec\nrounds: 556"
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
        "date": 1771221101674,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 376.4815680077241,
            "unit": "iter/sec",
            "range": "stddev: 0.008223971784789542",
            "extra": "mean: 2.6561725326735877 msec\nrounds: 505"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 424.6628198447732,
            "unit": "iter/sec",
            "range": "stddev: 0.0005437140333140507",
            "extra": "mean: 2.3548093999976962 msec\nrounds: 545"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6641.235114840147,
            "unit": "iter/sec",
            "range": "stddev: 0.000021904212360241553",
            "extra": "mean: 150.574401102809 usec\nrounds: 6527"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4737.386410669504,
            "unit": "iter/sec",
            "range": "stddev: 0.00003731739996338217",
            "extra": "mean: 211.0868553487231 usec\nrounds: 4618"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 393.5948912976752,
            "unit": "iter/sec",
            "range": "stddev: 0.00042719372425951177",
            "extra": "mean: 2.540683383117647 msec\nrounds: 462"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 316.3525908371282,
            "unit": "iter/sec",
            "range": "stddev: 0.001002775047858696",
            "extra": "mean: 3.1610299044929984 msec\nrounds: 356"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 321.3196666516207,
            "unit": "iter/sec",
            "range": "stddev: 0.0010170565063554453",
            "extra": "mean: 3.1121655590543544 msec\nrounds: 381"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 326.28905590405515,
            "unit": "iter/sec",
            "range": "stddev: 0.0009524989320328607",
            "extra": "mean: 3.064767211481493 msec\nrounds: 331"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 308.22398078487714,
            "unit": "iter/sec",
            "range": "stddev: 0.0005590210302522171",
            "extra": "mean: 3.244393889967774 msec\nrounds: 309"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 15896.813739003403,
            "unit": "iter/sec",
            "range": "stddev: 0.000017959946677663522",
            "extra": "mean: 62.90568766912479 usec\nrounds: 16633"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14615.304552122083,
            "unit": "iter/sec",
            "range": "stddev: 0.000017221969020620482",
            "extra": "mean: 68.42142744502742 usec\nrounds: 11033"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 13409.716098017592,
            "unit": "iter/sec",
            "range": "stddev: 0.00006141889848695152",
            "extra": "mean: 74.57279428517012 usec\nrounds: 10009"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6207.928105787974,
            "unit": "iter/sec",
            "range": "stddev: 0.00011233482595526947",
            "extra": "mean: 161.08433972804033 usec\nrounds: 5160"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15640.367415629751,
            "unit": "iter/sec",
            "range": "stddev: 0.000020401111309013686",
            "extra": "mean: 63.93711691201568 usec\nrounds: 15687"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53531.811191715984,
            "unit": "iter/sec",
            "range": "stddev: 0.000026309894192068106",
            "extra": "mean: 18.680481338818392 usec\nrounds: 47237"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 215681.21587860663,
            "unit": "iter/sec",
            "range": "stddev: 0.000008414598785437887",
            "extra": "mean: 4.636472378581346 usec\nrounds: 187970"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 126.81290700948958,
            "unit": "iter/sec",
            "range": "stddev: 0.019397556957631075",
            "extra": "mean: 7.885632650351345 msec\nrounds: 143"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4234.488811551356,
            "unit": "iter/sec",
            "range": "stddev: 0.00006470279043990101",
            "extra": "mean: 236.15601422114466 usec\nrounds: 4008"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 239.75899313200588,
            "unit": "iter/sec",
            "range": "stddev: 0.0005271096054077551",
            "extra": "mean: 4.170855019604718 msec\nrounds: 255"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 159.89425053175452,
            "unit": "iter/sec",
            "range": "stddev: 0.000833225865030977",
            "extra": "mean: 6.254133570621434 msec\nrounds: 177"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 183.60139622792542,
            "unit": "iter/sec",
            "range": "stddev: 0.00039308822333319833",
            "extra": "mean: 5.446581673913774 msec\nrounds: 184"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 92.40225232456952,
            "unit": "iter/sec",
            "range": "stddev: 0.000703520225453693",
            "extra": "mean: 10.822247021505802 msec\nrounds: 93"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 119.59180078202571,
            "unit": "iter/sec",
            "range": "stddev: 0.001029539614209783",
            "extra": "mean: 8.36177725781262 msec\nrounds: 128"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 56.892943427815446,
            "unit": "iter/sec",
            "range": "stddev: 0.0270171518402784",
            "extra": "mean: 17.5768722753601 msec\nrounds: 69"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 6.256263383793501,
            "unit": "iter/sec",
            "range": "stddev: 0.0013728499921967998",
            "extra": "mean: 159.83981790000144 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 776.1306456779797,
            "unit": "iter/sec",
            "range": "stddev: 0.00018820164748184538",
            "extra": "mean: 1.2884428743648717 msec\nrounds: 788"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1657308.3579178168,
            "unit": "iter/sec",
            "range": "stddev: 8.69392733945982e-8",
            "extra": "mean: 603.3880148027276 nsec\nrounds: 166639"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 820332.7048071247,
            "unit": "iter/sec",
            "range": "stddev: 1.0520394612651513e-7",
            "extra": "mean: 1.2190175938860297 usec\nrounds: 82631"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23715.66027847538,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017741645117537765",
            "extra": "mean: 42.166230594372784 usec\nrounds: 23743"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1506.2997519441901,
            "unit": "iter/sec",
            "range": "stddev: 0.000005432409207699807",
            "extra": "mean: 663.8784868080169 usec\nrounds: 1516"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 150.93439500685116,
            "unit": "iter/sec",
            "range": "stddev: 0.000013174455926611024",
            "extra": "mean: 6.625395092713019 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10196.384204263675,
            "unit": "iter/sec",
            "range": "stddev: 0.000002633180671052996",
            "extra": "mean: 98.07398191035645 usec\nrounds: 10282"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1437.2260844758823,
            "unit": "iter/sec",
            "range": "stddev: 0.000007226595298078242",
            "extra": "mean: 695.784755649403 usec\nrounds: 1416"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 71439.067811329,
            "unit": "iter/sec",
            "range": "stddev: 0.000015195868403443704",
            "extra": "mean: 13.997943011252692 usec\nrounds: 63416"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1074294.245087703,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018816245770285992",
            "extra": "mean: 930.8436720875873 nsec\nrounds: 114469"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5431.3129918038385,
            "unit": "iter/sec",
            "range": "stddev: 0.00005294809717424982",
            "extra": "mean: 184.11754238966842 usec\nrounds: 5131"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 257.29129560477656,
            "unit": "iter/sec",
            "range": "stddev: 0.00029819642878829375",
            "extra": "mean: 3.886645281370472 msec\nrounds: 263"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 71474.41906196985,
            "unit": "iter/sec",
            "range": "stddev: 0.00001976961342982294",
            "extra": "mean: 13.991019628057119 usec\nrounds: 64856"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1978.3191908882434,
            "unit": "iter/sec",
            "range": "stddev: 0.00153084911885047",
            "extra": "mean: 505.47960339555266 usec\nrounds: 3298"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 341319.0491089927,
            "unit": "iter/sec",
            "range": "stddev: 0.000004660711242504372",
            "extra": "mean: 2.9298101076118725 usec\nrounds: 171792"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3132.5098770250765,
            "unit": "iter/sec",
            "range": "stddev: 0.000024414056221235008",
            "extra": "mean: 319.23283222005136 usec\nrounds: 2545"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2625.6968037747993,
            "unit": "iter/sec",
            "range": "stddev: 0.00003410425017987574",
            "extra": "mean: 380.85128433807085 usec\nrounds: 1660"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4977.426651056771,
            "unit": "iter/sec",
            "range": "stddev: 0.000030111161541165353",
            "extra": "mean: 200.90702889367284 usec\nrounds: 4257"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1001.5200278860309,
            "unit": "iter/sec",
            "range": "stddev: 0.00004885876954453764",
            "extra": "mean: 998.4822790920723 usec\nrounds: 1014"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 355614.3143341797,
            "unit": "iter/sec",
            "range": "stddev: 5.682600317611311e-7",
            "extra": "mean: 2.812035285678278 usec\nrounds: 112411"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 123182.1009206056,
            "unit": "iter/sec",
            "range": "stddev: 0.0000015334812205046308",
            "extra": "mean: 8.118062547451832 usec\nrounds: 73720"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 270316.19243626925,
            "unit": "iter/sec",
            "range": "stddev: 9.58330963503845e-7",
            "extra": "mean: 3.6993714323486695 usec\nrounds: 169751"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 40.023106474900544,
            "unit": "iter/sec",
            "range": "stddev: 0.001400739150239145",
            "extra": "mean: 24.98556679070187 msec\nrounds: 43"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.457791607452212,
            "unit": "iter/sec",
            "range": "stddev: 0.008801705153593853",
            "extra": "mean: 224.32632299999682 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1402.959348352113,
            "unit": "iter/sec",
            "range": "stddev: 0.00039390039119401976",
            "extra": "mean: 712.7790275424439 usec\nrounds: 1525"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 386.3429051772721,
            "unit": "iter/sec",
            "range": "stddev: 0.0004282818009848436",
            "extra": "mean: 2.5883741789981976 msec\nrounds: 419"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 994100.580271515,
            "unit": "iter/sec",
            "range": "stddev: 1.0551521270609858e-7",
            "extra": "mean: 1.0059344294184738 usec\nrounds: 101544"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 482678.8428165562,
            "unit": "iter/sec",
            "range": "stddev: 1.4304975984670139e-7",
            "extra": "mean: 2.071770940206827 usec\nrounds: 48149"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59742.62863150593,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013174660728910685",
            "extra": "mean: 16.738466701357012 usec\nrounds: 59627"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3951.219651157506,
            "unit": "iter/sec",
            "range": "stddev: 0.000006128756945428776",
            "extra": "mean: 253.08641085216584 usec\nrounds: 3870"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 367.45399993310946,
            "unit": "iter/sec",
            "range": "stddev: 0.0001207590129729846",
            "extra": "mean: 2.721429077332232 msec\nrounds: 375"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18154.057459701555,
            "unit": "iter/sec",
            "range": "stddev: 0.0000029337886520362806",
            "extra": "mean: 55.08410459864434 usec\nrounds: 16941"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18338.594032549478,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026682336764702213",
            "extra": "mean: 54.52980736827934 usec\nrounds: 18403"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18255.380043327328,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027718548474774423",
            "extra": "mean: 54.77837205396982 usec\nrounds: 18328"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1506.7029777585476,
            "unit": "iter/sec",
            "range": "stddev: 0.000005327784654198195",
            "extra": "mean: 663.7008187822486 usec\nrounds: 1512"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 150.9339086737415,
            "unit": "iter/sec",
            "range": "stddev: 0.000014317485536954994",
            "extra": "mean: 6.625416440791965 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39551.23460623003,
            "unit": "iter/sec",
            "range": "stddev: 0.0000019149363770731097",
            "extra": "mean: 25.283660799870002 usec\nrounds: 39941"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3920.223495573623,
            "unit": "iter/sec",
            "range": "stddev: 0.000009684074115823661",
            "extra": "mean: 255.08749721262407 usec\nrounds: 3946"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7958.980475822313,
            "unit": "iter/sec",
            "range": "stddev: 0.000005276844903894982",
            "extra": "mean: 125.64423333337568 usec\nrounds: 8220"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1216.574463213818,
            "unit": "iter/sec",
            "range": "stddev: 0.00008797794153164795",
            "extra": "mean: 821.9801008795675 usec\nrounds: 1249"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 431.2623530507192,
            "unit": "iter/sec",
            "range": "stddev: 0.000048508517242595946",
            "extra": "mean: 2.318774158991786 msec\nrounds: 434"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11263.622715980318,
            "unit": "iter/sec",
            "range": "stddev: 0.000014317647298553459",
            "extra": "mean: 88.78138279447563 usec\nrounds: 11194"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1018.7903374400863,
            "unit": "iter/sec",
            "range": "stddev: 0.00002315967749594243",
            "extra": "mean: 981.55622727312 usec\nrounds: 968"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1023.1944868172342,
            "unit": "iter/sec",
            "range": "stddev: 0.00002235979284743723",
            "extra": "mean: 977.3313019996978 usec\nrounds: 1000"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1166.0666165201217,
            "unit": "iter/sec",
            "range": "stddev: 0.00004165659262022837",
            "extra": "mean: 857.5839371718639 usec\nrounds: 1146"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1577.959845558501,
            "unit": "iter/sec",
            "range": "stddev: 0.000034071838013325074",
            "extra": "mean: 633.7296876182938 usec\nrounds: 1591"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 955.8648398049182,
            "unit": "iter/sec",
            "range": "stddev: 0.000024262591555105552",
            "extra": "mean: 1.0461730135445608 msec\nrounds: 886"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 999.3457231921186,
            "unit": "iter/sec",
            "range": "stddev: 0.00008571767137326928",
            "extra": "mean: 1.0006547051662877 msec\nrounds: 987"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1006.2867433400833,
            "unit": "iter/sec",
            "range": "stddev: 0.000038098605921900436",
            "extra": "mean: 993.752532882212 usec\nrounds: 1034"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 747.2300514840207,
            "unit": "iter/sec",
            "range": "stddev: 0.000030378037746103192",
            "extra": "mean: 1.3382759406075422 msec\nrounds: 724"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1074.8051836631623,
            "unit": "iter/sec",
            "range": "stddev: 0.00002058090597361598",
            "extra": "mean: 930.4011696257264 usec\nrounds: 1014"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 955.7180065773488,
            "unit": "iter/sec",
            "range": "stddev: 0.00002366861855927288",
            "extra": "mean: 1.0463337439683023 msec\nrounds: 953"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1643.8806227952284,
            "unit": "iter/sec",
            "range": "stddev: 0.000020112535046295572",
            "extra": "mean: 608.3166783118447 usec\nrounds: 1660"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3008.557442046792,
            "unit": "iter/sec",
            "range": "stddev: 0.00001599012479546955",
            "extra": "mean: 332.3852109400566 usec\nrounds: 2797"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3042.5758636794567,
            "unit": "iter/sec",
            "range": "stddev: 0.00001674565427020361",
            "extra": "mean: 328.66887952981955 usec\nrounds: 3063"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4067.4998222600298,
            "unit": "iter/sec",
            "range": "stddev: 0.000016310849733445165",
            "extra": "mean: 245.85127073081682 usec\nrounds: 4052"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7596.142447470066,
            "unit": "iter/sec",
            "range": "stddev: 0.000006466974721992326",
            "extra": "mean: 131.6457671660772 usec\nrounds: 7413"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 640.4443140111459,
            "unit": "iter/sec",
            "range": "stddev: 0.00010435154024030977",
            "extra": "mean: 1.5614160015520047 msec\nrounds: 645"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4618.834347340129,
            "unit": "iter/sec",
            "range": "stddev: 0.000008295654095465016",
            "extra": "mean: 216.50484187116925 usec\nrounds: 4490"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 434.4584008139016,
            "unit": "iter/sec",
            "range": "stddev: 0.00003712606987568118",
            "extra": "mean: 2.301716339531309 msec\nrounds: 430"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 469.14961754814476,
            "unit": "iter/sec",
            "range": "stddev: 0.011717705173956178",
            "extra": "mean: 2.1315161786258487 msec\nrounds: 627"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 8866.465302255298,
            "unit": "iter/sec",
            "range": "stddev: 0.000004977039365166093",
            "extra": "mean: 112.78451625425492 usec\nrounds: 8736"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 503.2737708002907,
            "unit": "iter/sec",
            "range": "stddev: 0.008586619498947371",
            "extra": "mean: 1.9869900996625163 msec\nrounds: 592"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.298505466643289,
            "unit": "iter/sec",
            "range": "stddev: 0.0003843087493482162",
            "extra": "mean: 137.01435240000137 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 460.0658797922921,
            "unit": "iter/sec",
            "range": "stddev: 0.00009532861983889329",
            "extra": "mean: 2.173601746887803 msec\nrounds: 482"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 328.0646781648699,
            "unit": "iter/sec",
            "range": "stddev: 0.0000774968073118695",
            "extra": "mean: 3.0481794187469546 msec\nrounds: 320"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 756298.9977963367,
            "unit": "iter/sec",
            "range": "stddev: 1.0731011780377415e-7",
            "extra": "mean: 1.3222283817825307 usec\nrounds: 73660"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 475.8345052642643,
            "unit": "iter/sec",
            "range": "stddev: 0.00006782138583431067",
            "extra": "mean: 2.101571006172892 msec\nrounds: 486"
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
        "date": 1771222161389,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 431.93628765691795,
            "unit": "iter/sec",
            "range": "stddev: 0.0043490737525574",
            "extra": "mean: 2.3151562593284325 msec\nrounds: 536"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 434.6082850934408,
            "unit": "iter/sec",
            "range": "stddev: 0.001388871743820951",
            "extra": "mean: 2.300922541743538 msec\nrounds: 539"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6558.085028480087,
            "unit": "iter/sec",
            "range": "stddev: 0.000020113361843837962",
            "extra": "mean: 152.48353683388603 usec\nrounds: 6285"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4588.9345022752805,
            "unit": "iter/sec",
            "range": "stddev: 0.00003789205276344657",
            "extra": "mean: 217.91550947266322 usec\nrounds: 4434"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 383.4232672836955,
            "unit": "iter/sec",
            "range": "stddev: 0.00042576624006056225",
            "extra": "mean: 2.6080837688446756 msec\nrounds: 398"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 324.5831996075916,
            "unit": "iter/sec",
            "range": "stddev: 0.000704525631427246",
            "extra": "mean: 3.0808741832878623 msec\nrounds: 371"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 328.20887517664164,
            "unit": "iter/sec",
            "range": "stddev: 0.0005349901566134202",
            "extra": "mean: 3.0468402155846674 msec\nrounds: 385"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 328.73202339187554,
            "unit": "iter/sec",
            "range": "stddev: 0.0008029567409423298",
            "extra": "mean: 3.041991436313212 msec\nrounds: 369"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 303.2062254928723,
            "unit": "iter/sec",
            "range": "stddev: 0.000746538264212969",
            "extra": "mean: 3.298085315941205 msec\nrounds: 345"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 14328.002058710043,
            "unit": "iter/sec",
            "range": "stddev: 0.000019569302090851546",
            "extra": "mean: 69.79340147373141 usec\nrounds: 15473"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15415.346733461085,
            "unit": "iter/sec",
            "range": "stddev: 0.00001796882371712488",
            "extra": "mean: 64.87041889426757 usec\nrounds: 17095"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 13650.34597239843,
            "unit": "iter/sec",
            "range": "stddev: 0.000055454250069605913",
            "extra": "mean: 73.25821646001074 usec\nrounds: 13633"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6378.892876562148,
            "unit": "iter/sec",
            "range": "stddev: 0.00009487243793184413",
            "extra": "mean: 156.76701574254085 usec\nrounds: 5844"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15502.748496138931,
            "unit": "iter/sec",
            "range": "stddev: 0.000018758818649899764",
            "extra": "mean: 64.50469091007037 usec\nrounds: 17228"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53547.37750015731,
            "unit": "iter/sec",
            "range": "stddev: 0.000021471555829081913",
            "extra": "mean: 18.675050892960396 usec\nrounds: 46254"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 222037.2754847366,
            "unit": "iter/sec",
            "range": "stddev: 0.000005400839087649758",
            "extra": "mean: 4.503748290987936 usec\nrounds: 111907"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 139.59629065438983,
            "unit": "iter/sec",
            "range": "stddev: 0.01530043292618211",
            "extra": "mean: 7.163514125714008 msec\nrounds: 175"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4255.800446157089,
            "unit": "iter/sec",
            "range": "stddev: 0.00005046537254316957",
            "extra": "mean: 234.97342336692077 usec\nrounds: 3980"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 243.62231726834682,
            "unit": "iter/sec",
            "range": "stddev: 0.00028917335851173684",
            "extra": "mean: 4.104714261044127 msec\nrounds: 249"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 166.2971815591892,
            "unit": "iter/sec",
            "range": "stddev: 0.0005051410046206682",
            "extra": "mean: 6.013331017543889 msec\nrounds: 171"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 182.16620118434227,
            "unit": "iter/sec",
            "range": "stddev: 0.00037845160537671786",
            "extra": "mean: 5.489492526596932 msec\nrounds: 188"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 89.37094493683912,
            "unit": "iter/sec",
            "range": "stddev: 0.0007453348922825338",
            "extra": "mean: 11.189318863157675 msec\nrounds: 95"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 132.2609075744388,
            "unit": "iter/sec",
            "range": "stddev: 0.0005613802066119309",
            "extra": "mean: 7.560813080291183 msec\nrounds: 137"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 57.90320237736115,
            "unit": "iter/sec",
            "range": "stddev: 0.024044437223093545",
            "extra": "mean: 17.27020197402722 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 5.353429101162226,
            "unit": "iter/sec",
            "range": "stddev: 0.014038103170174064",
            "extra": "mean: 186.7961601999923 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 784.2816873815559,
            "unit": "iter/sec",
            "range": "stddev: 0.00015041902297713514",
            "extra": "mean: 1.2750520840779194 msec\nrounds: 785"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1690817.453200145,
            "unit": "iter/sec",
            "range": "stddev: 7.127775395284981e-8",
            "extra": "mean: 591.429901617906 nsec\nrounds: 168322"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 820336.4462505566,
            "unit": "iter/sec",
            "range": "stddev: 1.0007332290449851e-7",
            "extra": "mean: 1.2190120341118775 usec\nrounds: 82291"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23709.08044206323,
            "unit": "iter/sec",
            "range": "stddev: 0.0000018086318271355194",
            "extra": "mean: 42.177932731033295 usec\nrounds: 24023"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1506.457653264142,
            "unit": "iter/sec",
            "range": "stddev: 0.0000057318128582656666",
            "extra": "mean: 663.808901520221 usec\nrounds: 1513"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 150.9749316962014,
            "unit": "iter/sec",
            "range": "stddev: 0.000013809103611172748",
            "extra": "mean: 6.6236161776330205 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10198.518818992803,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025080550064185755",
            "extra": "mean: 98.05345440336787 usec\nrounds: 10253"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1435.7554117595569,
            "unit": "iter/sec",
            "range": "stddev: 0.000006701630764302255",
            "extra": "mean: 696.4974617608949 usec\nrounds: 1386"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 70229.77235834987,
            "unit": "iter/sec",
            "range": "stddev: 0.000014718120775964137",
            "extra": "mean: 14.238975386357014 usec\nrounds: 62973"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1149883.916502226,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014140505035735479",
            "extra": "mean: 869.6530020541984 nsec\nrounds: 117303"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5459.0598593123195,
            "unit": "iter/sec",
            "range": "stddev: 0.00005181742451402052",
            "extra": "mean: 183.18172465065632 usec\nrounds: 5306"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 253.9922169245129,
            "unit": "iter/sec",
            "range": "stddev: 0.000275291721685123",
            "extra": "mean: 3.937128515623778 msec\nrounds: 256"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 70188.41924052256,
            "unit": "iter/sec",
            "range": "stddev: 0.0000163940805965133",
            "extra": "mean: 14.247364605451326 usec\nrounds: 64818"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1977.4306727492735,
            "unit": "iter/sec",
            "range": "stddev: 0.0015459627594309152",
            "extra": "mean: 505.70673034502585 usec\nrounds: 2811"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 352466.28459728963,
            "unit": "iter/sec",
            "range": "stddev: 0.000008088537937104742",
            "extra": "mean: 2.8371507962599885 usec\nrounds: 179212"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3069.253738852611,
            "unit": "iter/sec",
            "range": "stddev: 0.00003086850790404174",
            "extra": "mean: 325.8120980163188 usec\nrounds: 2571"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2555.826814074562,
            "unit": "iter/sec",
            "range": "stddev: 0.000029409370045812347",
            "extra": "mean: 391.26281737602375 usec\nrounds: 1692"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4894.369399667342,
            "unit": "iter/sec",
            "range": "stddev: 0.000029577599601197342",
            "extra": "mean: 204.31641307416794 usec\nrounds: 3595"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1084.3439871102078,
            "unit": "iter/sec",
            "range": "stddev: 0.00009518156716587463",
            "extra": "mean: 922.216576923172 usec\nrounds: 650"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 365773.0577323092,
            "unit": "iter/sec",
            "range": "stddev: 5.408949539824985e-7",
            "extra": "mean: 2.7339356435920146 usec\nrounds: 112918"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 126694.3121211422,
            "unit": "iter/sec",
            "range": "stddev: 0.0000015677589532450019",
            "extra": "mean: 7.893014163444235 usec\nrounds: 79854"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 277224.7095754687,
            "unit": "iter/sec",
            "range": "stddev: 8.428793652850028e-7",
            "extra": "mean: 3.6071820637177754 usec\nrounds: 165810"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.018961785491044,
            "unit": "iter/sec",
            "range": "stddev: 0.0009612026888909044",
            "extra": "mean: 23.798779348834255 msec\nrounds: 43"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.576432638485088,
            "unit": "iter/sec",
            "range": "stddev: 0.0075348048060423405",
            "extra": "mean: 218.51080940000998 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1379.5085391971456,
            "unit": "iter/sec",
            "range": "stddev: 0.00040847116409911286",
            "extra": "mean: 724.8958390514827 usec\nrounds: 1603"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 388.26354077776256,
            "unit": "iter/sec",
            "range": "stddev: 0.0004776535553599087",
            "extra": "mean: 2.575570186159684 msec\nrounds: 419"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 953091.1678041399,
            "unit": "iter/sec",
            "range": "stddev: 1.0888640648696513e-7",
            "extra": "mean: 1.049217570973756 usec\nrounds: 94787"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 466262.4515798124,
            "unit": "iter/sec",
            "range": "stddev: 1.7581427871665882e-7",
            "extra": "mean: 2.144714841634262 usec\nrounds: 46383"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59423.283548603664,
            "unit": "iter/sec",
            "range": "stddev: 0.000001366054337227611",
            "extra": "mean: 16.82842044872995 usec\nrounds: 59446"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3972.040813919348,
            "unit": "iter/sec",
            "range": "stddev: 0.000007629022133805923",
            "extra": "mean: 251.75974942041597 usec\nrounds: 3883"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 378.71460884527534,
            "unit": "iter/sec",
            "range": "stddev: 0.00006266236119103584",
            "extra": "mean: 2.6405107609898204 msec\nrounds: 364"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18356.424276757854,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027879936806342928",
            "extra": "mean: 54.47684063753956 usec\nrounds: 17821"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18326.798535962636,
            "unit": "iter/sec",
            "range": "stddev: 0.000003385430639388613",
            "extra": "mean: 54.56490385037529 usec\nrounds: 18492"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18344.64415412992,
            "unit": "iter/sec",
            "range": "stddev: 0.000002937840374709253",
            "extra": "mean: 54.51182326558624 usec\nrounds: 18491"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1506.0441281214614,
            "unit": "iter/sec",
            "range": "stddev: 0.0000055098713466119224",
            "extra": "mean: 663.9911682052325 usec\nrounds: 1516"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 150.9664075846408,
            "unit": "iter/sec",
            "range": "stddev: 0.000013566783978733205",
            "extra": "mean: 6.623990171054049 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 40138.819770920476,
            "unit": "iter/sec",
            "range": "stddev: 0.000002035451875451245",
            "extra": "mean: 24.913537710056783 usec\nrounds: 40427"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3915.2008169257315,
            "unit": "iter/sec",
            "range": "stddev: 0.000018600759284117725",
            "extra": "mean: 255.4147403313053 usec\nrounds: 3982"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8039.723999051629,
            "unit": "iter/sec",
            "range": "stddev: 0.000005446178744535836",
            "extra": "mean: 124.38237931027989 usec\nrounds: 8265"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1226.8125023224356,
            "unit": "iter/sec",
            "range": "stddev: 0.00001678033135159076",
            "extra": "mean: 815.1204834536126 usec\nrounds: 1239"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 425.7571416065745,
            "unit": "iter/sec",
            "range": "stddev: 0.000022998812067279173",
            "extra": "mean: 2.3487568434590367 msec\nrounds: 428"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11312.31345911063,
            "unit": "iter/sec",
            "range": "stddev: 0.000004923190209766901",
            "extra": "mean: 88.399247741374 usec\nrounds: 10628"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1042.9198591366562,
            "unit": "iter/sec",
            "range": "stddev: 0.000022212256378266226",
            "extra": "mean: 958.8464456202935 usec\nrounds: 1039"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1048.2565369611386,
            "unit": "iter/sec",
            "range": "stddev: 0.00002715266169352945",
            "extra": "mean: 953.9649548945025 usec\nrounds: 1042"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1183.2881284717855,
            "unit": "iter/sec",
            "range": "stddev: 0.000022812988987218496",
            "extra": "mean: 845.1027065500084 usec\nrounds: 1145"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1624.9746521503946,
            "unit": "iter/sec",
            "range": "stddev: 0.000020770541894879585",
            "extra": "mean: 615.3942147200016 usec\nrounds: 1644"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 969.6673647496335,
            "unit": "iter/sec",
            "range": "stddev: 0.000027458574522864152",
            "extra": "mean: 1.0312814851288703 msec\nrounds: 975"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 997.6232298301894,
            "unit": "iter/sec",
            "range": "stddev: 0.00005326962328429731",
            "extra": "mean: 1.0023824326646997 msec\nrounds: 1047"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1002.6135331135998,
            "unit": "iter/sec",
            "range": "stddev: 0.000033131843618851695",
            "extra": "mean: 997.3932796363883 usec\nrounds: 987"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 747.8671882727125,
            "unit": "iter/sec",
            "range": "stddev: 0.00004148934509655864",
            "extra": "mean: 1.3371358119208545 msec\nrounds: 755"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1084.7864670727295,
            "unit": "iter/sec",
            "range": "stddev: 0.00002691784226254306",
            "extra": "mean: 921.8404085538385 usec\nrounds: 1099"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 945.3704952532665,
            "unit": "iter/sec",
            "range": "stddev: 0.000043566105192638884",
            "extra": "mean: 1.05778634410639 msec\nrounds: 959"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1632.748821199139,
            "unit": "iter/sec",
            "range": "stddev: 0.000029450209230049553",
            "extra": "mean: 612.4640771539925 usec\nrounds: 1672"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3121.7073246512177,
            "unit": "iter/sec",
            "range": "stddev: 0.00001617605026064046",
            "extra": "mean: 320.33752559161775 usec\nrounds: 3126"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3129.5401170564996,
            "unit": "iter/sec",
            "range": "stddev: 0.000016802810211708765",
            "extra": "mean: 319.5357664692772 usec\nrounds: 3203"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 3983.918746557622,
            "unit": "iter/sec",
            "range": "stddev: 0.000013313596672185685",
            "extra": "mean: 251.00913538060192 usec\nrounds: 3745"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7624.553049839948,
            "unit": "iter/sec",
            "range": "stddev: 0.000005549653036621286",
            "extra": "mean: 131.15522883285487 usec\nrounds: 7311"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 645.941678853062,
            "unit": "iter/sec",
            "range": "stddev: 0.00009210500732288413",
            "extra": "mean: 1.5481273816788015 msec\nrounds: 655"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4562.036584858973,
            "unit": "iter/sec",
            "range": "stddev: 0.00000751842235335603",
            "extra": "mean: 219.20034646782938 usec\nrounds: 4416"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 431.33362150140016,
            "unit": "iter/sec",
            "range": "stddev: 0.00008531676929884166",
            "extra": "mean: 2.3183910322575074 msec\nrounds: 434"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 610.3554898756587,
            "unit": "iter/sec",
            "range": "stddev: 0.00003474558775908306",
            "extra": "mean: 1.638389457599078 msec\nrounds: 625"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 8958.465618404058,
            "unit": "iter/sec",
            "range": "stddev: 0.000009622486752855997",
            "extra": "mean: 111.6262586246493 usec\nrounds: 8986"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 634.7994610941095,
            "unit": "iter/sec",
            "range": "stddev: 0.00003470535767322632",
            "extra": "mean: 1.5753006442010027 msec\nrounds: 638"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.385554405364059,
            "unit": "iter/sec",
            "range": "stddev: 0.0010483038654230086",
            "extra": "mean: 135.39944939999486 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 549.0918798193213,
            "unit": "iter/sec",
            "range": "stddev: 0.00006118135599563333",
            "extra": "mean: 1.8211888333315909 msec\nrounds: 564"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 358.9497662605443,
            "unit": "iter/sec",
            "range": "stddev: 0.00010829639950838889",
            "extra": "mean: 2.78590514326773 msec\nrounds: 349"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 760627.8135502869,
            "unit": "iter/sec",
            "range": "stddev: 1.0543197787358013e-7",
            "extra": "mean: 1.3147034360108731 usec\nrounds: 69994"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 546.1200793140597,
            "unit": "iter/sec",
            "range": "stddev: 0.00007127470421329194",
            "extra": "mean: 1.8310991261409482 msec\nrounds: 547"
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
        "date": 1771222521709,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 421.4055674041677,
            "unit": "iter/sec",
            "range": "stddev: 0.007572847169591702",
            "extra": "mean: 2.3730108886788996 msec\nrounds: 530"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 474.50344639787113,
            "unit": "iter/sec",
            "range": "stddev: 0.0004026743109325923",
            "extra": "mean: 2.1074662525454033 msec\nrounds: 491"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6107.59633095341,
            "unit": "iter/sec",
            "range": "stddev: 0.000022401260150734506",
            "extra": "mean: 163.7305325717061 usec\nrounds: 6739"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4686.729596107147,
            "unit": "iter/sec",
            "range": "stddev: 0.000031498523549272094",
            "extra": "mean: 213.36840103397725 usec\nrounds: 4062"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 407.0804599302125,
            "unit": "iter/sec",
            "range": "stddev: 0.0002605881463944207",
            "extra": "mean: 2.456516827585962 msec\nrounds: 493"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 334.31062724466176,
            "unit": "iter/sec",
            "range": "stddev: 0.001151507721145618",
            "extra": "mean: 2.991230067203818 msec\nrounds: 372"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 345.23577974503047,
            "unit": "iter/sec",
            "range": "stddev: 0.0007448477398307255",
            "extra": "mean: 2.896571151282574 msec\nrounds: 390"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 177.15634668380594,
            "unit": "iter/sec",
            "range": "stddev: 0.00936609269577047",
            "extra": "mean: 5.644731440442438 msec\nrounds: 361"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 318.8578543107296,
            "unit": "iter/sec",
            "range": "stddev: 0.0003972709462492698",
            "extra": "mean: 3.136193719178364 msec\nrounds: 292"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 13702.799094423517,
            "unit": "iter/sec",
            "range": "stddev: 0.00001931571048111065",
            "extra": "mean: 72.97779038495568 usec\nrounds: 16807"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14107.40248502309,
            "unit": "iter/sec",
            "range": "stddev: 0.000017560829824693103",
            "extra": "mean: 70.88477138591847 usec\nrounds: 17453"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 13006.967261792486,
            "unit": "iter/sec",
            "range": "stddev: 0.00006272970555519596",
            "extra": "mean: 76.88187260511259 usec\nrounds: 14145"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6510.608064823945,
            "unit": "iter/sec",
            "range": "stddev: 0.00008110651816216024",
            "extra": "mean: 153.59548448368184 usec\nrounds: 6316"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15212.020604218358,
            "unit": "iter/sec",
            "range": "stddev: 0.000019582415119887768",
            "extra": "mean: 65.73748655866899 usec\nrounds: 18376"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53404.002785737306,
            "unit": "iter/sec",
            "range": "stddev: 0.000022406896090664533",
            "extra": "mean: 18.725188147639592 usec\nrounds: 46995"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 214042.70902558297,
            "unit": "iter/sec",
            "range": "stddev: 0.000007957123515652696",
            "extra": "mean: 4.671964789421897 usec\nrounds: 188324"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 142.6555237064479,
            "unit": "iter/sec",
            "range": "stddev: 0.015398595783733598",
            "extra": "mean: 7.009893301136863 msec\nrounds: 176"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4289.5551939171155,
            "unit": "iter/sec",
            "range": "stddev: 0.00005147742767439695",
            "extra": "mean: 233.12440446460946 usec\nrounds: 4166"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 244.6802779346613,
            "unit": "iter/sec",
            "range": "stddev: 0.0002620293041319118",
            "extra": "mean: 4.0869660948604825 msec\nrounds: 253"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 167.86323827177563,
            "unit": "iter/sec",
            "range": "stddev: 0.0004411147955207352",
            "extra": "mean: 5.957230482954046 msec\nrounds: 176"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 184.7754081525789,
            "unit": "iter/sec",
            "range": "stddev: 0.00033376007277510674",
            "extra": "mean: 5.411975597825479 msec\nrounds: 184"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 91.62785100917579,
            "unit": "iter/sec",
            "range": "stddev: 0.0006007900965407138",
            "extra": "mean: 10.913712249999818 msec\nrounds: 96"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 133.39256491633233,
            "unit": "iter/sec",
            "range": "stddev: 0.0003755497216252452",
            "extra": "mean: 7.4966697028970755 msec\nrounds: 138"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 69.32875670614926,
            "unit": "iter/sec",
            "range": "stddev: 0.0014046964664671058",
            "extra": "mean: 14.424029039472199 msec\nrounds: 76"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 5.722361070990409,
            "unit": "iter/sec",
            "range": "stddev: 0.0027800234447956018",
            "extra": "mean: 174.75304119998896 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 778.8446949599404,
            "unit": "iter/sec",
            "range": "stddev: 0.00013995910103248906",
            "extra": "mean: 1.2839530223049598 msec\nrounds: 807"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1648121.354107171,
            "unit": "iter/sec",
            "range": "stddev: 7.345077489658762e-8",
            "extra": "mean: 606.7514370273574 nsec\nrounds: 163079"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 815854.1208085904,
            "unit": "iter/sec",
            "range": "stddev: 9.822258297502074e-8",
            "extra": "mean: 1.225709320446777 usec\nrounds: 81820"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23730.28623577736,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017448190626119181",
            "extra": "mean: 42.14024180173324 usec\nrounds: 23908"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1507.9363660085885,
            "unit": "iter/sec",
            "range": "stddev: 0.00000528176747279195",
            "extra": "mean: 663.1579571536804 usec\nrounds: 1517"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 151.1718799878753,
            "unit": "iter/sec",
            "range": "stddev: 0.00000662467048562738",
            "extra": "mean: 6.61498686184365 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10203.339545088902,
            "unit": "iter/sec",
            "range": "stddev: 0.000002431021893055137",
            "extra": "mean: 98.00712752731263 usec\nrounds: 10288"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1443.8532788911064,
            "unit": "iter/sec",
            "range": "stddev: 0.000005744563735044264",
            "extra": "mean: 692.5911480202544 usec\nrounds: 1439"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 71170.15188501288,
            "unit": "iter/sec",
            "range": "stddev: 0.000017530664535062137",
            "extra": "mean: 14.050834142038436 usec\nrounds: 62035"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1176430.3227524243,
            "unit": "iter/sec",
            "range": "stddev: 8.163164922826263e-7",
            "extra": "mean: 850.029092807094 nsec\nrounds: 115929"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5403.97548002745,
            "unit": "iter/sec",
            "range": "stddev: 0.00005732372979699248",
            "extra": "mean: 185.0489521456749 usec\nrounds: 5266"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 257.26510957963524,
            "unit": "iter/sec",
            "range": "stddev: 0.0002474960912609019",
            "extra": "mean: 3.887040888031708 msec\nrounds: 259"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 70810.59453808813,
            "unit": "iter/sec",
            "range": "stddev: 0.000018303293693047686",
            "extra": "mean: 14.122180537011484 usec\nrounds: 65067"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1962.4488206188346,
            "unit": "iter/sec",
            "range": "stddev: 0.0018459811037402662",
            "extra": "mean: 509.56742896594983 usec\nrounds: 3625"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 346564.6710797762,
            "unit": "iter/sec",
            "range": "stddev: 0.000005047203386141348",
            "extra": "mean: 2.8854643402754943 usec\nrounds: 175408"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3159.901126079362,
            "unit": "iter/sec",
            "range": "stddev: 0.000026458239588698527",
            "extra": "mean: 316.46559816279665 usec\nrounds: 2613"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2575.1030267996016,
            "unit": "iter/sec",
            "range": "stddev: 0.000029872643596174836",
            "extra": "mean: 388.33397716239085 usec\nrounds: 1664"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5095.770393624883,
            "unit": "iter/sec",
            "range": "stddev: 0.000040397483434527625",
            "extra": "mean: 196.24118097060662 usec\nrounds: 2680"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1023.6632414267988,
            "unit": "iter/sec",
            "range": "stddev: 0.000016243133487132382",
            "extra": "mean: 976.8837636547187 usec\nrounds: 1007"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 361920.3163521827,
            "unit": "iter/sec",
            "range": "stddev: 5.094626894255342e-7",
            "extra": "mean: 2.763039140988442 usec\nrounds: 110779"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 128093.27252676286,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013082183655150234",
            "extra": "mean: 7.8068112421053755 usec\nrounds: 75965"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 280633.0848754914,
            "unit": "iter/sec",
            "range": "stddev: 8.216608105533825e-7",
            "extra": "mean: 3.5633717259091897 usec\nrounds: 145731"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 44.51000343457636,
            "unit": "iter/sec",
            "range": "stddev: 0.0006943611699396486",
            "extra": "mean: 22.466859645829135 msec\nrounds: 48"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.555137753765928,
            "unit": "iter/sec",
            "range": "stddev: 0.010669979719719513",
            "extra": "mean: 219.53232899998625 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1422.7988940676282,
            "unit": "iter/sec",
            "range": "stddev: 0.0003457671464216293",
            "extra": "mean: 702.8400177772897 usec\nrounds: 1575"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 388.6192930408631,
            "unit": "iter/sec",
            "range": "stddev: 0.000585728144769098",
            "extra": "mean: 2.573212441861065 msec\nrounds: 387"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 942771.2389873845,
            "unit": "iter/sec",
            "range": "stddev: 1.0588444811611564e-7",
            "extra": "mean: 1.0607027014040904 usec\nrounds: 92507"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 466383.1200331098,
            "unit": "iter/sec",
            "range": "stddev: 2.001359842618986e-7",
            "extra": "mean: 2.1441599342810846 usec\nrounds: 47170"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59367.9920357607,
            "unit": "iter/sec",
            "range": "stddev: 0.0000011940004976116433",
            "extra": "mean: 16.844093352486027 usec\nrounds: 60202"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3989.1178182024264,
            "unit": "iter/sec",
            "range": "stddev: 0.00000622442830944322",
            "extra": "mean: 250.68199175190554 usec\nrounds: 4001"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 399.06703448213483,
            "unit": "iter/sec",
            "range": "stddev: 0.000033446569901686774",
            "extra": "mean: 2.505844666667819 msec\nrounds: 402"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18194.21085516005,
            "unit": "iter/sec",
            "range": "stddev: 0.00000280477816451056",
            "extra": "mean: 54.96253769733522 usec\nrounds: 17216"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 17891.95813612813,
            "unit": "iter/sec",
            "range": "stddev: 0.000007193532477281519",
            "extra": "mean: 55.89103173569144 usec\nrounds: 18339"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 17492.158611768988,
            "unit": "iter/sec",
            "range": "stddev: 0.000007232644136277379",
            "extra": "mean: 57.16847315386134 usec\nrounds: 18457"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1505.5462520674673,
            "unit": "iter/sec",
            "range": "stddev: 0.000005716818842706174",
            "extra": "mean: 664.2107465159347 usec\nrounds: 1507"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 150.86936850768626,
            "unit": "iter/sec",
            "range": "stddev: 0.000015376339151570834",
            "extra": "mean: 6.628250717103344 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39703.750491826904,
            "unit": "iter/sec",
            "range": "stddev: 0.000002339422957865169",
            "extra": "mean: 25.18653748355214 usec\nrounds: 39484"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3893.22806091914,
            "unit": "iter/sec",
            "range": "stddev: 0.000009039433675866166",
            "extra": "mean: 256.8562602427953 usec\nrounds: 3954"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8148.34019407794,
            "unit": "iter/sec",
            "range": "stddev: 0.0000068180443381049435",
            "extra": "mean: 122.7243802028272 usec\nrounds: 8193"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1252.7944096437325,
            "unit": "iter/sec",
            "range": "stddev: 0.00003741612369378617",
            "extra": "mean: 798.2155669774885 usec\nrounds: 1284"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 432.1372907901113,
            "unit": "iter/sec",
            "range": "stddev: 0.000029255271885619464",
            "extra": "mean: 2.3140793940083713 msec\nrounds: 434"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11401.853233035514,
            "unit": "iter/sec",
            "range": "stddev: 0.000004620718927587104",
            "extra": "mean: 87.70504053697331 usec\nrounds: 10879"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1025.7715656398107,
            "unit": "iter/sec",
            "range": "stddev: 0.00002639191053798792",
            "extra": "mean: 974.8759212059695 usec\nrounds: 1028"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1028.9206823460108,
            "unit": "iter/sec",
            "range": "stddev: 0.000019997615978297695",
            "extra": "mean: 971.8922140042228 usec\nrounds: 1014"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1181.7351382504198,
            "unit": "iter/sec",
            "range": "stddev: 0.000018099819525304485",
            "extra": "mean: 846.2133075610478 usec\nrounds: 1164"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1586.5480850170447,
            "unit": "iter/sec",
            "range": "stddev: 0.00001710286293896009",
            "extra": "mean: 630.2992070922684 usec\nrounds: 1579"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 943.7796259351857,
            "unit": "iter/sec",
            "range": "stddev: 0.00002820056276516746",
            "extra": "mean: 1.0595693873017293 msec\nrounds: 945"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1026.3922903261787,
            "unit": "iter/sec",
            "range": "stddev: 0.00003284070198254055",
            "extra": "mean: 974.2863517439405 usec\nrounds: 1032"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1015.6742585824895,
            "unit": "iter/sec",
            "range": "stddev: 0.00002865766926902986",
            "extra": "mean: 984.5676323387727 usec\nrounds: 1039"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 759.5851735295179,
            "unit": "iter/sec",
            "range": "stddev: 0.00002034751748741845",
            "extra": "mean: 1.3165080557764988 msec\nrounds: 753"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1104.9745367219323,
            "unit": "iter/sec",
            "range": "stddev: 0.000020411135971881214",
            "extra": "mean: 904.9982300647808 usec\nrounds: 1091"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 930.5462790919207,
            "unit": "iter/sec",
            "range": "stddev: 0.0000202439412197982",
            "extra": "mean: 1.074637578451075 msec\nrounds: 956"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1614.7195984411183,
            "unit": "iter/sec",
            "range": "stddev: 0.00001834906107821681",
            "extra": "mean: 619.3025717687575 usec\nrounds: 1679"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3067.016061641947,
            "unit": "iter/sec",
            "range": "stddev: 0.000012358110257404319",
            "extra": "mean: 326.04980864190304 usec\nrounds: 3078"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3067.969874354483,
            "unit": "iter/sec",
            "range": "stddev: 0.000011589773158536924",
            "extra": "mean: 325.94844178853134 usec\nrounds: 2929"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4003.0456400899493,
            "unit": "iter/sec",
            "range": "stddev: 0.000009778684256579578",
            "extra": "mean: 249.8097923204118 usec\nrounds: 3828"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7811.113812586113,
            "unit": "iter/sec",
            "range": "stddev: 0.000007371689243105133",
            "extra": "mean: 128.02271532501442 usec\nrounds: 7700"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 652.8668001765444,
            "unit": "iter/sec",
            "range": "stddev: 0.000023980948914665025",
            "extra": "mean: 1.5317060076107192 msec\nrounds: 657"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4698.476690118805,
            "unit": "iter/sec",
            "range": "stddev: 0.000010784788613177272",
            "extra": "mean: 212.83493905653796 usec\nrounds: 4578"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 437.14646969061124,
            "unit": "iter/sec",
            "range": "stddev: 0.00003179556897440532",
            "extra": "mean: 2.2875627949317447 msec\nrounds: 434"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 613.660637095125,
            "unit": "iter/sec",
            "range": "stddev: 0.00003617892650367759",
            "extra": "mean: 1.6295651693315103 msec\nrounds: 626"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 9332.584297398413,
            "unit": "iter/sec",
            "range": "stddev: 0.000004254042494544697",
            "extra": "mean: 107.15145645978936 usec\nrounds: 9164"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 645.4862663389762,
            "unit": "iter/sec",
            "range": "stddev: 0.000026405771357997436",
            "extra": "mean: 1.5492196381988574 msec\nrounds: 644"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.449235987622486,
            "unit": "iter/sec",
            "range": "stddev: 0.0006099020687986837",
            "extra": "mean: 134.2419546999963 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 558.3302150962876,
            "unit": "iter/sec",
            "range": "stddev: 0.000050023047445159704",
            "extra": "mean: 1.7910547789851272 msec\nrounds: 552"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 371.1099279917935,
            "unit": "iter/sec",
            "range": "stddev: 0.00004884347086796868",
            "extra": "mean: 2.694619369013791 msec\nrounds: 355"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 750899.9522880001,
            "unit": "iter/sec",
            "range": "stddev: 1.2432987509518665e-7",
            "extra": "mean: 1.3317353356502282 usec\nrounds: 73665"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 553.7039802509559,
            "unit": "iter/sec",
            "range": "stddev: 0.00006127952923173089",
            "extra": "mean: 1.8060191648735646 msec\nrounds: 558"
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
        "date": 1771223701205,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 341.6829846712853,
            "unit": "iter/sec",
            "range": "stddev: 0.005848920642739884",
            "extra": "mean: 2.9266894895630986 msec\nrounds: 527"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 322.9563318264388,
            "unit": "iter/sec",
            "range": "stddev: 0.008160016523381849",
            "extra": "mean: 3.09639385097244 msec\nrounds: 463"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6234.696373874726,
            "unit": "iter/sec",
            "range": "stddev: 0.000018364995185044574",
            "extra": "mean: 160.39273447064787 usec\nrounds: 5393"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4615.502259195862,
            "unit": "iter/sec",
            "range": "stddev: 0.000028935307523456908",
            "extra": "mean: 216.6611440840732 usec\nrounds: 4775"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 291.47760844950307,
            "unit": "iter/sec",
            "range": "stddev: 0.0011439842445615548",
            "extra": "mean: 3.430795268698126 msec\nrounds: 361"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 311.44711942955564,
            "unit": "iter/sec",
            "range": "stddev: 0.0011610800356528174",
            "extra": "mean: 3.210817945054663 msec\nrounds: 364"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 295.7141500615898,
            "unit": "iter/sec",
            "range": "stddev: 0.0011671612210347833",
            "extra": "mean: 3.3816440633352345 msec\nrounds: 300"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 291.9818099646808,
            "unit": "iter/sec",
            "range": "stddev: 0.0009625269463701617",
            "extra": "mean: 3.4248708853505767 msec\nrounds: 314"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 262.3913818124666,
            "unit": "iter/sec",
            "range": "stddev: 0.0024691457455035615",
            "extra": "mean: 3.8111007804162895 msec\nrounds: 337"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 16727.08348400505,
            "unit": "iter/sec",
            "range": "stddev: 0.00001631673960099257",
            "extra": "mean: 59.78328505123028 usec\nrounds: 16804"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 13866.863826007007,
            "unit": "iter/sec",
            "range": "stddev: 0.000030961088650561166",
            "extra": "mean: 72.11435927743959 usec\nrounds: 16998"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 12818.019845522002,
            "unit": "iter/sec",
            "range": "stddev: 0.00007049113638801794",
            "extra": "mean: 78.01517020972251 usec\nrounds: 13595"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6384.31565287901,
            "unit": "iter/sec",
            "range": "stddev: 0.0000949079420848319",
            "extra": "mean: 156.63385934701546 usec\nrounds: 5638"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 15712.356996652188,
            "unit": "iter/sec",
            "range": "stddev: 0.000016824219738852755",
            "extra": "mean: 63.64417510454152 usec\nrounds: 16967"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53556.30189572893,
            "unit": "iter/sec",
            "range": "stddev: 0.000015072449197707426",
            "extra": "mean: 18.671938961486603 usec\nrounds: 40925"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 216678.12308322857,
            "unit": "iter/sec",
            "range": "stddev: 0.000006928613837324156",
            "extra": "mean: 4.6151405862782395 usec\nrounds: 180800"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 147.82003105924298,
            "unit": "iter/sec",
            "range": "stddev: 0.0014147176513139958",
            "extra": "mean: 6.764983019109381 msec\nrounds: 157"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4225.801441591521,
            "unit": "iter/sec",
            "range": "stddev: 0.00006673647184125988",
            "extra": "mean: 236.64150193090472 usec\nrounds: 4144"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 242.60884087213438,
            "unit": "iter/sec",
            "range": "stddev: 0.0003772497275913376",
            "extra": "mean: 4.121861332032184 msec\nrounds: 256"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 171.26660380409447,
            "unit": "iter/sec",
            "range": "stddev: 0.0004177894939994609",
            "extra": "mean: 5.838849943821288 msec\nrounds: 178"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 186.91201148369908,
            "unit": "iter/sec",
            "range": "stddev: 0.00035142606020299044",
            "extra": "mean: 5.350110953608841 msec\nrounds: 194"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 73.17211249631094,
            "unit": "iter/sec",
            "range": "stddev: 0.020919630507831473",
            "extra": "mean: 13.666408770833508 msec\nrounds: 96"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 133.63750434682692,
            "unit": "iter/sec",
            "range": "stddev: 0.0004971945171035453",
            "extra": "mean: 7.482929323528212 msec\nrounds: 136"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 56.01934024675215,
            "unit": "iter/sec",
            "range": "stddev: 0.02761081274222008",
            "extra": "mean: 17.850977815790632 msec\nrounds: 76"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 5.164628610564991,
            "unit": "iter/sec",
            "range": "stddev: 0.01635028777587631",
            "extra": "mean: 193.62476480000055 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 778.5537266420956,
            "unit": "iter/sec",
            "range": "stddev: 0.0001570214884052583",
            "extra": "mean: 1.2844328731338848 msec\nrounds: 804"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1633699.8519744414,
            "unit": "iter/sec",
            "range": "stddev: 7.681617088030076e-8",
            "extra": "mean: 612.107541536121 nsec\nrounds: 162023"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 808793.5051188447,
            "unit": "iter/sec",
            "range": "stddev: 1.5637818329173257e-7",
            "extra": "mean: 1.2364095330526415 usec\nrounds: 80887"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23681.39846627714,
            "unit": "iter/sec",
            "range": "stddev: 0.000002692525897078392",
            "extra": "mean: 42.22723592206867 usec\nrounds: 23885"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1507.6132674528371,
            "unit": "iter/sec",
            "range": "stddev: 0.000005316225689332191",
            "extra": "mean: 663.3000793960465 usec\nrounds: 1524"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 151.11262495914818,
            "unit": "iter/sec",
            "range": "stddev: 0.000010066304675919498",
            "extra": "mean: 6.617580763158209 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10202.067350617079,
            "unit": "iter/sec",
            "range": "stddev: 0.000002483472397982199",
            "extra": "mean: 98.01934898415607 usec\nrounds: 10287"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1441.4694660074088,
            "unit": "iter/sec",
            "range": "stddev: 0.0000054499854443414945",
            "extra": "mean: 693.7365123451463 usec\nrounds: 1458"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 72175.1648433841,
            "unit": "iter/sec",
            "range": "stddev: 0.000014332267877980112",
            "extra": "mean: 13.855181379494478 usec\nrounds: 65669"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1143773.655338741,
            "unit": "iter/sec",
            "range": "stddev: 0.0000010924743138799684",
            "extra": "mean: 874.2988574114685 nsec\nrounds: 115527"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5462.912961150183,
            "unit": "iter/sec",
            "range": "stddev: 0.00007330445482927966",
            "extra": "mean: 183.05252291434203 usec\nrounds: 5324"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 258.8624844234541,
            "unit": "iter/sec",
            "range": "stddev: 0.00034097031816968516",
            "extra": "mean: 3.8630549429641325 msec\nrounds: 263"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 70000.54755936738,
            "unit": "iter/sec",
            "range": "stddev: 0.000020386684887796908",
            "extra": "mean: 14.285602539778724 usec\nrounds: 67486"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1635.0414745029937,
            "unit": "iter/sec",
            "range": "stddev: 0.00041702930476647174",
            "extra": "mean: 611.6052807186263 usec\nrounds: 3060"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 360139.13925693213,
            "unit": "iter/sec",
            "range": "stddev: 0.000004084581615388612",
            "extra": "mean: 2.7767045871861638 usec\nrounds: 178540"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3057.8250949537965,
            "unit": "iter/sec",
            "range": "stddev: 0.000026157256994992355",
            "extra": "mean: 327.0298231413756 usec\nrounds: 2731"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2592.7530272815884,
            "unit": "iter/sec",
            "range": "stddev: 0.0000316636477301511",
            "extra": "mean: 385.69041843852955 usec\nrounds: 1692"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4925.328265132599,
            "unit": "iter/sec",
            "range": "stddev: 0.000029257210282688145",
            "extra": "mean: 203.0321526139087 usec\nrounds: 2621"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1103.4691169222417,
            "unit": "iter/sec",
            "range": "stddev: 0.00008621164413300592",
            "extra": "mean: 906.2328837885068 usec\nrounds: 697"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 366938.40198409156,
            "unit": "iter/sec",
            "range": "stddev: 5.113391817669373e-7",
            "extra": "mean: 2.725253052263945 usec\nrounds: 110902"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 126574.50343195487,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016170465665714657",
            "extra": "mean: 7.90048527061842 usec\nrounds: 78346"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 272336.98026258976,
            "unit": "iter/sec",
            "range": "stddev: 0.0000010362990462910999",
            "extra": "mean: 3.6719214520032906 usec\nrounds: 158680"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 35.122501357807636,
            "unit": "iter/sec",
            "range": "stddev: 0.0017998826839600085",
            "extra": "mean: 28.47177625000512 msec\nrounds: 40"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 3.9936312460278653,
            "unit": "iter/sec",
            "range": "stddev: 0.004486198587846249",
            "extra": "mean: 250.39868189999197 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1424.0888631168725,
            "unit": "iter/sec",
            "range": "stddev: 0.00026263818123555075",
            "extra": "mean: 702.203370800416 usec\nrounds: 1548"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 387.44639434481593,
            "unit": "iter/sec",
            "range": "stddev: 0.0005925685606958875",
            "extra": "mean: 2.581002209843846 msec\nrounds: 386"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 952855.2702283992,
            "unit": "iter/sec",
            "range": "stddev: 1.4938167852375396e-7",
            "extra": "mean: 1.0494773248830331 usec\nrounds: 97371"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 475565.9844173662,
            "unit": "iter/sec",
            "range": "stddev: 1.528208365604001e-7",
            "extra": "mean: 2.102757625159288 usec\nrounds: 47599"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59712.08352713881,
            "unit": "iter/sec",
            "range": "stddev: 0.000001595509510808745",
            "extra": "mean: 16.74702909245338 usec\nrounds: 59981"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3939.054140779309,
            "unit": "iter/sec",
            "range": "stddev: 0.000014049818793804407",
            "extra": "mean: 253.86805163387737 usec\nrounds: 4009"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 396.15296374495057,
            "unit": "iter/sec",
            "range": "stddev: 0.00002916741316135609",
            "extra": "mean: 2.524277467336621 msec\nrounds: 398"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18387.08738168737,
            "unit": "iter/sec",
            "range": "stddev: 0.000003557927924453158",
            "extra": "mean: 54.3859926937613 usec\nrounds: 18477"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18328.653327201828,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027290510930304637",
            "extra": "mean: 54.5593820859651 usec\nrounds: 18488"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18020.706301007056,
            "unit": "iter/sec",
            "range": "stddev: 0.000006602768359256724",
            "extra": "mean: 55.491720651599366 usec\nrounds: 18536"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1506.9684438150014,
            "unit": "iter/sec",
            "range": "stddev: 0.000005314454514074571",
            "extra": "mean: 663.5839019086733 usec\nrounds: 1519"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 150.98178638169887,
            "unit": "iter/sec",
            "range": "stddev: 0.000012392000385118853",
            "extra": "mean: 6.6233154605277225 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39826.8306507787,
            "unit": "iter/sec",
            "range": "stddev: 0.000001743677520608801",
            "extra": "mean: 25.108701437191765 usec\nrounds: 40005"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3915.289750128646,
            "unit": "iter/sec",
            "range": "stddev: 0.000017999660263639042",
            "extra": "mean: 255.40893875533544 usec\nrounds: 3984"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8047.905197039351,
            "unit": "iter/sec",
            "range": "stddev: 0.000005068366649282075",
            "extra": "mean: 124.25593685769039 usec\nrounds: 8267"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1246.8422382981478,
            "unit": "iter/sec",
            "range": "stddev: 0.000024060910704639688",
            "extra": "mean: 802.0260858061159 usec\nrounds: 1247"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 431.5591087214868,
            "unit": "iter/sec",
            "range": "stddev: 0.000041340702049775967",
            "extra": "mean: 2.317179685912655 msec\nrounds: 433"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11619.585683239238,
            "unit": "iter/sec",
            "range": "stddev: 0.000004793865014016662",
            "extra": "mean: 86.06158836131806 usec\nrounds: 9331"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1012.0755747301055,
            "unit": "iter/sec",
            "range": "stddev: 0.000021109573276955492",
            "extra": "mean: 988.0685049302512 usec\nrounds: 1014"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1013.1798424606869,
            "unit": "iter/sec",
            "range": "stddev: 0.00001997173232421237",
            "extra": "mean: 986.991606121301 usec\nrounds: 1013"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1173.4544680596553,
            "unit": "iter/sec",
            "range": "stddev: 0.00001786865978918446",
            "extra": "mean: 852.1847478697083 usec\nrounds: 1174"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1576.634814063458,
            "unit": "iter/sec",
            "range": "stddev: 0.000018855108257460232",
            "extra": "mean: 634.2622851405278 usec\nrounds: 1494"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 957.4987911143044,
            "unit": "iter/sec",
            "range": "stddev: 0.000018562994563580773",
            "extra": "mean: 1.044387741561777 msec\nrounds: 948"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1027.9487228967637,
            "unit": "iter/sec",
            "range": "stddev: 0.000032782845031307886",
            "extra": "mean: 972.8111701739323 usec\nrounds: 1046"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1028.2333333843949,
            "unit": "iter/sec",
            "range": "stddev: 0.00004205206676576744",
            "extra": "mean: 972.5419002985773 usec\nrounds: 1013"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 720.315220091001,
            "unit": "iter/sec",
            "range": "stddev: 0.000022299117159231817",
            "extra": "mean: 1.388281091538875 msec\nrounds: 721"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1079.1711175317623,
            "unit": "iter/sec",
            "range": "stddev: 0.00001681952682905068",
            "extra": "mean: 926.6371048617021 usec\nrounds: 1049"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 947.4657586366147,
            "unit": "iter/sec",
            "range": "stddev: 0.000014424933210774928",
            "extra": "mean: 1.055447113401735 msec\nrounds: 970"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1621.7323977655033,
            "unit": "iter/sec",
            "range": "stddev: 0.00004448081536765138",
            "extra": "mean: 616.6245438383332 usec\nrounds: 1688"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3154.5048848746624,
            "unit": "iter/sec",
            "range": "stddev: 0.000013413482367309612",
            "extra": "mean: 317.00695877658563 usec\nrounds: 3008"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3118.8522675023964,
            "unit": "iter/sec",
            "range": "stddev: 0.00001552151605967795",
            "extra": "mean: 320.6307687028756 usec\nrounds: 3061"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4033.3196928422035,
            "unit": "iter/sec",
            "range": "stddev: 0.000016353717504767088",
            "extra": "mean: 247.9347227978646 usec\nrounds: 3474"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7719.445652910322,
            "unit": "iter/sec",
            "range": "stddev: 0.0000065044559504527576",
            "extra": "mean: 129.54298079979205 usec\nrounds: 7552"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 508.09065743633556,
            "unit": "iter/sec",
            "range": "stddev: 0.00938706245878985",
            "extra": "mean: 1.9681527014208116 msec\nrounds: 633"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4639.163175820092,
            "unit": "iter/sec",
            "range": "stddev: 0.000010574580929152333",
            "extra": "mean: 215.5561169333571 usec\nrounds: 4618"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 426.5164938958196,
            "unit": "iter/sec",
            "range": "stddev: 0.000034537409386072295",
            "extra": "mean: 2.3445752141164764 msec\nrounds: 425"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 498.2575531808648,
            "unit": "iter/sec",
            "range": "stddev: 0.008269381054777867",
            "extra": "mean: 2.0069941611843567 msec\nrounds: 608"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 9080.366492791354,
            "unit": "iter/sec",
            "range": "stddev: 0.0000043803227470324116",
            "extra": "mean: 110.12771354480809 usec\nrounds: 9059"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 620.8582264146916,
            "unit": "iter/sec",
            "range": "stddev: 0.000020918285902641394",
            "extra": "mean: 1.6106736730779292 msec\nrounds: 624"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.297403349276542,
            "unit": "iter/sec",
            "range": "stddev: 0.000550502872837657",
            "extra": "mean: 137.03504550000503 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 564.5866921644284,
            "unit": "iter/sec",
            "range": "stddev: 0.00005560231200601916",
            "extra": "mean: 1.7712071748739753 msec\nrounds: 589"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 363.9968152937217,
            "unit": "iter/sec",
            "range": "stddev: 0.00005204821892229368",
            "extra": "mean: 2.747276783707751 msec\nrounds: 356"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 757661.4306226881,
            "unit": "iter/sec",
            "range": "stddev: 1.0421215233499387e-7",
            "extra": "mean: 1.3198507401625876 usec\nrounds: 73228"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 566.7265997131386,
            "unit": "iter/sec",
            "range": "stddev: 0.00004491945517233707",
            "extra": "mean: 1.764519259385694 msec\nrounds: 586"
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
        "date": 1771223988252,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 348.89578711751545,
            "unit": "iter/sec",
            "range": "stddev: 0.0030258906596098574",
            "extra": "mean: 2.866185368019875 msec\nrounds: 394"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 312.64935970567404,
            "unit": "iter/sec",
            "range": "stddev: 0.0037337062411228073",
            "extra": "mean: 3.198471287263768 msec\nrounds: 369"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6698.104599240838,
            "unit": "iter/sec",
            "range": "stddev: 0.000017991285392262175",
            "extra": "mean: 149.29596652063927 usec\nrounds: 6840"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4710.552995363604,
            "unit": "iter/sec",
            "range": "stddev: 0.000034061649316659574",
            "extra": "mean: 212.28930042486672 usec\nrounds: 5176"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 297.254303262976,
            "unit": "iter/sec",
            "range": "stddev: 0.0008381050678627163",
            "extra": "mean: 3.364122870629451 msec\nrounds: 286"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 308.41627664204725,
            "unit": "iter/sec",
            "range": "stddev: 0.001042423103636417",
            "extra": "mean: 3.2423710281692286 msec\nrounds: 355"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 306.4013161646194,
            "unit": "iter/sec",
            "range": "stddev: 0.0014513875242834942",
            "extra": "mean: 3.263693552356455 msec\nrounds: 382"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 309.1346291013577,
            "unit": "iter/sec",
            "range": "stddev: 0.0009288689315809915",
            "extra": "mean: 3.2348365594206028 msec\nrounds: 345"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 292.9442961451199,
            "unit": "iter/sec",
            "range": "stddev: 0.0006527988375983086",
            "extra": "mean: 3.4136182651756295 msec\nrounds: 313"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 14812.582274850838,
            "unit": "iter/sec",
            "range": "stddev: 0.00001892611466162551",
            "extra": "mean: 67.51017354332771 usec\nrounds: 16578"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 15843.100446932618,
            "unit": "iter/sec",
            "range": "stddev: 0.000017216105265562572",
            "extra": "mean: 63.11895852390496 usec\nrounds: 15961"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 13703.513449998705,
            "unit": "iter/sec",
            "range": "stddev: 0.00006602560769924034",
            "extra": "mean: 72.97398609844065 usec\nrounds: 13092"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6496.396376226512,
            "unit": "iter/sec",
            "range": "stddev: 0.00009826814627661547",
            "extra": "mean: 153.931494029442 usec\nrounds: 6532"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16969.864614769856,
            "unit": "iter/sec",
            "range": "stddev: 0.000017608896259437856",
            "extra": "mean: 58.927989273977005 usec\nrounds: 18553"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 52336.036667512315,
            "unit": "iter/sec",
            "range": "stddev: 0.000016490974708446276",
            "extra": "mean: 19.107293247154725 usec\nrounds: 45003"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 209524.66904919693,
            "unit": "iter/sec",
            "range": "stddev: 0.000012496177839094835",
            "extra": "mean: 4.772707693742722 usec\nrounds: 184163"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 154.95165729650682,
            "unit": "iter/sec",
            "range": "stddev: 0.0007472828271071726",
            "extra": "mean: 6.453625714286204 msec\nrounds: 154"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4251.399476693272,
            "unit": "iter/sec",
            "range": "stddev: 0.00006728935238219837",
            "extra": "mean: 235.21666347331762 usec\nrounds: 4071"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 247.11296876543534,
            "unit": "iter/sec",
            "range": "stddev: 0.00028875724805760066",
            "extra": "mean: 4.046732168675535 msec\nrounds: 249"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 171.5363742401182,
            "unit": "iter/sec",
            "range": "stddev: 0.0003803146935076525",
            "extra": "mean: 5.829667348571742 msec\nrounds: 175"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 183.25359175775137,
            "unit": "iter/sec",
            "range": "stddev: 0.0004492569579026263",
            "extra": "mean: 5.456918963541686 msec\nrounds: 192"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 89.83184020397182,
            "unit": "iter/sec",
            "range": "stddev: 0.0006272953486483838",
            "extra": "mean: 11.13191044210387 msec\nrounds: 95"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 129.73933665498333,
            "unit": "iter/sec",
            "range": "stddev: 0.0004903390723660795",
            "extra": "mean: 7.707762547447783 msec\nrounds: 137"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 66.93941053154454,
            "unit": "iter/sec",
            "range": "stddev: 0.0014884411986287524",
            "extra": "mean: 14.938882671050111 msec\nrounds: 76"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 5.245588376246053,
            "unit": "iter/sec",
            "range": "stddev: 0.01576707778111289",
            "extra": "mean: 190.636384000004 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 774.0208752224495,
            "unit": "iter/sec",
            "range": "stddev: 0.00017752564750859494",
            "extra": "mean: 1.2919548193226769 msec\nrounds: 797"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1700557.2145449163,
            "unit": "iter/sec",
            "range": "stddev: 6.851352937310181e-8",
            "extra": "mean: 588.0425494931721 nsec\nrounds: 169492"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 821326.8070156794,
            "unit": "iter/sec",
            "range": "stddev: 9.970252644115397e-8",
            "extra": "mean: 1.2175421421267572 usec\nrounds: 82217"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23734.128385959622,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016824343290383178",
            "extra": "mean: 42.133420016029284 usec\nrounds: 23811"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1506.2876258198496,
            "unit": "iter/sec",
            "range": "stddev: 0.000005669115293546518",
            "extra": "mean: 663.8838312541504 usec\nrounds: 1523"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 151.0267275855256,
            "unit": "iter/sec",
            "range": "stddev: 0.000009627441486479047",
            "extra": "mean: 6.621344552630298 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10204.607423494983,
            "unit": "iter/sec",
            "range": "stddev: 0.000002437313155199487",
            "extra": "mean: 97.99495056493896 usec\nrounds: 10256"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1437.8689222881142,
            "unit": "iter/sec",
            "range": "stddev: 0.0000054193993862738824",
            "extra": "mean: 695.4736864391483 usec\nrounds: 1416"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 71591.63366839595,
            "unit": "iter/sec",
            "range": "stddev: 0.000015132304020790974",
            "extra": "mean: 13.968112595836025 usec\nrounds: 63537"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1113207.6575420818,
            "unit": "iter/sec",
            "range": "stddev: 0.0000011902074868626513",
            "extra": "mean: 898.3049956806444 nsec\nrounds: 116741"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5471.151663804303,
            "unit": "iter/sec",
            "range": "stddev: 0.000055208324640526095",
            "extra": "mean: 182.7768743125394 usec\nrounds: 5275"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 203.16536705618026,
            "unit": "iter/sec",
            "range": "stddev: 0.015274724339557613",
            "extra": "mean: 4.922098753787477 msec\nrounds: 264"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 71230.2830244292,
            "unit": "iter/sec",
            "range": "stddev: 0.000018598165127143398",
            "extra": "mean: 14.03897271694174 usec\nrounds: 65755"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1735.016211374392,
            "unit": "iter/sec",
            "range": "stddev: 0.0005248623791014176",
            "extra": "mean: 576.3634906949086 usec\nrounds: 2633"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 357971.5978849495,
            "unit": "iter/sec",
            "range": "stddev: 0.000004565984777820131",
            "extra": "mean: 2.793517714557331 usec\nrounds: 181489"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3054.3424092060127,
            "unit": "iter/sec",
            "range": "stddev: 0.00002805650586755453",
            "extra": "mean: 327.4027158795053 usec\nrounds: 2689"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2497.9074358189414,
            "unit": "iter/sec",
            "range": "stddev: 0.000028331469913613836",
            "extra": "mean: 400.33509074852844 usec\nrounds: 1697"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5048.060866052424,
            "unit": "iter/sec",
            "range": "stddev: 0.000031463377288486484",
            "extra": "mean: 198.0958682025557 usec\nrounds: 2648"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1112.1711838485905,
            "unit": "iter/sec",
            "range": "stddev: 0.00004493164605413468",
            "extra": "mean: 899.1421595186184 usec\nrounds: 583"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 363468.1330624284,
            "unit": "iter/sec",
            "range": "stddev: 5.45002772827969e-7",
            "extra": "mean: 2.751272832571109 usec\nrounds: 111770"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 127476.0895197998,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013794606878693325",
            "extra": "mean: 7.844608379241805 usec\nrounds: 80103"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 274622.1995500065,
            "unit": "iter/sec",
            "range": "stddev: 8.79988717751177e-7",
            "extra": "mean: 3.6413662174383252 usec\nrounds: 167477"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 38.470955496173495,
            "unit": "iter/sec",
            "range": "stddev: 0.0012280946484136306",
            "extra": "mean: 25.99363564285438 msec\nrounds: 42"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.036030481992602,
            "unit": "iter/sec",
            "range": "stddev: 0.008211504608517245",
            "extra": "mean: 247.7681981000046 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1463.7850179054624,
            "unit": "iter/sec",
            "range": "stddev: 0.00019394885334226119",
            "extra": "mean: 683.1604284561575 usec\nrounds: 1237"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 317.21279859397504,
            "unit": "iter/sec",
            "range": "stddev: 0.010310707883208103",
            "extra": "mean: 3.15245792235507 msec\nrounds: 425"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 972926.9768050852,
            "unit": "iter/sec",
            "range": "stddev: 1.2717572486557363e-7",
            "extra": "mean: 1.027826367076199 usec\nrounds: 98532"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 483823.2099440352,
            "unit": "iter/sec",
            "range": "stddev: 1.3601423499808083e-7",
            "extra": "mean: 2.0668706656625093 usec\nrounds: 48762"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59748.02460171938,
            "unit": "iter/sec",
            "range": "stddev: 0.0000012117604280536521",
            "extra": "mean: 16.736955015098907 usec\nrounds: 57864"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3962.903818926017,
            "unit": "iter/sec",
            "range": "stddev: 0.00000921595446464196",
            "extra": "mean: 252.34021457301208 usec\nrounds: 3980"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 394.60462427495537,
            "unit": "iter/sec",
            "range": "stddev: 0.00002909046382072628",
            "extra": "mean: 2.534182162303331 msec\nrounds: 382"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18439.141170561626,
            "unit": "iter/sec",
            "range": "stddev: 0.0000030209760173739978",
            "extra": "mean: 54.232460761052984 usec\nrounds: 18502"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18463.79120842046,
            "unit": "iter/sec",
            "range": "stddev: 0.0000028068737867545316",
            "extra": "mean: 54.160057851171295 usec\nrounds: 18392"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18470.621090726814,
            "unit": "iter/sec",
            "range": "stddev: 0.0000024717717824773057",
            "extra": "mean: 54.14003108439329 usec\nrounds: 18498"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1506.0535913160818,
            "unit": "iter/sec",
            "range": "stddev: 0.000005672404437400921",
            "extra": "mean: 663.9869960577822 usec\nrounds: 1522"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 150.88886838682748,
            "unit": "iter/sec",
            "range": "stddev: 0.000029440654627818894",
            "extra": "mean: 6.627394125830024 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 40008.09766638243,
            "unit": "iter/sec",
            "range": "stddev: 0.0000022333974354510236",
            "extra": "mean: 24.994939982869248 usec\nrounds: 38539"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3895.289882848241,
            "unit": "iter/sec",
            "range": "stddev: 0.000007852649654394578",
            "extra": "mean: 256.72030325732743 usec\nrounds: 3868"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8010.776249013094,
            "unit": "iter/sec",
            "range": "stddev: 0.0000056478316588618025",
            "extra": "mean: 124.83184761566613 usec\nrounds: 8157"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1222.8095218662183,
            "unit": "iter/sec",
            "range": "stddev: 0.00010480472734106816",
            "extra": "mean: 817.7888560058213 usec\nrounds: 1257"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 430.9853179051225,
            "unit": "iter/sec",
            "range": "stddev: 0.0000305205123105293",
            "extra": "mean: 2.32026465509468 msec\nrounds: 432"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11351.207514090183,
            "unit": "iter/sec",
            "range": "stddev: 0.000004397219547858343",
            "extra": "mean: 88.09635439742479 usec\nrounds: 8846"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 973.6284115739704,
            "unit": "iter/sec",
            "range": "stddev: 0.000038719247330501084",
            "extra": "mean: 1.0270858862709205 msec\nrounds: 976"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 981.9539066122408,
            "unit": "iter/sec",
            "range": "stddev: 0.0000161268327448249",
            "extra": "mean: 1.0183777397963807 msec\nrounds: 980"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1153.8511947222796,
            "unit": "iter/sec",
            "range": "stddev: 0.000014197860428768436",
            "extra": "mean: 866.6628804251401 usec\nrounds: 1129"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1551.891524233216,
            "unit": "iter/sec",
            "range": "stddev: 0.000016925469441320162",
            "extra": "mean: 644.3749349646693 usec\nrounds: 1553"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 926.1384923033813,
            "unit": "iter/sec",
            "range": "stddev: 0.000017869154737900656",
            "extra": "mean: 1.0797521194836845 msec\nrounds: 929"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1017.886792924598,
            "unit": "iter/sec",
            "range": "stddev: 0.000031593798867323496",
            "extra": "mean: 982.4275223443999 usec\nrounds: 1007"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1005.1953176981796,
            "unit": "iter/sec",
            "range": "stddev: 0.000026133382028254177",
            "extra": "mean: 994.8315341240582 usec\nrounds: 1011"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 731.398091862628,
            "unit": "iter/sec",
            "range": "stddev: 0.00003447851185457386",
            "extra": "mean: 1.3672444748294765 msec\nrounds: 735"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1065.2104123027025,
            "unit": "iter/sec",
            "range": "stddev: 0.000016339452931954343",
            "extra": "mean: 938.7816608347503 usec\nrounds: 1029"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 958.8375763005257,
            "unit": "iter/sec",
            "range": "stddev: 0.00002072591959831133",
            "extra": "mean: 1.0429295062238706 msec\nrounds: 964"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1608.336850774199,
            "unit": "iter/sec",
            "range": "stddev: 0.00002375695048373221",
            "extra": "mean: 621.7602982351824 usec\nrounds: 1643"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3025.5193526050402,
            "unit": "iter/sec",
            "range": "stddev: 0.000013761449258804306",
            "extra": "mean: 330.52176616850176 usec\nrounds: 2814"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3017.9749253779596,
            "unit": "iter/sec",
            "range": "stddev: 0.000020244071679919275",
            "extra": "mean: 331.3480147204218 usec\nrounds: 3057"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4009.6394592419983,
            "unit": "iter/sec",
            "range": "stddev: 0.000010632420930118617",
            "extra": "mean: 249.39898216909629 usec\nrounds: 3982"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7480.524534873535,
            "unit": "iter/sec",
            "range": "stddev: 0.000006728595965602687",
            "extra": "mean: 133.68046523182826 usec\nrounds: 7435"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 614.3612673644126,
            "unit": "iter/sec",
            "range": "stddev: 0.000028533833670356316",
            "extra": "mean: 1.627706779579324 msec\nrounds: 617"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4632.4677956077185,
            "unit": "iter/sec",
            "range": "stddev: 0.00000804792600910745",
            "extra": "mean: 215.86766365610822 usec\nrounds: 4540"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 423.814598717583,
            "unit": "iter/sec",
            "range": "stddev: 0.00005845076873568443",
            "extra": "mean: 2.3595223076927776 msec\nrounds: 429"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 494.81627563820956,
            "unit": "iter/sec",
            "range": "stddev: 0.0080258063933507",
            "extra": "mean: 2.0209521174504803 msec\nrounds: 596"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 8799.999947079908,
            "unit": "iter/sec",
            "range": "stddev: 0.000004757814190388602",
            "extra": "mean: 113.6363643197326 usec\nrounds: 8918"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 600.783021864066,
            "unit": "iter/sec",
            "range": "stddev: 0.00002130805834253353",
            "extra": "mean: 1.664494440767105 msec\nrounds: 574"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.344172450569583,
            "unit": "iter/sec",
            "range": "stddev: 0.00038617114791989404",
            "extra": "mean: 136.1623800000018 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 543.6314737771822,
            "unit": "iter/sec",
            "range": "stddev: 0.00005527364318069557",
            "extra": "mean: 1.8394814285713508 msec\nrounds: 539"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 347.9121516977709,
            "unit": "iter/sec",
            "range": "stddev: 0.0001570289164547608",
            "extra": "mean: 2.874288797100406 msec\nrounds: 345"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 761205.1436478085,
            "unit": "iter/sec",
            "range": "stddev: 1.0570978672145897e-7",
            "extra": "mean: 1.3137063094553607 usec\nrounds: 74935"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 543.1670763736769,
            "unit": "iter/sec",
            "range": "stddev: 0.00005008895437128719",
            "extra": "mean: 1.8410541498138238 msec\nrounds: 534"
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
        "date": 1771224662733,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 362.0280238848811,
            "unit": "iter/sec",
            "range": "stddev: 0.0069326806834404744",
            "extra": "mean: 2.7622171048227564 msec\nrounds: 477"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 395.3554970723507,
            "unit": "iter/sec",
            "range": "stddev: 0.0006089030478200908",
            "extra": "mean: 2.5293691561268425 msec\nrounds: 506"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6063.197052751006,
            "unit": "iter/sec",
            "range": "stddev: 0.00001864497349333839",
            "extra": "mean: 164.92949038268154 usec\nrounds: 5251"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4749.193811312948,
            "unit": "iter/sec",
            "range": "stddev: 0.000031486251420576764",
            "extra": "mean: 210.56205320951995 usec\nrounds: 4191"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 351.4492897002138,
            "unit": "iter/sec",
            "range": "stddev: 0.00046320418615111925",
            "extra": "mean: 2.8453607086615538 msec\nrounds: 381"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 324.467465309696,
            "unit": "iter/sec",
            "range": "stddev: 0.0011707608693042385",
            "extra": "mean: 3.0819731002784057 msec\nrounds: 359"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 324.3702268120226,
            "unit": "iter/sec",
            "range": "stddev: 0.0007488454812948803",
            "extra": "mean: 3.0828970026879654 msec\nrounds: 372"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 327.67838623183883,
            "unit": "iter/sec",
            "range": "stddev: 0.0007934437559500329",
            "extra": "mean: 3.0517728419612045 msec\nrounds: 367"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 302.16803377103696,
            "unit": "iter/sec",
            "range": "stddev: 0.0006590016732255419",
            "extra": "mean: 3.309416907937172 msec\nrounds: 315"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 17059.273752447065,
            "unit": "iter/sec",
            "range": "stddev: 0.000015495609104192943",
            "extra": "mean: 58.619142556203784 usec\nrounds: 16141"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14070.181621183443,
            "unit": "iter/sec",
            "range": "stddev: 0.00002586074375769263",
            "extra": "mean: 71.07228797206457 usec\nrounds: 16071"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 13042.445018882197,
            "unit": "iter/sec",
            "range": "stddev: 0.00006268961553396635",
            "extra": "mean: 76.67274031458443 usec\nrounds: 13293"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6090.497121251155,
            "unit": "iter/sec",
            "range": "stddev: 0.00009544909593914479",
            "extra": "mean: 164.19020977955452 usec\nrounds: 5215"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 16951.948141613106,
            "unit": "iter/sec",
            "range": "stddev: 0.000014717203553418678",
            "extra": "mean: 58.99027012389399 usec\nrounds: 13827"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54095.923147776346,
            "unit": "iter/sec",
            "range": "stddev: 0.000017562690379922046",
            "extra": "mean: 18.485681393554437 usec\nrounds: 44205"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 212673.55848617482,
            "unit": "iter/sec",
            "range": "stddev: 0.000012633029733952123",
            "extra": "mean: 4.70204197982142 usec\nrounds: 162840"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 132.04766094580177,
            "unit": "iter/sec",
            "range": "stddev: 0.01722133993640001",
            "extra": "mean: 7.5730232011488985 msec\nrounds: 174"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4126.539805055712,
            "unit": "iter/sec",
            "range": "stddev: 0.00011368428699075084",
            "extra": "mean: 242.33378259791172 usec\nrounds: 4011"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 235.868902807112,
            "unit": "iter/sec",
            "range": "stddev: 0.00048003107842944215",
            "extra": "mean: 4.239643242915224 msec\nrounds: 247"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 163.4449233131881,
            "unit": "iter/sec",
            "range": "stddev: 0.0005994947967330047",
            "extra": "mean: 6.118268954024537 msec\nrounds: 174"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 176.65438835861264,
            "unit": "iter/sec",
            "range": "stddev: 0.0005251138282540878",
            "extra": "mean: 5.6607707812498615 msec\nrounds: 192"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 88.84104915470834,
            "unit": "iter/sec",
            "range": "stddev: 0.0008288944810467934",
            "extra": "mean: 11.256057976742191 msec\nrounds: 86"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 125.13677072687821,
            "unit": "iter/sec",
            "range": "stddev: 0.000794605552353608",
            "extra": "mean: 7.991256240602422 msec\nrounds: 133"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 55.61588334012969,
            "unit": "iter/sec",
            "range": "stddev: 0.028339837885472306",
            "extra": "mean: 17.980474999997874 msec\nrounds: 75"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 4.864651686361549,
            "unit": "iter/sec",
            "range": "stddev: 0.018349154254269476",
            "extra": "mean: 205.56456340000295 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 760.0167581911849,
            "unit": "iter/sec",
            "range": "stddev: 0.00019513613794439362",
            "extra": "mean: 1.3157604608350577 msec\nrounds: 766"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1714612.8289315586,
            "unit": "iter/sec",
            "range": "stddev: 8.135340158095547e-8",
            "extra": "mean: 583.2220447243117 nsec\nrounds: 160746"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 822127.9767906365,
            "unit": "iter/sec",
            "range": "stddev: 1.060984186430404e-7",
            "extra": "mean: 1.2163556383322793 usec\nrounds: 82693"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23505.22339607138,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017821973375806697",
            "extra": "mean: 42.54373520088042 usec\nrounds: 23954"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1506.1563403669895,
            "unit": "iter/sec",
            "range": "stddev: 0.000005879955524743599",
            "extra": "mean: 663.9416992769425 usec\nrounds: 1523"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 150.9666627866323,
            "unit": "iter/sec",
            "range": "stddev: 0.000009371981823607156",
            "extra": "mean: 6.623978973512472 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10163.482888949979,
            "unit": "iter/sec",
            "range": "stddev: 0.00000295030825615441",
            "extra": "mean: 98.39146785864399 usec\nrounds: 10236"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1412.857654567733,
            "unit": "iter/sec",
            "range": "stddev: 0.000008039404745461448",
            "extra": "mean: 707.7853857159815 usec\nrounds: 1400"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 68559.58667774002,
            "unit": "iter/sec",
            "range": "stddev: 0.000029826412336971562",
            "extra": "mean: 14.585852226624942 usec\nrounds: 54455"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1144471.9818886146,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013069896627215259",
            "extra": "mean: 873.7653833602757 nsec\nrounds: 113431"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5238.95387323358,
            "unit": "iter/sec",
            "range": "stddev: 0.00007422013532294803",
            "extra": "mean: 190.87780198048992 usec\nrounds: 4848"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 247.3672136421441,
            "unit": "iter/sec",
            "range": "stddev: 0.00042397143220603234",
            "extra": "mean: 4.042572923373178 msec\nrounds: 261"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 69597.35238360499,
            "unit": "iter/sec",
            "range": "stddev: 0.000019088136428621246",
            "extra": "mean: 14.368362671157724 usec\nrounds: 65842"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1978.2642548435626,
            "unit": "iter/sec",
            "range": "stddev: 0.0008919754729655523",
            "extra": "mean: 505.49364047376883 usec\nrounds: 2787"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 349830.97561590915,
            "unit": "iter/sec",
            "range": "stddev: 0.000003992911107967089",
            "extra": "mean: 2.8585233146933584 usec\nrounds: 172981"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3121.1195588320593,
            "unit": "iter/sec",
            "range": "stddev: 0.000025115086329505843",
            "extra": "mean: 320.39785120381794 usec\nrounds: 2742"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2552.6129154542105,
            "unit": "iter/sec",
            "range": "stddev: 0.000025977328614085483",
            "extra": "mean: 391.75544162835223 usec\nrounds: 1696"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5075.221579336421,
            "unit": "iter/sec",
            "range": "stddev: 0.000038932096693173636",
            "extra": "mean: 197.0357322075283 usec\nrounds: 4440"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1099.9992139305598,
            "unit": "iter/sec",
            "range": "stddev: 0.000025577215143457583",
            "extra": "mean: 909.0915587355388 usec\nrounds: 664"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 367032.4650003824,
            "unit": "iter/sec",
            "range": "stddev: 5.121908970921269e-7",
            "extra": "mean: 2.7245546248857253 usec\nrounds: 112284"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 126505.42278990777,
            "unit": "iter/sec",
            "range": "stddev: 0.0000014133257155379958",
            "extra": "mean: 7.904799477732564 usec\nrounds: 77737"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 282249.4728289658,
            "unit": "iter/sec",
            "range": "stddev: 7.445698747037154e-7",
            "extra": "mean: 3.5429649875943903 usec\nrounds: 170911"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.320023248405924,
            "unit": "iter/sec",
            "range": "stddev: 0.000690034313945824",
            "extra": "mean: 23.629476622219652 msec\nrounds: 45"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.44373538622664,
            "unit": "iter/sec",
            "range": "stddev: 0.006847163356257521",
            "extra": "mean: 225.03590179998127 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1406.7996008708456,
            "unit": "iter/sec",
            "range": "stddev: 0.00040272943091100355",
            "extra": "mean: 710.8332980624774 usec\nrounds: 1446"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 384.0483862101531,
            "unit": "iter/sec",
            "range": "stddev: 0.0005121292592311069",
            "extra": "mean: 2.603838568020424 msec\nrounds: 419"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 985269.8860058466,
            "unit": "iter/sec",
            "range": "stddev: 1.0087135064996406e-7",
            "extra": "mean: 1.0149503341199917 usec\nrounds: 94429"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 477618.6402828328,
            "unit": "iter/sec",
            "range": "stddev: 1.4418657592618694e-7",
            "extra": "mean: 2.0937206290940136 usec\nrounds: 48291"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59515.21378355192,
            "unit": "iter/sec",
            "range": "stddev: 0.0000016160751671978102",
            "extra": "mean: 16.80242641212469 usec\nrounds: 59874"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3958.7892839763726,
            "unit": "iter/sec",
            "range": "stddev: 0.000008714196966936505",
            "extra": "mean: 252.6024822911409 usec\nrounds: 3981"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 392.68243734998526,
            "unit": "iter/sec",
            "range": "stddev: 0.0000423761179182726",
            "extra": "mean: 2.5465870252525 msec\nrounds: 396"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18446.502314064557,
            "unit": "iter/sec",
            "range": "stddev: 0.0000036153276528360345",
            "extra": "mean: 54.21081910132898 usec\nrounds: 18093"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18391.709529894426,
            "unit": "iter/sec",
            "range": "stddev: 0.000002514881908598602",
            "extra": "mean: 54.37232457236074 usec\nrounds: 18301"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18358.592955244803,
            "unit": "iter/sec",
            "range": "stddev: 0.000002686861840489409",
            "extra": "mean: 54.47040535393065 usec\nrounds: 18342"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1506.5478485906776,
            "unit": "iter/sec",
            "range": "stddev: 0.0000054708194959896965",
            "extra": "mean: 663.7691600273199 usec\nrounds: 1506"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 151.01103633955435,
            "unit": "iter/sec",
            "range": "stddev: 0.000008025424901823999",
            "extra": "mean: 6.622032562914542 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 37429.927382330905,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027454882549625443",
            "extra": "mean: 26.716589369394764 usec\nrounds: 27392"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3721.741317660365,
            "unit": "iter/sec",
            "range": "stddev: 0.00002088195422163565",
            "extra": "mean: 268.69143087801706 usec\nrounds: 3964"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7829.516108239089,
            "unit": "iter/sec",
            "range": "stddev: 0.000005315193505545681",
            "extra": "mean: 127.72181398894993 usec\nrounds: 7849"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1239.407862207656,
            "unit": "iter/sec",
            "range": "stddev: 0.00004027030904873215",
            "extra": "mean: 806.836902114516 usec\nrounds: 1277"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 422.16005413902434,
            "unit": "iter/sec",
            "range": "stddev: 0.00008853184294516119",
            "extra": "mean: 2.3687698307682217 msec\nrounds: 390"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11185.86125473405,
            "unit": "iter/sec",
            "range": "stddev: 0.000004788539824537502",
            "extra": "mean: 89.39856996498885 usec\nrounds: 10734"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1005.6575281515284,
            "unit": "iter/sec",
            "range": "stddev: 0.000019188673003445985",
            "extra": "mean: 994.3742994079432 usec\nrounds: 1012"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1005.5689398458276,
            "unit": "iter/sec",
            "range": "stddev: 0.0000356205249966521",
            "extra": "mean: 994.4619014916258 usec\nrounds: 1005"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1129.8880974162253,
            "unit": "iter/sec",
            "range": "stddev: 0.00001962476640442932",
            "extra": "mean: 885.0433970290977 usec\nrounds: 1010"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1552.9297506238922,
            "unit": "iter/sec",
            "range": "stddev: 0.000022717096415006573",
            "extra": "mean: 643.9441317923418 usec\nrounds: 1601"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 953.5567119560206,
            "unit": "iter/sec",
            "range": "stddev: 0.00002167264782416774",
            "extra": "mean: 1.0487053234083064 msec\nrounds: 974"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1028.2342586260563,
            "unit": "iter/sec",
            "range": "stddev: 0.000040278536691386356",
            "extra": "mean: 972.5410251708756 usec\nrounds: 1033"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1024.1397720223538,
            "unit": "iter/sec",
            "range": "stddev: 0.000038092536067594524",
            "extra": "mean: 976.4292212041671 usec\nrounds: 981"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 701.6474299027967,
            "unit": "iter/sec",
            "range": "stddev: 0.000047122778411920134",
            "extra": "mean: 1.4252172207607685 msec\nrounds: 684"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1057.528046467707,
            "unit": "iter/sec",
            "range": "stddev: 0.000024815528947174295",
            "extra": "mean: 945.6013987904539 usec\nrounds: 993"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 952.3362127133994,
            "unit": "iter/sec",
            "range": "stddev: 0.000012473512242819223",
            "extra": "mean: 1.0500493278007321 msec\nrounds: 964"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1612.8674734837082,
            "unit": "iter/sec",
            "range": "stddev: 0.000010702125491294245",
            "extra": "mean: 620.013743497507 usec\nrounds: 1653"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 2987.4194493278737,
            "unit": "iter/sec",
            "range": "stddev: 0.00001682158374290171",
            "extra": "mean: 334.7370588435399 usec\nrounds: 2974"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 2999.754206640083,
            "unit": "iter/sec",
            "range": "stddev: 0.000013454713686075892",
            "extra": "mean: 333.3606459444103 usec\nrounds: 3008"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4043.950766838699,
            "unit": "iter/sec",
            "range": "stddev: 0.000014366073245403792",
            "extra": "mean: 247.28293138487828 usec\nrounds: 4037"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7799.246091897467,
            "unit": "iter/sec",
            "range": "stddev: 0.0000045089218336840935",
            "extra": "mean: 128.21752105487306 usec\nrounds: 7243"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 645.0935651346095,
            "unit": "iter/sec",
            "range": "stddev: 0.000020768919211468262",
            "extra": "mean: 1.5501627268461953 msec\nrounds: 637"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4615.315603714137,
            "unit": "iter/sec",
            "range": "stddev: 0.000007377437152754419",
            "extra": "mean: 216.6699064296401 usec\nrounds: 3826"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 421.7062714591611,
            "unit": "iter/sec",
            "range": "stddev: 0.00004203968909105737",
            "extra": "mean: 2.3713187772613957 msec\nrounds: 431"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 611.3558712235962,
            "unit": "iter/sec",
            "range": "stddev: 0.000023051244110072603",
            "extra": "mean: 1.6357085080389484 msec\nrounds: 622"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 9079.725589460757,
            "unit": "iter/sec",
            "range": "stddev: 0.000005530764461092359",
            "extra": "mean: 110.13548704167277 usec\nrounds: 9106"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 632.7045387230415,
            "unit": "iter/sec",
            "range": "stddev: 0.000023869914097714478",
            "extra": "mean: 1.5805165583579566 msec\nrounds: 634"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.403547894017933,
            "unit": "iter/sec",
            "range": "stddev: 0.0006466938864420219",
            "extra": "mean: 135.0703762999899 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 555.320248102035,
            "unit": "iter/sec",
            "range": "stddev: 0.00005386503899108069",
            "extra": "mean: 1.8007627192017304 msec\nrounds: 552"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 355.6714366188232,
            "unit": "iter/sec",
            "range": "stddev: 0.00008735510566576695",
            "extra": "mean: 2.811583661332103 msec\nrounds: 375"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 727039.8959213396,
            "unit": "iter/sec",
            "range": "stddev: 1.1766125673421095e-7",
            "extra": "mean: 1.3754403377448117 usec\nrounds: 69166"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 551.1373232242261,
            "unit": "iter/sec",
            "range": "stddev: 0.000060991660543024716",
            "extra": "mean: 1.8144298305726565 msec\nrounds: 543"
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
        "date": 1771225208079,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 352.8453905223362,
            "unit": "iter/sec",
            "range": "stddev: 0.006777365042167641",
            "extra": "mean: 2.834102490384374 msec\nrounds: 520"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 388.2772992175374,
            "unit": "iter/sec",
            "range": "stddev: 0.0006731145646684722",
            "extra": "mean: 2.5754789219334118 msec\nrounds: 538"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6772.408869675596,
            "unit": "iter/sec",
            "range": "stddev: 0.000018795739910057525",
            "extra": "mean: 147.65794848530766 usec\nrounds: 6833"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4691.354110449657,
            "unit": "iter/sec",
            "range": "stddev: 0.00003162770747954178",
            "extra": "mean: 213.1580725856041 usec\nrounds: 4753"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 338.6806740431798,
            "unit": "iter/sec",
            "range": "stddev: 0.001198365310095189",
            "extra": "mean: 2.9526337835045933 msec\nrounds: 388"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 303.78325838307575,
            "unit": "iter/sec",
            "range": "stddev: 0.001685217343425668",
            "extra": "mean: 3.291820639895117 msec\nrounds: 386"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 304.13451697531906,
            "unit": "iter/sec",
            "range": "stddev: 0.0011440622735079105",
            "extra": "mean: 3.2880187686199114 msec\nrounds: 376"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 304.31090714731306,
            "unit": "iter/sec",
            "range": "stddev: 0.002365369610991343",
            "extra": "mean: 3.2861129079277873 msec\nrounds: 391"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 292.690003031558,
            "unit": "iter/sec",
            "range": "stddev: 0.001220244249205503",
            "extra": "mean: 3.416584063830084 msec\nrounds: 329"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 16831.856104365856,
            "unit": "iter/sec",
            "range": "stddev: 0.000014943970301030243",
            "extra": "mean: 59.411154289788605 usec\nrounds: 16294"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14308.90402943993,
            "unit": "iter/sec",
            "range": "stddev: 0.000020387741825227006",
            "extra": "mean: 69.88655440993558 usec\nrounds: 16872"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 13426.128695041114,
            "unit": "iter/sec",
            "range": "stddev: 0.00006180567131363402",
            "extra": "mean: 74.48163373924353 usec\nrounds: 14203"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6728.572239815646,
            "unit": "iter/sec",
            "range": "stddev: 0.00008240027205903083",
            "extra": "mean: 148.61993961848268 usec\nrounds: 6492"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 13002.757735605943,
            "unit": "iter/sec",
            "range": "stddev: 0.000014878971905696284",
            "extra": "mean: 76.90676242176399 usec\nrounds: 17207"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 52298.66152471525,
            "unit": "iter/sec",
            "range": "stddev: 0.000013910502474112534",
            "extra": "mean: 19.120948239323887 usec\nrounds: 43933"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 214130.87163331732,
            "unit": "iter/sec",
            "range": "stddev: 0.00001387292873327833",
            "extra": "mean: 4.67004123400022 usec\nrounds: 177305"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 155.07506531809636,
            "unit": "iter/sec",
            "range": "stddev: 0.001177580798776292",
            "extra": "mean: 6.448489948714571 msec\nrounds: 156"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4307.883298108484,
            "unit": "iter/sec",
            "range": "stddev: 0.00006504593666058412",
            "extra": "mean: 232.1325650671833 usec\nrounds: 4042"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 247.86939932393005,
            "unit": "iter/sec",
            "range": "stddev: 0.0002207832509215127",
            "extra": "mean: 4.034382633465546 msec\nrounds: 251"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 169.53808240270396,
            "unit": "iter/sec",
            "range": "stddev: 0.0002878192603260559",
            "extra": "mean: 5.898379796609349 msec\nrounds: 177"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 183.79650389276173,
            "unit": "iter/sec",
            "range": "stddev: 0.000530721869553845",
            "extra": "mean: 5.440799899999523 msec\nrounds: 190"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 91.65849029177939,
            "unit": "iter/sec",
            "range": "stddev: 0.0006718935672831134",
            "extra": "mean: 10.910064052077098 msec\nrounds: 96"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 131.07624620178416,
            "unit": "iter/sec",
            "range": "stddev: 0.0006168948220800389",
            "extra": "mean: 7.629147377783148 msec\nrounds: 135"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 58.04669826629531,
            "unit": "iter/sec",
            "range": "stddev: 0.023049327073815618",
            "extra": "mean: 17.22750871052812 msec\nrounds: 76"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 5.848098474932589,
            "unit": "iter/sec",
            "range": "stddev: 0.0018897239236182791",
            "extra": "mean: 170.99575259999824 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 771.2839994574196,
            "unit": "iter/sec",
            "range": "stddev: 0.00015853044615342322",
            "extra": "mean: 1.2965392782729537 msec\nrounds: 787"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1672838.8786920726,
            "unit": "iter/sec",
            "range": "stddev: 7.260972264143753e-8",
            "extra": "mean: 597.7862020889071 nsec\nrounds: 164177"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 817588.4764924069,
            "unit": "iter/sec",
            "range": "stddev: 1.0434495689318988e-7",
            "extra": "mean: 1.2231092153966863 usec\nrounds: 82015"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23722.313065220816,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017187685153058265",
            "extra": "mean: 42.154405316659265 usec\nrounds: 23811"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1507.8103805403669,
            "unit": "iter/sec",
            "range": "stddev: 0.000005122188467533469",
            "extra": "mean: 663.2133674803468 usec\nrounds: 1513"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 151.07530146733222,
            "unit": "iter/sec",
            "range": "stddev: 0.000012843707306358137",
            "extra": "mean: 6.6192156513170035 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10199.891379738365,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025266551351866406",
            "extra": "mean: 98.04025972143742 usec\nrounds: 10261"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1439.9195030246835,
            "unit": "iter/sec",
            "range": "stddev: 0.0000050487323714198855",
            "extra": "mean: 694.4832665294191 usec\nrounds: 1452"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 71627.33408310878,
            "unit": "iter/sec",
            "range": "stddev: 0.000019033713531885248",
            "extra": "mean: 13.961150624979364 usec\nrounds: 66410"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1134082.0645984202,
            "unit": "iter/sec",
            "range": "stddev: 0.0000010667140822865457",
            "extra": "mean: 881.7704037618312 nsec\nrounds: 112146"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5494.903150068314,
            "unit": "iter/sec",
            "range": "stddev: 0.00004467606279094595",
            "extra": "mean: 181.9868290103653 usec\nrounds: 5205"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 257.280836688611,
            "unit": "iter/sec",
            "range": "stddev: 0.0003554137654594116",
            "extra": "mean: 3.88680328030147 msec\nrounds: 264"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 72089.79604675827,
            "unit": "iter/sec",
            "range": "stddev: 0.000011987269799149826",
            "extra": "mean: 13.871588696843983 usec\nrounds: 61840"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1958.060751459437,
            "unit": "iter/sec",
            "range": "stddev: 0.0008270472464754767",
            "extra": "mean: 510.70938389151206 usec\nrounds: 2657"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 348501.27749750606,
            "unit": "iter/sec",
            "range": "stddev: 0.0000057242790342424805",
            "extra": "mean: 2.8694299406324446 usec\nrounds: 170591"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3110.4737307734845,
            "unit": "iter/sec",
            "range": "stddev: 0.000023481042521995305",
            "extra": "mean: 321.4944367176279 usec\nrounds: 2718"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2618.872867277835,
            "unit": "iter/sec",
            "range": "stddev: 0.00003156717226957299",
            "extra": "mean: 381.84365972657594 usec\nrounds: 1681"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5037.8596576936325,
            "unit": "iter/sec",
            "range": "stddev: 0.00003198488966318882",
            "extra": "mean: 198.4969943481528 usec\nrounds: 2831"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1115.8624469228416,
            "unit": "iter/sec",
            "range": "stddev: 0.000033458424013037155",
            "extra": "mean: 896.1678052323117 usec\nrounds: 688"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 362780.526786383,
            "unit": "iter/sec",
            "range": "stddev: 5.410037605318035e-7",
            "extra": "mean: 2.7564875349244766 usec\nrounds: 112033"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 134701.3540295877,
            "unit": "iter/sec",
            "range": "stddev: 0.0000012622948858331013",
            "extra": "mean: 7.423830348285482 usec\nrounds: 84084"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 277245.8449792287,
            "unit": "iter/sec",
            "range": "stddev: 8.299995976599573e-7",
            "extra": "mean: 3.6069070758298296 usec\nrounds: 171204"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.033221592925265,
            "unit": "iter/sec",
            "range": "stddev: 0.0016641969948284604",
            "extra": "mean: 24.370496909080494 msec\nrounds: 44"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.446483629503996,
            "unit": "iter/sec",
            "range": "stddev: 0.002837916300316989",
            "extra": "mean: 224.89681359999736 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1439.1192492744306,
            "unit": "iter/sec",
            "range": "stddev: 0.0003359692072748765",
            "extra": "mean: 694.8694491468834 usec\nrounds: 1583"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 393.72204552941736,
            "unit": "iter/sec",
            "range": "stddev: 0.0005587910650051453",
            "extra": "mean: 2.5398628584674565 msec\nrounds: 431"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 960273.6818516569,
            "unit": "iter/sec",
            "range": "stddev: 1.0695976901139788e-7",
            "extra": "mean: 1.0413697874878134 usec\nrounds: 100422"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 475187.89655524795,
            "unit": "iter/sec",
            "range": "stddev: 1.3838574774011006e-7",
            "extra": "mean: 2.1044307046733346 usec\nrounds: 47781"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59737.91716345707,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013117090771295106",
            "extra": "mean: 16.739786846999763 usec\nrounds: 60093"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3968.209672831032,
            "unit": "iter/sec",
            "range": "stddev: 0.000005908374182182157",
            "extra": "mean: 252.00281296793773 usec\nrounds: 4010"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 391.0320168991034,
            "unit": "iter/sec",
            "range": "stddev: 0.00004315694230455404",
            "extra": "mean: 2.5573353505169027 msec\nrounds: 388"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 17446.323805351793,
            "unit": "iter/sec",
            "range": "stddev: 0.0000027576742307399563",
            "extra": "mean: 57.318665591500846 usec\nrounds: 17051"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18213.530233313246,
            "unit": "iter/sec",
            "range": "stddev: 0.0000029367446335170194",
            "extra": "mean: 54.90423806862887 usec\nrounds: 17789"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18340.846178048265,
            "unit": "iter/sec",
            "range": "stddev: 0.0000033638598222366",
            "extra": "mean: 54.5231114362039 usec\nrounds: 18450"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1506.4108679661251,
            "unit": "iter/sec",
            "range": "stddev: 0.000005671356859982891",
            "extra": "mean: 663.8295177398356 usec\nrounds: 1522"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 151.0039362087412,
            "unit": "iter/sec",
            "range": "stddev: 0.000010383586913203156",
            "extra": "mean: 6.622343927628774 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39810.872640611604,
            "unit": "iter/sec",
            "range": "stddev: 0.0000019004259024214683",
            "extra": "mean: 25.11876614781578 usec\nrounds: 40021"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3903.6440837031487,
            "unit": "iter/sec",
            "range": "stddev: 0.000009477701291943285",
            "extra": "mean: 256.1708953372002 usec\nrounds: 3946"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8175.883860219759,
            "unit": "iter/sec",
            "range": "stddev: 0.00000650870073794202",
            "extra": "mean: 122.31093507401181 usec\nrounds: 8271"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1237.8657119636025,
            "unit": "iter/sec",
            "range": "stddev: 0.000027183631917003466",
            "extra": "mean: 807.8420706990254 usec\nrounds: 1273"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 430.35727015127765,
            "unit": "iter/sec",
            "range": "stddev: 0.0000903039047954494",
            "extra": "mean: 2.3236507649760014 msec\nrounds: 434"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11510.552867642631,
            "unit": "iter/sec",
            "range": "stddev: 0.000004240462668187288",
            "extra": "mean: 86.87680005459205 usec\nrounds: 10988"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1045.7619666909281,
            "unit": "iter/sec",
            "range": "stddev: 0.000019037709665727415",
            "extra": "mean: 956.2405517235139 usec\nrounds: 1044"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1049.6203597120796,
            "unit": "iter/sec",
            "range": "stddev: 0.00002259430231472734",
            "extra": "mean: 952.7254218604421 usec\nrounds: 1043"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1197.1311009469907,
            "unit": "iter/sec",
            "range": "stddev: 0.00003622440648382089",
            "extra": "mean: 835.330398825116 usec\nrounds: 1191"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1613.2023812448183,
            "unit": "iter/sec",
            "range": "stddev: 0.000013581895446085174",
            "extra": "mean: 619.8850259744569 usec\nrounds: 1617"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 959.8965690540172,
            "unit": "iter/sec",
            "range": "stddev: 0.000031459134996954836",
            "extra": "mean: 1.0417789085187636 msec\nrounds: 951"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1042.2068627905394,
            "unit": "iter/sec",
            "range": "stddev: 0.000024658481448989257",
            "extra": "mean: 959.5024132948719 usec\nrounds: 1038"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1031.7779436326937,
            "unit": "iter/sec",
            "range": "stddev: 0.000026909294507788632",
            "extra": "mean: 969.2007918672796 usec\nrounds: 1033"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 750.30092018881,
            "unit": "iter/sec",
            "range": "stddev: 0.000016419120043753404",
            "extra": "mean: 1.3327985786667493 msec\nrounds: 750"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1088.6385342884785,
            "unit": "iter/sec",
            "range": "stddev: 0.000015902006854406228",
            "extra": "mean: 918.5785442121874 usec\nrounds: 1097"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 961.8661287075454,
            "unit": "iter/sec",
            "range": "stddev: 0.000014013210177890152",
            "extra": "mean: 1.0396457159206707 msec\nrounds: 961"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1640.9105200317665,
            "unit": "iter/sec",
            "range": "stddev: 0.000014286707275523793",
            "extra": "mean: 609.4177517861492 usec\nrounds: 1680"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3151.9367851965285,
            "unit": "iter/sec",
            "range": "stddev: 0.00001601648379480613",
            "extra": "mean: 317.2652461485354 usec\nrounds: 2986"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3123.7509922121876,
            "unit": "iter/sec",
            "range": "stddev: 0.00001625264071126402",
            "extra": "mean: 320.1279495366617 usec\nrounds: 3131"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4124.396677556008,
            "unit": "iter/sec",
            "range": "stddev: 0.00001010100269970705",
            "extra": "mean: 242.45970457734182 usec\nrounds: 4282"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7672.606696757612,
            "unit": "iter/sec",
            "range": "stddev: 0.000006225057485895294",
            "extra": "mean: 130.33380173423888 usec\nrounds: 7611"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 654.5988300413517,
            "unit": "iter/sec",
            "range": "stddev: 0.00008746584998149227",
            "extra": "mean: 1.5276532039277078 msec\nrounds: 662"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4596.934722787906,
            "unit": "iter/sec",
            "range": "stddev: 0.000020968472144798396",
            "extra": "mean: 217.5362628150459 usec\nrounds: 4604"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 441.7250502863447,
            "unit": "iter/sec",
            "range": "stddev: 0.00003570372338306296",
            "extra": "mean: 2.2638516863640814 msec\nrounds: 440"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 628.6772841992507,
            "unit": "iter/sec",
            "range": "stddev: 0.00007314905086800854",
            "extra": "mean: 1.5906412163017865 msec\nrounds: 638"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 9102.53887499223,
            "unit": "iter/sec",
            "range": "stddev: 0.000003922620414028228",
            "extra": "mean: 109.85945940284202 usec\nrounds: 9114"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 653.4295557063772,
            "unit": "iter/sec",
            "range": "stddev: 0.00004795305652458794",
            "extra": "mean: 1.5303868508350065 msec\nrounds: 657"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.3510683188499435,
            "unit": "iter/sec",
            "range": "stddev: 0.0004031645663704237",
            "extra": "mean: 136.03464920000192 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 493.8553519807716,
            "unit": "iter/sec",
            "range": "stddev: 0.000056443880454395035",
            "extra": "mean: 2.024884403882972 msec\nrounds: 515"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 344.2298688419418,
            "unit": "iter/sec",
            "range": "stddev: 0.00006633411699049184",
            "extra": "mean: 2.9050355315307184 msec\nrounds: 333"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 755566.0036652125,
            "unit": "iter/sec",
            "range": "stddev: 1.0789572863368914e-7",
            "extra": "mean: 1.323511109749579 usec\nrounds: 74322"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 492.4729468684932,
            "unit": "iter/sec",
            "range": "stddev: 0.00006277005204841415",
            "extra": "mean: 2.0305683923527957 msec\nrounds: 497"
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
        "date": 1771225472847,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 401.73103957157116,
            "unit": "iter/sec",
            "range": "stddev: 0.00646025524986954",
            "extra": "mean: 2.489227621212583 msec\nrounds: 528"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 428.4750857738675,
            "unit": "iter/sec",
            "range": "stddev: 0.0005991787249988959",
            "extra": "mean: 2.3338579842837377 msec\nrounds: 509"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6024.248899001112,
            "unit": "iter/sec",
            "range": "stddev: 0.000024125255227799765",
            "extra": "mean: 165.99579744552244 usec\nrounds: 6107"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4499.0439699774915,
            "unit": "iter/sec",
            "range": "stddev: 0.000036953473547751776",
            "extra": "mean: 222.26944361359574 usec\nrounds: 4948"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 388.1667849326915,
            "unit": "iter/sec",
            "range": "stddev: 0.0004505134628846714",
            "extra": "mean: 2.576212182022223 msec\nrounds: 445"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 320.68338791254143,
            "unit": "iter/sec",
            "range": "stddev: 0.000921973589714444",
            "extra": "mean: 3.1183405118344507 msec\nrounds: 338"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 323.9953749364676,
            "unit": "iter/sec",
            "range": "stddev: 0.000903660101151088",
            "extra": "mean: 3.086463812009941 msec\nrounds: 383"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 326.57958261838303,
            "unit": "iter/sec",
            "range": "stddev: 0.0009292037400777574",
            "extra": "mean: 3.0620407803280427 msec\nrounds: 305"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 301.9889167370667,
            "unit": "iter/sec",
            "range": "stddev: 0.000966094464444253",
            "extra": "mean: 3.3113798042815987 msec\nrounds: 327"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 16015.52035672735,
            "unit": "iter/sec",
            "range": "stddev: 0.00001843128850121861",
            "extra": "mean: 62.43943235849643 usec\nrounds: 17201"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 16961.923787022875,
            "unit": "iter/sec",
            "range": "stddev: 0.000014561269475394258",
            "extra": "mean: 58.95557677042941 usec\nrounds: 16875"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 13018.266723859351,
            "unit": "iter/sec",
            "range": "stddev: 0.00007838805734725766",
            "extra": "mean: 76.81514146328256 usec\nrounds: 13735"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6297.721114771418,
            "unit": "iter/sec",
            "range": "stddev: 0.00010329624450200638",
            "extra": "mean: 158.78759662038416 usec\nrounds: 5563"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 14966.487438252798,
            "unit": "iter/sec",
            "range": "stddev: 0.000017784654808516618",
            "extra": "mean: 66.81594489860748 usec\nrounds: 10762"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 53723.560866109154,
            "unit": "iter/sec",
            "range": "stddev: 0.00002539114065026382",
            "extra": "mean: 18.613807124442445 usec\nrounds: 45786"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 207968.95403116857,
            "unit": "iter/sec",
            "range": "stddev: 0.000010242713787003158",
            "extra": "mean: 4.808410008400238 usec\nrounds: 190840"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 164.14203427662207,
            "unit": "iter/sec",
            "range": "stddev: 0.0007580453610155827",
            "extra": "mean: 6.092284675324174 msec\nrounds: 154"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4331.264853527033,
            "unit": "iter/sec",
            "range": "stddev: 0.00005508230393809003",
            "extra": "mean: 230.87943910557226 usec\nrounds: 4204"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 252.31356024886833,
            "unit": "iter/sec",
            "range": "stddev: 0.00023125264624584692",
            "extra": "mean: 3.963322458823278 msec\nrounds: 255"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 168.40002781550592,
            "unit": "iter/sec",
            "range": "stddev: 0.0005139615737313486",
            "extra": "mean: 5.938241299434762 msec\nrounds: 177"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 185.18869694732683,
            "unit": "iter/sec",
            "range": "stddev: 0.0004430692646234996",
            "extra": "mean: 5.399897598957833 msec\nrounds: 192"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 93.40300330916492,
            "unit": "iter/sec",
            "range": "stddev: 0.0006592691538993571",
            "extra": "mean: 10.706293851065897 msec\nrounds: 94"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 128.44172755651437,
            "unit": "iter/sec",
            "range": "stddev: 0.0007631334687768265",
            "extra": "mean: 7.785631811593316 msec\nrounds: 138"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 57.299740890714844,
            "unit": "iter/sec",
            "range": "stddev: 0.024235886830698283",
            "extra": "mean: 17.452085898734758 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 5.6042645381997955,
            "unit": "iter/sec",
            "range": "stddev: 0.016857822034646572",
            "extra": "mean: 178.4355455000025 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 785.8284491168185,
            "unit": "iter/sec",
            "range": "stddev: 0.00016972989822464226",
            "extra": "mean: 1.2725423737507668 msec\nrounds: 800"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1657929.582583116,
            "unit": "iter/sec",
            "range": "stddev: 7.467586027691013e-8",
            "extra": "mean: 603.1619258774326 nsec\nrounds: 162814"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 821851.2608725174,
            "unit": "iter/sec",
            "range": "stddev: 1.0539968493877734e-7",
            "extra": "mean: 1.2167651832015822 usec\nrounds: 82150"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23726.802685567483,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017603857313819688",
            "extra": "mean: 42.146428798359715 usec\nrounds: 23897"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1506.656964957669,
            "unit": "iter/sec",
            "range": "stddev: 0.0000063426330941673915",
            "extra": "mean: 663.7210879837507 usec\nrounds: 1523"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 150.9780564102832,
            "unit": "iter/sec",
            "range": "stddev: 0.00002162009381913537",
            "extra": "mean: 6.623479092103941 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10198.873995523378,
            "unit": "iter/sec",
            "range": "stddev: 0.000002588858189826579",
            "extra": "mean: 98.05003968466842 usec\nrounds: 10281"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1435.7160753908245,
            "unit": "iter/sec",
            "range": "stddev: 0.000007397839316398493",
            "extra": "mean: 696.5165446989819 usec\nrounds: 1443"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 71567.54289034182,
            "unit": "iter/sec",
            "range": "stddev: 0.000013933561991754459",
            "extra": "mean: 13.972814485642374 usec\nrounds: 63332"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1134641.6638033006,
            "unit": "iter/sec",
            "range": "stddev: 8.85293652513099e-7",
            "extra": "mean: 881.3355193110185 nsec\nrounds: 116469"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5404.453846090652,
            "unit": "iter/sec",
            "range": "stddev: 0.00006600720682057082",
            "extra": "mean: 185.0325728516225 usec\nrounds: 5422"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 258.6618329244948,
            "unit": "iter/sec",
            "range": "stddev: 0.00040419855565274445",
            "extra": "mean: 3.866051626920571 msec\nrounds: 260"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 73145.11889502106,
            "unit": "iter/sec",
            "range": "stddev: 0.000012028833911365394",
            "extra": "mean: 13.6714522459826 usec\nrounds: 66099"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 2010.9554556245803,
            "unit": "iter/sec",
            "range": "stddev: 0.0013442951039221523",
            "extra": "mean: 497.276057111574 usec\nrounds: 2784"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 353781.8755155896,
            "unit": "iter/sec",
            "range": "stddev: 0.000006365376022387499",
            "extra": "mean: 2.8266004258771997 usec\nrounds: 180800"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3133.9371146114404,
            "unit": "iter/sec",
            "range": "stddev: 0.000023429933514495345",
            "extra": "mean: 319.08744924640405 usec\nrounds: 2522"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2539.0262322589233,
            "unit": "iter/sec",
            "range": "stddev: 0.000029745384771234636",
            "extra": "mean: 393.85177958965755 usec\nrounds: 1656"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 4977.668479790826,
            "unit": "iter/sec",
            "range": "stddev: 0.000022029582559264346",
            "extra": "mean: 200.89726828131842 usec\nrounds: 4089"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1080.404177263641,
            "unit": "iter/sec",
            "range": "stddev: 0.000038583178544835986",
            "extra": "mean: 925.579538698858 usec\nrounds: 646"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 362596.1676074204,
            "unit": "iter/sec",
            "range": "stddev: 5.155318607098697e-7",
            "extra": "mean: 2.7578890494030013 usec\nrounds: 112158"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 126052.41116865553,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013319600194189531",
            "extra": "mean: 7.9332080261600115 usec\nrounds: 73611"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 282429.33330303204,
            "unit": "iter/sec",
            "range": "stddev: 8.367772753286366e-7",
            "extra": "mean: 3.5407087086349205 usec\nrounds: 164447"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 42.42073875626091,
            "unit": "iter/sec",
            "range": "stddev: 0.000762584775700942",
            "extra": "mean: 23.573375413043916 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.42543720541526,
            "unit": "iter/sec",
            "range": "stddev: 0.009508801360293253",
            "extra": "mean: 225.9663742999976 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1363.189619986836,
            "unit": "iter/sec",
            "range": "stddev: 0.0003179833766984814",
            "extra": "mean: 733.5736608746014 usec\nrounds: 1554"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 392.4234079971873,
            "unit": "iter/sec",
            "range": "stddev: 0.0003631457633320917",
            "extra": "mean: 2.5482679667446533 msec\nrounds: 421"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 967374.9261643119,
            "unit": "iter/sec",
            "range": "stddev: 1.1645461028778183e-7",
            "extra": "mean: 1.0337253664047796 usec\nrounds: 95248"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 478782.2968571297,
            "unit": "iter/sec",
            "range": "stddev: 1.429736389780635e-7",
            "extra": "mean: 2.0886319451748725 usec\nrounds: 48289"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59394.91950561708,
            "unit": "iter/sec",
            "range": "stddev: 0.0000013222822469222897",
            "extra": "mean: 16.83645686068197 usec\nrounds: 59841"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3931.969536851433,
            "unit": "iter/sec",
            "range": "stddev: 0.000007896462963852088",
            "extra": "mean: 254.32546987654453 usec\nrounds: 3967"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 394.0944694737276,
            "unit": "iter/sec",
            "range": "stddev: 0.0000224630469556093",
            "extra": "mean: 2.5374626579647175 msec\nrounds: 383"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18347.532083165268,
            "unit": "iter/sec",
            "range": "stddev: 0.000002713370811165525",
            "extra": "mean: 54.503243022942996 usec\nrounds: 17558"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18446.81468917541,
            "unit": "iter/sec",
            "range": "stddev: 0.0000032094418381008288",
            "extra": "mean: 54.209901104866624 usec\nrounds: 18646"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18019.042633471578,
            "unit": "iter/sec",
            "range": "stddev: 0.000005927448080080254",
            "extra": "mean: 55.49684410771264 usec\nrounds: 18423"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1503.6634622769643,
            "unit": "iter/sec",
            "range": "stddev: 0.000007094139942389416",
            "extra": "mean: 665.0424281013799 usec\nrounds: 1523"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 150.86088655743112,
            "unit": "iter/sec",
            "range": "stddev: 0.000022279243354909616",
            "extra": "mean: 6.628623381576847 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 38923.10373792225,
            "unit": "iter/sec",
            "range": "stddev: 0.000004598747366963801",
            "extra": "mean: 25.691681905256537 usec\nrounds: 18265"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3816.828143260173,
            "unit": "iter/sec",
            "range": "stddev: 0.000039835256264914814",
            "extra": "mean: 261.9976489551459 usec\nrounds: 3877"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 7941.781396280583,
            "unit": "iter/sec",
            "range": "stddev: 0.00002009558600769552",
            "extra": "mean: 125.91633414492314 usec\nrounds: 8215"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1228.7091847811053,
            "unit": "iter/sec",
            "range": "stddev: 0.00012990343238343315",
            "extra": "mean: 813.8622323215972 usec\nrounds: 1287"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 422.1073442278421,
            "unit": "iter/sec",
            "range": "stddev: 0.00028541738932741346",
            "extra": "mean: 2.3690656267288897 msec\nrounds: 434"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11157.911540664043,
            "unit": "iter/sec",
            "range": "stddev: 0.000014005969356690822",
            "extra": "mean: 89.62250653767836 usec\nrounds: 10937"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1008.0058669835612,
            "unit": "iter/sec",
            "range": "stddev: 0.00012492867871719793",
            "extra": "mean: 992.0577178707118 usec\nrounds: 996"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1011.5642959705322,
            "unit": "iter/sec",
            "range": "stddev: 0.00012520223785477373",
            "extra": "mean: 988.5679081234902 usec\nrounds: 1034"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1157.2735158303449,
            "unit": "iter/sec",
            "range": "stddev: 0.00008805105876892915",
            "extra": "mean: 864.099961090442 usec\nrounds: 1028"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1584.7350074455276,
            "unit": "iter/sec",
            "range": "stddev: 0.00004705959631753649",
            "extra": "mean: 631.0203253551671 usec\nrounds: 1546"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 967.2866585966763,
            "unit": "iter/sec",
            "range": "stddev: 0.00005873332018896993",
            "extra": "mean: 1.0338196966872093 msec\nrounds: 966"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1000.7359381598706,
            "unit": "iter/sec",
            "range": "stddev: 0.000058914406410615765",
            "extra": "mean: 999.26460304681 usec\nrounds: 985"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 996.212446774235,
            "unit": "iter/sec",
            "range": "stddev: 0.00020487031779318351",
            "extra": "mean: 1.0038019533263505 msec\nrounds: 1007"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 747.8049657945953,
            "unit": "iter/sec",
            "range": "stddev: 0.000023301366524392376",
            "extra": "mean: 1.3372470707484936 msec\nrounds: 735"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1091.1713793750375,
            "unit": "iter/sec",
            "range": "stddev: 0.0000992822615168884",
            "extra": "mean: 916.4463244744786 usec\nrounds: 1091"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 946.1985704002342,
            "unit": "iter/sec",
            "range": "stddev: 0.000018129115211910815",
            "extra": "mean: 1.0568606118026667 msec\nrounds: 966"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1609.486287812974,
            "unit": "iter/sec",
            "range": "stddev: 0.000014967756192642278",
            "extra": "mean: 621.316259462412 usec\nrounds: 1638"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3138.774504623538,
            "unit": "iter/sec",
            "range": "stddev: 0.000014469047530665894",
            "extra": "mean: 318.5956807432202 usec\nrounds: 2960"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3076.6756711269622,
            "unit": "iter/sec",
            "range": "stddev: 0.000015526932335235738",
            "extra": "mean: 325.02613433859534 usec\nrounds: 3119"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4057.0988345114833,
            "unit": "iter/sec",
            "range": "stddev: 0.000016208764708041774",
            "extra": "mean: 246.48154772409194 usec\nrounds: 4086"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7805.465130261359,
            "unit": "iter/sec",
            "range": "stddev: 0.00000618528607933539",
            "extra": "mean: 128.1153631861162 usec\nrounds: 7671"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 648.806842208529,
            "unit": "iter/sec",
            "range": "stddev: 0.000026092297952306057",
            "extra": "mean: 1.5412907739937123 msec\nrounds: 646"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4748.089839251672,
            "unit": "iter/sec",
            "range": "stddev: 0.000009216835308269925",
            "extra": "mean: 210.6110107128062 usec\nrounds: 4574"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 437.8066676402394,
            "unit": "iter/sec",
            "range": "stddev: 0.00005809468275911359",
            "extra": "mean: 2.2841132260272787 msec\nrounds: 438"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 618.4150062103399,
            "unit": "iter/sec",
            "range": "stddev: 0.00002645279236226548",
            "extra": "mean: 1.6170370866774737 msec\nrounds: 623"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 9338.71521697417,
            "unit": "iter/sec",
            "range": "stddev: 0.0000043558157812100085",
            "extra": "mean: 107.08111092009607 usec\nrounds: 9304"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 635.9852886050761,
            "unit": "iter/sec",
            "range": "stddev: 0.000023777333358806755",
            "extra": "mean: 1.5723634145584207 msec\nrounds: 632"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.411108295302236,
            "unit": "iter/sec",
            "range": "stddev: 0.0007916219864958482",
            "extra": "mean: 134.93258499999 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 537.630687300035,
            "unit": "iter/sec",
            "range": "stddev: 0.000048963128682202425",
            "extra": "mean: 1.8600128743059101 msec\nrounds: 541"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 349.517366455356,
            "unit": "iter/sec",
            "range": "stddev: 0.000146456805705583",
            "extra": "mean: 2.86108816320499 msec\nrounds: 337"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 761148.2922873773,
            "unit": "iter/sec",
            "range": "stddev: 1.17951128989931e-7",
            "extra": "mean: 1.3138044322412308 usec\nrounds: 73552"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 532.5770232005433,
            "unit": "iter/sec",
            "range": "stddev: 0.00005117030476170687",
            "extra": "mean: 1.877662678706001 msec\nrounds: 526"
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
        "date": 1771225816334,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 352.7061851810095,
            "unit": "iter/sec",
            "range": "stddev: 0.007441117307727335",
            "extra": "mean: 2.835221048042574 msec\nrounds: 562"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 397.942243543573,
            "unit": "iter/sec",
            "range": "stddev: 0.0004896839540957733",
            "extra": "mean: 2.5129274818759075 msec\nrounds: 469"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6655.989227978266,
            "unit": "iter/sec",
            "range": "stddev: 0.000017456404050512883",
            "extra": "mean: 150.2406277637181 usec\nrounds: 6649"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4665.396648546087,
            "unit": "iter/sec",
            "range": "stddev: 0.000028943714107527574",
            "extra": "mean: 214.34404731945733 usec\nrounds: 5093"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 336.70073013544766,
            "unit": "iter/sec",
            "range": "stddev: 0.0011858650219550295",
            "extra": "mean: 2.969996529552285 msec\nrounds: 423"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 323.26970587602545,
            "unit": "iter/sec",
            "range": "stddev: 0.0008673203855249624",
            "extra": "mean: 3.0933922412869146 msec\nrounds: 373"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 318.2052358098321,
            "unit": "iter/sec",
            "range": "stddev: 0.0008065996241577214",
            "extra": "mean: 3.1426258510643317 msec\nrounds: 376"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 320.89426694355365,
            "unit": "iter/sec",
            "range": "stddev: 0.0009154296765892531",
            "extra": "mean: 3.1162912616818526 msec\nrounds: 321"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 305.9491071601188,
            "unit": "iter/sec",
            "range": "stddev: 0.0005726564998031833",
            "extra": "mean: 3.268517464496633 msec\nrounds: 338"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 13109.555647201792,
            "unit": "iter/sec",
            "range": "stddev: 0.000019029241222980955",
            "extra": "mean: 76.2802361049856 usec\nrounds: 11173"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 17344.004182647994,
            "unit": "iter/sec",
            "range": "stddev: 0.000007197050131675101",
            "extra": "mean: 57.65681266385195 usec\nrounds: 11450"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 14136.80544755743,
            "unit": "iter/sec",
            "range": "stddev: 0.00005131739794606303",
            "extra": "mean: 70.73733904803655 usec\nrounds: 14116"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6648.91952538851,
            "unit": "iter/sec",
            "range": "stddev: 0.0000927842296642081",
            "extra": "mean: 150.4003765095304 usec\nrounds: 6045"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 17114.615283353243,
            "unit": "iter/sec",
            "range": "stddev: 0.00001175321049179358",
            "extra": "mean: 58.42959268693952 usec\nrounds: 18050"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 54879.08977067807,
            "unit": "iter/sec",
            "range": "stddev: 0.000012117534408475708",
            "extra": "mean: 18.221876568628886 usec\nrounds: 45661"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 214926.87715286945,
            "unit": "iter/sec",
            "range": "stddev: 0.000011213037569270299",
            "extra": "mean: 4.652745218499301 usec\nrounds: 171233"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 160.26764129162027,
            "unit": "iter/sec",
            "range": "stddev: 0.0009407680885736873",
            "extra": "mean: 6.239562721088639 msec\nrounds: 147"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4341.280488487327,
            "unit": "iter/sec",
            "range": "stddev: 0.000049282327919666125",
            "extra": "mean: 230.34678423840785 usec\nrounds: 4162"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 248.35594016491686,
            "unit": "iter/sec",
            "range": "stddev: 0.00023476270609333122",
            "extra": "mean: 4.0264790901959735 msec\nrounds: 255"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 170.50532507388334,
            "unit": "iter/sec",
            "range": "stddev: 0.00033399071053027",
            "extra": "mean: 5.864919465516283 msec\nrounds: 174"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 187.47531289312607,
            "unit": "iter/sec",
            "range": "stddev: 0.00038297828265388475",
            "extra": "mean: 5.334035636841792 msec\nrounds: 190"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 75.82214331119614,
            "unit": "iter/sec",
            "range": "stddev: 0.023600359915642476",
            "extra": "mean: 13.188759329787196 msec\nrounds: 94"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 131.5595560804499,
            "unit": "iter/sec",
            "range": "stddev: 0.0005544286878129988",
            "extra": "mean: 7.601120205882197 msec\nrounds: 136"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 60.497264340130656,
            "unit": "iter/sec",
            "range": "stddev: 0.02389753699380795",
            "extra": "mean: 16.5296730506317 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 3.665285316539337,
            "unit": "iter/sec",
            "range": "stddev: 0.25776048102682164",
            "extra": "mean: 272.830056499987 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 772.4625583287524,
            "unit": "iter/sec",
            "range": "stddev: 0.00015593059216324003",
            "extra": "mean: 1.2945611269024253 msec\nrounds: 788"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1635465.306615757,
            "unit": "iter/sec",
            "range": "stddev: 1.0374541767376445e-7",
            "extra": "mean: 611.4467827319949 nsec\nrounds: 161239"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 816145.976527423,
            "unit": "iter/sec",
            "range": "stddev: 1.0547042106808035e-7",
            "extra": "mean: 1.225271003913844 usec\nrounds: 81680"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23708.568978444273,
            "unit": "iter/sec",
            "range": "stddev: 0.0000019511053472693055",
            "extra": "mean: 42.17884263319291 usec\nrounds: 23925"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1507.129845581511,
            "unit": "iter/sec",
            "range": "stddev: 0.000005263098877146601",
            "extra": "mean: 663.5128372858676 usec\nrounds: 1518"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 150.947134678753,
            "unit": "iter/sec",
            "range": "stddev: 0.00002092083484755839",
            "extra": "mean: 6.6248359210541405 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10199.373483918323,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025289517738480893",
            "extra": "mean: 98.04523793316636 usec\nrounds: 10276"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1439.4145801381344,
            "unit": "iter/sec",
            "range": "stddev: 0.000005152544164124174",
            "extra": "mean: 694.7268798013943 usec\nrounds: 1406"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 71826.89554065584,
            "unit": "iter/sec",
            "range": "stddev: 0.000011657822711478213",
            "extra": "mean: 13.922361428442562 usec\nrounds: 63902"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1105552.95941128,
            "unit": "iter/sec",
            "range": "stddev: 0.0000015729922437097085",
            "extra": "mean: 904.5247371347201 nsec\nrounds: 113174"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5362.591518637719,
            "unit": "iter/sec",
            "range": "stddev: 0.00005642780334929731",
            "extra": "mean: 186.4770039866908 usec\nrounds: 5267"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 259.52618771056734,
            "unit": "iter/sec",
            "range": "stddev: 0.00020311723085606275",
            "extra": "mean: 3.853175700000013 msec\nrounds: 260"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 72189.73529278403,
            "unit": "iter/sec",
            "range": "stddev: 0.000011675290230467437",
            "extra": "mean: 13.85238491240123 usec\nrounds: 66810"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1962.6471431927619,
            "unit": "iter/sec",
            "range": "stddev: 0.0008663734175224899",
            "extra": "mean: 509.51593793535244 usec\nrounds: 2868"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 355747.8753019025,
            "unit": "iter/sec",
            "range": "stddev: 0.000005437883030955545",
            "extra": "mean: 2.81097954317045 usec\nrounds: 175101"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3102.414909950666,
            "unit": "iter/sec",
            "range": "stddev: 0.000028147554556397043",
            "extra": "mean: 322.3295494076587 usec\nrounds: 2783"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2588.5381372131005,
            "unit": "iter/sec",
            "range": "stddev: 0.00003059071899118537",
            "extra": "mean: 386.31843418642103 usec\nrounds: 1679"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5044.881126713767,
            "unit": "iter/sec",
            "range": "stddev: 0.00002725147275100275",
            "extra": "mean: 198.22072609496738 usec\nrounds: 2855"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1089.9922075921222,
            "unit": "iter/sec",
            "range": "stddev: 0.000019229431092791315",
            "extra": "mean: 917.4377514212492 usec\nrounds: 704"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 370405.3402289735,
            "unit": "iter/sec",
            "range": "stddev: 4.875133753335052e-7",
            "extra": "mean: 2.6997450937986747 usec\nrounds: 112918"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 126937.17871639504,
            "unit": "iter/sec",
            "range": "stddev: 0.000001366865691927428",
            "extra": "mean: 7.877912603006683 usec\nrounds: 79854"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 281068.4490004654,
            "unit": "iter/sec",
            "range": "stddev: 7.428559096642413e-7",
            "extra": "mean: 3.557852201327458 usec\nrounds: 165536"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 40.93454094464787,
            "unit": "iter/sec",
            "range": "stddev: 0.00741046237834124",
            "extra": "mean: 24.429246717392306 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.515869675082825,
            "unit": "iter/sec",
            "range": "stddev: 0.004414328101834504",
            "extra": "mean: 221.4412885999991 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1462.1215591605771,
            "unit": "iter/sec",
            "range": "stddev: 0.00025041910916300713",
            "extra": "mean: 683.9376614993031 usec\nrounds: 1548"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 391.13117175909514,
            "unit": "iter/sec",
            "range": "stddev: 0.00048054313521183543",
            "extra": "mean: 2.5566870456848125 msec\nrounds: 394"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 1001278.2915942784,
            "unit": "iter/sec",
            "range": "stddev: 9.944419362187853e-8",
            "extra": "mean: 998.7233403490222 nsec\nrounds: 100624"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 487615.17887098435,
            "unit": "iter/sec",
            "range": "stddev: 1.4054169561186398e-7",
            "extra": "mean: 2.0507975209372735 usec\nrounds: 49172"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59796.22676071114,
            "unit": "iter/sec",
            "range": "stddev: 0.0000012300699656247568",
            "extra": "mean: 16.72346323793537 usec\nrounds: 60021"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3974.2270312355076,
            "unit": "iter/sec",
            "range": "stddev: 0.0000058666531789229045",
            "extra": "mean: 251.6212566973357 usec\nrounds: 3845"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 397.65864430874063,
            "unit": "iter/sec",
            "range": "stddev: 0.00003511161688481458",
            "extra": "mean: 2.514719632810506 msec\nrounds: 384"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18524.945569897325,
            "unit": "iter/sec",
            "range": "stddev: 0.000002656578028583871",
            "extra": "mean: 53.98126522028656 usec\nrounds: 17329"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18396.74048940205,
            "unit": "iter/sec",
            "range": "stddev: 0.000002509816650130108",
            "extra": "mean: 54.35745536423028 usec\nrounds: 18315"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18375.378655899112,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025732902605822854",
            "extra": "mean: 54.420647254469856 usec\nrounds: 18322"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1507.1760083682218,
            "unit": "iter/sec",
            "range": "stddev: 0.000005020518417831052",
            "extra": "mean: 663.4925147744838 usec\nrounds: 1523"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 150.93194478611676,
            "unit": "iter/sec",
            "range": "stddev: 0.00001689208559113629",
            "extra": "mean: 6.625502649005709 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39918.003234069765,
            "unit": "iter/sec",
            "range": "stddev: 0.000001715282805847568",
            "extra": "mean: 25.05135324871426 usec\nrounds: 39768"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3857.8941793325653,
            "unit": "iter/sec",
            "range": "stddev: 0.000021071674047144352",
            "extra": "mean: 259.20876870008004 usec\nrounds: 3917"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8144.187024057581,
            "unit": "iter/sec",
            "range": "stddev: 0.000004729923281913421",
            "extra": "mean: 122.7869641311088 usec\nrounds: 8085"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1222.4475869277767,
            "unit": "iter/sec",
            "range": "stddev: 0.00010922196261473852",
            "extra": "mean: 818.030982017948 usec\nrounds: 1279"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 431.15436138169935,
            "unit": "iter/sec",
            "range": "stddev: 0.0000738919665408216",
            "extra": "mean: 2.3193549447008928 msec\nrounds: 434"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11502.125805187627,
            "unit": "iter/sec",
            "range": "stddev: 0.000004519231648745615",
            "extra": "mean: 86.94045056862319 usec\nrounds: 10378"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1045.2847301696806,
            "unit": "iter/sec",
            "range": "stddev: 0.000028464662270243075",
            "extra": "mean: 956.6771341217913 usec\nrounds: 1014"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1052.6122007787342,
            "unit": "iter/sec",
            "range": "stddev: 0.00004729872877902305",
            "extra": "mean: 950.0174891191542 usec\nrounds: 1057"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1199.4922975526513,
            "unit": "iter/sec",
            "range": "stddev: 0.00002185530225725093",
            "extra": "mean: 833.6860537081568 usec\nrounds: 1173"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1627.1332497936933,
            "unit": "iter/sec",
            "range": "stddev: 0.000013360684424582436",
            "extra": "mean: 614.5778166150754 usec\nrounds: 1625"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 987.4910254010081,
            "unit": "iter/sec",
            "range": "stddev: 0.000019298189296136875",
            "extra": "mean: 1.0126674311737793 msec\nrounds: 988"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1024.3535566480293,
            "unit": "iter/sec",
            "range": "stddev: 0.00006495563783869183",
            "extra": "mean: 976.225438482665 usec\nrounds: 1081"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1035.1268864313815,
            "unit": "iter/sec",
            "range": "stddev: 0.00002244614665076892",
            "extra": "mean: 966.0651395574487 usec\nrounds: 1039"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 758.1511007316644,
            "unit": "iter/sec",
            "range": "stddev: 0.000018511997552417646",
            "extra": "mean: 1.3189982828422144 msec\nrounds: 746"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1116.0615662234406,
            "unit": "iter/sec",
            "range": "stddev: 0.000013879112995603438",
            "extra": "mean: 896.0079177207285 usec\nrounds: 1106"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 942.2049066032581,
            "unit": "iter/sec",
            "range": "stddev: 0.000012911885941754358",
            "extra": "mean: 1.061340259418834 msec\nrounds: 929"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1616.260831409976,
            "unit": "iter/sec",
            "range": "stddev: 0.000012148183393000111",
            "extra": "mean: 618.7120176188586 usec\nrounds: 1646"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3149.094647082997,
            "unit": "iter/sec",
            "range": "stddev: 0.000013504247938717747",
            "extra": "mean: 317.5515861126305 usec\nrounds: 3298"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3123.3731414799813,
            "unit": "iter/sec",
            "range": "stddev: 0.000012836090761247351",
            "extra": "mean: 320.1666770836607 usec\nrounds: 3168"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4089.183533999885,
            "unit": "iter/sec",
            "range": "stddev: 0.000016452177502957524",
            "extra": "mean: 244.54759530488417 usec\nrounds: 4260"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7609.12212348197,
            "unit": "iter/sec",
            "range": "stddev: 0.000007889586506562662",
            "extra": "mean: 131.4212052023677 usec\nrounds: 7651"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 645.1815291644339,
            "unit": "iter/sec",
            "range": "stddev: 0.000060507626387417116",
            "extra": "mean: 1.549951377707739 msec\nrounds: 646"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4487.906057229508,
            "unit": "iter/sec",
            "range": "stddev: 0.000007250192129931436",
            "extra": "mean: 222.82106337522666 usec\nrounds: 4355"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 423.7925830705265,
            "unit": "iter/sec",
            "range": "stddev: 0.000025959886076246623",
            "extra": "mean: 2.359644882774134 msec\nrounds: 418"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 617.0567191549496,
            "unit": "iter/sec",
            "range": "stddev: 0.00003130868225046841",
            "extra": "mean: 1.6205965658545713 msec\nrounds: 615"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 9367.389330168062,
            "unit": "iter/sec",
            "range": "stddev: 0.000003595608027802278",
            "extra": "mean: 106.75332953008144 usec\nrounds: 9465"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 637.1330319242171,
            "unit": "iter/sec",
            "range": "stddev: 0.00002860827806125705",
            "extra": "mean: 1.569530929796375 msec\nrounds: 641"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.442169893232658,
            "unit": "iter/sec",
            "range": "stddev: 0.0005146283271556057",
            "extra": "mean: 134.3694130000074 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 569.8408637938479,
            "unit": "iter/sec",
            "range": "stddev: 0.00005013552300020833",
            "extra": "mean: 1.7548759022690437 msec\nrounds: 573"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 364.55197480367224,
            "unit": "iter/sec",
            "range": "stddev: 0.00006169160815831436",
            "extra": "mean: 2.743093081688956 msec\nrounds: 355"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 762273.0501903171,
            "unit": "iter/sec",
            "range": "stddev: 1.1271878846632882e-7",
            "extra": "mean: 1.311865872406652 usec\nrounds: 74157"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 563.6577671947199,
            "unit": "iter/sec",
            "range": "stddev: 0.00005340290515939591",
            "extra": "mean: 1.774126177621788 msec\nrounds: 563"
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
        "date": 1771226015942,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_without_permissions",
            "value": 387.6980053171663,
            "unit": "iter/sec",
            "range": "stddev: 0.008309834070034702",
            "extra": "mean: 2.579327172916261 msec\nrounds: 480"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_write_performance_with_permissions",
            "value": 431.5097126590226,
            "unit": "iter/sec",
            "range": "stddev: 0.0006722337563325286",
            "extra": "mean: 2.3174449396234014 msec\nrounds: 530"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_without_permissions",
            "value": 6617.020893974539,
            "unit": "iter/sec",
            "range": "stddev: 0.000018674222121383496",
            "extra": "mean: 151.12541066790348 usec\nrounds: 6918"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_read_performance_with_permissions",
            "value": 4969.48398893395,
            "unit": "iter/sec",
            "range": "stddev: 0.000028177777788871715",
            "extra": "mean: 201.228136005026 usec\nrounds: 4941"
          },
          {
            "name": "tests/benchmarks/test_async_permission_performance.py::test_permission_overhead_acceptable",
            "value": 379.9755339433415,
            "unit": "iter/sec",
            "range": "stddev: 0.0007959369490106143",
            "extra": "mean: 2.631748390803264 msec\nrounds: 435"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_tiny_file",
            "value": 334.71178804710587,
            "unit": "iter/sec",
            "range": "stddev: 0.0007500635596172507",
            "extra": "mean: 2.9876450000000125 msec\nrounds: 357"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_small_file",
            "value": 332.0516396539323,
            "unit": "iter/sec",
            "range": "stddev: 0.0009372367758465264",
            "extra": "mean: 3.0115797682619805 msec\nrounds: 397"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_medium_file",
            "value": 340.5427952560473,
            "unit": "iter/sec",
            "range": "stddev: 0.0005635654875853416",
            "extra": "mean: 2.9364884940470404 msec\nrounds: 336"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_write_large_file",
            "value": 312.5642098592626,
            "unit": "iter/sec",
            "range": "stddev: 0.0005806466758441234",
            "extra": "mean: 3.199342626112782 msec\nrounds: 337"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_tiny_file",
            "value": 16451.29650643661,
            "unit": "iter/sec",
            "range": "stddev: 0.000022591787264635387",
            "extra": "mean: 60.78548274956612 usec\nrounds: 15130"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_small_file",
            "value": 14338.20668801161,
            "unit": "iter/sec",
            "range": "stddev: 0.000021676483946536515",
            "extra": "mean: 69.7437288887818 usec\nrounds: 15809"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_medium_file",
            "value": 11972.180865902388,
            "unit": "iter/sec",
            "range": "stddev: 0.00006270757961486395",
            "extra": "mean: 83.52697066647816 usec\nrounds: 13091"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_large_file",
            "value": 6421.765149007959,
            "unit": "iter/sec",
            "range": "stddev: 0.00009638376997613213",
            "extra": "mean: 155.7204252719334 usec\nrounds: 5801"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_read_cached_file",
            "value": 14532.404208351929,
            "unit": "iter/sec",
            "range": "stddev: 0.000016722410199919195",
            "extra": "mean: 68.81173862651642 usec\nrounds: 17607"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check",
            "value": 44085.80143436438,
            "unit": "iter/sec",
            "range": "stddev: 0.0007807304930608532",
            "extra": "mean: 22.683040059707555 usec\nrounds: 46081"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_exists_check_nonexistent",
            "value": 216122.1262519344,
            "unit": "iter/sec",
            "range": "stddev: 0.000006681924634241735",
            "extra": "mean: 4.6270135193575515 usec\nrounds: 175747"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestFileOperationBenchmarks::test_delete_file",
            "value": 162.51694155462923,
            "unit": "iter/sec",
            "range": "stddev: 0.0010353443690573904",
            "extra": "mean: 6.153204647060473 msec\nrounds: 170"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_small_directory",
            "value": 4285.825116864783,
            "unit": "iter/sec",
            "range": "stddev: 0.00007447049271508135",
            "extra": "mean: 233.32729934895983 usec\nrounds: 4149"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_large_directory",
            "value": 243.70526626836318,
            "unit": "iter/sec",
            "range": "stddev: 0.0005233526938674339",
            "extra": "mean: 4.1033171556449695 msec\nrounds: 257"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_recursive",
            "value": 170.63078830079573,
            "unit": "iter/sec",
            "range": "stddev: 0.0004299850831906279",
            "extra": "mean: 5.860607044944048 msec\nrounds: 178"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_simple_pattern",
            "value": 185.62617101742435,
            "unit": "iter/sec",
            "range": "stddev: 0.0003379198166944395",
            "extra": "mean: 5.387171402173307 msec\nrounds: 184"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_extension_pattern",
            "value": 93.51593226702579,
            "unit": "iter/sec",
            "range": "stddev: 0.0004962417863395262",
            "extra": "mean: 10.693365031581953 msec\nrounds: 95"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_recursive_pattern",
            "value": 131.86216899465265,
            "unit": "iter/sec",
            "range": "stddev: 0.00044181659116757923",
            "extra": "mean: 7.583676255473644 msec\nrounds: 137"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_1k_files",
            "value": 60.22980761858513,
            "unit": "iter/sec",
            "range": "stddev: 0.023633882655447778",
            "extra": "mean: 16.603074782052428 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_list_10k_files",
            "value": 3.8704355355747126,
            "unit": "iter/sec",
            "range": "stddev: 0.23609237175292966",
            "extra": "mean: 258.368855599997 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestGlobBenchmarks::test_glob_deep_path",
            "value": 785.5016321530011,
            "unit": "iter/sec",
            "range": "stddev: 0.00015828615471847888",
            "extra": "mean: 1.2730718296015182 msec\nrounds: 804"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_tiny",
            "value": 1698169.8243744557,
            "unit": "iter/sec",
            "range": "stddev: 7.671411271412682e-8",
            "extra": "mean: 588.8692553869657 nsec\nrounds: 170329"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_small",
            "value": 823405.8514935572,
            "unit": "iter/sec",
            "range": "stddev: 1.0179393249073256e-7",
            "extra": "mean: 1.2144679299838865 usec\nrounds: 82217"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_medium",
            "value": 23737.78651635802,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017506580815010082",
            "extra": "mean: 42.12692701195568 usec\nrounds: 23908"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_large",
            "value": 1507.1801572205266,
            "unit": "iter/sec",
            "range": "stddev: 0.0000060005300585020855",
            "extra": "mean: 663.4906883621362 usec\nrounds: 1521"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_xlarge",
            "value": 151.070819737202,
            "unit": "iter/sec",
            "range": "stddev: 0.000008464895735395247",
            "extra": "mean: 6.619412019737288 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_md5_medium",
            "value": 10199.400369659596,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025791816014168536",
            "extra": "mean: 98.04497948474739 usec\nrounds: 10285"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestHashingBenchmarks::test_sha256_incremental",
            "value": 1439.917571604475,
            "unit": "iter/sec",
            "range": "stddev: 0.0000052901528936108995",
            "extra": "mean: 694.4841980681696 usec\nrounds: 1449"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_single",
            "value": 72700.8386528211,
            "unit": "iter/sec",
            "range": "stddev: 0.000010893293879695204",
            "extra": "mean: 13.754999509365298 usec\nrounds: 65236"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_metadata_nonexistent",
            "value": 1154821.3585524398,
            "unit": "iter/sec",
            "range": "stddev: 9.103619797025774e-7",
            "extra": "mean: 865.9347981349192 nsec\nrounds: 118400"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_small",
            "value": 5471.2035111057285,
            "unit": "iter/sec",
            "range": "stddev: 0.000045232674997540816",
            "extra": "mean: 182.77514224615274 usec\nrounds: 5378"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_list_metadata_large",
            "value": 259.25261367275374,
            "unit": "iter/sec",
            "range": "stddev: 0.00032618325656109856",
            "extra": "mean: 3.857241729729552 msec\nrounds: 259"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_exists_metadata_cached",
            "value": 72598.96183499433,
            "unit": "iter/sec",
            "range": "stddev: 0.000008625863327332866",
            "extra": "mean: 13.774301652864374 usec\nrounds: 67034"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_set_file_metadata",
            "value": 1982.3413455175903,
            "unit": "iter/sec",
            "range": "stddev: 0.0011116533402507796",
            "extra": "mean: 504.4539893501033 usec\nrounds: 2629"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestMetadataBenchmarks::test_get_file_metadata",
            "value": 356318.5112577446,
            "unit": "iter/sec",
            "range": "stddev: 0.000003869702394494722",
            "extra": "mean: 2.806477823647634 usec\nrounds: 174217"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_simple",
            "value": 3120.903317344662,
            "unit": "iter/sec",
            "range": "stddev: 0.000024143384401634895",
            "extra": "mean: 320.4200509648673 usec\nrounds: 2747"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_python",
            "value": 2556.3844651335635,
            "unit": "iter/sec",
            "range": "stddev: 0.00002793139507216414",
            "extra": "mean: 391.1774670981476 usec\nrounds: 1702"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_bulk_rust",
            "value": 5026.038561469535,
            "unit": "iter/sec",
            "range": "stddev: 0.00003161537102914131",
            "extra": "mean: 198.96385349411557 usec\nrounds: 4150"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPermissionBenchmarks::test_permission_check_scale_1000",
            "value": 1111.0925036984747,
            "unit": "iter/sec",
            "range": "stddev: 0.00004397978750505639",
            "extra": "mean: 900.0150722566457 usec\nrounds: 775"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_simple",
            "value": 363608.60788012104,
            "unit": "iter/sec",
            "range": "stddev: 5.117288079222397e-7",
            "extra": "mean: 2.750209918929346 usec\nrounds: 110657"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_validation_deep",
            "value": 126960.73146543147,
            "unit": "iter/sec",
            "range": "stddev: 0.0000012666867814486457",
            "extra": "mean: 7.876451155074491 usec\nrounds: 78964"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestPathResolutionBenchmarks::test_path_resolution_deep",
            "value": 280946.50446149125,
            "unit": "iter/sec",
            "range": "stddev: 8.096434855651356e-7",
            "extra": "mean: 3.5593964833866365 usec\nrounds: 170620"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_10",
            "value": 41.48699434812597,
            "unit": "iter/sec",
            "range": "stddev: 0.0017627230280952695",
            "extra": "mean: 24.103939456514798 msec\nrounds: 46"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_write_batch_100",
            "value": 4.452027454192912,
            "unit": "iter/sec",
            "range": "stddev: 0.005237240752296161",
            "extra": "mean: 224.6167640000067 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_10",
            "value": 1461.9727545644696,
            "unit": "iter/sec",
            "range": "stddev: 0.00021420959002853423",
            "extra": "mean: 684.0072750178617 usec\nrounds: 1509"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBulkOperationBenchmarks::test_read_bulk_50",
            "value": 393.05936977182256,
            "unit": "iter/sec",
            "range": "stddev: 0.0005328524223060705",
            "extra": "mean: 2.544144922891716 msec\nrounds: 415"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_tiny_content",
            "value": 920998.8363630341,
            "unit": "iter/sec",
            "range": "stddev: 1.0375423413031078e-7",
            "extra": "mean: 1.085777701901271 usec\nrounds: 91241"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1kb_content",
            "value": 457393.25006793236,
            "unit": "iter/sec",
            "range": "stddev: 1.6528200465488392e-7",
            "extra": "mean: 2.1863024866490255 usec\nrounds: 44719"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_64kb_content",
            "value": 59217.74638143415,
            "unit": "iter/sec",
            "range": "stddev: 0.000001368154935415908",
            "extra": "mean: 16.886829727676332 usec\nrounds: 59305"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_1mb_content",
            "value": 3976.5295312893495,
            "unit": "iter/sec",
            "range": "stddev: 0.000005686153261676928",
            "extra": "mean: 251.4755623292857 usec\nrounds: 4019"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_10mb_content",
            "value": 396.9973775731818,
            "unit": "iter/sec",
            "range": "stddev: 0.000056860576198607186",
            "extra": "mean: 2.5189083265812298 msec\nrounds: 395"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_256kb_content",
            "value": 18458.390772349918,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025021856901213175",
            "extra": "mean: 54.175903649085605 usec\nrounds: 18443"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_1mb_content",
            "value": 18322.732116208557,
            "unit": "iter/sec",
            "range": "stddev: 0.0000026542154506858496",
            "extra": "mean: 54.57701360570486 usec\nrounds: 18007"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_hash_smart_10mb_content",
            "value": 18328.051515485466,
            "unit": "iter/sec",
            "range": "stddev: 0.0000025904181948798884",
            "extra": "mean: 54.56117357347533 usec\nrounds: 18315"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_1mb",
            "value": 1507.0392349026486,
            "unit": "iter/sec",
            "range": "stddev: 0.000004949190178662531",
            "extra": "mean: 663.5527309709345 usec\nrounds: 1524"
          },
          {
            "name": "tests/benchmarks/test_core_operations.py::TestBlake3HashingBenchmarks::test_sha256_baseline_10mb",
            "value": 151.08934102793117,
            "unit": "iter/sec",
            "range": "stddev: 0.000009753552714436232",
            "extra": "mean: 6.618600578945769 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_1k_lines",
            "value": 39502.25673107694,
            "unit": "iter/sec",
            "range": "stddev: 0.0000017822111227474496",
            "extra": "mean: 25.315009388141796 usec\nrounds: 39625"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_simple_10k_lines",
            "value": 3930.1883175802222,
            "unit": "iter/sec",
            "range": "stddev: 0.000010484405774903389",
            "extra": "mean: 254.4407339279076 usec\nrounds: 3920"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_complex_pattern",
            "value": 8152.857933418197,
            "unit": "iter/sec",
            "range": "stddev: 0.000004588236334368996",
            "extra": "mean: 122.65637499962376 usec\nrounds: 8248"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_line_by_line",
            "value": 1253.8938740775852,
            "unit": "iter/sec",
            "range": "stddev: 0.00002036336246295126",
            "extra": "mean: 797.5156595574249 usec\nrounds: 1266"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestPythonRegexBenchmarks::test_python_regex_case_insensitive",
            "value": 432.73396624646034,
            "unit": "iter/sec",
            "range": "stddev: 0.000016404993755790914",
            "extra": "mean: 2.3108886244220948 msec\nrounds: 434"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_1k_lines",
            "value": 11653.023756977562,
            "unit": "iter/sec",
            "range": "stddev: 0.000004552829059460292",
            "extra": "mean: 85.81463668614106 usec\nrounds: 10151"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_10k_lines",
            "value": 1039.0421327442502,
            "unit": "iter/sec",
            "range": "stddev: 0.00001928340740600146",
            "extra": "mean: 962.4248800757147 usec\nrounds: 1034"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_multiple_files",
            "value": 1046.9588970699335,
            "unit": "iter/sec",
            "range": "stddev: 0.000025935472201547647",
            "extra": "mean: 955.1473346266458 usec\nrounds: 1031"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_regex_pattern",
            "value": 1191.2535575438988,
            "unit": "iter/sec",
            "range": "stddev: 0.00001347623456244313",
            "extra": "mean: 839.4518477340615 usec\nrounds: 1169"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustGrepBenchmarks::test_rust_grep_case_insensitive",
            "value": 1605.1092428265174,
            "unit": "iter/sec",
            "range": "stddev: 0.000014844135266714644",
            "extra": "mean: 623.0105548698042 usec\nrounds: 1622"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_single_file",
            "value": 978.474841120115,
            "unit": "iter/sec",
            "range": "stddev: 0.000018230038059387954",
            "extra": "mean: 1.0219986840491924 msec\nrounds: 978"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_multiple_files",
            "value": 1036.3207709369979,
            "unit": "iter/sec",
            "range": "stddev: 0.00002621738176491446",
            "extra": "mean: 964.952192452769 usec\nrounds: 1060"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_vs_bulk_grep_comparison",
            "value": 1032.3280692760288,
            "unit": "iter/sec",
            "range": "stddev: 0.000023576587094237333",
            "extra": "mean: 968.6843066287054 usec\nrounds: 1011"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_case_insensitive",
            "value": 733.7280504800174,
            "unit": "iter/sec",
            "range": "stddev: 0.00012059660880949623",
            "extra": "mean: 1.362902780322741 msec\nrounds: 742"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestRustMmapGrepBenchmarks::test_mmap_grep_regex_pattern",
            "value": 1094.6590762675914,
            "unit": "iter/sec",
            "range": "stddev: 0.000014560081936969404",
            "extra": "mean: 913.5264318180724 usec\nrounds: 1056"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_simple",
            "value": 928.506421695978,
            "unit": "iter/sec",
            "range": "stddev: 0.00002159704162809324",
            "extra": "mean: 1.076998474790766 msec\nrounds: 952"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_python_fnmatch_complex",
            "value": 1581.5325108928594,
            "unit": "iter/sec",
            "range": "stddev: 0.000015829295166802238",
            "extra": "mean: 632.2980989087899 usec\nrounds: 1648"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_simple",
            "value": 3170.7172394727113,
            "unit": "iter/sec",
            "range": "stddev: 0.000013237847003939929",
            "extra": "mean: 315.3860544708488 usec\nrounds: 3176"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_multiple_patterns",
            "value": 3181.9478415179706,
            "unit": "iter/sec",
            "range": "stddev: 0.00001342941015499468",
            "extra": "mean: 314.2729076045895 usec\nrounds: 3182"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestGlobPatternBenchmarks::test_rust_glob_recursive_pattern",
            "value": 4125.198995993312,
            "unit": "iter/sec",
            "range": "stddev: 0.000013284251888265171",
            "extra": "mean: 242.41254809071555 usec\nrounds: 4138"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_100_results",
            "value": 7825.646099863581,
            "unit": "iter/sec",
            "range": "stddev: 0.000005121987863058109",
            "extra": "mean: 127.78497612068507 usec\nrounds: 7831"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_fusion_1k_results",
            "value": 652.7331246865423,
            "unit": "iter/sec",
            "range": "stddev: 0.00002273597257118191",
            "extra": "mean: 1.5320196910187809 msec\nrounds: 657"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_100_results",
            "value": 4617.563640573122,
            "unit": "iter/sec",
            "range": "stddev: 0.000007746089443179905",
            "extra": "mean: 216.56442181181984 usec\nrounds: 4438"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_weighted_fusion_1k_results",
            "value": 434.55729776116294,
            "unit": "iter/sec",
            "range": "stddev: 0.000033871446072650905",
            "extra": "mean: 2.301192512821658 msec\nrounds: 429"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_rrf_weighted_fusion_1k_results",
            "value": 607.752637755579,
            "unit": "iter/sec",
            "range": "stddev: 0.000020785785869506773",
            "extra": "mean: 1.6454062687296336 msec\nrounds: 614"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_normalization_overhead",
            "value": 9138.638245191629,
            "unit": "iter/sec",
            "range": "stddev: 0.000003976545979085716",
            "extra": "mean: 109.42549351115396 usec\nrounds: 9169"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestHybridSearchFusionBenchmarks::test_fuse_results_dispatcher",
            "value": 641.9044232069089,
            "unit": "iter/sec",
            "range": "stddev: 0.000025532464593114958",
            "extra": "mean: 1.5578643234830367 msec\nrounds: 643"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_build_1k_files",
            "value": 7.339657087932649,
            "unit": "iter/sec",
            "range": "stddev: 0.0006194382856371275",
            "extra": "mean: 136.24614720000068 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_literal",
            "value": 568.2816872861997,
            "unit": "iter/sec",
            "range": "stddev: 0.00004314078429146477",
            "extra": "mean: 1.7596906998278428 msec\nrounds: 583"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_regex",
            "value": 365.2978307201107,
            "unit": "iter/sec",
            "range": "stddev: 0.00006966778681568069",
            "extra": "mean: 2.7374923032767606 msec\nrounds: 366"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_search_no_match",
            "value": 758111.9865880243,
            "unit": "iter/sec",
            "range": "stddev: 9.850918759046569e-8",
            "extra": "mean: 1.3190663354376209 usec\nrounds: 73769"
          },
          {
            "name": "tests/benchmarks/test_search_benchmarks.py::TestTrigramBenchmarks::test_trigram_vs_mmap_grep",
            "value": 568.0118096604292,
            "unit": "iter/sec",
            "range": "stddev: 0.00004080501958538198",
            "extra": "mean: 1.7605267760151389 msec\nrounds: 567"
          }
        ]
      }
    ]
  }
}