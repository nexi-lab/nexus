window.BENCHMARK_DATA = {
  "lastUpdate": 1771209387666,
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
      }
    ]
  }
}