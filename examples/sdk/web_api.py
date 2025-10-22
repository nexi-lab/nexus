"""
Simple Web API Example using Nexus SDK and Flask

This example demonstrates building a web API on top of Nexus
using the SDK without any CLI dependencies.

Requirements:
    pip install flask

Usage:
    python web_api.py
    # Then visit http://localhost:5000 in your browser
"""

try:
    from flask import Flask, jsonify, request
except ImportError:
    print("This example requires Flask. Install it with: pip install flask")
    exit(1)

from nexus.sdk import FileNotFoundError as NexusFileNotFoundError
from nexus.sdk import connect

app = Flask(__name__)
nx = connect()


@app.route("/")
def index():
    """API documentation."""
    return jsonify(
        {
            "name": "Nexus Web API Example",
            "version": "1.0",
            "endpoints": {
                "/files": "GET - List all files",
                "/files/<path:path>": "GET - Read file, POST - Write file, DELETE - Delete file",
                "/search": "GET - Search files (query param: pattern)",
                "/stats": "GET - Get filesystem statistics",
            },
        }
    )


@app.route("/files", methods=["GET"])
def list_files():
    """List all files."""
    try:
        path = request.args.get("path", "/")
        recursive = request.args.get("recursive", "false").lower() == "true"

        files = nx.list(path, recursive=recursive)
        return jsonify(
            {
                "path": path,
                "files": [
                    {
                        "path": f.path,
                        "size": f.size,
                        "modified": str(f.modified_at),
                        "is_directory": nx.is_directory(f.path),
                    }
                    for f in files
                ],
            }
        )
    except NexusFileNotFoundError:
        return jsonify({"error": "Path not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/files/<path:filepath>", methods=["GET"])
def read_file(filepath):
    """Read a file."""
    try:
        content = nx.read(f"/{filepath}")
        return jsonify(
            {
                "path": f"/{filepath}",
                "content": content.decode("utf-8", errors="replace"),
                "size": len(content),
            }
        )
    except NexusFileNotFoundError:
        return jsonify({"error": "File not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/files/<path:filepath>", methods=["POST"])
def write_file(filepath):
    """Write a file."""
    try:
        data = request.get_json()
        if not data or "content" not in data:
            return jsonify({"error": "Missing 'content' in request body"}), 400

        content = data["content"].encode("utf-8")
        nx.write(f"/{filepath}", content)

        return jsonify({"path": f"/{filepath}", "size": len(content), "status": "written"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/files/<path:filepath>", methods=["DELETE"])
def delete_file(filepath):
    """Delete a file."""
    try:
        nx.delete(f"/{filepath}")
        return jsonify({"path": f"/{filepath}", "status": "deleted"})
    except NexusFileNotFoundError:
        return jsonify({"error": "File not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/search", methods=["GET"])
def search_files():
    """Search files using glob patterns."""
    try:
        pattern = request.args.get("pattern")
        if not pattern:
            return jsonify({"error": "Missing 'pattern' query parameter"}), 400

        results = nx.glob(pattern)
        return jsonify(
            {
                "pattern": pattern,
                "results": [{"path": f.path} for f in results],
                "count": len(results),
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/stats", methods=["GET"])
def get_stats():
    """Get filesystem statistics."""
    try:
        # Count files and directories
        all_files = nx.list("/", recursive=True)
        total_files = len(all_files)
        total_size = sum(f.size for f in all_files)

        return jsonify(
            {
                "total_files": total_files,
                "total_size_bytes": total_size,
                "total_size_mb": round(total_size / (1024 * 1024), 2),
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def main():
    print("=" * 60)
    print("  Nexus SDK Web API Example")
    print("=" * 60)
    print("\nStarting server on http://localhost:5000")
    print("\nAPI Endpoints:")
    print("  GET  /                      - API documentation")
    print("  GET  /files?path=/&recursive=true - List files")
    print("  GET  /files/<path>          - Read file")
    print("  POST /files/<path>          - Write file")
    print("  DELETE /files/<path>        - Delete file")
    print("  GET  /search?pattern=**/*.py - Search files")
    print("  GET  /stats                 - Get statistics")
    print("\nPress Ctrl+C to stop")
    print("=" * 60)

    app.run(debug=True, host="0.0.0.0", port=5000)


if __name__ == "__main__":
    main()
