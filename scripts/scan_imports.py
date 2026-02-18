"""Scan all nexus modules for import errors."""
import importlib
import os

src = "src/nexus"
ok = 0
fail = 0
errors = []

for root, dirs, files in os.walk(src):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for f in sorted(files):
        if not f.endswith(".py"):
            continue
        path = os.path.join(root, f)
        rel = os.path.relpath(path, "src")
        mod = rel.replace("/", ".").replace(".py", "")
        if mod.endswith(".__init__"):
            mod = mod[:-9]
        try:
            importlib.import_module(mod)
            ok += 1
        except Exception as e:
            fail += 1
            errors.append((mod, type(e).__name__, str(e)[:120]))

print(f"OK: {ok}")
print(f"FAIL: {fail}")
print(f"Total: {ok + fail}")
if errors:
    print()
    for mod, etype, msg in errors:
        print(f"  {etype}: {mod}: {msg}")
