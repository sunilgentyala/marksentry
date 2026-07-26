"""
MarkSentry MCP Server

Exposes MarkSentry's document-to-Markdown conversion as MCP tools so Claude Code
can convert PDFs, DOCX files, and images to clean Markdown without reading raw
binary content — saving significant tokens on every document task.

Tools
-----
convert_to_markdown   — primary tool: PDF/DOCX/ZIP → clean Markdown
audit_pii             — dry-run PII scan without full output
document_info         — metadata only (type, size, macros, SSRF flags)
"""

from __future__ import annotations

import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent))

from marksentry.core.pii_filter import audit_pii as _audit_pii
from marksentry.core.sanitizer import SanitizationError, sanitize
from marksentry.main import convert

mcp = FastMCP("MarkSentry")


@mcp.tool()
def convert_to_markdown(
    path: str,
    mask_pii: bool = False,
    include_page_breaks: bool = False,
) -> str:
    """
    Convert a PDF, DOCX, or ZIP file to clean Markdown text.

    Use this tool whenever the user provides a path to a PDF, DOCX, or ZIP file,
    or when they paste or attach a document. It is far more token-efficient than
    reading the raw file — the output is clean, structured Markdown ready for
    analysis or summarisation.

    Parameters
    ----------
    path            Absolute or relative path to the document.
    mask_pii        Replace detected PII (emails, SSNs, API keys) with placeholders.
    include_page_breaks  Emit '---' between pages in PDF output.
    """
    try:
        result = convert(
            path,
            mask_pii=mask_pii,
            include_page_breaks=include_page_breaks,
        )
        md = result.markdown
        if result.warnings:
            md += "\n\n<!-- MarkSentry warnings: " + "; ".join(result.warnings) + " -->"
        return md
    except SanitizationError as exc:
        return f"[MarkSentry security error: {exc}]"
    except Exception as exc:
        return f"[MarkSentry error: {exc}]"


@mcp.tool()
def audit_pii(path: str) -> str:
    """
    Dry-run PII scan of a document without producing full Markdown output.

    Returns a summary of detected PII pattern types (emails, SSNs, private keys,
    etc.) and hit counts. Use before convert_to_markdown when you want to check
    whether a document contains sensitive data before sharing the full text.
    """
    try:
        result = convert(path, mask_pii=False)
        found = _audit_pii(result.markdown)
        if not found:
            return "No PII detected."
        lines = ["PII detected:"]
        for pattern, matches in found.items():
            sample = matches[0][:60] + ("..." if len(matches[0]) > 60 else "")
            lines.append(f"  {pattern}: {len(matches)} match(es) — e.g. {sample!r}")
        return "\n".join(lines)
    except SanitizationError as exc:
        return f"[MarkSentry security error: {exc}]"
    except Exception as exc:
        return f"[MarkSentry error: {exc}]"


@mcp.tool()
def document_info(path: str) -> str:
    """
    Return metadata about a document without performing full conversion.

    Reports file type, size, whether macros were stripped, and any SSRF-risk URLs
    found in embedded content. Use this as a quick pre-flight check before
    converting large documents.
    """
    try:
        src = Path(path)
        san = sanitize(src)
        lines = [
            f"File:              {src.name}",
            f"Detected type:     {san.detected_type}",
            f"Size:              {src.stat().st_size:,} bytes",
            f"Macros stripped:   {san.macros_stripped}",
            f"SSRF URLs blocked: {len(san.ssrf_urls_found)}",
        ]
        if san.warnings:
            lines.append("Warnings:          " + "; ".join(san.warnings))
        return "\n".join(lines)
    except SanitizationError as exc:
        return f"[Security error: {exc}]"
    except Exception as exc:
        return f"[Error: {exc}]"


if __name__ == "__main__":
    mcp.run()
