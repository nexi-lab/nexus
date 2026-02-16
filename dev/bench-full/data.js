window.BENCHMARK_DATA = {
  "lastUpdate": 1771212732196,
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
      }
    ]
  }
}