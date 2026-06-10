"""
DOCX Parser.

Converts Word Open XML documents (.docx) to Markdown using python-docx for
paragraph/style traversal and lxml for OMML (Office Math Markup Language)
equation parsing.

Handles:
  - ATX headings derived from Word built-in styles (Heading 1–6)
  - Bold, italic, strikethrough inline runs
  - Numbered and bulleted lists with correct nesting
  - Hyperlinks
  - Tables (GFM pipe syntax)
  - Embedded equations via OMML → LaTeX conversion
  - Image alt-text / caption extraction (no binary export)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator

from marksentry.parsers.base import BaseParser, ConversionOptions, ConversionResult

logger = logging.getLogger(__name__)

try:
    import docx
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    from docx.text.run import Run
    _DOCX_AVAILABLE = True
except ImportError:
    _DOCX_AVAILABLE = False
    logger.warning("python-docx is not installed. DOCX parsing will be unavailable.")

try:
    from lxml import etree as ET
    _LXML_AVAILABLE = True
except ImportError:
    _LXML_AVAILABLE = False


# Heading style names used by Word (EN and localized variants handled by prefix)
_HEADING_PREFIXES: tuple[str, ...] = (
    "heading 1", "heading 2", "heading 3",
    "heading 4", "heading 5", "heading 6",
)

_LIST_PREFIXES: tuple[str, ...] = ("list paragraph", "list bullet", "list number")


class DocxParser(BaseParser):

    @classmethod
    def can_handle(cls, path: Path) -> bool:
        return path.suffix.lower() in (".docx", ".docm") and _DOCX_AVAILABLE

    def convert(self, path: Path, options: ConversionOptions) -> ConversionResult:
        if not _DOCX_AVAILABLE:
            raise RuntimeError(
                "python-docx is required for DOCX parsing. "
                "Install it with: pip install python-docx"
            )

        try:
            doc = docx.Document(str(path))
        except Exception as exc:
            raise RuntimeError(f"Failed to open '{path.name}': {exc}") from exc

        warnings: list[str] = []
        metadata = self._extract_metadata(doc)

        parts: list[str] = []
        list_state: _ListState = _ListState()

        for block in doc.element.body:
            tag = _local_tag(block.tag)

            if tag == "p":
                para = docx.text.paragraph.Paragraph(block, doc)
                md = self._render_paragraph(para, options, list_state)
                if md is not None:
                    parts.append(md)

            elif tag == "tbl":
                # Close any open list before a table
                parts.extend(list_state.flush())
                tbl = docx.table.Table(block, doc)
                parts.append(self._render_table(tbl))

            elif tag == "sdt":  # structured document tag (content control)
                # Recurse into sdt content
                for child in block:
                    if _local_tag(child.tag) == "sdtContent":
                        for inner in child:
                            if _local_tag(inner.tag) == "p":
                                para = docx.text.paragraph.Paragraph(inner, doc)
                                md = self._render_paragraph(para, options, list_state)
                                if md is not None:
                                    parts.append(md)

        parts.extend(list_state.flush())
        markdown = "\n\n".join(p for p in parts if p)

        if options.mask_pii:
            from marksentry.core.pii_filter import mask_pii
            result = mask_pii(markdown, patterns=options.pii_patterns)
            markdown = result.masked_text
            if result.redaction_count:
                warnings.append(
                    f"PII masking: {result.redaction_count} value(s) redacted."
                )

        return ConversionResult(
            markdown=markdown,
            source_path=path,
            page_count=0,  # page count not available without COM/win32
            warnings=warnings,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Paragraph rendering
    # ------------------------------------------------------------------

    def _render_paragraph(
        self,
        para: "Paragraph",
        options: ConversionOptions,
        list_state: "_ListState",
    ) -> str | None:
        style_name = (para.style.name or "").lower()

        # Equations embedded in a paragraph as OMML
        if options.detect_math and _LXML_AVAILABLE:
            math_latex = self._extract_omml(para._element)
            if math_latex:
                list_state.flush()
                display = len(math_latex) > 60 or "\\" in math_latex
                if display:
                    return f"$$\n{math_latex}\n$$"
                return f"${math_latex}$"

        # Heading styles
        for level, prefix in enumerate(_HEADING_PREFIXES, start=1):
            if style_name.startswith(prefix):
                list_state.flush()
                text = para.text.strip()
                return f"{'#' * level} {text}" if text else None

        # List paragraphs
        if any(style_name.startswith(p) for p in _LIST_PREFIXES):
            return list_state.add(para)

        # Flush any open list
        list_state.flush()

        # Normal paragraph: render runs with inline formatting
        inline = self._render_runs(para, options)
        return inline.strip() if inline.strip() else None

    # ------------------------------------------------------------------
    # Inline run rendering
    # ------------------------------------------------------------------

    def _render_runs(self, para: "Paragraph", options: ConversionOptions) -> str:
        parts: list[str] = []
        for run in para.runs:
            text = run.text
            if not text:
                continue

            # Convert Unicode math symbols even in non-math paragraphs
            if options.detect_math:
                from marksentry.utils.math_converter import text_contains_math, unicode_to_latex
                if text_contains_math(text):
                    text = f"${unicode_to_latex(text)}$"

            if run.bold and run.italic:
                text = f"***{text}***"
            elif run.bold:
                text = f"**{text}**"
            elif run.italic:
                text = f"*{text}*"
            elif run.underline:
                pass  # Markdown has no underline; preserve as-is
            if getattr(run, "strike", False) or getattr(run.font, "strike", False):
                text = f"~~{text}~~"

            parts.append(text)

        # Check for hyperlinks in the paragraph XML
        full = "".join(parts)
        full = self._inject_hyperlinks(para, full)
        return full

    def _inject_hyperlinks(self, para: "Paragraph", fallback: str) -> str:
        """Reconstruct [text](url) Markdown links from w:hyperlink elements."""
        links: list[tuple[str, str]] = []
        for hl in para._element.findall(f".//{qn('w:hyperlink')}"):
            r_ns = hl.nsmap.get("r", "")
            rel_id = hl.get(f"{{{r_ns}}}id") if hl.nsmap else None
            text_parts = [
                (node.text or "")
                for node in hl.iter(qn("w:t"))
            ]
            link_text = "".join(text_parts)
            if rel_id and link_text:
                try:
                    url = para.part.rels[rel_id].target_ref
                    links.append((link_text, url))
                except (KeyError, AttributeError):
                    pass

        result = fallback
        for link_text, url in links:
            escaped = re.escape(link_text)
            result = re.sub(escaped, f"[{link_text}]({url})", result, count=1)
        return result

    # ------------------------------------------------------------------
    # OMML → LaTeX extraction
    # ------------------------------------------------------------------

    def _extract_omml(self, para_element: "_Element") -> str:
        from marksentry.utils.math_converter import omml_to_latex
        OMML_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
        math_elements = para_element.findall(f"{{{OMML_NS}}}oMath")
        if not math_elements:
            return ""
        results = [omml_to_latex(m).strip() for m in math_elements]
        return " ".join(r for r in results if r)

    # ------------------------------------------------------------------
    # Table rendering
    # ------------------------------------------------------------------

    def _render_table(self, table: "Table") -> str:
        rows: list[list[str]] = []
        for row in table.rows:
            cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            rows.append(cells)

        if not rows:
            return ""

        from marksentry.parsers.pdf_parser import _render_table as _render
        return _render(rows)

    # ------------------------------------------------------------------
    # Metadata extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_metadata(doc: "docx.Document") -> dict[str, str]:
        props = doc.core_properties
        meta: dict[str, str] = {}
        for attr in ("author", "title", "subject", "description", "keywords", "created", "modified"):
            val = getattr(props, attr, None)
            if val:
                meta[attr] = str(val)
        return meta


# ---------------------------------------------------------------------------
# List state machine
# ---------------------------------------------------------------------------

class _ListState:
    """Tracks open list context to emit properly nested GFM lists."""

    def __init__(self) -> None:
        self._items: list[str] = []
        self._level: int = 0
        self._ordered: bool = False
        self._counters: dict[int, int] = {}

    def add(self, para: "Paragraph") -> str | None:
        """Buffer a list item and return None (caller collects via flush)."""
        style_name = (para.style.name or "").lower()
        ordered = "number" in style_name

        # Attempt to read indentation level from paragraph XML
        level = self._detect_level(para)

        indent = "  " * level
        if ordered:
            self._counters[level] = self._counters.get(level, 0) + 1
            prefix = f"{self._counters[level]}."
        else:
            prefix = "-"

        self._items.append(f"{indent}{prefix} {para.text.strip()}")
        return None

    def flush(self) -> list[str]:
        if not self._items:
            return []
        result = ["\n".join(self._items)]
        self._items.clear()
        self._counters.clear()
        return result

    @staticmethod
    def _detect_level(para: "Paragraph") -> int:
        try:
            pPr = para._element.find(qn("w:pPr"))
            if pPr is None:
                return 0
            numPr = pPr.find(qn("w:numPr"))
            if numPr is None:
                return 0
            ilvl = numPr.find(qn("w:ilvl"))
            if ilvl is None:
                return 0
            return int(ilvl.get(qn("w:val"), "0"))
        except (AttributeError, TypeError, ValueError):
            return 0


def _local_tag(tag: str) -> str:
    """Strip namespace URI and return the local element name."""
    return tag.split("}")[-1] if "}" in tag else tag
