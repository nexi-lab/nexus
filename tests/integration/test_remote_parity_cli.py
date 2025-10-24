#!/usr/bin/env python3
"""Simple CLI test for issue #243 - Remote vs Local Nexus Parity

This is a simplified version that uses Python API directly instead of FUSE mounts.
This makes it more reliable and cross-platform than the bash script.

Usage:
    python tests/integration/test_remote_parity_cli.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import nexus
from nexus.remote import RemoteNexusFS
from nexus.server import NexusRPCServer


class Colors:
    """ANSI color codes."""

    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    CYAN = "\033[0;36m"
    NC = "\033[0m"  # No Color


class ParityTester:
    """Test harness for remote vs local parity."""

    def __init__(self):
        self.total_tests = 0
        self.passed_tests = 0
        self.failed_tests = 0
        self.test_dir = None
        self.local_nx = None
        self.remote_nx = None
        self.server = None

    def print_result(self, test_name, passed, details=None):
        """Print test result."""
        self.total_tests += 1

        if passed:
            self.passed_tests += 1
            print(f"{Colors.GREEN}✓{Colors.NC} {test_name}")
        else:
            self.failed_tests += 1
            print(f"{Colors.RED}✗{Colors.NC} {test_name}")
            if details:
                print(f"  {Colors.RED}{details}{Colors.NC}")

    def setup(self):
        """Set up test environment."""
        print(f"{Colors.BLUE}{'=' * 50}{Colors.NC}")
        print(f"{Colors.BLUE}Nexus Remote vs Local Parity Test{Colors.NC}")
        print(f"{Colors.BLUE}Issue #243 Verification{Colors.NC}")
        print(f"{Colors.BLUE}{'=' * 50}{Colors.NC}\n")

        print(f"{Colors.CYAN}Setting up test environment...{Colors.NC}")

        # Create temp directories
        self.test_dir = Path(tempfile.mkdtemp(prefix="nexus-parity-cli-"))
        local_data_dir = self.test_dir / "local-data"
        remote_data_dir = self.test_dir / "remote-data"

        local_data_dir.mkdir()
        remote_data_dir.mkdir()

        # Create local filesystem
        print("  Creating local Nexus instance...")
        self.local_nx = nexus.connect(config={"data_dir": str(local_data_dir)})

        # Create remote filesystem with server
        print("  Creating remote Nexus instance...")
        remote_nx_backend = nexus.connect(config={"data_dir": str(remote_data_dir)})

        # Start server
        print("  Starting RPC server...")
        self.server = NexusRPCServer(remote_nx_backend, host="127.0.0.1", port=0)
        port = self.server.server.server_address[1]

        server_thread = threading.Thread(target=self.server.server.serve_forever, daemon=True)
        server_thread.start()

        time.sleep(0.5)

        # Create remote client
        print(f"  Connecting remote client to http://127.0.0.1:{port}")
        self.remote_nx = RemoteNexusFS(f"http://127.0.0.1:{port}", timeout=10)

        print(f"{Colors.GREEN}✓ Setup complete{Colors.NC}\n")

    def cleanup(self):
        """Clean up test environment."""
        print(f"\n{Colors.CYAN}Cleaning up...{Colors.NC}")

        if self.remote_nx:
            self.remote_nx.close()
        if self.server:
            self.server.shutdown()
        if self.local_nx:
            self.local_nx.close()
        if self.test_dir:
            shutil.rmtree(self.test_dir)

        print(f"{Colors.GREEN}✓ Cleanup complete{Colors.NC}\n")

    def test_basic_write_read(self):
        """Test basic write and read."""
        path = "/workspace/basic_test.txt"
        content = b"Hello, World!"

        try:
            # Write
            self.local_nx.write(path, content)
            self.remote_nx.write(path, content)

            # Read
            local_content = self.local_nx.read(path)
            remote_content = self.remote_nx.read(path)

            if local_content == remote_content == content:
                self.print_result("Basic write/read", True)
            else:
                self.print_result("Basic write/read", False, "Content mismatch")
        except Exception as e:
            self.print_result("Basic write/read", False, str(e))

    def test_exists(self):
        """Test exists operation."""
        path = "/workspace/exists_test.txt"

        try:
            # Initially should not exist
            local_exists_before = self.local_nx.exists(path)
            remote_exists_before = self.remote_nx.exists(path)

            # Create file
            self.local_nx.write(path, b"test")
            self.remote_nx.write(path, b"test")

            # Should exist now
            local_exists_after = self.local_nx.exists(path)
            remote_exists_after = self.remote_nx.exists(path)

            if (
                not local_exists_before
                and not remote_exists_before
                and local_exists_after
                and remote_exists_after
            ):
                self.print_result("Exists operation", True)
            else:
                self.print_result("Exists operation", False, "Existence check mismatch")
        except Exception as e:
            self.print_result("Exists operation", False, str(e))

    def test_delete(self):
        """Test delete operation."""
        path = "/workspace/delete_test.txt"

        try:
            # Create files
            self.local_nx.write(path, b"test")
            self.remote_nx.write(path, b"test")

            # Verify exist
            assert self.local_nx.exists(path) and self.remote_nx.exists(path)

            # Delete
            self.local_nx.delete(path)
            self.remote_nx.delete(path)

            # Verify deleted
            local_exists = self.local_nx.exists(path)
            remote_exists = self.remote_nx.exists(path)

            if not local_exists and not remote_exists:
                self.print_result("Delete operation", True)
            else:
                self.print_result("Delete operation", False, "Files still exist")
        except Exception as e:
            self.print_result("Delete operation", False, str(e))

    def test_list(self):
        """Test list operation."""
        try:
            # Create test files
            paths = [
                "/workspace/list_test/file1.txt",
                "/workspace/list_test/file2.txt",
                "/workspace/list_test/sub/file3.txt",
            ]

            for path in paths:
                self.local_nx.write(path, b"test")
                self.remote_nx.write(path, b"test")

            # List files
            local_files = sorted(self.local_nx.list("/workspace/list_test", recursive=True))
            remote_files = sorted(self.remote_nx.list("/workspace/list_test", recursive=True))

            if local_files == remote_files and len(local_files) == 3:
                self.print_result("List operation", True)
            else:
                self.print_result(
                    "List operation",
                    False,
                    f"Lists differ: {len(local_files)} vs {len(remote_files)}",
                )
        except Exception as e:
            self.print_result("List operation", False, str(e))

    def test_glob(self):
        """Test glob operation."""
        try:
            # Create test files
            self.local_nx.write("/workspace/glob_test/test1.txt", b"a")
            self.local_nx.write("/workspace/glob_test/test2.py", b"b")
            self.local_nx.write("/workspace/glob_test/test3.txt", b"c")

            self.remote_nx.write("/workspace/glob_test/test1.txt", b"a")
            self.remote_nx.write("/workspace/glob_test/test2.py", b"b")
            self.remote_nx.write("/workspace/glob_test/test3.txt", b"c")

            # Glob for .txt files
            local_matches = sorted(self.local_nx.glob("*.txt", "/workspace/glob_test"))
            remote_matches = sorted(self.remote_nx.glob("*.txt", "/workspace/glob_test"))

            if local_matches == remote_matches and len(local_matches) == 2:
                self.print_result("Glob operation", True)
            else:
                self.print_result("Glob operation", False, "Glob results differ")
        except Exception as e:
            self.print_result("Glob operation", False, str(e))

    def test_large_file(self):
        """Test large file handling."""
        path = "/workspace/large_file.bin"

        try:
            # Create 1MB of random data
            large_content = os.urandom(1024 * 1024)

            # Write
            self.local_nx.write(path, large_content)
            self.remote_nx.write(path, large_content)

            # Read
            local_read = self.local_nx.read(path)
            remote_read = self.remote_nx.read(path)

            if local_read == remote_read == large_content:
                self.print_result("Large file handling (1MB)", True)
            else:
                self.print_result("Large file handling (1MB)", False, "Content mismatch")
        except Exception as e:
            self.print_result("Large file handling (1MB)", False, str(e))

    def test_unicode(self):
        """Test Unicode content."""
        path = "/workspace/unicode.txt"

        try:
            unicode_content = "Hello 世界 🌍 Привет مرحبا".encode()

            self.local_nx.write(path, unicode_content)
            self.remote_nx.write(path, unicode_content)

            local_read = self.local_nx.read(path)
            remote_read = self.remote_nx.read(path)

            if local_read == remote_read == unicode_content:
                self.print_result("Unicode content", True)
            else:
                self.print_result("Unicode content", False, "Content mismatch")
        except Exception as e:
            self.print_result("Unicode content", False, str(e))

    def test_binary_data(self):
        """Test binary data handling."""
        path = "/workspace/binary.dat"

        try:
            # All byte values
            binary_content = bytes(range(256))

            self.local_nx.write(path, binary_content)
            self.remote_nx.write(path, binary_content)

            local_read = self.local_nx.read(path)
            remote_read = self.remote_nx.read(path)

            if local_read == remote_read == binary_content:
                self.print_result("Binary data handling", True)
            else:
                self.print_result("Binary data handling", False, "Content mismatch")
        except Exception as e:
            self.print_result("Binary data handling", False, str(e))

    def test_directory_operations(self):
        """Test directory operations."""
        dir_path = "/workspace/testdir/subdir"

        try:
            # Create
            self.local_nx.mkdir(dir_path, parents=True, exist_ok=True)
            self.remote_nx.mkdir(dir_path, parents=True, exist_ok=True)

            # Verify
            local_is_dir = self.local_nx.is_directory(dir_path)
            remote_is_dir = self.remote_nx.is_directory(dir_path)

            if local_is_dir and remote_is_dir:
                self.print_result("Directory operations", True)
            else:
                self.print_result("Directory operations", False, "Directory check failed")
        except Exception as e:
            self.print_result("Directory operations", False, str(e))

    def test_metadata(self):
        """Test metadata handling."""
        path = "/workspace/metadata_test.txt"

        try:
            content = b"test content"

            # Write and get metadata
            local_result = self.local_nx.write(path, content)
            remote_result = self.remote_nx.write(path, content)

            # Read with metadata
            local_meta = self.local_nx.read(path, return_metadata=True)
            remote_meta = self.remote_nx.read(path, return_metadata=True)

            if (
                "etag" in local_result
                and "etag" in remote_result
                and "etag" in local_meta
                and "etag" in remote_meta
                and local_meta["content"] == remote_meta["content"] == content
            ):
                self.print_result("Metadata handling", True)
            else:
                self.print_result("Metadata handling", False, "Metadata missing or incorrect")
        except Exception as e:
            self.print_result("Metadata handling", False, str(e))

    def run_all_tests(self):
        """Run all tests."""
        print(f"{Colors.BLUE}{'=' * 50}{Colors.NC}")
        print(f"{Colors.BLUE}Running Tests{Colors.NC}")
        print(f"{Colors.BLUE}{'=' * 50}{Colors.NC}\n")

        self.test_basic_write_read()
        self.test_exists()
        self.test_delete()
        self.test_list()
        self.test_glob()
        self.test_large_file()
        self.test_unicode()
        self.test_binary_data()
        self.test_directory_operations()
        self.test_metadata()

    def print_summary(self):
        """Print test summary."""
        print(f"\n{Colors.BLUE}{'=' * 50}{Colors.NC}")
        print(f"{Colors.BLUE}Test Summary{Colors.NC}")
        print(f"{Colors.BLUE}{'=' * 50}{Colors.NC}")
        print(f"Total tests:  {self.total_tests}")
        print(f"{Colors.GREEN}Passed:       {self.passed_tests}{Colors.NC}")
        if self.failed_tests > 0:
            print(f"{Colors.RED}Failed:       {self.failed_tests}{Colors.NC}")
        else:
            print(f"Failed:       {self.failed_tests}")

        if self.failed_tests == 0:
            print(f"\n{Colors.GREEN}✓ All tests passed!{Colors.NC}")
            print(f"{Colors.GREEN}Remote Nexus behavior matches embedded Nexus.{Colors.NC}")
            return 0
        else:
            print(f"\n{Colors.RED}✗ Some tests failed.{Colors.NC}")
            print(f"{Colors.RED}Remote Nexus behavior differs from embedded Nexus.{Colors.NC}")
            return 1


def main():
    """Main entry point."""
    tester = ParityTester()

    try:
        tester.setup()
        tester.run_all_tests()
        tester.cleanup()
        return tester.print_summary()
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Test interrupted by user{Colors.NC}")
        tester.cleanup()
        return 1
    except Exception as e:
        print(f"\n{Colors.RED}Error: {e}{Colors.NC}")
        import traceback

        traceback.print_exc()
        tester.cleanup()
        return 1


if __name__ == "__main__":
    sys.exit(main())
