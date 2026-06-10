"""
Multi-Column Layout Processor.

Detects vertical column bands in a page's text-block stream and re-orders
blocks into natural reading sequence: left column top-to-bottom, then next
column, etc.  Handles:

  - 1, 2, 3-column academic/journal layouts
  - Full-width header / footer regions that span all columns
  - Figures and captions that span multiple columns
  - Irregular column widths and gutters

The algorithm is purely geometric (no ML): it finds vertical whitespace gaps
wider than a configurable gutter threshold, treats each gap as a column
separator, and assigns every text block to a column bucket.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BBox:
    """Axis-aligned bounding box in PDF points (origin = bottom-left)."""
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    def h_overlap(self, other: "BBox") -> float:
        return max(0.0, min(self.x1, other.x1) - max(self.x0, other.x0))

    def v_overlap(self, other: "BBox") -> float:
        return max(0.0, min(self.y1, other.y1) - max(self.y0, other.y0))


@dataclass
class TextBlock:
    """A contiguous run of text extracted from a single page."""
    bbox: BBox
    text: str
    font_size: float = 12.0
    is_bold: bool = False
    column_index: int = -1   # assigned by LayoutProcessor


@dataclass
class PageLayout:
    """Fully ordered text blocks for a single page after layout analysis."""
    page_number: int
    page_width: float
    page_height: float
    columns: list[list[TextBlock]] = field(default_factory=list)  # outer=col, inner=blocks top→bottom
    full_width_blocks: list[TextBlock] = field(default_factory=list)

    def reading_order(self) -> list[TextBlock]:
        """
        Flatten columns into a single reading-order list.

        Full-width blocks (headers, footers, figures spanning columns) are
        interleaved at their vertical position relative to column content.
        """
        all_blocks: list[TextBlock] = []

        if not self.columns:
            return sorted(self.full_width_blocks, key=lambda b: -b.bbox.y1)

        # Determine the vertical band each full-width block occupies
        col_content: list[TextBlock] = []
        for col in self.columns:
            col_content.extend(col)

        ordered_full_width = sorted(self.full_width_blocks, key=lambda b: -b.bbox.y1)

        # Interleave: insert full-width blocks above the first column block
        # they are positioned above, maintaining PDF top-to-bottom flow.
        fw_above: list[TextBlock] = []
        fw_between: list[TextBlock] = []
        fw_below: list[TextBlock] = []

        if col_content:
            top_of_columns = max(b.bbox.y1 for b in col_content)
            bottom_of_columns = min(b.bbox.y0 for b in col_content)

            for fw in ordered_full_width:
                if fw.bbox.y0 >= top_of_columns:
                    fw_above.append(fw)
                elif fw.bbox.y1 <= bottom_of_columns:
                    fw_below.append(fw)
                else:
                    fw_between.append(fw)

            all_blocks.extend(fw_above)
            for col in self.columns:
                all_blocks.extend(col)
            all_blocks.extend(fw_between)
            all_blocks.extend(fw_below)
        else:
            all_blocks.extend(ordered_full_width)

        return all_blocks


# ---------------------------------------------------------------------------
# Layout Processor
# ---------------------------------------------------------------------------

class LayoutProcessor:
    """
    Analyse a page's raw text blocks and return a structured PageLayout.

    Parameters
    ----------
    min_column_gap:
        Minimum horizontal whitespace (in points) to be considered a column
        separator.  Typical journal gutters are 12-24 pt.
    full_width_tolerance:
        A block whose width exceeds ``page_width * full_width_tolerance`` is
        treated as spanning all columns regardless of x-position.
    max_columns:
        Safety cap; if the algorithm detects more separators than this it
        falls back to single-column ordering.
    """

    def __init__(
        self,
        min_column_gap: float = 18.0,
        full_width_tolerance: float = 0.75,
        max_columns: int = 4,
    ) -> None:
        self.min_column_gap = min_column_gap
        self.full_width_tolerance = full_width_tolerance
        self.max_columns = max_columns

    def process(
        self,
        blocks: Sequence[TextBlock],
        page_number: int,
        page_width: float,
        page_height: float,
    ) -> PageLayout:
        layout = PageLayout(
            page_number=page_number,
            page_width=page_width,
            page_height=page_height,
        )

        if not blocks:
            return layout

        full_width_threshold = page_width * self.full_width_tolerance
        regular_blocks: list[TextBlock] = []

        for block in blocks:
            if block.bbox.width >= full_width_threshold:
                layout.full_width_blocks.append(block)
            else:
                regular_blocks.append(block)

        if not regular_blocks:
            layout.full_width_blocks.sort(key=lambda b: -b.bbox.y1)
            return layout

        column_bounds = self._detect_columns(regular_blocks, page_width)

        if len(column_bounds) < 2:
            # Single column or column detection ambiguous — sort top to bottom
            layout.columns = [sorted(regular_blocks, key=lambda b: -b.bbox.y1)]
        else:
            layout.columns = self._assign_to_columns(regular_blocks, column_bounds)

        logger.debug(
            "Page %d: %d column(s) detected, %d full-width blocks",
            page_number, len(layout.columns), len(layout.full_width_blocks),
        )
        return layout

    # ------------------------------------------------------------------
    # Column detection
    # ------------------------------------------------------------------

    def _detect_columns(
        self, blocks: list[TextBlock], page_width: float
    ) -> list[tuple[float, float]]:
        """
        Return a list of (x_start, x_end) column bands.

        Strategy: build a 1-px-resolution coverage histogram over the page
        width, find contiguous uncovered gaps wider than ``min_column_gap``,
        and use the gaps as column separators.
        """
        resolution = 1.0  # points per bin
        n_bins = max(1, int(page_width / resolution) + 1)
        coverage = [False] * n_bins

        for block in blocks:
            lo = max(0, int(block.bbox.x0 / resolution))
            hi = min(n_bins - 1, int(block.bbox.x1 / resolution))
            for i in range(lo, hi + 1):
                coverage[i] = True

        # Collect gap intervals
        gaps: list[tuple[float, float]] = []
        in_gap = not coverage[0]
        gap_start = 0

        for i, covered in enumerate(coverage):
            if covered and in_gap:
                gap_width = (i - gap_start) * resolution
                if gap_width >= self.min_column_gap:
                    gaps.append((gap_start * resolution, i * resolution))
                in_gap = False
            elif not covered and not in_gap:
                gap_start = i
                in_gap = True

        # Handle trailing gap
        if in_gap:
            gap_width = (n_bins - gap_start) * resolution
            if gap_width >= self.min_column_gap:
                gaps.append((gap_start * resolution, n_bins * resolution))

        if not gaps:
            return [(0.0, page_width)]

        # Convert gaps to column bands
        column_starts = [0.0] + [g[1] for g in gaps]
        column_ends = [g[0] for g in gaps] + [page_width]
        columns = list(zip(column_starts, column_ends))

        if len(columns) > self.max_columns:
            logger.debug(
                "Detected %d potential columns > max %d; falling back to single-column.",
                len(columns), self.max_columns,
            )
            return [(0.0, page_width)]

        return columns

    # ------------------------------------------------------------------
    # Block assignment
    # ------------------------------------------------------------------

    def _assign_to_columns(
        self,
        blocks: list[TextBlock],
        column_bounds: list[tuple[float, float]],
    ) -> list[list[TextBlock]]:
        """
        Assign each block to the column whose x-range it overlaps most,
        then sort each column's blocks from top to bottom (descending y).
        """
        buckets: list[list[TextBlock]] = [[] for _ in column_bounds]

        for block in blocks:
            best_col = 0
            best_overlap = -1.0

            for col_idx, (cx0, cx1) in enumerate(column_bounds):
                col_bbox = BBox(cx0, 0.0, cx1, 1.0)
                overlap = max(
                    0.0,
                    min(block.bbox.x1, cx1) - max(block.bbox.x0, cx0),
                )
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_col = col_idx

            block.column_index = best_col
            buckets[best_col].append(block)

        # Sort each column top → bottom (PDF y-axis increases upward)
        for col in buckets:
            col.sort(key=lambda b: -b.bbox.y1)

        # Remove empty columns (can occur with aggressive gap detection)
        return [col for col in buckets if col]


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def blocks_to_markdown(layout: PageLayout, *, join_paragraphs: bool = True) -> str:
    """
    Convert an ordered PageLayout into a Markdown string for this page.

    Adjacent blocks within the same column are joined into paragraphs.
    Blocks separated by vertical whitespace larger than 1.5x font size get
    an extra blank line (paragraph break).
    """
    ordered = layout.reading_order()
    if not ordered:
        return ""

    parts: list[str] = []
    prev_block: TextBlock | None = None

    for block in ordered:
        text = block.text.strip()
        if not text:
            continue

        if prev_block is not None and join_paragraphs:
            # Compute vertical gap between this block and the previous one
            v_gap = prev_block.bbox.y0 - block.bbox.y1
            line_height = max(block.font_size, prev_block.font_size) * 1.5
            if v_gap > line_height or block.column_index != prev_block.column_index:
                parts.append("")  # blank line = paragraph separator

        if block.is_bold and len(text) < 120:
            parts.append(f"**{text}**")
        else:
            parts.append(text)

        prev_block = block

    return "\n".join(parts)
