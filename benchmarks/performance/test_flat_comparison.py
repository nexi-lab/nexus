#!/usr/bin/env python3
"""Comprehensive flat test: Native Bash vs Native Python vs Nexus vs Sandbox with CSV output."""

import csv
import os
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


def test_native_bash(file_count=1000):
    """Test native filesystem using bash commands."""
    print(f"\n{'=' * 70}")
    print(f"NATIVE BASH - {file_count} FLAT FILES")
    print(f"{'=' * 70}")

    source_dir = DATA_DIR / "flat_50k"
    results = {}

    # Test 1: List (using find)
    print("\n[1/3] List operation (bash find)...")
    start = time.time()
    result = subprocess.run(
        f"find {source_dir} -maxdepth 1 -type f -name '*.txt' | head -{file_count}",
        shell=True,
        capture_output=True,
        text=True,
    )
    file_paths = result.stdout.strip().split("\n") if result.stdout.strip() else []
    list_duration = time.time() - start
    list_count = len(file_paths)

    print(f"  ✓ Listed {list_count} files in {list_duration:.3f}s")

    # Test 2: Read (using cat)
    print("\n[2/3] Read operation (bash cat)...")
    file_list = " ".join([f'"{f}"' for f in file_paths])
    start = time.time()
    result = subprocess.run(f"cat {file_list} | wc -c", shell=True, capture_output=True, text=True)
    total_bytes = int(result.stdout.strip()) if result.stdout.strip() else 0
    read_duration = time.time() - start
    read_count = len(file_paths)

    print(f"  ✓ Read {read_count} files ({total_bytes} bytes) in {read_duration:.3f}s")

    # Test 3: Stat (using stat on ALL files for flat)
    print("\n[3/3] Stat operation (bash stat)...")
    start = time.time()
    if sys.platform == "darwin":
        result = subprocess.run(
            f"stat -f '%z' {file_list}", shell=True, capture_output=True, text=True
        )
    else:
        result = subprocess.run(
            f"stat -c '%s' {file_list}", shell=True, capture_output=True, text=True
        )
    sizes = [int(s) for s in result.stdout.strip().split("\n") if s]
    stat_duration = time.time() - start
    stat_count = len(sizes)

    print(f"  ✓ Stat'd {stat_count} files in {stat_duration:.3f}s")

    results = {
        "method": "native_bash",
        "file_count_target": file_count,
        "list_count": list_count,
        "list_duration": list_duration,
        "read_count": read_count,
        "read_bytes": total_bytes,
        "read_duration": read_duration,
        "stat_count": stat_count,
        "stat_duration": stat_duration,
    }

    return results


def test_native_python(file_count=1000):
    """Test native filesystem using Python pathlib."""
    print(f"\n{'=' * 70}")
    print(f"NATIVE PYTHON - {file_count} FLAT FILES")
    print(f"{'=' * 70}")

    source_dir = DATA_DIR / "flat_50k"
    results = {}

    # Test 1: List (using pathlib glob)
    print("\n[1/3] List operation (Python pathlib)...")
    start = time.time()
    file_paths = list(source_dir.glob("*.txt"))[:file_count]
    list_duration = time.time() - start
    list_count = len(file_paths)

    print(f"  ✓ Listed {list_count} files in {list_duration:.3f}s")

    # Test 2: Read (using pathlib read_bytes)
    print("\n[2/3] Read operation (Python pathlib)...")
    total_bytes = 0
    start = time.time()
    for file_path in file_paths:
        content = file_path.read_bytes()
        total_bytes += len(content)
    read_duration = time.time() - start
    read_count = len(file_paths)

    print(f"  ✓ Read {read_count} files ({total_bytes} bytes) in {read_duration:.3f}s")

    # Test 3: Stat (using pathlib stat on ALL files for flat)
    print("\n[3/3] Stat operation (Python pathlib)...")
    start = time.time()
    sizes = [f.stat().st_size for f in file_paths]
    stat_duration = time.time() - start
    stat_count = len(sizes)

    print(f"  ✓ Stat'd {stat_count} files in {stat_duration:.3f}s")

    results = {
        "method": "native_python",
        "file_count_target": file_count,
        "list_count": list_count,
        "list_duration": list_duration,
        "read_count": read_count,
        "read_bytes": total_bytes,
        "read_duration": read_duration,
        "stat_count": stat_count,
        "stat_duration": stat_duration,
    }

    return results


def test_nexus(file_count=1000):
    """Test Nexus filesystem."""
    print(f"\n{'=' * 70}")
    print(f"NEXUS - {file_count} FLAT FILES")
    print(f"{'=' * 70}")

    client = RemoteNexusFS(server_url=NEXUS_URL, api_key=NEXUS_API_KEY)
    source_dir = DATA_DIR / "flat_50k"
    nexus_path = f"/perf_test/flat_{file_count}"

    results = {}

    # Get source files
    files = list(source_dir.glob("*.txt"))[:file_count]

    # Upload files
    print(f"\n📤 Uploading {len(files)} files...")
    upload_count = 0
    upload_bytes = 0
    start = time.time()

    for i, file in enumerate(files):
        content = file.read_bytes()

        # Retry upload up to 3 times with exponential backoff
        max_retries = 3
        retry_delay = 0.1
        uploaded = False

        for attempt in range(max_retries):
            try:
                client.write(f"{nexus_path}/{file.name}", content)
                upload_count += 1
                upload_bytes += len(content)
                uploaded = True
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    print(f"  ⚠️ Failed to upload {file.name} after {max_retries} attempts: {e}")

        if not uploaded:
            continue

        if (i + 1) % 1000 == 0:
            print(f"  Uploaded {i + 1}/{len(files)} files...")

    upload_duration = time.time() - start
    print(f"✓ Uploaded {upload_count} files in {upload_duration:.2f}s")

    # Test 1: List
    print("\n[1/3] List operation (Nexus)...")
    start = time.time()
    listed = client.list(nexus_path, recursive=False)
    list_duration = time.time() - start
    list_count = len(listed)

    print(f"  ✓ Listed {list_count} files in {list_duration:.3f}s")

    # Test 2: Read (using read_bulk for efficient batch reading)
    print("\n[2/3] Read operation (Nexus read_bulk)...")
    start = time.time()
    bulk_results = client.read_bulk(listed)
    read_duration = time.time() - start
    total_bytes = sum(len(v) for v in bulk_results.values() if v)
    read_count = len([v for v in bulk_results.values() if v])

    print(f"  ✓ Read {read_count} files ({total_bytes} bytes) in {read_duration:.3f}s")

    # Test 3: Stat (using stat_bulk for efficient batch metadata)
    print("\n[3/3] Stat operation (Nexus stat_bulk)...")
    start = time.time()
    stat_results = client.stat_bulk(listed)
    stat_duration = time.time() - start
    stat_count = len([v for v in stat_results.values() if v])

    print(f"  ✓ Stat'd {stat_count} files in {stat_duration:.3f}s")

    results = {
        "method": "nexus",
        "file_count_target": file_count,
        "list_count": list_count,
        "list_duration": list_duration,
        "read_count": read_count,
        "read_bytes": total_bytes,
        "read_duration": read_duration,
        "stat_count": stat_count,
        "stat_duration": stat_duration,
        "upload_count": upload_count,
        "upload_duration": upload_duration,
    }

    return results


def test_sandbox_bash(file_count=1000):
    """Test Nexus mounted via FUSE in Docker sandbox using bash commands."""
    print(f"\n{'=' * 70}")
    print(f"SANDBOX BASH - {file_count} FLAT FILES (FUSE-mounted)")
    print(f"{'=' * 70}")

    client = RemoteNexusFS(server_url=NEXUS_URL, api_key=NEXUS_API_KEY)
    nexus_path = "/perf_test/flat_1000" if file_count == 1000 else "/perf_test/flat_10000"
    mount_path = "/mnt/nexus"

    # Step 1: Create sandbox
    print("\n[1/2] Creating Docker sandbox...")
    sandbox_name = f"perf_flat_{uuid.uuid4().hex[:8]}"
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
        # Test 1: List
        print("\n[1/3] List operation (bash ls via FUSE)...")
        print(f"  → Executing: ls {mount_path}{nexus_path} | wc -l")
        start = time.time()
        result = client.sandbox_run(
            sandbox_id=sandbox_id,
            language="bash",
            code=f"ls {mount_path}{nexus_path} | wc -l",
            timeout=30,
        )
        list_duration = time.time() - start
        list_count = int(result.get("stdout", "0").strip())
        print(f"  ✓ Listed {list_count} files in {list_duration:.3f}s")

        # Test 2: Read first N files
        print("\n[2/3] Read operation (bash cat via FUSE)...")
        read_limit = min(file_count, 100)  # Read first 100 files
        print(f"  → Reading {read_limit} files...")
        bash_read = f"""
files=({mount_path}{nexus_path}/*.txt)
total=0
for i in $(seq 0 {read_limit - 1}); do
    size=$(cat "${{files[$i]}}" | wc -c)
    total=$((total + size))
done
echo $total
"""
        start = time.time()
        result = client.sandbox_run(
            sandbox_id=sandbox_id, language="bash", code=bash_read, timeout=60
        )
        read_duration = time.time() - start
        total_bytes = int(result.get("stdout", "0").strip())
        print(f"  ✓ Read {read_limit} files ({total_bytes} bytes) in {read_duration:.3f}s")

        # Test 3: Stat first N files
        print("\n[3/3] Stat operation (bash stat via FUSE)...")
        stat_limit = min(file_count, 100)  # Stat first 100 files
        print(f"  → Stat'ing {stat_limit} files...")
        bash_stat = f"""
files=({mount_path}{nexus_path}/*.txt)
count=0
for i in $(seq 0 {stat_limit - 1}); do
    [ -f "${{files[$i]}}" ] && ((count++))
done
echo $count
"""
        start = time.time()
        result = client.sandbox_run(
            sandbox_id=sandbox_id, language="bash", code=bash_stat, timeout=60
        )
        stat_duration = time.time() - start
        stat_count = int(result.get("stdout", "0").strip())
        print(f"  ✓ Stat'd {stat_count} files in {stat_duration:.3f}s")

        results = {
            "method": "sandbox_bash",
            "file_count_target": file_count,
            "list_count": list_count,
            "list_duration": list_duration,
            "read_count": read_limit,
            "read_bytes": total_bytes,
            "read_duration": read_duration,
            "stat_count": stat_count,
            "stat_duration": stat_duration,
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
    """Test Nexus mounted via FUSE in Docker sandbox using Python."""
    print(f"\n{'=' * 70}")
    print(f"SANDBOX PYTHON - {file_count} FLAT FILES (FUSE-mounted)")
    print(f"{'=' * 70}")

    client = RemoteNexusFS(server_url=NEXUS_URL, api_key=NEXUS_API_KEY)
    nexus_path = "/perf_test/flat_1000" if file_count == 1000 else "/perf_test/flat_10000"
    mount_path = "/mnt/nexus"

    # Step 1: Create sandbox
    print("\n[1/2] Creating Docker sandbox...")
    sandbox_name = f"perf_flat_{uuid.uuid4().hex[:8]}"
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
        # Run Python operations
        read_limit = min(file_count, 100)  # Read first 100 files
        stat_limit = min(file_count, 100)  # Stat first 100 files

        python_code = f"""
import time
from pathlib import Path

mount_path = "{mount_path}{nexus_path}"

# List files
start = time.time()
files = list(Path(mount_path).glob("*.txt"))
list_duration = time.time() - start
print(f"LIST|{{len(files)}}|{{list_duration:.4f}}")

# Read first {read_limit} files
total_bytes = 0
start = time.time()
for filepath in files[:{read_limit}]:
    content = filepath.read_text()
    total_bytes += len(content)
read_duration = time.time() - start
print(f"READ|{read_limit}|{{total_bytes}}|{{read_duration:.4f}}")

# Stat first {stat_limit} files
start = time.time()
for filepath in files[:{stat_limit}]:
    stat_info = filepath.stat()
stat_duration = time.time() - start
print(f"STAT|{stat_limit}|{{stat_duration:.4f}}")
"""
        print("\n[1/3] Running Python operations via FUSE...")
        print(
            f"  → Executing Python script (list + read {read_limit} + stat {stat_limit} files)..."
        )
        start = time.time()
        result = client.sandbox_run(
            sandbox_id=sandbox_id, language="python", code=python_code, timeout=120
        )
        total_duration = time.time() - start

        # Parse output
        stdout = result.get("stdout", "").strip()
        lines = stdout.split("\n")

        list_count = 0
        list_duration = 0.0
        read_count = 0
        total_bytes = 0
        read_duration = 0.0
        stat_count = 0
        stat_duration = 0.0

        for line in lines:
            if line.startswith("LIST|"):
                parts = line.split("|")
                list_count = int(parts[1])
                list_duration = float(parts[2])
                print(f"  ✓ Listed {list_count} files in {list_duration:.3f}s")
            elif line.startswith("READ|"):
                parts = line.split("|")
                read_count = int(parts[1])
                total_bytes = int(parts[2])
                read_duration = float(parts[3])
                print(f"  ✓ Read {read_count} files ({total_bytes} bytes) in {read_duration:.3f}s")
            elif line.startswith("STAT|"):
                parts = line.split("|")
                stat_count = int(parts[1])
                stat_duration = float(parts[2])
                print(f"  ✓ Stat'd {stat_count} files in {stat_duration:.3f}s")

        results = {
            "method": "sandbox_python",
            "file_count_target": file_count,
            "list_count": list_count,
            "list_duration": list_duration,
            "read_count": read_count,
            "read_bytes": total_bytes,
            "read_duration": read_duration,
            "stat_count": stat_count,
            "stat_duration": stat_duration,
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
        "list_count",
        "list_duration",
        "list_rate",
        "read_count",
        "read_bytes",
        "read_duration",
        "read_rate",
        "stat_count",
        "stat_duration",
        "stat_rate",
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
                "list_count": result["list_count"],
                "list_duration": f"{result['list_duration']:.4f}",
                "list_rate": f"{result['list_count'] / result['list_duration']:.0f}"
                if result["list_duration"] > 0
                else "0",
                "read_count": result["read_count"],
                "read_bytes": result["read_bytes"],
                "read_duration": f"{result['read_duration']:.4f}",
                "read_rate": f"{result['read_count'] / result['read_duration']:.0f}"
                if result["read_duration"] > 0
                else "0",
                "stat_count": result["stat_count"],
                "stat_duration": f"{result['stat_duration']:.4f}",
                "stat_rate": f"{result['stat_count'] / result['stat_duration']:.0f}"
                if result["stat_duration"] > 0
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
    print(f"{'COMPREHENSIVE FLAT DIRECTORY TEST':^120}")
    print(f"{'=' * 120}")
    print("\nTesting 5 methods: Native Bash, Native Python, Nexus, Sandbox Bash, Sandbox Python")
    print("Scales: 1K and 10K files")

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
    all_results.append(test_sandbox_bash(10000))
    all_results.append(test_sandbox_python(10000))

    # Write CSV
    csv_filename = "flat_comparison_results.csv"
    write_csv(all_results, csv_filename)

    print(f"\n{'=' * 120}")
    print(f"{'TEST COMPLETE!':^120}")
    print(f"{'=' * 120}")


if __name__ == "__main__":
    main()
