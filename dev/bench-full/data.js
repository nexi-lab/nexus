window.BENCHMARK_DATA = {
  "lastUpdate": 1771210259866,
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
      }
    ]
  }
}