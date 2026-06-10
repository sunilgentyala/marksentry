"""Abstract base class for all MarkSentry document parsers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ConversionOptions:
    """Shared options passed to every parser."""
    mask_pii: bool = False
    pii_patterns: list[str] | None = None       # None = all patterns
    detect_tables: bool = True
    detect_math: bool = True
    multi_column: bool = True
    min_column_gap: float = 18.0
    include_page_breaks: bool = False
    heading_size_threshold: float = 1.2         # font-size ratio to body text for heading detection


@dataclass
class ConversionResult:
    """Output from a parser after converting a document."""
    markdown: str
    source_path: Path
    page_count: int = 0
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


class BaseParser(ABC):
    """
    All parsers must implement ``can_handle`` and ``convert``.

    Parsers should not call the sanitizer themselves; the caller (main.py)
    runs sanitization before dispatching to a parser.
    """

    @classmethod
    @abstractmethod
    def can_handle(cls, path: Path) -> bool:
        """Return True if this parser knows how to process ``path``."""

    @abstractmethod
    def convert(self, path: Path, options: ConversionOptions) -> ConversionResult:
        """Convert ``path`` to Markdown and return a ConversionResult."""
