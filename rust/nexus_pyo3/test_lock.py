#!/usr/bin/env python3
"""Direct tests for the Rust VFSLockManager via nexus_fast (Issue #1398).

Run after building with: maturin develop --release
Usage: python rust/nexus_pyo3/test_lock.py
"""

from __future__ import annotations

import sys
import threading
import time


def main() -> int:
    try:
        from nexus_fast import VFSLockManager
    except ImportError:
        print("SKIP: nexus_fast not available (run `maturin develop` first)")
        return 0

    passed = 0
    failed = 0

    def check(name: str, condition: bool) -> None:
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  PASS: {name}")
        else:
            failed += 1
            print(f"  FAIL: {name}")

    mgr = VFSLockManager()

    # 1. Basic read acquire/release
    h = mgr.acquire("/foo", "read")
    check("read acquire returns handle > 0", h > 0)
    check("is_locked after acquire", mgr.is_locked("/foo"))
    check("release returns True", mgr.release(h))
    check("not locked after release", not mgr.is_locked("/foo"))

    # 2. Basic write acquire/release
    h = mgr.acquire("/bar", "write")
    check("write acquire returns handle > 0", h > 0)
    check("release write", mgr.release(h))

    # 3. Read-read coexistence
    h1 = mgr.acquire("/shared", "read")
    h2 = mgr.acquire("/shared", "read")
    check("two readers coexist", h1 > 0 and h2 > 0 and h1 != h2)
    mgr.release(h1)
    mgr.release(h2)

    # 4. Write blocks read
    w = mgr.acquire("/excl", "write")
    r = mgr.acquire("/excl", "read")
    check("write blocks read (non-blocking)", r == 0)
    mgr.release(w)

    # 5. Read blocks write
    r = mgr.acquire("/excl2", "read")
    w = mgr.acquire("/excl2", "write")
    check("read blocks write", w == 0)
    mgr.release(r)

    # 6. Ancestor conflict
    w = mgr.acquire("/a", "write")
    child = mgr.acquire("/a/b", "read")
    check("ancestor write blocks child read", child == 0)
    mgr.release(w)

    # 7. Descendant conflict
    w = mgr.acquire("/x/y/z", "write")
    parent = mgr.acquire("/x", "write")
    check("descendant write blocks parent write", parent == 0)
    mgr.release(w)

    # 8. Release wrong handle
    check("release invalid handle returns False", not mgr.release(99999))

    # 9. Timeout behaviour
    w = mgr.acquire("/timeout", "write")
    start = time.monotonic()
    result = mgr.acquire("/timeout", "read", 50)
    elapsed = (time.monotonic() - start) * 1000
    check("blocking timeout returns 0", result == 0)
    check("elapsed >= ~40ms", elapsed >= 40)
    mgr.release(w)

    # 10. Holders info
    h = mgr.acquire("/info", "read")
    info = mgr.holders("/info")
    check("holders returns dict", info is not None)
    check("holders has readers=1", info is not None and info["readers"] == 1)
    mgr.release(h)

    # 11. Stats
    s = mgr.stats()
    check("stats has acquire_count", "acquire_count" in s)
    check("stats has avg_acquire_ns", "avg_acquire_ns" in s)

    # 12. active_locks property
    h = mgr.acquire("/prop", "write")
    check("active_locks >= 1", mgr.active_locks >= 1)
    mgr.release(h)

    # 13. Unicode path
    h = mgr.acquire("/unicode/path", "write")
    check("unicode path works", h > 0)
    mgr.release(h)

    # 14. Concurrent readers
    mgr2 = VFSLockManager()
    handles: list[int] = []
    errors: list[str] = []

    def reader() -> None:
        try:
            rh = mgr2.acquire("/concurrent", "read", 1000)
            if rh > 0:
                handles.append(rh)
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=reader) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    check("10 concurrent readers all succeed", len(handles) == 10 and not errors)
    for rh in handles:
        mgr2.release(rh)

    # 15. Invalid mode
    try:
        mgr.acquire("/bad", "exclusive")
        check("invalid mode raises error", False)
    except (ValueError, Exception):
        check("invalid mode raises error", True)

    print(f"\nResults: {passed} passed, {failed} failed")
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
