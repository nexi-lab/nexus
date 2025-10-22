"""
Basic Nexus SDK Usage Example

This example demonstrates basic file operations using the Nexus SDK.
"""

from nexus.sdk import connect


def main():
    # Connect to Nexus (auto-discovers configuration)
    print("Connecting to Nexus...")
    nx = connect()

    # Create a test file
    print("\n1. Writing a file...")
    test_path = "/workspace/sdk-example.txt"
    content = b"Hello from Nexus SDK!"
    nx.write(test_path, content)
    print(f"   ✓ Wrote {len(content)} bytes to {test_path}")

    # Read the file back
    print("\n2. Reading the file...")
    read_content = nx.read(test_path)
    print(f"   ✓ Read content: {read_content.decode()}")

    # List files in workspace
    print("\n3. Listing files in /workspace...")
    files = nx.list("/workspace", recursive=False)
    for filepath in files:
        print(f"   - {filepath}")

    # Check file exists
    print("\n4. Checking if file exists...")
    if nx.exists(test_path):
        print(f"   ✓ File exists: {test_path}")

    # Search for files
    print("\n5. Searching for text files...")
    txt_files = nx.glob("**/*.txt")
    print(f"   Found {len(txt_files)} .txt files:")
    for filepath in list(txt_files)[:5]:  # Show first 5
        print(f"   - {filepath}")

    # Clean up
    print("\n6. Cleaning up...")
    nx.delete(test_path)
    print(f"   ✓ Deleted {test_path}")

    print("\n✅ SDK example completed successfully!")


if __name__ == "__main__":
    main()
