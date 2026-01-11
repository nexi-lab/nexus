#!/usr/bin/env python3
"""Comprehensive grep test: Native Bash vs Native Python vs Nexus with CSV output."""

import csv
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from nexus.remote.client import RemoteNexusFS

DATA_DIR = Path("/tmp/nexus_perf_data")
NEXUS_URL = os.getenv("NEXUS_URL", "http://localhost:2026")
NEXUS_API_KEY = os.getenv(
    "NEXUS_API_KEY", "sk-default_admin_dddddddd_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
)

# Pattern to search for
SEARCH_PATTERN = "ERROR"


def test_native_bash(file_count=1000):
    """Test native filesystem using bash grep."""
    print(f"\n{'=' * 70}")
    print(f"NATIVE BASH - GREP {file_count} FILES")
    print(f"{'=' * 70}")

    if file_count == 1000:
        source_dir = DATA_DIR / "grep_medium_1k"
    else:
        source_dir = DATA_DIR / "grep_medium_10k"

    results = {}

    # Get files
    files = list(source_dir.glob("*.txt"))[:file_count]
    total_bytes = sum(f.stat().st_size for f in files)

    print(f"📁 Dataset: {len(files)} files ({total_bytes / (1024 * 1024):.2f} MB)")
    print(f"🔍 Pattern: '{SEARCH_PATTERN}'")

    # Test: Grep (using bash grep)
    print("\n[1/1] Grep operation (bash grep)...")
    file_list = " ".join([f'"{f}"' for f in files])
    start = time.time()
    result = subprocess.run(
        f"grep -c '{SEARCH_PATTERN}' {file_list} | awk -F: '{{sum += $2}} END {{print sum}}'",
        shell=True,
        capture_output=True,
        text=True,
    )
    # Count total matches
    match_count = int(result.stdout.strip()) if result.stdout.strip() else 0
    grep_duration = time.time() - start

    print(f"  ✓ Searched {len(files)} files in {grep_duration:.3f}s")
    print(f"    Matches: {match_count}")

    results = {
        "method": "native_bash",
        "file_count_target": file_count,
        "files_searched": len(files),
        "match_count": match_count,
        "total_bytes": total_bytes,
        "grep_duration": grep_duration,
    }

    return results


def test_native_python(file_count=1000):
    """Test native filesystem using Python re."""
    print(f"\n{'=' * 70}")
    print(f"NATIVE PYTHON - GREP {file_count} FILES")
    print(f"{'=' * 70}")

    if file_count == 1000:
        source_dir = DATA_DIR / "grep_medium_1k"
    else:
        source_dir = DATA_DIR / "grep_medium_10k"

    results = {}

    # Get files
    files = list(source_dir.glob("*.txt"))[:file_count]
    total_bytes = sum(f.stat().st_size for f in files)

    print(f"📁 Dataset: {len(files)} files ({total_bytes / (1024 * 1024):.2f} MB)")
    print(f"🔍 Pattern: '{SEARCH_PATTERN}'")

    # Test: Grep (using Python re)
    print("\n[1/1] Grep operation (Python re)...")
    match_count = 0
    start = time.time()

    pattern = re.compile(SEARCH_PATTERN)
    for file in files:
        content = file.read_text()
        matches = pattern.findall(content)
        match_count += len(matches)

    grep_duration = time.time() - start

    print(f"  ✓ Searched {len(files)} files in {grep_duration:.3f}s")
    print(f"    Matches: {match_count}")

    results = {
        "method": "native_python",
        "file_count_target": file_count,
        "files_searched": len(files),
        "match_count": match_count,
        "total_bytes": total_bytes,
        "grep_duration": grep_duration,
    }

    return results


def test_nexus(file_count=1000):
    """Test Nexus filesystem grep."""
    print(f"\n{'=' * 70}")
    print(f"NEXUS - GREP {file_count} FILES")
    print(f"{'=' * 70}")

    client = RemoteNexusFS(server_url=NEXUS_URL, api_key=NEXUS_API_KEY)

    if file_count == 1000:
        source_dir = DATA_DIR / "grep_medium_1k"
    else:
        source_dir = DATA_DIR / "grep_medium_10k"

    nexus_path = f"/perf_test/grep_medium_{file_count}"

    results = {}

    # Get source files
    files = list(source_dir.glob("*.txt"))[:file_count]

    # Upload files
    print(f"\n📤 Uploading {len(files)} files...")
    upload_count = 0
    upload_bytes = 0
    start = time.time()

    for i, file in enumerate(files):
        try:
            content = file.read_bytes()
            client.write(f"{nexus_path}/{file.name}", content)
            upload_count += 1
            upload_bytes += len(content)

            if (i + 1) % 1000 == 0:
                print(f"  Uploaded {i + 1}/{len(files)} files...")
        except Exception as e:
            print(f"  ⚠️ Error uploading {file.name}: {e}")

    upload_duration = time.time() - start
    print(f"✓ Uploaded {upload_count} files in {upload_duration:.2f}s")

    # Verification: Check uploaded files
    print("\n🔍 Verifying upload...")
    try:
        uploaded_files = client.list(nexus_path, recursive=False)
        print(f"  ✓ Found {len(uploaded_files)} files in Nexus")

        if uploaded_files:
            # Read first file to verify content
            first_file = uploaded_files[0]
            content = client.read(first_file)
            has_pattern = SEARCH_PATTERN.encode() in content
            print(
                f"  ✓ Sample file '{Path(first_file).name}': {len(content)} bytes, contains '{SEARCH_PATTERN}': {has_pattern}"
            )
    except Exception as e:
        print(f"  ⚠️ Verification error: {e}")

    print(f"\n📁 Dataset: {upload_count} files ({upload_bytes / (1024 * 1024):.2f} MB)")
    print(f"🔍 Pattern: '{SEARCH_PATTERN}'")

    # Test: Grep
    print("\n[1/1] Grep operation (Nexus)...")
    start = time.time()
    try:
        grep_results = client.grep(
            pattern=SEARCH_PATTERN,
            path=nexus_path,
            file_pattern="**/*.txt",  # Use .txt files to avoid gitignore filtering
            max_results=1000000,  # Large number to capture all matches
        )

        grep_duration = time.time() - start

        # Count matches
        match_count = len(grep_results) if grep_results else 0

        print(f"  ✓ Searched {upload_count} files in {grep_duration:.3f}s")
        print(f"    Matches: {match_count}")

        # If no matches, try alternative patterns for debugging
        if match_count == 0:
            print("\n  ⚠️ No matches found, trying alternative patterns...")

            # Try without file_pattern
            print("  Testing without file_pattern...")
            alt_results1 = client.grep(pattern=SEARCH_PATTERN, path=nexus_path, max_results=100)
            print(f"    Without file_pattern: {len(alt_results1) if alt_results1 else 0} matches")

            # Try with single * pattern
            print("  Testing with file_pattern='*.txt'...")
            alt_results2 = client.grep(
                pattern=SEARCH_PATTERN, path=nexus_path, file_pattern="*.txt", max_results=100
            )
            print(f"    With '*.txt': {len(alt_results2) if alt_results2 else 0} matches")

            # Update match_count if any alternative worked
            if alt_results1:
                match_count = len(alt_results1)
                print(f"  ✓ Using results without file_pattern: {match_count} matches")
            elif alt_results2:
                match_count = len(alt_results2)
                print(f"  ✓ Using results with '*.txt' pattern: {match_count} matches")

        results = {
            "method": "nexus",
            "file_count_target": file_count,
            "files_searched": upload_count,
            "match_count": match_count,
            "total_bytes": upload_bytes,
            "grep_duration": grep_duration,
            "upload_count": upload_count,
            "upload_duration": upload_duration,
        }

    except Exception as e:
        print(f"  ❌ Error during grep: {e}")
        grep_duration = time.time() - start
        results = {
            "method": "nexus",
            "file_count_target": file_count,
            "files_searched": upload_count,
            "match_count": 0,
            "total_bytes": upload_bytes,
            "grep_duration": grep_duration,
            "upload_count": upload_count,
            "upload_duration": upload_duration,
            "error": str(e),
        }

    return results


def test_sandbox_bash(file_count=1000):
    """Test grep via FUSE-mounted Nexus in Docker sandbox using bash."""
    print(f"\n{'=' * 70}")
    print(f"SANDBOX BASH - GREP {file_count} FILES (FUSE-mounted)")
    print(f"{'=' * 70}")

    client = RemoteNexusFS(server_url=NEXUS_URL, api_key=NEXUS_API_KEY)
    nexus_path = (
        "/perf_test/grep_medium_1000" if file_count == 1000 else "/perf_test/grep_medium_10000"
    )
    mount_path = "/mnt/nexus"

    # Step 1: Create sandbox
    print("\n[1/2] Creating Docker sandbox...")
    sandbox_name = f"perf_grep_{uuid.uuid4().hex[:8]}"
    start = time.time()
    result = client.sandbox_get_or_create(
        name=sandbox_name, ttl_minutes=60, provider="docker", verify_status=True
    )
    sandbox_id = result["sandbox_id"]
    create_duration = time.time() - start
    print(f"  ✓ Created sandbox: {sandbox_id} in {create_duration:.2f}s")

    # Step 2: Connect and mount Nexus
    print("\n[2/2] Mounting Nexus filesystem via FUSE...")
    print("  → Installing nexus-ai-fs[fuse] in sandbox...")
    print("  → Starting FUSE mount (this may take 5-10 seconds)...")
    start = time.time()
    mount_result = client.sandbox_connect(
        sandbox_id=sandbox_id, provider="docker", mount_path=mount_path
    )
    mount_duration = time.time() - start
    print(f"  ✓ Mounted in {mount_duration:.2f}s at {mount_result.get('mount_path', mount_path)}")

    setup_duration = create_duration + mount_duration

    try:
        # Get source directory to determine total bytes
        if file_count == 1000:
            source_dir = DATA_DIR / "grep_medium_1k"
        else:
            source_dir = DATA_DIR / "grep_medium_10k"

        files = list(source_dir.glob("*.txt"))[:file_count]
        total_bytes = sum(f.stat().st_size for f in files)

        # Step 1: Verify files are accessible
        print("\n[Step 1/4] Verifying files are accessible via FUSE...")
        print(f"  → Executing: ls {mount_path}{nexus_path} | wc -l")
        start = time.time()
        result = client.sandbox_run(
            sandbox_id=sandbox_id,
            language="bash",
            code=f"ls {mount_path}{nexus_path} | wc -l",
            timeout=180,
        )
        ls_duration = time.time() - start
        file_count_visible = int(result.get("stdout", "0").strip())
        print(f"  ✓ Listed files in {ls_duration:.3f}s - {file_count_visible} files visible")

        # Step 2: Test reading a single file
        print("\n[Step 2/4] Testing single file read via FUSE...")
        print(f"  → Executing: ls {mount_path}{nexus_path}/*.txt | head -1 | xargs cat | wc -c")
        start = time.time()
        result = client.sandbox_run(
            sandbox_id=sandbox_id,
            language="bash",
            code=f"ls {mount_path}{nexus_path}/*.txt | head -1 | xargs cat | wc -c",
            timeout=180,
        )
        read_duration = time.time() - start
        bytes_read = result.get("stdout", "0").strip()
        print(f"  ✓ Read single file in {read_duration:.3f}s - {bytes_read} bytes")

        # Step 3: Test grep on first 10 files
        print("\n[Step 3/4] Testing grep on first 10 files...")
        print(
            f"  → Executing: ls {mount_path}{nexus_path}/*.txt | head -10 | xargs grep -c '{SEARCH_PATTERN}' | awk '{{sum += $0}} END {{print sum}}'"
        )
        start = time.time()
        result = client.sandbox_run(
            sandbox_id=sandbox_id,
            language="bash",
            code=f"ls {mount_path}{nexus_path}/*.txt | head -10 | xargs grep -c '{SEARCH_PATTERN}' | awk '{{sum += $0}} END {{print sum}}'",
            timeout=180,
        )
        sample_grep_duration = time.time() - start
        sample_matches = result.get("stdout", "0").strip()
        print(f"  ✓ Grepped 10 files in {sample_grep_duration:.3f}s - {sample_matches} matches")
        print(
            f"  📊 Estimated time for {file_count} files: {sample_grep_duration * file_count / 10:.1f}s"
        )

        # Step 4: Full grep operation
        print("\n[Step 4/4] Full grep operation (bash grep via FUSE)...")
        print(f"  → Executing: grep -r '{SEARCH_PATTERN}' {mount_path}{nexus_path} | wc -l")
        print(f"  ⚠️  This may take a while for {file_count} files...")
        start = time.time()
        result = client.sandbox_run(
            sandbox_id=sandbox_id,
            language="bash",
            code=f"grep -r '{SEARCH_PATTERN}' {mount_path}{nexus_path} | wc -l",
            timeout=300,  # Increased timeout to 5 minutes
        )
        grep_duration = time.time() - start
        match_count = int(result.get("stdout", "0").strip())

        print(f"  ✓ Searched {len(files)} files in {grep_duration:.3f}s")
        print(f"    Matches: {match_count}")
        print(
            f"    Breakdown: ls={ls_duration:.1f}s, single_read={read_duration:.1f}s, 10_file_grep={sample_grep_duration:.1f}s, full_grep={grep_duration:.1f}s"
        )

        results = {
            "method": "sandbox_bash",
            "file_count_target": file_count,
            "files_searched": len(files),
            "match_count": match_count,
            "total_bytes": total_bytes,
            "grep_duration": grep_duration,
            "setup_duration": setup_duration,
        }

        return results

    finally:
        # Cleanup
        print("\n[Cleanup] Stopping sandbox...")
        try:
            client.sandbox_stop(sandbox_id)
            print(f"  ✓ Stopped sandbox {sandbox_id}")
        except Exception as e:
            print(f"  ⚠️ Error stopping sandbox: {e}")


def test_sandbox_python(file_count=1000):
    """Test grep via FUSE-mounted Nexus in Docker sandbox using Python."""
    print(f"\n{'=' * 70}")
    print(f"SANDBOX PYTHON - GREP {file_count} FILES (FUSE-mounted)")
    print(f"{'=' * 70}")

    client = RemoteNexusFS(server_url=NEXUS_URL, api_key=NEXUS_API_KEY)
    nexus_path = (
        "/perf_test/grep_medium_1000" if file_count == 1000 else "/perf_test/grep_medium_10000"
    )
    mount_path = "/mnt/nexus"

    # Step 1: Create sandbox
    print("\n[1/2] Creating Docker sandbox...")
    sandbox_name = f"perf_grep_{uuid.uuid4().hex[:8]}"
    start = time.time()
    result = client.sandbox_get_or_create(
        name=sandbox_name, ttl_minutes=60, provider="docker", verify_status=True
    )
    sandbox_id = result["sandbox_id"]
    create_duration = time.time() - start
    print(f"  ✓ Created sandbox: {sandbox_id} in {create_duration:.2f}s")

    # Step 2: Connect and mount Nexus
    print("\n[2/2] Mounting Nexus filesystem via FUSE...")
    print("  → Installing nexus-ai-fs[fuse] in sandbox...")
    print("  → Starting FUSE mount (this may take 5-10 seconds)...")
    start = time.time()
    mount_result = client.sandbox_connect(
        sandbox_id=sandbox_id, provider="docker", mount_path=mount_path
    )
    mount_duration = time.time() - start
    print(f"  ✓ Mounted in {mount_duration:.2f}s at {mount_result.get('mount_path', mount_path)}")

    setup_duration = create_duration + mount_duration

    try:
        # Get source directory to determine total bytes
        if file_count == 1000:
            source_dir = DATA_DIR / "grep_medium_1k"
        else:
            source_dir = DATA_DIR / "grep_medium_10k"

        files = list(source_dir.glob("*.txt"))[:file_count]
        total_bytes = sum(f.stat().st_size for f in files)

        # Step 1: Verify files are accessible via Python
        print("\n[Step 1/4] Verifying files are accessible via FUSE (Python)...")
        print(f"  → Executing: list(Path('{mount_path}{nexus_path}').glob('*.txt'))")
        start = time.time()
        result = client.sandbox_run(
            sandbox_id=sandbox_id,
            language="python",
            code=f"""
from pathlib import Path
files = list(Path("{mount_path}{nexus_path}").glob("*.txt"))
print(len(files))
""",
            timeout=180,
        )
        ls_duration = time.time() - start
        file_count_visible = int(result.get("stdout", "0").strip())
        print(f"  ✓ Listed files in {ls_duration:.3f}s - {file_count_visible} files visible")

        # Step 2: Test reading a single file
        print("\n[Step 2/4] Testing single file read via FUSE (Python)...")
        start = time.time()
        result = client.sandbox_run(
            sandbox_id=sandbox_id,
            language="python",
            code=f"""
from pathlib import Path
files = list(Path("{mount_path}{nexus_path}").glob("*.txt"))
if files:
    content = files[0].read_text()
    print(len(content))
else:
    print(0)
""",
            timeout=180,
        )
        read_duration = time.time() - start
        bytes_read = result.get("stdout", "0").strip()
        print(f"  ✓ Read single file in {read_duration:.3f}s - {bytes_read} bytes")

        # Step 3: Test grep on first 10 files
        print("\n[Step 3/4] Testing grep on first 10 files (Python)...")
        start = time.time()
        result = client.sandbox_run(
            sandbox_id=sandbox_id,
            language="python",
            code=f"""
import re
from pathlib import Path
files = list(Path("{mount_path}{nexus_path}").glob("*.txt"))[:10]
match_count = 0
for f in files:
    content = f.read_text()
    match_count += len(re.findall("{SEARCH_PATTERN}", content))
print(match_count)
""",
            timeout=180,
        )
        sample_grep_duration = time.time() - start
        sample_matches = result.get("stdout", "0").strip()
        print(f"  ✓ Grepped 10 files in {sample_grep_duration:.3f}s - {sample_matches} matches")
        print(
            f"  📊 Estimated time for {file_count} files: {sample_grep_duration * file_count / 10:.1f}s"
        )

        # Step 4: Full grep operation
        print("\n[Step 4/4] Full grep operation (Python grep via FUSE)...")
        print(f"  → Executing Python script (grep pattern '{SEARCH_PATTERN}')...")
        print(f"  ⚠️  This may take a while for {file_count} files...")
        python_code = f"""
import re
from pathlib import Path

mount_path = "{mount_path}{nexus_path}"
pattern = "{SEARCH_PATTERN}"

match_count = 0
files_searched = 0

for filepath in Path(mount_path).rglob("*.txt"):
    try:
        content = filepath.read_text()
        matches = len(re.findall(pattern, content))
        match_count += matches
        files_searched += 1
    except Exception:
        pass

print(f"GREP|{{files_searched}}|{{match_count}}")
"""
        start = time.time()
        result = client.sandbox_run(
            sandbox_id=sandbox_id,
            language="python",
            code=python_code,
            timeout=300,  # Increased timeout to 5 minutes
        )
        grep_duration = time.time() - start

        # Parse output
        stdout = result.get("stdout", "").strip()
        files_searched = len(files)
        match_count = 0

        for line in stdout.split("\n"):
            if line.startswith("GREP|"):
                parts = line.split("|")
                files_searched = int(parts[1])
                match_count = int(parts[2])

        print(f"  ✓ Searched {files_searched} files in {grep_duration:.3f}s")
        print(f"    Matches: {match_count}")
        print(
            f"    Breakdown: ls={ls_duration:.1f}s, single_read={read_duration:.1f}s, 10_file_grep={sample_grep_duration:.1f}s, full_grep={grep_duration:.1f}s"
        )

        results = {
            "method": "sandbox_python",
            "file_count_target": file_count,
            "files_searched": files_searched,
            "match_count": match_count,
            "total_bytes": total_bytes,
            "grep_duration": grep_duration,
            "setup_duration": setup_duration,
        }

        return results

    finally:
        # Cleanup
        print("\n[Cleanup] Stopping sandbox...")
        try:
            client.sandbox_stop(sandbox_id)
            print(f"  ✓ Stopped sandbox {sandbox_id}")
        except Exception as e:
            print(f"  ⚠️ Error stopping sandbox: {e}")


def write_csv(results, filename):
    """Write results to CSV file."""
    fieldnames = [
        "scale",
        "method",
        "file_count_target",
        "files_searched",
        "match_count",
        "total_bytes",
        "grep_duration",
        "grep_rate",
        "upload_count",
        "upload_duration",
        "upload_rate",
    ]

    with open(filename, "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for result in results:
            row = {
                "scale": result["file_count_target"],
                "method": result["method"],
                "file_count_target": result["file_count_target"],
                "files_searched": result["files_searched"],
                "match_count": result["match_count"],
                "total_bytes": result["total_bytes"],
                "grep_duration": f"{result['grep_duration']:.4f}",
                "grep_rate": f"{result['files_searched'] / result['grep_duration']:.0f}"
                if result["grep_duration"] > 0
                else "0",
                "upload_count": result.get("upload_count", ""),
                "upload_duration": f"{result['upload_duration']:.4f}"
                if "upload_duration" in result
                else "",
                "upload_rate": f"{result['upload_count'] / result['upload_duration']:.0f}"
                if result.get("upload_duration", 0) > 0
                else "",
            }
            writer.writerow(row)

    print(f"\n✅ Results written to {filename}")


def main():
    print(f"\n{'=' * 120}")
    print(f"{'COMPREHENSIVE GREP TEST':^120}")
    print(f"{'=' * 120}")
    print("\nTesting 5 methods: Native Bash, Native Python, Nexus, Sandbox Bash, Sandbox Python")
    print("Scales: 1K and 10K files (medium content)")

    all_results = []

    # Test 1K files
    print(f"\n{'#' * 120}")
    print(f"{'1K FILES TEST':^120}")
    print(f"{'#' * 120}")

    all_results.append(test_native_bash(1000))
    all_results.append(test_native_python(1000))
    all_results.append(test_nexus(1000))
    all_results.append(test_sandbox_bash(1000))
    all_results.append(test_sandbox_python(1000))

    # Test 10K files
    print(f"\n{'#' * 120}")
    print(f"{'10K FILES TEST':^120}")
    print(f"{'#' * 120}")

    all_results.append(test_native_bash(10000))
    all_results.append(test_native_python(10000))
    all_results.append(test_nexus(10000))
    # Sandbox tests only run with 1K files due to FUSE glob performance
    # all_results.append(test_sandbox_bash(10000))
    # all_results.append(test_sandbox_python(10000))

    # Write CSV
    csv_filename = "grep_comparison_results.csv"
    write_csv(all_results, csv_filename)

    print(f"\n{'=' * 120}")
    print(f"{'TEST COMPLETE!':^120}")
    print(f"{'=' * 120}")


if __name__ == "__main__":
    main()
