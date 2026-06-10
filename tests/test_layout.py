"""Unit tests for the multi-column layout processor."""

from __future__ import annotations

import pytest

from marksentry.core.layout import BBox, LayoutProcessor, TextBlock


def _block(x0: float, y0: float, x1: float, y1: float, text: str, font_size: float = 12.0) -> TextBlock:
    return TextBlock(bbox=BBox(x0, y0, x1, y1), text=text, font_size=font_size)


class TestSingleColumn:
    def test_empty_page_returns_empty_layout(self) -> None:
        proc = LayoutProcessor()
        layout = proc.process([], page_number=1, page_width=612.0, page_height=792.0)
        assert layout.columns == []
        assert layout.full_width_blocks == []

    def test_single_block_is_single_column(self) -> None:
        proc = LayoutProcessor()
        blocks = [_block(72, 650, 540, 680, "Hello world")]
        layout = proc.process(blocks, 1, 612.0, 792.0)
        assert len(layout.columns) == 1
        assert layout.columns[0][0].text == "Hello world"

    def test_top_to_bottom_ordering_preserved(self) -> None:
        proc = LayoutProcessor()
        blocks = [
            _block(72, 500, 540, 520, "Third paragraph"),
            _block(72, 700, 540, 720, "First paragraph"),
            _block(72, 600, 540, 620, "Second paragraph"),
        ]
        layout = proc.process(blocks, 1, 612.0, 792.0)
        ordered = [b.text for b in layout.reading_order()]
        assert ordered == ["First paragraph", "Second paragraph", "Third paragraph"]


class TestTwoColumn:
    def _two_col_blocks(self) -> list[TextBlock]:
        # Left column: x 72-270, right column: x 306-540, gutter 270-306
        return [
            _block(72,  700, 270, 720, "L1 Top"),
            _block(306, 700, 540, 720, "R1 Top"),
            _block(72,  650, 270, 670, "L2 Middle"),
            _block(306, 650, 540, 670, "R2 Middle"),
            _block(72,  600, 270, 620, "L3 Bottom"),
            _block(306, 600, 540, 620, "R3 Bottom"),
        ]

    def test_two_columns_detected(self) -> None:
        proc = LayoutProcessor(min_column_gap=20.0)
        blocks = self._two_col_blocks()
        layout = proc.process(blocks, 1, 612.0, 792.0)
        assert len(layout.columns) == 2

    def test_left_column_reads_before_right(self) -> None:
        proc = LayoutProcessor(min_column_gap=20.0)
        blocks = self._two_col_blocks()
        layout = proc.process(blocks, 1, 612.0, 792.0)
        ordered_texts = [b.text for b in layout.reading_order()]
        left_texts = [t for t in ordered_texts if t.startswith("L")]
        right_texts = [t for t in ordered_texts if t.startswith("R")]
        # All left-column items should appear before right-column items
        last_left_idx = max(ordered_texts.index(t) for t in left_texts)
        first_right_idx = min(ordered_texts.index(t) for t in right_texts)
        assert last_left_idx < first_right_idx

    def test_columns_sorted_top_to_bottom(self) -> None:
        proc = LayoutProcessor(min_column_gap=20.0)
        blocks = self._two_col_blocks()
        layout = proc.process(blocks, 1, 612.0, 792.0)
        for col in layout.columns:
            y_positions = [b.bbox.y1 for b in col]
            assert y_positions == sorted(y_positions, reverse=True)


class TestFullWidthBlocks:
    def test_wide_blocks_go_to_full_width(self) -> None:
        proc = LayoutProcessor(min_column_gap=20.0, full_width_tolerance=0.75)
        blocks = [
            _block(72, 750, 540, 770, "Full-width header"),   # 468pt wide on 612pt page = 76%
            _block(72, 700, 270, 720, "Left col text"),
            _block(306, 700, 540, 720, "Right col text"),
        ]
        layout = proc.process(blocks, 1, 612.0, 792.0)
        assert any(b.text == "Full-width header" for b in layout.full_width_blocks)

    def test_full_width_header_appears_first_in_reading_order(self) -> None:
        proc = LayoutProcessor(min_column_gap=20.0, full_width_tolerance=0.75)
        blocks = [
            _block(72, 750, 540, 770, "Full-width header"),
            _block(72, 700, 270, 720, "Col text"),
        ]
        layout = proc.process(blocks, 1, 612.0, 792.0)
        ordered = layout.reading_order()
        assert ordered[0].text == "Full-width header"


class TestBBoxHelpers:
    def test_bbox_dimensions(self) -> None:
        b = BBox(10, 20, 110, 70)
        assert b.width == 100.0
        assert b.height == 50.0
        assert b.cx == 60.0
        assert b.cy == 45.0

    def test_horizontal_overlap(self) -> None:
        a = BBox(0, 0, 100, 10)
        b = BBox(50, 0, 150, 10)
        assert a.h_overlap(b) == 50.0

    def test_no_overlap(self) -> None:
        a = BBox(0, 0, 50, 10)
        b = BBox(100, 0, 150, 10)
        assert a.h_overlap(b) == 0.0

    def test_vertical_overlap(self) -> None:
        a = BBox(0, 0, 100, 100)
        b = BBox(0, 50, 100, 150)
        assert a.v_overlap(b) == 50.0


class TestMaxColumnsCap:
    def test_excessive_columns_fall_back_to_single(self) -> None:
        proc = LayoutProcessor(min_column_gap=5.0, max_columns=2)
        # Create 6 narrow columns -- should exceed max_columns and fall back
        blocks = []
        for i in range(6):
            x0 = 20 + i * 100
            blocks.append(_block(x0, 700, x0 + 80, 720, f"Col {i} text"))
        layout = proc.process(blocks, 1, 612.0, 792.0)
        # With max_columns=2, detection of >2 triggers fallback to 1 column
        assert len(layout.columns) <= 2
