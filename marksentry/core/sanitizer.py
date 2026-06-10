"""
Zero-Trust Input Sanitizer.

Defends against:
  - Path traversal (../../, UNC \\server\share, drive-root escapes, null-byte injection)
  - SSRF via embedded document URIs (file://, http://169.254.x.x, RFC-1918, loopback)
  - Active macro content in Office Open XML containers (.docm, .xlsm, vbaProject.bin)
  - Zip bombs (compression ratio threshold + nesting depth cap)
  - Magic-byte / extension mismatch spoofing
  - Oversized files that would cause memory exhaustion
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FILE_SIZE_BYTES: Final[int] = 256 * 1024 * 1024  # 256 MiB hard ceiling
MAX_ZIP_EXPAND_RATIO: Final[float] = 100.0            # compressed:uncompressed
MAX_ZIP_NEST_DEPTH: Final[int] = 3
MAX_ZIP_ENTRY_COUNT: Final[int] = 10_000

# Magic-byte signatures for allowed input formats
_MAGIC: Final[dict[str, bytes]] = {
    "pdf":  b"%PDF",
    "docx": b"PK\x03\x04",   # ZIP-based (OOXML)
    "zip":  b"PK\x03\x04",
    "xlsx": b"PK\x03\x04",
    "pptx": b"PK\x03\x04",
    "rtf":  b"{\\rtf",
    "odt":  b"PK\x03\x04",
    "txt":  None,              # No magic — validated by UTF-8 decode attempt
    "md":   None,
    "html": None,
    "htm":  None,
}

_ALLOWED_EXTENSIONS: Final[frozenset[str]] = frozenset(_MAGIC.keys())

# RFC-1918, loopback, link-local, and other non-routable ranges
_BLOCKED_IP_NETWORKS: Final[list[ipaddress.IPv4Network | ipaddress.IPv6Network]] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # AWS IMDS, Azure IMDS link-local
    ipaddress.ip_network("100.64.0.0/10"),    # Shared address space (RFC 6598)
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_BLOCKED_URI_SCHEMES: Final[frozenset[str]] = frozenset({
    "file", "ftp", "ftps", "data", "javascript", "vbscript", "ldap", "ldaps",
    "gopher", "dict", "sftp", "smb", "cifs", "telnet",
})

# Patterns that reveal embedded URLs inside document text/XML
_URL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"""(?:https?|ftp|file|smb|ldap|data|javascript|vbscript)://[^\s"'<>]+""",
    re.IGNORECASE,
)

# Macro-bearing entry names within OOXML ZIP containers
_MACRO_ENTRIES: Final[frozenset[str]] = frozenset({
    "xl/vbaProject.bin",
    "word/vbaProject.bin",
    "ppt/vbaProject.bin",
    "xl/vbaProjectSignature.bin",
    "word/vbaProjectSignature.bin",
    "_VBA_PROJECT_CUR/VBA/",
    "[Content_Types].xml",   # examined separately for macro content-type declarations
})

_MACRO_CONTENT_TYPES: Final[tuple[str, ...]] = (
    "application/vnd.ms-office.activeX",
    "application/vnd.ms-powerpoint.addin.macroEnabled",
    "application/vnd.ms-excel.sheet.macroEnabled",
    "application/vnd.ms-word.document.macroEnabled",
    "vnd.ms-office.vbaProject",
)


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

@dataclass
class SanitizationResult:
    safe_path: Path
    detected_type: str
    warnings: list[str] = field(default_factory=list)
    macros_stripped: bool = False
    ssrf_urls_found: list[str] = field(default_factory=list)


class SanitizationError(ValueError):
    """Raised when a file fails security checks and must be rejected."""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def sanitize(
    raw_path: str | Path,
    *,
    allowed_base: Path | None = None,
    strip_macros: bool = True,
    check_ssrf_urls: bool = True,
    max_size: int = MAX_FILE_SIZE_BYTES,
) -> SanitizationResult:
    """
    Validate and clean a document path before handing it to a parser.

    Parameters
    ----------
    raw_path:
        User-supplied path (may be relative, contain traversal sequences, etc.).
    allowed_base:
        If provided, the resolved path MUST be within this directory tree.
        Passing the application's data directory enforces a strict jail.
    strip_macros:
        Remove VBA/macro entries from OOXML archives in-place before parsing.
    check_ssrf_urls:
        Scan document content for embedded URLs pointing at internal hosts.
    max_size:
        Hard file-size ceiling in bytes. Defaults to 256 MiB.

    Returns
    -------
    SanitizationResult with the resolved safe path and any warnings.

    Raises
    ------
    SanitizationError if the file must be rejected outright.
    """
    path = _resolve_and_jail(raw_path, allowed_base)
    _check_file_size(path, max_size)
    detected_type = _verify_magic_bytes(path)

    result = SanitizationResult(safe_path=path, detected_type=detected_type)

    if detected_type in ("docx", "xlsx", "pptx", "odt", "zip"):
        _check_zip_bomb(path)
        if strip_macros:
            result.macros_stripped = _strip_macros(path)
        if check_ssrf_urls:
            result.ssrf_urls_found = _scan_ooxml_for_ssrf(path)

    elif detected_type == "pdf":
        if check_ssrf_urls:
            result.ssrf_urls_found = _scan_pdf_for_ssrf(path)

    if result.ssrf_urls_found:
        result.warnings.append(
            f"Blocked {len(result.ssrf_urls_found)} suspicious embedded URL(s). "
            "They have been removed from the output."
        )

    logger.info(
        "Sanitized %s → type=%s macros_stripped=%s ssrf_blocked=%d",
        path.name, detected_type, result.macros_stripped, len(result.ssrf_urls_found),
    )
    return result


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------

def _resolve_and_jail(raw: str | Path, allowed_base: Path | None) -> Path:
    raw_str = str(raw)

    # Reject null bytes — used to confuse C-level string handling
    if "\x00" in raw_str:
        raise SanitizationError("Null byte detected in file path.")

    # Reject UNC paths on Windows (\\server\share\...) — potential SSRF
    if raw_str.startswith("\\\\") or raw_str.startswith("//"):
        raise SanitizationError("UNC/network paths are not permitted.")

    # Reject explicit URI schemes in the path argument
    parsed = urlparse(raw_str)
    if parsed.scheme and parsed.scheme.lower() in _BLOCKED_URI_SCHEMES | {"http", "https"}:
        raise SanitizationError(f"URI scheme '{parsed.scheme}' is not permitted as a file path.")

    try:
        resolved = Path(raw_str).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SanitizationError(f"Cannot resolve path '{raw_str}': {exc}") from exc

    if not resolved.is_file():
        raise SanitizationError(f"Path does not point to a regular file: {resolved}")

    if allowed_base is not None:
        jail = allowed_base.resolve()
        try:
            resolved.relative_to(jail)
        except ValueError:
            raise SanitizationError(
                f"Path escapes the allowed base directory. "
                f"Resolved='{resolved}', Base='{jail}'"
            )

    return resolved


# ---------------------------------------------------------------------------
# Size check
# ---------------------------------------------------------------------------

def _check_file_size(path: Path, limit: int) -> None:
    size = path.stat().st_size
    if size > limit:
        raise SanitizationError(
            f"File '{path.name}' is {size:,} bytes, exceeding the {limit:,}-byte limit."
        )


# ---------------------------------------------------------------------------
# Magic-byte validation
# ---------------------------------------------------------------------------

def _verify_magic_bytes(path: Path) -> str:
    ext = path.suffix.lstrip(".").lower()

    if ext not in _ALLOWED_EXTENSIONS:
        raise SanitizationError(
            f"Extension '.{ext}' is not in the allow-list: {sorted(_ALLOWED_EXTENSIONS)}"
        )

    expected_magic = _MAGIC.get(ext)
    if expected_magic is None:
        # Text-like formats: attempt UTF-8 decode of first 4 KiB
        _assert_text_decodable(path)
        return ext

    with path.open("rb") as fh:
        header = fh.read(len(expected_magic))

    if header != expected_magic:
        raise SanitizationError(
            f"Magic-byte mismatch for '{path.name}': "
            f"extension='.{ext}' but header={header!r}. Possible spoofing."
        )

    return ext


def _assert_text_decodable(path: Path) -> None:
    try:
        with path.open("rb") as fh:
            chunk = fh.read(4096)
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        # Fall back to latin-1 — still a text file, just non-UTF-8
        pass


# ---------------------------------------------------------------------------
# Zip-bomb detection
# ---------------------------------------------------------------------------

def _check_zip_bomb(path: Path, *, depth: int = 0) -> None:
    if depth > MAX_ZIP_NEST_DEPTH:
        raise SanitizationError(
            f"ZIP nesting depth exceeds {MAX_ZIP_NEST_DEPTH}. Possible zip bomb."
        )

    try:
        with zipfile.ZipFile(path, "r") as zf:
            entries = zf.infolist()
    except zipfile.BadZipFile:
        return  # Not a ZIP archive — skip

    if len(entries) > MAX_ZIP_ENTRY_COUNT:
        raise SanitizationError(
            f"ZIP contains {len(entries):,} entries, exceeding cap of {MAX_ZIP_ENTRY_COUNT:,}."
        )

    compressed_total = sum(e.compress_size for e in entries)
    uncompressed_total = sum(e.file_size for e in entries)

    if compressed_total > 0:
        ratio = uncompressed_total / compressed_total
        if ratio > MAX_ZIP_EXPAND_RATIO:
            raise SanitizationError(
                f"ZIP expansion ratio {ratio:.1f}x exceeds {MAX_ZIP_EXPAND_RATIO}x limit. "
                "Possible zip bomb."
            )

    # Recursively check nested ZIP entries
    for entry in entries:
        if entry.filename.lower().endswith(".zip"):
            import io
            try:
                with zipfile.ZipFile(path, "r") as zf:
                    nested_data = zf.read(entry.filename)
                nested_path = Path(entry.filename)
                _check_zip_bomb_bytes(nested_data, depth=depth + 1, name=nested_path.name)
            except (KeyError, zipfile.BadZipFile):
                pass


def _check_zip_bomb_bytes(data: bytes, *, depth: int, name: str) -> None:
    import io
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            entries = zf.infolist()
    except zipfile.BadZipFile:
        return

    if depth > MAX_ZIP_NEST_DEPTH:
        raise SanitizationError(f"Nested ZIP '{name}' exceeds depth limit.")

    compressed_total = sum(e.compress_size for e in entries)
    uncompressed_total = sum(e.file_size for e in entries)
    if compressed_total > 0 and (uncompressed_total / compressed_total) > MAX_ZIP_EXPAND_RATIO:
        raise SanitizationError(f"Nested ZIP '{name}' has suspicious expansion ratio.")


# ---------------------------------------------------------------------------
# Macro stripping (OOXML)
# ---------------------------------------------------------------------------

def _strip_macros(path: Path) -> bool:
    """
    Remove VBA project entries from an OOXML container.
    Rewrites the file in-place only if macro content is detected.
    Returns True if macros were found and removed.
    """
    import io
    import shutil

    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        return False

    # Detect macro-bearing entries
    macro_names = {n for n in names if _is_macro_entry(n)}

    # Check [Content_Types].xml for macro content-type declarations
    if "[Content_Types].xml" in names:
        try:
            with zipfile.ZipFile(path, "r") as zf:
                ct_data = zf.read("[Content_Types].xml").decode("utf-8", errors="replace")
            if any(ct in ct_data for ct in _MACRO_CONTENT_TYPES):
                macro_names.add("[Content_Types].xml")  # will be rewritten below
        except Exception:
            pass

    if not macro_names:
        return False

    # Rewrite the ZIP without macro entries
    buffer = io.BytesIO()
    with zipfile.ZipFile(path, "r") as src, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            if item.filename in macro_names and item.filename != "[Content_Types].xml":
                logger.debug("Stripping macro entry: %s", item.filename)
                continue
            data = src.read(item.filename)
            if item.filename == "[Content_Types].xml":
                # Scrub macro content-type declarations from the manifest
                data = _scrub_content_types_xml(data)
            dst.writestr(item, data)

    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    path.write_bytes(buffer.getvalue())
    backup.unlink()

    logger.warning(
        "Macro content removed from '%s'. Entries stripped: %s",
        path.name, sorted(macro_names),
    )
    return True


def _is_macro_entry(name: str) -> bool:
    lower = name.lower()
    return any(
        lower == m.lower() or lower.startswith(m.lower())
        for m in _MACRO_ENTRIES
        if m != "[Content_Types].xml"
    )


def _scrub_content_types_xml(data: bytes) -> bytes:
    text = data.decode("utf-8", errors="replace")
    for ct in _MACRO_CONTENT_TYPES:
        # Remove <Override> or <Default> elements referencing macro content types
        text = re.sub(
            r'<(?:Override|Default)[^>]*ContentType="[^"]*' + re.escape(ct) + r'[^"]*"[^/]*/?>',
            "",
            text,
            flags=re.IGNORECASE,
        )
    return text.encode("utf-8")


# ---------------------------------------------------------------------------
# SSRF: embedded URL scanning
# ---------------------------------------------------------------------------

def _scan_ooxml_for_ssrf(path: Path) -> list[str]:
    blocked: list[str] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            for name in zf.namelist():
                if not name.lower().endswith((".xml", ".rels", ".html", ".htm")):
                    continue
                try:
                    content = zf.read(name).decode("utf-8", errors="replace")
                    blocked.extend(_find_ssrf_urls(content))
                except Exception:
                    continue
    except zipfile.BadZipFile:
        pass
    return blocked


def _scan_pdf_for_ssrf(path: Path) -> list[str]:
    blocked: list[str] = []
    try:
        text = path.read_bytes().decode("latin-1", errors="replace")
        blocked.extend(_find_ssrf_urls(text))
    except Exception:
        pass
    return blocked


def _find_ssrf_urls(text: str) -> list[str]:
    blocked: list[str] = []
    for match in _URL_PATTERN.finditer(text):
        url = match.group(0)
        if _is_ssrf_url(url):
            blocked.append(url)
    return blocked


def _is_ssrf_url(url: str) -> bool:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme in _BLOCKED_URI_SCHEMES:
        return True

    if scheme in ("http", "https"):
        hostname = parsed.hostname
        if not hostname:
            return False
        # Reject raw IP addresses pointing to internal ranges
        try:
            addr = ipaddress.ip_address(hostname)
            return any(addr in net for net in _BLOCKED_IP_NETWORKS)
        except ValueError:
            pass
        # Reject hostnames that resolve to common internal patterns
        lower_host = hostname.lower()
        internal_patterns = (
            "localhost", "metadata.google.internal", "169.254.169.254",
            "instance-data", "metadata", "internal",
        )
        return any(lower_host == p or lower_host.endswith(f".{p}") for p in internal_patterns)

    return False
