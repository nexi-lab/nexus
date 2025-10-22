"""
Custom File Browser Example using Nexus SDK

This example demonstrates building a simple interactive file browser
using the Nexus SDK without any CLI dependencies.
"""

from nexus.sdk import FileNotFoundError, connect


class SimpleBrowser:
    """Simple terminal-based file browser using Nexus SDK."""

    def __init__(self):
        self.nx = connect()
        self.current_path = "/"

    def list_directory(self, path=None):
        """List files in a directory."""
        if path:
            self.current_path = path

        try:
            files = self.nx.list(self.current_path, recursive=False)
            print(f"\n📁 Contents of {self.current_path}:")
            print("=" * 60)

            if not files:
                print("  (empty directory)")
                return

            # Sort: directories first, then files
            dirs = [f for f in files if self.nx.is_directory(f.path)]
            regular_files = [f for f in files if not self.nx.is_directory(f.path)]

            # Show directories
            for file in sorted(dirs, key=lambda x: x.path):
                print(f"  📁 {file.path}/")

            # Show files
            for file in sorted(regular_files, key=lambda x: x.path):
                size_kb = file.size / 1024
                print(f"  📄 {file.path} ({size_kb:.1f} KB)")

        except FileNotFoundError:
            print(f"❌ Path not found: {self.current_path}")
        except Exception as e:
            print(f"❌ Error: {e}")

    def read_file(self, path):
        """Read and display file contents."""
        try:
            content = self.nx.read(path)
            print(f"\n📄 Contents of {path}:")
            print("=" * 60)
            print(content.decode("utf-8", errors="replace"))
            print("=" * 60)
        except FileNotFoundError:
            print(f"❌ File not found: {path}")
        except Exception as e:
            print(f"❌ Error reading file: {e}")

    def search(self, pattern):
        """Search for files matching a glob pattern."""
        try:
            results = self.nx.glob(pattern)
            print(f"\n🔍 Search results for '{pattern}':")
            print("=" * 60)

            if not results:
                print("  No files found")
                return

            for i, file in enumerate(results, 1):
                print(f"  {i}. {file.path}")

        except Exception as e:
            print(f"❌ Error searching: {e}")

    def run(self):
        """Run the interactive browser."""
        print("=" * 60)
        print("      Nexus SDK File Browser Example")
        print("=" * 60)
        print("\nCommands:")
        print("  ls [path]      - List directory contents")
        print("  cd <path>      - Change directory")
        print("  cat <path>     - Display file contents")
        print("  search <glob>  - Search files (e.g., '**/*.py')")
        print("  pwd            - Print current directory")
        print("  quit           - Exit browser")
        print("=" * 60)

        # Show initial directory
        self.list_directory()

        # Command loop
        while True:
            try:
                cmd = input(f"\n{self.current_path}> ").strip()

                if not cmd:
                    continue

                parts = cmd.split(maxsplit=1)
                command = parts[0].lower()
                arg = parts[1] if len(parts) > 1 else None

                if command == "quit" or command == "exit":
                    print("👋 Goodbye!")
                    break
                elif command == "ls":
                    self.list_directory(arg if arg else self.current_path)
                elif command == "cd":
                    if arg:
                        self.current_path = arg
                        self.list_directory()
                    else:
                        print("Usage: cd <path>")
                elif command == "cat":
                    if arg:
                        self.read_file(arg)
                    else:
                        print("Usage: cat <path>")
                elif command == "search":
                    if arg:
                        self.search(arg)
                    else:
                        print("Usage: search <glob-pattern>")
                elif command == "pwd":
                    print(self.current_path)
                else:
                    print(f"Unknown command: {command}")

            except KeyboardInterrupt:
                print("\n\n👋 Goodbye!")
                break
            except Exception as e:
                print(f"❌ Error: {e}")


def main():
    browser = SimpleBrowser()
    browser.run()


if __name__ == "__main__":
    main()
