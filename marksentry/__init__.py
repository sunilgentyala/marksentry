"""
MarkSentry - Secure, local-first document-to-Markdown conversion.

Author: Sunil Gentyala <sunil.gentyala@ieee.org>
"""

from __future__ import annotations

__version__ = "1.0.0"
__author__ = "Sunil Gentyala"
__license__ = "MIT"

from marksentry.main import convert  # noqa: F401 — public SDK surface

__all__ = ["convert", "__version__"]
