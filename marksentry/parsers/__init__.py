"""
Parser registry.

Parsers are tried in priority order.  The first parser whose ``can_handle``
method returns True is used.
"""

from __future__ import annotations

from pathlib import Path

from marksentry.parsers.base import BaseParser, ConversionOptions, ConversionResult
from marksentry.parsers.docx_parser import DocxParser
from marksentry.parsers.pdf_parser import PdfParser
from marksentry.parsers.zip_parser import ZipParser

_REGISTRY: list[type[BaseParser]] = [
    PdfParser,
    DocxParser,
    ZipParser,
]


def get_parser(path: Path) -> BaseParser | None:
    """Return an instantiated parser for ``path``, or None if unsupported."""
    for cls in _REGISTRY:
        if cls.can_handle(path):
            return cls()
    return None


__all__ = [
    "get_parser",
    "BaseParser",
    "ConversionOptions",
    "ConversionResult",
    "PdfParser",
    "DocxParser",
    "ZipParser",
]
