"""Unit tests for the Zero-Trust Input Sanitizer."""

from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path

import pytest

from marksentry.core.sanitizer import (
    SanitizationError,
    _is_ssrf_url,
    _find_ssrf_urls,
    sanitize,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "sample.pdf"
    p.write_bytes(b"%PDF-1.4 %\xa0\n1 0 obj\n<< /Type /Catalog >>\nendobj\n")
    return p


@pytest.fixture()
def tmp_txt(tmp_path: Path) -> Path:
    p = tmp_path / "sample.txt"
    p.write_text("Hello, world.", encoding="utf-8")
    return p


@pytest.fixture()
def tmp_docx(tmp_path: Path) -> Path:
    """Minimal valid OOXML ZIP."""
    p = tmp_path / "sample.docx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="..."/>')
        zf.writestr("word/document.xml", "<w:document/>")
    return p


@pytest.fixture()
def tmp_docm_with_macros(tmp_path: Path) -> Path:
    """OOXML ZIP that contains vbaProject.bin."""
    p = tmp_path / "malicious.docm"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("[Content_Types].xml", (
            '<?xml version="1.0"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/vbaProject.bin" '
            'ContentType="application/vnd.ms-office.vbaProject"/>'
            '</Types>'
        ))
        zf.writestr("word/document.xml", "<w:document/>")
        zf.writestr("word/vbaProject.bin", b"\xd0\xcf\x11\xe0" + b"\x00" * 100)
    return p


# ---------------------------------------------------------------------------
# Path traversal tests
# ---------------------------------------------------------------------------

def test_null_byte_rejected(tmp_path: Path) -> None:
    with pytest.raises(SanitizationError, match="Null byte"):
        sanitize(str(tmp_path / "file.pdf") + "\x00extra")


def test_unc_path_rejected() -> None:
    with pytest.raises(SanitizationError, match="UNC"):
        sanitize("\\\\server\\share\\file.pdf")


def test_url_scheme_rejected() -> None:
    with pytest.raises(SanitizationError, match="URI scheme"):
        sanitize("http://example.com/file.pdf")


def test_file_scheme_rejected() -> None:
    with pytest.raises(SanitizationError, match="URI scheme"):
        sanitize("file:///etc/passwd")


def test_nonexistent_file_rejected(tmp_path: Path) -> None:
    with pytest.raises(SanitizationError):
        sanitize(tmp_path / "does_not_exist.pdf")


def test_path_jail_escape_rejected(tmp_path: Path, tmp_pdf: Path) -> None:
    jail = tmp_path / "jail"
    jail.mkdir()
    with pytest.raises(SanitizationError, match="escapes"):
        sanitize(tmp_pdf, allowed_base=jail)


def test_path_jail_inside_allowed(tmp_path: Path) -> None:
    jail = tmp_path
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF" + b"\x00" * 16)
    result = sanitize(p, allowed_base=jail)
    assert result.safe_path == p.resolve()


# ---------------------------------------------------------------------------
# Magic byte / extension tests
# ---------------------------------------------------------------------------

def test_extension_not_in_allowlist(tmp_path: Path) -> None:
    p = tmp_path / "evil.exe"
    p.write_bytes(b"MZ\x90\x00" + b"\x00" * 100)
    with pytest.raises(SanitizationError, match="allow-list"):
        sanitize(p)


def test_magic_mismatch_rejected(tmp_path: Path) -> None:
    p = tmp_path / "not_a_pdf.pdf"
    p.write_bytes(b"PK\x03\x04" + b"\x00" * 50)   # ZIP magic, .pdf extension
    with pytest.raises(SanitizationError, match="Magic-byte mismatch"):
        sanitize(p)


def test_valid_pdf_accepted(tmp_pdf: Path) -> None:
    result = sanitize(tmp_pdf)
    assert result.detected_type == "pdf"


def test_valid_docx_accepted(tmp_docx: Path) -> None:
    result = sanitize(tmp_docx)
    assert result.detected_type == "docx"


# ---------------------------------------------------------------------------
# SSRF URL detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("http://169.254.169.254/latest/meta-data/", True),   # AWS IMDS
    ("http://10.0.0.1/admin", True),                       # RFC-1918
    ("http://192.168.1.1/", True),                         # RFC-1918
    ("http://127.0.0.1/", True),                           # loopback
    ("http://localhost/api", True),                        # loopback hostname
    ("file:///etc/passwd", True),                          # file scheme
    ("ftp://internal/data", True),                         # ftp scheme
    ("http://example.com/public", False),                  # safe external
    ("https://api.openai.com/v1/", False),                 # safe external
])
def test_ssrf_url_detection(url: str, expected: bool) -> None:
    assert _is_ssrf_url(url) == expected


def test_find_ssrf_urls_in_text() -> None:
    text = (
        'rel="http://169.254.169.254/meta" '
        'href="https://safe.example.com/page"'
    )
    blocked = _find_ssrf_urls(text)
    assert any("169.254" in u for u in blocked)
    assert not any("safe.example.com" in u for u in blocked)


# ---------------------------------------------------------------------------
# Macro stripping
# ---------------------------------------------------------------------------

def test_macro_stripped(tmp_docm_with_macros: Path) -> None:
    result = sanitize(tmp_docm_with_macros, strip_macros=True)
    assert result.macros_stripped is True
    # Verify the VBA entry is gone from the rewritten file
    with zipfile.ZipFile(tmp_docm_with_macros, "r") as zf:
        assert "word/vbaProject.bin" not in zf.namelist()


def test_clean_docx_macro_flag_false(tmp_docx: Path) -> None:
    result = sanitize(tmp_docx, strip_macros=True)
    assert result.macros_stripped is False


# ---------------------------------------------------------------------------
# Zip bomb detection
# ---------------------------------------------------------------------------

def test_zip_bomb_ratio_rejected(tmp_path: Path) -> None:
    import zlib
    p = tmp_path / "bomb.zip"
    # 1 MiB of zeros compresses to ~1 KiB -- not extreme, but we patch the limit
    payload = b"\x00" * (1024 * 1024)
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.bin", payload)

    from marksentry.core import sanitizer as s
    original_ratio = s.MAX_ZIP_EXPAND_RATIO
    s.MAX_ZIP_EXPAND_RATIO = 1.0  # force rejection
    try:
        with pytest.raises(SanitizationError, match="expansion ratio"):
            s._check_zip_bomb(p)
    finally:
        s.MAX_ZIP_EXPAND_RATIO = original_ratio
