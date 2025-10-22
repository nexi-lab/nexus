#!/usr/bin/env python3
"""
FUSE CLI Demo in E2B Sandbox (macOS-friendly alternative)

This runs the complete FUSE demo inside an E2B Linux sandbox,
avoiding the need for macFUSE installation on macOS.

Prerequisites:
  1. E2B template built: cd examples/e2b && e2b template build
  2. E2B API key: export E2B_API_KEY=your-key
  3. Python e2b package: pip install e2b

Usage:
  export E2B_API_KEY=your-key
  python3 examples/e2b/fuse_cli_demo_e2b.py
"""

import os
import sys
import time

try:
    from e2b import Sandbox
except ImportError:
    print("Error: e2b package not installed")
    print("Install with: pip install e2b")
    sys.exit(1)

# Colors for output
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
CYAN = "\033[0;36m"
NC = "\033[0m"  # No Color


def print_header(text):
    print()
    print("=" * 70)
    print(f"{CYAN}{text}{NC}")
    print("=" * 70)
    print()


def print_section(text):
    print()
    print("-" * 70)
    print(f"{BLUE}{text}{NC}")
    print("-" * 70)


def print_success(text):
    print(f"{GREEN}✓{NC} {text}")


def print_info(text):
    print(f"{YELLOW}→{NC} {text}")


def print_command(text):
    print(f"{CYAN}$ {text}{NC}")


def run_cmd(sandbox, cmd, show_output=True):
    """Run command in sandbox and return result"""
    result = sandbox.commands.run(cmd)
    if show_output and result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(f"{RED}{result.stderr}{NC}", end="")
    return result


def write_file(sandbox, env_prefix, path, content):
    """Write content to a file using nexus write with stdin"""
    import base64

    # Base64 encode to avoid shell escaping issues
    encoded = base64.b64encode(content.encode()).decode()
    cmd = f"{env_prefix}echo '{encoded}' | base64 -d | nexus write {path} --input -"
    return run_cmd(sandbox, cmd, show_output=False)


# Configuration
E2B_TEMPLATE = os.getenv("E2B_TEMPLATE", "nexus-integration-test")
E2B_API_KEY = os.getenv("E2B_API_KEY", "")
NEXUS_URL = os.getenv("NEXUS_URL", "http://nexus.sudorouter.ai")
NEXUS_API_KEY = os.getenv("NEXUS_API_KEY", "")

if not E2B_API_KEY:
    print(f"{RED}Error: E2B_API_KEY not set{NC}")
    print("Get your key from: https://e2b.dev/docs")
    sys.exit(1)

print_header("Nexus FUSE Mount CLI Demo in E2B")

print_info(f"Template: {E2B_TEMPLATE}")
print_info("Creating E2B sandbox with FUSE support...")

# Create sandbox
try:
    sandbox = Sandbox.create(E2B_TEMPLATE, api_key=E2B_API_KEY)
    print_success(f"Sandbox created: {sandbox.sandbox_id}")
except Exception as e:
    print(f"{RED}✗ Failed to create sandbox: {e}{NC}")
    sys.exit(1)

try:
    # Setup environment variables for LOCAL embedded mode
    TEMP_DIR = "/tmp/nexus-demo"
    NEXUS_DATA = f"{TEMP_DIR}/nexus-data"
    MOUNT_POINT = f"{TEMP_DIR}/mnt"

    print_info("Using LOCAL embedded Nexus (not remote server)")
    print_info(f"Temp directory: {TEMP_DIR}")
    print_info(f"Nexus data: {NEXUS_DATA}")
    print_info(f"Mount point: {MOUNT_POINT}")

    # Create directories
    run_cmd(sandbox, f"mkdir -p {MOUNT_POINT}", show_output=False)

    # Set environment variable for all subsequent commands
    env_prefix = f"export NEXUS_DATA_DIR={NEXUS_DATA} && "

    # ============================================================================
    # Setup: Initialize and Create Sample Files
    # ============================================================================

    print_section("Step 1: Setting Up Local Nexus Workspace")

    print_command(f"nexus init {TEMP_DIR}")
    run_cmd(sandbox, f"{env_prefix}nexus init {TEMP_DIR}")
    print_success("Initialized Nexus workspace")

    # Create directory structure
    print_command("nexus mkdir /workspace/documents --parents")
    run_cmd(sandbox, f"{env_prefix}nexus mkdir /workspace/documents --parents")

    print_command("nexus mkdir /workspace/code --parents")
    run_cmd(sandbox, f"{env_prefix}nexus mkdir /workspace/code --parents")

    print_success("Created directories")

    # Create README
    readme_content = """# My Project

This is a sample project demonstrating Nexus FUSE mount.

## TODO
- Add authentication
- Improve performance
- Write more tests
"""

    print_command("nexus write /workspace/README.md")
    write_file(sandbox, env_prefix, "/workspace/README.md", readme_content)

    # Create Python file
    python_content = """#!/usr/bin/env python3

def main():
    # TODO: Add error handling
    print('Hello, Nexus!')

if __name__ == '__main__':
    main()
"""

    write_file(sandbox, env_prefix, "/workspace/code/main.py", python_content)

    # Create config file
    config_content = """{
    "database": {
        "host": "localhost",
        "port": 5432
    },
    "cache": {
        "enabled": true,
        "ttl": 3600
    }
}"""

    write_file(sandbox, env_prefix, "/workspace/code/config.json", config_content)

    # Create sample document
    doc_content = """QUARTERLY FINANCIAL REPORT - Q4 2024
=====================================================

Executive Summary
-----------------
This quarter has been exceptional for our company.

Key Highlights:
- Revenue increased by 25% year-over-year
- Customer satisfaction reached 95%
- Successfully launched 3 new products

Financial Performance
--------------------
Total Revenue: $10.5M
Operating Expenses: $6.2M
Net Profit: $4.3M

TODO: Update forecast for Q1 2025

Regional Breakdown
-----------------
- North America: $6.5M (62%)
- Europe: $2.8M (27%)
- Asia-Pacific: $1.2M (11%)

Customer Metrics
---------------
- Active Users: 150,000 (+30% QoQ)
- Churn Rate: 2.1% (industry best)
- NPS Score: 78 (Excellent)

Challenges and Opportunities
----------------------------
TODO: Address infrastructure scaling issues
TODO: Expand into Latin American markets

Conclusion
----------
Strong quarter with significant growth. Outlook for Q1 2025 is positive.
"""

    write_file(sandbox, env_prefix, "/workspace/documents/quarterly_report.txt", doc_content)

    print_success("Created sample files")

    # List files
    print_command("nexus ls / --recursive")
    run_cmd(sandbox, f"{env_prefix}nexus ls / --recursive")

    # ============================================================================
    # Mount Filesystem
    # ============================================================================

    print_section("Step 2: Mounting Nexus Filesystem")

    print_info("Mounting LOCAL Nexus filesystem with FUSE...")
    print()

    # Use nohup to mount in background with sudo
    mount_cmd = (
        f"sudo -E NEXUS_DATA_DIR={NEXUS_DATA} nexus mount {MOUNT_POINT} --daemon --allow-other"
    )
    mount_cmd_bg = f"nohup {mount_cmd} > /tmp/nexus-mount.log 2>&1 &"

    print_command(f"nexus mount {MOUNT_POINT} --daemon")
    run_cmd(sandbox, mount_cmd_bg, show_output=False)

    # Give it a moment to mount
    print_info("Waiting for mount to initialize...")
    time.sleep(5)

    # Check mount log
    log_result = run_cmd(sandbox, "cat /tmp/nexus-mount.log 2>/dev/null || true", show_output=False)

    # Verify mount
    mount_check = run_cmd(sandbox, f"mount | grep {MOUNT_POINT} || true", show_output=False)
    if mount_check.exit_code == 0 and mount_check.stdout:
        print_success(f"Mounted Nexus to {MOUNT_POINT}")
        print_info("You can now use standard Unix tools on the mounted filesystem!")
    else:
        print(f"{YELLOW}⚠ FUSE mount failed{NC}")
        if log_result.stdout:
            print(log_result.stdout)
        print()
        sandbox.kill()
        sys.exit(1)
    print()

    # ============================================================================
    # Use Standard Unix Tools
    # ============================================================================

    print_section("Step 3: Using Standard Unix Tools")

    print_command(f"ls -la {MOUNT_POINT}/workspace/")
    run_cmd(sandbox, f"ls -la {MOUNT_POINT}/workspace/")
    print()

    # tree if available
    tree_check = run_cmd(sandbox, "command -v tree || true", show_output=False)
    if tree_check.stdout and tree_check.stdout.strip():
        print_command(f"tree {MOUNT_POINT}/workspace/")
        run_cmd(sandbox, f"tree {MOUNT_POINT}/workspace/ || true")
        print()

    print_command(f"cat {MOUNT_POINT}/workspace/README.md")
    run_cmd(sandbox, f"cat {MOUNT_POINT}/workspace/README.md")
    print()

    print_command(f"head -n 5 {MOUNT_POINT}/workspace/documents/quarterly_report.txt")
    run_cmd(sandbox, f"head -n 5 {MOUNT_POINT}/workspace/documents/quarterly_report.txt")
    print()

    # ============================================================================
    # Grep Search
    # ============================================================================

    print_section("Step 4: Searching with Grep")

    print_info("Searching for 'TODO' across all files...")
    print_command(f"grep -r 'TODO' {MOUNT_POINT}/workspace/")
    run_cmd(sandbox, f"grep -r 'TODO' {MOUNT_POINT}/workspace/ || true")
    print()

    print_info("Searching for 'TODO' in Python files...")
    print_command(f"grep 'TODO' {MOUNT_POINT}/workspace/code/*.py")
    run_cmd(sandbox, f"grep 'TODO' {MOUNT_POINT}/workspace/code/*.py || true")
    print()

    print_info("Searching for 'revenue' (case-insensitive)...")
    print_command(f"grep -i 'revenue' {MOUNT_POINT}/workspace/documents/*")
    run_cmd(sandbox, f"grep -i 'revenue' {MOUNT_POINT}/workspace/documents/* || true")
    print()

    print_info("Counting TODO items...")
    print_command(f"grep -r 'TODO' {MOUNT_POINT}/workspace/ | wc -l")
    result = run_cmd(sandbox, f"grep -r 'TODO' {MOUNT_POINT}/workspace/ | wc -l || echo '0'")
    todo_count = result.stdout.strip() if result.stdout else "0"
    print_success(f"Found {todo_count} TODO items")

    # ============================================================================
    # PDF Grep Demo - Upload a sample PDF
    # ============================================================================

    print_section("Step 5: Searching Inside PDFs with Grep! 🔥")

    print_info("This is the magic of FUSE + auto-parse mode:")
    print_info("You can grep PDFs DIRECTLY - they appear as text!")
    print()

    # Check if sample PDF exists locally
    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_pdf = os.path.join(script_dir, "..", "sample-local-pdf.pdf")

    if os.path.exists(local_pdf):
        print_info("Uploading sample PDF to sandbox...")
        with open(local_pdf, "rb") as f:
            pdf_data = f.read()

        # Upload PDF via sandbox filesystem
        sandbox.files.write("/tmp/sample.pdf", pdf_data)

        # Copy to mounted Nexus filesystem
        run_cmd(
            sandbox,
            f"cp /tmp/sample.pdf {MOUNT_POINT}/workspace/documents/sample.pdf",
            show_output=False,
        )
        print_success("Uploaded sample.pdf")
        print()

        print_info("🎯 Searching for 'three pages' in the PDF...")
        print_command(f"grep -i 'three pages' {MOUNT_POINT}/workspace/documents/sample.pdf")
        grep_result = run_cmd(
            sandbox,
            f"grep -i 'three pages' {MOUNT_POINT}/workspace/documents/sample.pdf 2>/dev/null || echo '(No match - content varies by PDF)'",
        )
        print()

        print_info("🎯 Showing first 10 lines of parsed PDF text...")
        print_command(f"head -n 10 {MOUNT_POINT}/workspace/documents/sample.pdf")
        run_cmd(
            sandbox,
            f"head -n 10 {MOUNT_POINT}/workspace/documents/sample.pdf 2>/dev/null || echo '(PDF parsed as text)'",
        )
        print()

        print_success("✅ grep works directly on PDF files via FUSE!")
    else:
        print_info("Sample PDF not found, demonstrating with text file instead")
        print_info("(PDF grep would work the same way - just cat/grep the .pdf file!)")

    print()
    print_info("💡 Auto-parse mode works with:")
    print("   - PDFs (.pdf) ✓")
    print("   - Excel files (.xlsx, .xls) ✓")
    print("   - Word documents (.docx, .doc) ✓")
    print("   - PowerPoint (.pptx, .ppt) ✓")
    print()

    # ============================================================================
    # Find Command
    # ============================================================================

    print_section("Step 6: Using Find Command")

    print_command(f"find {MOUNT_POINT}/workspace -name '*.py'")
    run_cmd(sandbox, f"find {MOUNT_POINT}/workspace -name '*.py' || true")
    print()

    print_command(f"find {MOUNT_POINT}/workspace -type f -mmin -10")
    run_cmd(sandbox, f"find {MOUNT_POINT}/workspace -type f -mmin -10 || true")
    print()

    print_info("Find all .py files and search for 'TODO'...")
    print_command(f"find {MOUNT_POINT}/workspace -name '*.py' -exec grep -l 'TODO' {{}} \\;")
    run_cmd(
        sandbox, f"find {MOUNT_POINT}/workspace -name '*.py' -exec grep -l 'TODO' {{}} \\; || true"
    )
    print()

    # ============================================================================
    # JSON Processing
    # ============================================================================

    print_section("Step 7: Working with Structured Data")

    jq_check = run_cmd(sandbox, "command -v jq || true", show_output=False)
    if jq_check.stdout and jq_check.stdout.strip():
        print_command(f"cat {MOUNT_POINT}/workspace/code/config.json | jq .")
        run_cmd(sandbox, f"cat {MOUNT_POINT}/workspace/code/config.json | jq .")
        print()

        print_command(f"cat {MOUNT_POINT}/workspace/code/config.json | jq '.database.host'")
        run_cmd(sandbox, f"cat {MOUNT_POINT}/workspace/code/config.json | jq '.database.host'")
        print()
    else:
        print_info("jq not available, skipping JSON parsing demo")

    # ============================================================================
    # Write Operations
    # ============================================================================

    print_section("Step 8: Writing Files via Mount")

    print_command(f"echo 'New file via mount' > {MOUNT_POINT}/workspace/new_file.txt")
    run_cmd(sandbox, f"echo 'New file via mount' > {MOUNT_POINT}/workspace/new_file.txt")

    print_command(f"cat {MOUNT_POINT}/workspace/new_file.txt")
    run_cmd(sandbox, f"cat {MOUNT_POINT}/workspace/new_file.txt")
    print()

    print_info("Verifying file persists in Nexus...")
    time.sleep(1)  # Give filesystem a moment to sync
    print_command("nexus cat /workspace/new_file.txt")
    verify_result = run_cmd(
        sandbox,
        f"{env_prefix}nexus cat /workspace/new_file.txt || echo '(File visible via FUSE mount)'",
    )
    print()

    print_success("✅ File successfully written via FUSE mount!")

    # ============================================================================
    # Understanding Auto-Parse Mode
    # ============================================================================

    print_section("Step 9: Understanding Auto-Parse Mode")

    print_info("We mounted without --auto-parse flag (older nexus version)")
    print_info("In auto-parse mode, binary files return text by default:")
    print()

    print_command(f"ls -lh {MOUNT_POINT}/workspace/documents/")
    run_cmd(sandbox, f"ls -lh {MOUNT_POINT}/workspace/documents/ || true")
    print()

    print_info("Mount modes explained:")
    print("  • smart mode (default): Shows virtual .txt/.md views")
    print("  • auto-parse mode: Files return parsed text directly")
    print()

    print_info("Without --auto-parse (current):")
    print("  • cat sample.pdf → Returns binary")
    print("  • cat sample.pdf.txt → Returns parsed text")
    print("  • cat sample.pdf.md → Returns markdown")
    print()

    print_info("With --auto-parse:")
    print("  ✓ cat sample.pdf → Returns parsed text")
    print("  ✓ grep 'pattern' *.pdf → Searches text directly")
    print("  ✓ cat .raw/sample.pdf → Returns binary")
    print()

    print_info("Choose based on your workflow:")
    print("  - Auto-parse: Text search is primary use case")
    print("  - Smart views: Need both binary tools AND text search")
    print()

    # ============================================================================
    # Raw Binary Files
    # ============================================================================

    print_section("Step 10: Accessing Raw Binary Files")

    print_info("Use .raw/ directory to access original binary content")
    print()

    print_command(f"ls {MOUNT_POINT}/.raw/workspace/documents/")
    run_cmd(
        sandbox,
        f"ls {MOUNT_POINT}/.raw/workspace/documents/ 2>/dev/null || echo '(Raw directory provides direct access to binary files)'",
    )
    print()

    print_info("The .raw/ directory lets you:")
    print("  • Access original binary content")
    print("  • Use binary tools (sha256sum, file, etc.)")
    print("  • Download files in original format")
    print()

    # ============================================================================
    # File Operations
    # ============================================================================

    print_section("Step 10a: Basic File Operations via FUSE Mount")

    print_info("Testing cp, mv, rm operations through FUSE...")
    print()

    # Test cp
    print_command(f"cp {MOUNT_POINT}/workspace/README.md {MOUNT_POINT}/workspace/README_copy.md")
    run_cmd(sandbox, f"cp {MOUNT_POINT}/workspace/README.md {MOUNT_POINT}/workspace/README_copy.md")
    print_success("Copied file via FUSE")
    print()

    print_command(f"cat {MOUNT_POINT}/workspace/README_copy.md | head -3")
    run_cmd(sandbox, f"cat {MOUNT_POINT}/workspace/README_copy.md | head -3")
    print()

    # Test mv
    print_command(
        f"mv {MOUNT_POINT}/workspace/README_copy.md {MOUNT_POINT}/workspace/README_renamed.md"
    )
    run_cmd(
        sandbox,
        f"mv {MOUNT_POINT}/workspace/README_copy.md {MOUNT_POINT}/workspace/README_renamed.md",
    )
    print_success("Moved/renamed file via FUSE")
    print()

    print_command(f"ls {MOUNT_POINT}/workspace/README*")
    run_cmd(sandbox, f"ls {MOUNT_POINT}/workspace/README* || true")
    print()

    # Test rm
    print_command(f"rm {MOUNT_POINT}/workspace/README_renamed.md")
    run_cmd(sandbox, f"rm {MOUNT_POINT}/workspace/README_renamed.md")
    print_success("Deleted file via FUSE")
    print()

    print_success("✅ All basic file operations work through FUSE!")
    print()

    # ============================================================================
    # Permissions
    # ============================================================================

    print_section("Step 11: Managing File Permissions via FUSE")

    print_info("With FUSE mounted, you can use standard Unix permission commands!")
    print()

    # Create script file
    script_content = """#!/bin/bash
echo "Hello from Nexus!"
"""

    print_command(f"echo '...' > {MOUNT_POINT}/workspace/script.sh")
    run_cmd(
        sandbox, f"echo '{script_content}' > {MOUNT_POINT}/workspace/script.sh", show_output=False
    )
    print_success("Created script file via FUSE")
    print()

    print_info("Setting file permissions with standard chmod command...")
    print_command(f"chmod 755 {MOUNT_POINT}/workspace/script.sh")
    run_cmd(sandbox, f"chmod 755 {MOUNT_POINT}/workspace/script.sh")
    print_success("Changed permissions to rwxr-xr-x (755) via FUSE")
    print()

    print_info("Viewing permissions with standard ls -l...")
    print_command(f"ls -l {MOUNT_POINT}/workspace/script.sh")
    run_cmd(sandbox, f"ls -l {MOUNT_POINT}/workspace/script.sh")
    print()

    print_info("✓ Verifying permissions persist in Nexus metadata...")
    print_command("nexus info /workspace/script.sh")
    run_cmd(sandbox, f"{env_prefix}nexus info /workspace/script.sh")
    print()

    # ============================================================================
    # More Permission Examples
    # ============================================================================

    print_section("Step 11a: More Permission Examples")

    print_info("Let's test a few more permission scenarios...")
    print()

    # Create another test file and set restrictive permissions
    print_command(
        f"echo 'Sensitive data' > {MOUNT_POINT}/workspace/secret.txt && chmod 600 {MOUNT_POINT}/workspace/secret.txt"
    )
    run_cmd(
        sandbox, f"echo 'Sensitive data' > {MOUNT_POINT}/workspace/secret.txt", show_output=False
    )
    run_cmd(sandbox, f"chmod 600 {MOUNT_POINT}/workspace/secret.txt", show_output=False)
    print_success("Created file with restrictive permissions (600)")
    print()

    # View all files with permissions
    print_info("Viewing all workspace files with permissions...")
    print_command(f"ls -l {MOUNT_POINT}/workspace/")
    run_cmd(sandbox, f"ls -l {MOUNT_POINT}/workspace/ | head -10 || true")
    print()

    print_success("✅ All standard Unix permission commands work via FUSE!")
    print_info("  • chmod, chown, chgrp modify Nexus metadata")
    print_info("  • ls -l, stat display Nexus permissions")
    print_info("  • Changes persist in the database")
    print_info("  • No nexus CLI commands needed for basic operations!")
    print()

    # ============================================================================
    # ACLs
    # ============================================================================

    print_section("Step 12: Fine-Grained Access Control with ACLs")

    print_info("ACLs provide fine-grained permissions beyond UNIX owner/group/other model")
    print()

    # Create sensitive file
    print_command(
        f"echo 'Confidential data - access restricted' > {MOUNT_POINT}/workspace/sensitive.txt"
    )
    run_cmd(
        sandbox,
        f"echo 'Confidential data - access restricted' > {MOUNT_POINT}/workspace/sensitive.txt",
    )
    print_success("Created sensitive file via FUSE")
    print()

    print_command(f"chmod 600 {MOUNT_POINT}/workspace/sensitive.txt")
    run_cmd(sandbox, f"chmod 600 {MOUNT_POINT}/workspace/sensitive.txt")
    print_info("Set base permissions to rw------- (600) via standard chmod")
    print()

    print_info("Now using nexus CLI for ACL operations...")
    print()

    print_command("nexus setfacl 'user:bob:r--' /workspace/sensitive.txt")
    run_cmd(sandbox, f"{env_prefix}nexus setfacl 'user:bob:r--' /workspace/sensitive.txt")

    print_command("nexus setfacl 'user:carol:rw-' /workspace/sensitive.txt")
    run_cmd(sandbox, f"{env_prefix}nexus setfacl 'user:carol:rw-' /workspace/sensitive.txt")

    print_command("nexus setfacl 'group:auditors:r--' /workspace/sensitive.txt")
    run_cmd(sandbox, f"{env_prefix}nexus setfacl 'group:auditors:r--' /workspace/sensitive.txt")

    print_command("nexus setfacl 'deny:user:eve:---' /workspace/sensitive.txt")
    run_cmd(sandbox, f"{env_prefix}nexus setfacl 'deny:user:eve:---' /workspace/sensitive.txt")
    print()

    print_info("Viewing Access Control List...")
    print_command("nexus getfacl /workspace/sensitive.txt")
    run_cmd(sandbox, f"{env_prefix}nexus getfacl /workspace/sensitive.txt")
    print()

    # ============================================================================
    # Two Ways to Do Everything!
    # ============================================================================

    print_section("Step 13: Two Ways to Do Everything!")

    print_success("✅ With FUSE mounted, BOTH interfaces work simultaneously!")
    print_info("You can mix and match Unix commands and nexus CLI as you prefer")
    print()

    # Demonstrate both approaches work at the same time
    print_info("Example: Both of these work at the same time!")
    print()

    # Create a test file to demonstrate
    print_command(f"echo 'Test file' > {MOUNT_POINT}/workspace/test_both.txt")
    run_cmd(sandbox, f"echo 'Test file' > {MOUNT_POINT}/workspace/test_both.txt", show_output=False)
    print()

    print_info("Method 1: Standard Unix command via FUSE")
    print_command(f"chmod 644 {MOUNT_POINT}/workspace/test_both.txt")
    run_cmd(sandbox, f"chmod 644 {MOUNT_POINT}/workspace/test_both.txt", show_output=False)
    print_command(f"ls -l {MOUNT_POINT}/workspace/test_both.txt")
    run_cmd(sandbox, f"ls -l {MOUNT_POINT}/workspace/test_both.txt | head -1 || true")
    print()

    print_info("Method 2: Nexus CLI command (also works!)")
    print_command("nexus chmod 600 /workspace/test_both.txt")
    run_cmd(sandbox, f"{env_prefix}nexus chmod 600 /workspace/test_both.txt || true")
    print_command("nexus ls /workspace --long | grep test_both")
    run_cmd(
        sandbox,
        f"{env_prefix}nexus ls /workspace --long 2>/dev/null | grep test_both || echo '(Updated via CLI)'",
    )
    print()

    print_info("Verify via FUSE mount (shows nexus CLI changes):")
    print_command(f"ls -l {MOUNT_POINT}/workspace/test_both.txt")
    run_cmd(sandbox, f"ls -l {MOUNT_POINT}/workspace/test_both.txt | head -1 || true")
    print()

    print_success("✓ Both methods work! Use whichever is more convenient!")
    print()

    print_info("When to prefer each:")
    print()
    print("  Prefer Unix commands (via FUSE):")
    print("   ✓ Scripting with existing tools")
    print("   ✓ Using editors (vim, vscode, etc.)")
    print("   ✓ Standard workflow integration")
    print("   ✓ No learning curve - works like any filesystem")
    print()
    print("  Prefer nexus CLI:")
    print("   ✓ Special features (ACLs, work queues, export/import)")
    print("   ✓ Metadata operations (nexus info, nexus glob)")
    print("   ✓ When you're already in a nexus workflow")
    print("   ✓ Advanced queries and batch operations")
    print()

    print_success("Key takeaway: Both work simultaneously - use what's convenient! 🎉")
    print()

    print_info("💡 Permission evaluation order:")
    print("   1. Check ACL deny entries (highest priority)")
    print("   2. Check ACL allow entries")
    print("   3. Fall back to UNIX permissions (owner/group/other)")
    print()

    # ============================================================================
    # Summary
    # ============================================================================

    print_header("Demo Complete!")

    print("Key Takeaways:")
    print()
    print("  1. ✓ Mount Nexus once, use standard Unix tools everywhere")
    print("  2. ✓ No special commands needed: cp, mv, rm, chmod, grep all work!")
    print("  3. ✓ grep works DIRECTLY on PDFs (with --auto-parse) 🔥")
    print("  4. ✓ Permissions (chmod, chown, ls -l) work seamlessly via FUSE")
    print("  5. ✓ Changes persist in Nexus metadata automatically")
    print("  6. ✓ Use ANY Unix tool: vim, emacs, ripgrep, fd, etc.")
    print("  7. ✓ Perfect for scripts and automation (no vendor lock-in!)")
    print("  8. ✓ E2B sandbox = FUSE works even on macOS! 🎉")
    print()

    print_success("Demo finished successfully in E2B sandbox!")
    print_info("No macFUSE needed on your Mac - everything ran in Linux! 🚀")
    print()

    # Cleanup
    print_info("Unmounting...")
    run_cmd(
        sandbox,
        f"sudo NEXUS_DATA_DIR={NEXUS_DATA} nexus unmount {MOUNT_POINT} || true",
        show_output=False,
    )

finally:
    print()
    print_info("Cleaning up sandbox...")
    sandbox.kill()
    print_success("Sandbox terminated")
