"""Edge case test data for parametrized filesystem tests.
import pytest

Provides curated collections of tricky filenames, path patterns, and content
payloads that exercise boundary conditions in VFS operations.

Usage with pytest:
    from tests.helpers.edge_cases import UNICODE_PATHS, EDGE_CONTENT, SPECIAL_PATHS

    @pytest.mark.parametrize("path", UNICODE_PATHS)
    @pytest.mark.asyncio
    async def test_write_unicode(nexus_fs, path):
        nexus_fs.write(path, b"hello")
        assert nexus_fs.sys_read(path) == b"hello"
"""

# === Unicode filename edge cases ===
# Covers CJK, emoji, RTL, combining characters, and mixed scripts.

UNICODE_PATHS: list[str] = [
    # CJK characters
    "/files/\u4f60\u597d\u4e16\u754c.txt",  # Chinese: 你好世界
    "/files/\u3053\u3093\u306b\u3061\u306f.txt",  # Japanese hiragana: こんにちは
    "/files/\ud55c\uad6d\uc5b4.txt",  # Korean: 한국어
    # Emoji
    "/files/\U0001f680rocket.txt",  # 🚀
    "/files/data_\U0001f4ca_chart.csv",  # 📊
    "/files/\U0001f1fa\U0001f1f8flag.txt",  # 🇺🇸 (flag = two regional indicators)
    # RTL (Arabic, Hebrew)
    "/files/\u0645\u0644\u0641.txt",  # Arabic: ملف
    "/files/\u05e7\u05d5\u05d1\u05e5.txt",  # Hebrew: קובץ
    # Combining characters (accent marks)
    "/files/caf\u00e9.txt",  # precomposed é (U+00E9)
    "/files/cafe\u0301.txt",  # decomposed e + combining acute (U+0301)
    # Mixed scripts
    "/files/hello_\u4e16\u754c_\U0001f30d.txt",  # Latin + CJK + Globe emoji
    # Zero-width characters
    "/files/zero\u200bwidth.txt",  # zero-width space
    "/files/invisible\u200d\u200djoiner.txt",  # zero-width joiner
]

# === Content edge cases ===
# Covers boundary payloads that stress content-addressable storage.

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

# === Special path patterns ===
# Covers path normalization edge cases.

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

# === Path normalization edge cases ===
# These paths may be rejected or normalized by the VFS layer.
# Use these to verify consistent error handling.

PATHS_THAT_SHOULD_NORMALIZE_OR_REJECT: list[str] = [
    "relative/path.txt",  # no leading slash
    "/files//double//slashes.txt",  # double slashes
    "/files/./dot/./segments.txt",  # dot segments
    "/files/../parent/../traversal.txt",  # parent traversal
    "/files/trailing/slash/",  # trailing slash on file
    "",  # empty string
    "/",  # root only
]
