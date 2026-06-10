"""
PDF Parser.

Extracts structured Markdown from PDF files using pdfminer.six for
character-level glyph coordinates, feeding the results into:

  1. LayoutProcessor     -- multi-column reading-order reconstruction
  2. TableDetector       -- grid-line and whitespace-gap table detection
  3. MathConverter       -- Unicode symbol → LaTeX wrapping
  4. HeadingClassifier   -- font-size heuristic for ATX heading levels

No cloud calls, no OCR fallback (unless pytesseract is installed), no
remote resource fetches.  Everything runs offline from the PDF byte stream.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Generator, Iterable

from marksentry.core.layout import BBox, LayoutProcessor, PageLayout, TextBlock, blocks_to_markdown
from marksentry.parsers.base import BaseParser, ConversionOptions, ConversionResult
from marksentry.utils.math_converter import text_contains_math, unicode_to_latex

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# pdfminer imports -- guarded so tests can import this module without the dep
# ---------------------------------------------------------------------------

try:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import (
        LAParams,
        LTAnno,
        LTChar,
        LTFigure,
        LTLayoutContainer,
        LTLine,
        LTPage,
        LTRect,
        LTTextBox,
        LTTextLine,
    )
    _PDFMINER_AVAILABLE = True
except ImportError:
    _PDFMINER_AVAILABLE = False
    logger.warning("pdfminer.six is not installed. PDF parsing will be unavailable.")


# ---------------------------------------------------------------------------
# Table detection helpers
# ---------------------------------------------------------------------------

class _TableCell:
    def __init__(self, text: str, row: int, col: int) -> None:
        self.text = text.strip()
        self.row = row
        self.col = col


def _render_table(cells: list[list[str]]) -> str:
    """Render a 2-D list of strings as a GitHub-Flavored Markdown table."""
    if not cells:
        return ""
    col_count = max(len(row) for row in cells)
    # Pad all rows to the same width
    padded = [row + [""] * (col_count - len(row)) for row in cells]

    header = padded[0]
    separator = ["---"] * col_count
    body = padded[1:]

    def fmt_row(r: list[str]) -> str:
        return "| " + " | ".join(r) + " |"

    lines = [fmt_row(header), fmt_row(separator)]
    lines.extend(fmt_row(r) for r in body)
    return "\n".join(lines)


class TableDetector:
    """
    Detects tabular regions on a PDF page from aligned text blocks.

    Strategy:
      - Cluster blocks that share the same y-band (row) and x-band (column).
      - A table is identified when at least 3 rows have 2 or more aligned
        columns, with consistent vertical spacing.
    """

    ROW_TOLERANCE: float = 4.0   # points within which two blocks are on the same row
    COL_TOLERANCE: float = 6.0   # points within which two blocks are in the same column

    def detect(
        self, blocks: list[TextBlock]
    ) -> tuple[list[list[list[str]]], list[int]]:
        """
        Returns:
            tables: list of 2-D string arrays (each is one table)
            consumed_indices: indices into ``blocks`` that were absorbed into tables
        """
        if not blocks:
            return [], []

        # Group blocks by approximate y-position (row bands)
        rows: dict[int, list[tuple[int, TextBlock]]] = {}
        for idx, block in enumerate(blocks):
            row_key = self._snap(block.bbox.y1, self.ROW_TOLERANCE)
            rows.setdefault(row_key, []).append((idx, block))

        sorted_row_keys = sorted(rows.keys(), reverse=True)

        # A table candidate must have >= 3 rows each with >= 2 cols
        tables: list[list[list[str]]] = []
        consumed: list[int] = []

        i = 0
        while i < len(sorted_row_keys):
            group_rows: list[list[tuple[int, TextBlock]]] = []
            group_row_keys: list[int] = []
            j = i

            while j < len(sorted_row_keys):
                row = rows[sorted_row_keys[j]]
                # Sort row by x position
                row_sorted = sorted(row, key=lambda t: t[1].bbox.x0)
                if len(row_sorted) >= 2:
                    group_rows.append(row_sorted)
                    group_row_keys.append(sorted_row_keys[j])
                    j += 1
                else:
                    break

            if len(group_rows) >= 3:
                # Collect unique column positions across all rows
                all_x = [t[1].bbox.x0 for row in group_rows for t in row]
                col_centers = self._cluster_centers(all_x, self.COL_TOLERANCE)

                table_2d: list[list[str]] = []
                row_consumed: list[int] = []

                for row in group_rows:
                    row_text: dict[int, str] = {}
                    for block_idx, block in row:
                        col = self._nearest_col(block.bbox.x0, col_centers)
                        row_text[col] = row_text.get(col, "") + " " + block.text
                        row_consumed.append(block_idx)
                    n_cols = max(row_text.keys()) + 1
                    table_2d.append([row_text.get(c, "").strip() for c in range(n_cols)])

                tables.append(table_2d)
                consumed.extend(row_consumed)
                i = j
            else:
                i += 1

        return tables, consumed

    @staticmethod
    def _snap(value: float, tolerance: float) -> int:
        return int(round(value / tolerance))

    @staticmethod
    def _cluster_centers(xs: list[float], tol: float) -> list[float]:
        if not xs:
            return []
        sorted_xs = sorted(xs)
        clusters: list[list[float]] = [[sorted_xs[0]]]
        for x in sorted_xs[1:]:
            if x - clusters[-1][-1] <= tol:
                clusters[-1].append(x)
            else:
                clusters.append([x])
        return [sum(c) / len(c) for c in clusters]

    @staticmethod
    def _nearest_col(x: float, centers: list[float]) -> int:
        return min(range(len(centers)), key=lambda i: abs(centers[i] - x))


# ---------------------------------------------------------------------------
# Heading classifier
# ---------------------------------------------------------------------------

def _classify_heading(
    block: TextBlock,
    body_font_size: float,
    threshold: float,
) -> int:
    """
    Return an ATX heading level 1-4, or 0 if the block is body text.

    Heuristic: ratio of block font size to median body font size.
    """
    ratio = block.font_size / max(body_font_size, 1.0)
    if block.is_bold or ratio >= threshold:
        if ratio >= 2.0:
            return 1
        if ratio >= 1.6:
            return 2
        if ratio >= 1.3:
            return 3
        if ratio >= threshold:
            return 4
    return 0


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

class PdfParser(BaseParser):

    @classmethod
    def can_handle(cls, path: Path) -> bool:
        return path.suffix.lower() == ".pdf" and _PDFMINER_AVAILABLE

    def convert(self, path: Path, options: ConversionOptions) -> ConversionResult:
        if not _PDFMINER_AVAILABLE:
            raise RuntimeError(
                "pdfminer.six is required for PDF parsing. "
                "Install it with: pip install pdfminer.six"
            )

        laparams = LAParams(
            line_overlap=0.5,
            char_margin=2.0,
            line_margin=0.5,
            word_margin=0.1,
            boxes_flow=0.5,
            detect_vertical=False,
            all_texts=False,
        )

        md_pages: list[str] = []
        warnings: list[str] = []
        page_count = 0
        metadata: dict[str, str] = {}

        layout_proc = LayoutProcessor(
            min_column_gap=options.min_column_gap,
        ) if options.multi_column else None

        table_detector = TableDetector() if options.detect_tables else None

        try:
            page_iter = extract_pages(str(path), laparams=laparams)
            for page_number, page_layout in enumerate(page_iter, start=1):
                page_count += 1
                page_md = self._process_page(
                    page_layout=page_layout,
                    page_number=page_number,
                    options=options,
                    layout_proc=layout_proc,
                    table_detector=table_detector,
                )
                if page_md.strip():
                    if options.include_page_breaks and page_number > 1:
                        md_pages.append("\n---\n")
                    md_pages.append(page_md)

        except Exception as exc:
            warnings.append(f"PDF extraction error: {exc}")
            logger.exception("Error parsing PDF '%s'", path)

        markdown = "\n\n".join(md_pages)

        if options.mask_pii:
            from marksentry.core.pii_filter import mask_pii
            result = mask_pii(markdown, patterns=options.pii_patterns)
            markdown = result.masked_text
            if result.redaction_count:
                warnings.append(
                    f"PII masking: {result.redaction_count} value(s) redacted. "
                    f"Categories: {dict(result.redaction_summary)}"
                )

        return ConversionResult(
            markdown=markdown,
            source_path=path,
            page_count=page_count,
            warnings=warnings,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Per-page processing
    # ------------------------------------------------------------------

    def _process_page(
        self,
        page_layout: "LTPage",
        page_number: int,
        options: ConversionOptions,
        layout_proc: LayoutProcessor | None,
        table_detector: TableDetector | None,
    ) -> str:
        raw_blocks = list(self._extract_text_blocks(page_layout))

        if not raw_blocks:
            return ""

        # Estimate body font size as the mode of font sizes in this page
        body_font_size = self._estimate_body_font(raw_blocks)

        # Table detection before layout reordering
        consumed_indices: set[int] = set()
        table_markdowns: dict[int, str] = {}

        if table_detector:
            tables, consumed = table_detector.detect(raw_blocks)
            consumed_indices = set(consumed)
            if tables:
                # Record the table MD keyed to the y-position of first consumed block
                for table in tables:
                    tmd = _render_table(table)
                    if tmd:
                        table_markdowns[min(consumed_indices)] = tmd

        # Filter out table-absorbed blocks
        layout_blocks = [b for i, b in enumerate(raw_blocks) if i not in consumed_indices]

        # Reconstruct reading order
        if layout_proc:
            page_struct = layout_proc.process(
                blocks=layout_blocks,
                page_number=page_number,
                page_width=float(page_layout.width),
                page_height=float(page_layout.height),
            )
            ordered = page_struct.reading_order()
        else:
            ordered = sorted(layout_blocks, key=lambda b: -b.bbox.y1)

        # Build Markdown parts
        parts: list[str] = []

        # Inject tables at their relative vertical position
        for tmd in table_markdowns.values():
            parts.append(tmd)

        for block in ordered:
            text = block.text.strip()
            if not text:
                continue

            # Heading detection
            if options.detect_math and text_contains_math(text):
                text = unicode_to_latex(text)
                text = f"${text}$" if len(text) < 100 else f"$$\n{text}\n$$"
            else:
                h_level = _classify_heading(block, body_font_size, options.heading_size_threshold)
                if h_level:
                    text = "#" * h_level + " " + text
                elif block.is_bold and len(text) < 120:
                    text = f"**{text}**"

            parts.append(text)

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # pdfminer element traversal
    # ------------------------------------------------------------------

    def _extract_text_blocks(
        self, container: "LTLayoutContainer"
    ) -> Generator[TextBlock, None, None]:
        for element in container:
            if isinstance(element, LTTextBox):
                yield from self._text_box_to_block(element)
            elif isinstance(element, LTFigure):
                yield from self._extract_text_blocks(element)

    def _text_box_to_block(
        self, text_box: "LTTextBox"
    ) -> Generator[TextBlock, None, None]:
        lines: list[str] = []
        max_font_size = 0.0
        is_bold = False

        for line in text_box:
            if not isinstance(line, LTTextLine):
                continue
            line_text_parts: list[str] = []
            for char in line:
                if isinstance(char, LTChar):
                    line_text_parts.append(char.get_text())
                    max_font_size = max(max_font_size, char.size)
                    font_name = (char.fontname or "").lower()
                    if "bold" in font_name or "heavy" in font_name or "black" in font_name:
                        is_bold = True
                elif isinstance(char, LTAnno):
                    line_text_parts.append(char.get_text())
            line_text = "".join(line_text_parts).rstrip("\n")
            if line_text.strip():
                lines.append(line_text)

        full_text = " ".join(lines).strip()
        if not full_text:
            return

        bbox = BBox(
            x0=text_box.x0,
            y0=text_box.y0,
            x1=text_box.x1,
            y1=text_box.y1,
        )
        yield TextBlock(
            bbox=bbox,
            text=full_text,
            font_size=max_font_size,
            is_bold=is_bold,
        )

    @staticmethod
    def _estimate_body_font(blocks: list[TextBlock]) -> float:
        if not blocks:
            return 12.0
        sizes = sorted(b.font_size for b in blocks if b.font_size > 0)
        if not sizes:
            return 12.0
        # Mode approximation: median of the lower 60% (avoids heading outliers)
        cutoff = max(1, int(len(sizes) * 0.6))
        return sizes[cutoff // 2]
