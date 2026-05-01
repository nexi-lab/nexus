"""Edge case test data for parametrized filesystem tests."""

from __future__ import annotations

UNICODE_PATHS: list[str] = [
    "/files/\u4f60\u597d\u4e16\u754c.txt",
    "/files/\u3053\u3093\u306b\u3061\u306f.txt",
    "/files/\ud55c\uad6d\uc5b4.txt",
    "/files/\U0001f680rocket.txt",
    "/files/data_\U0001f4ca_chart.csv",
    "/files/\U0001f1fa\U0001f1f8flag.txt",
    "/files/\u0645\u0644\u0641.txt",
    "/files/\u05e7\u05d5\u05d1\u05e5.txt",
    "/files/caf\u00e9.txt",
    "/files/cafe\u0301.txt",
    "/files/hello_\u4e16\u754c_\U0001f30d.txt",
    "/files/zero\u200bwidth.txt",
    "/files/invisible\u200d\u200djoiner.txt",
]

EDGE_CONTENT: list[tuple[str, bytes]] = [
    ("empty", b""),
    ("single_byte", b"\x00"),
    ("null_bytes", b"\x00\x00\x00\x00"),
    ("single_newline", b"\n"),
    ("crlf", b"\r\n"),
    ("binary_ff", b"\xff" * 16),
    ("mixed_encoding", "café ☕ naïve".encode()),
    ("large_single_line", b"x" * 65536),
    ("many_newlines", b"\n" * 1000),
    ("bom_utf8", b"\xef\xbb\xbf" + b"hello"),
    ("bom_utf16", b"\xff\xfeh\x00e\x00l\x00l\x00o\x00"),
]

SPECIAL_PATHS: list[str] = [
    "/files/with spaces/file.txt",
    "/files/with.dots/file.name.ext",
    "/files/UPPERCASE/FILE.TXT",
    "/files/MiXeD.CaSe/FiLe.TxT",
    "/files/deep/nested/path/to/file.txt",
    "/files/trailing-dash-.txt",
    "/files/leading-.dot.txt",
    "/files/under_score/file_name.txt",
    "/files/hyphen-ated/file-name.txt",
    "/files/numbers123/456.txt",
    "/files/single.c",
    "/files/.hidden",
    "/files/no_extension",
]

PATHS_THAT_SHOULD_NORMALIZE_OR_REJECT: list[str] = [
    "relative/path.txt",
    "/files//double//slashes.txt",
    "/files/./dot/./segments.txt",
    "/files/../parent/../traversal.txt",
    "/files/trailing/slash/",
    "",
    "/",
]
