"""
ZIP Archive Parser.

Recursively unpacks a ZIP archive into a temporary directory, dispatches
each member to the appropriate parser, and concatenates results with
section headings derived from the original member filenames.

Security notes:
  - All member paths are validated against the extraction root before
    writing (second path-traversal fence after sanitizer.py).
  - The temp directory is deleted unconditionally via a context manager.
  - Nested ZIPs are recursed at most MAX_NEST_DEPTH levels.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

from marksentry.parsers.base import BaseParser, ConversionOptions, ConversionResult

logger = logging.getLogger(__name__)

MAX_NEST_DEPTH: int = 2


class ZipParser(BaseParser):

    @classmethod
    def can_handle(cls, path: Path) -> bool:
        return path.suffix.lower() == ".zip" and zipfile.is_zipfile(path)

    def convert(self, path: Path, options: ConversionOptions) -> ConversionResult:
        return self._convert_inner(path, options, depth=0)

    def _convert_inner(
        self, path: Path, options: ConversionOptions, depth: int
    ) -> ConversionResult:
        from marksentry.parsers import get_parser  # avoid circular import at module level

        all_parts: list[str] = []
        all_warnings: list[str] = []
        total_pages = 0

        tmp = tempfile.mkdtemp(prefix="marksentry_zip_")
        try:
            with zipfile.ZipFile(path, "r") as zf:
                # Safe extraction: validate each member path
                for member in zf.infolist():
                    member_path = Path(tmp) / member.filename
                    try:
                        member_path.resolve().relative_to(Path(tmp).resolve())
                    except ValueError:
                        all_warnings.append(
                            f"Skipped ZIP member with path traversal: '{member.filename}'"
                        )
                        continue
                    zf.extract(member, tmp)

            for file in sorted(Path(tmp).rglob("*")):
                if not file.is_file():
                    continue

                rel = file.relative_to(tmp)

                if file.suffix.lower() == ".zip" and depth < MAX_NEST_DEPTH:
                    nested = self._convert_inner(file, options, depth=depth + 1)
                    if nested.markdown.strip():
                        all_parts.append(f"## Archive: {rel}\n\n{nested.markdown}")
                    all_warnings.extend(nested.warnings)
                    total_pages += nested.page_count
                    continue

                parser = get_parser(file)
                if parser is None:
                    logger.debug("No parser for ZIP member: %s", rel)
                    continue

                try:
                    result = parser.convert(file, options)
                    if result.markdown.strip():
                        all_parts.append(f"## {rel}\n\n{result.markdown}")
                    all_warnings.extend(result.warnings)
                    total_pages += result.page_count
                except Exception as exc:
                    all_warnings.append(f"Failed to parse '{rel}': {exc}")

        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        markdown = "\n\n---\n\n".join(all_parts)
        return ConversionResult(
            markdown=markdown,
            source_path=path,
            page_count=total_pages,
            warnings=all_warnings,
        )
