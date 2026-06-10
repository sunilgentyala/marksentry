"""
MarkSentry -- CLI and programmatic SDK entry point.

CLI usage
---------
  marksentry convert document.pdf
  marksentry convert report.docx --mask-pii --output report.md
  marksentry convert archive.zip --no-multi-column --output-dir ./out/
  marksentry audit-pii document.pdf          # dry-run PII scan
  marksentry info document.pdf               # metadata only

Programmatic SDK
----------------
  from marksentry import convert
  result = convert("report.pdf", mask_pii=True)
  print(result.markdown)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

from marksentry.core.sanitizer import SanitizationError, sanitize
from marksentry.parsers import ConversionOptions, ConversionResult, get_parser

console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Public SDK function
# ---------------------------------------------------------------------------

def convert(
    path: str | Path,
    *,
    mask_pii: bool = False,
    pii_patterns: list[str] | None = None,
    detect_tables: bool = True,
    detect_math: bool = True,
    multi_column: bool = True,
    min_column_gap: float = 18.0,
    include_page_breaks: bool = False,
    heading_size_threshold: float = 1.2,
    allowed_base: Path | None = None,
    strip_macros: bool = True,
    check_ssrf: bool = True,
) -> ConversionResult:
    """
    Convert a document to Markdown.

    Parameters
    ----------
    path:
        Path to a PDF, DOCX, or ZIP file.
    mask_pii:
        Replace detected PII (SSNs, emails, keys, etc.) with placeholders.
    pii_patterns:
        Restrict PII masking to these pattern names (e.g. ``["SSN", "EMAIL"]``).
        None = all patterns.
    detect_tables:
        Reconstruct tabular data as GFM pipe tables.
    detect_math:
        Convert Unicode math symbols and OMML equations to LaTeX.
    multi_column:
        Enable multi-column reading-order reconstruction for PDFs.
    min_column_gap:
        Minimum horizontal whitespace (pt) to treat as a column separator.
    include_page_breaks:
        Emit ``---`` separators between pages in PDF output.
    heading_size_threshold:
        Font-size ratio (vs. body text) above which text is classified as a heading.
    allowed_base:
        If set, restrict file access to this directory tree (path-jail).
    strip_macros:
        Remove VBA macro content from OOXML containers before parsing.
    check_ssrf:
        Scan embedded document URIs for SSRF-risk addresses.

    Returns
    -------
    ConversionResult with ``.markdown``, ``.warnings``, ``.metadata``.

    Raises
    ------
    SanitizationError  -- file fails security checks.
    RuntimeError       -- no parser available or parse failure.
    """
    san = sanitize(
        path,
        allowed_base=allowed_base,
        strip_macros=strip_macros,
        check_ssrf_urls=check_ssrf,
    )

    safe_path = san.safe_path
    parser = get_parser(safe_path)

    if parser is None:
        raise RuntimeError(
            f"No parser available for '{safe_path.name}' "
            f"(detected type: '{san.detected_type}'). "
            f"Supported extensions: pdf, docx, zip"
        )

    options = ConversionOptions(
        mask_pii=mask_pii,
        pii_patterns=pii_patterns,
        detect_tables=detect_tables,
        detect_math=detect_math,
        multi_column=multi_column,
        min_column_gap=min_column_gap,
        include_page_breaks=include_page_breaks,
        heading_size_threshold=heading_size_threshold,
    )

    result = parser.convert(safe_path, options)
    result.warnings.extend(san.warnings)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        format="%(levelname)s [%(name)s] %(message)s",
        level=level,
        stream=sys.stderr,
    )


@click.group(
    context_settings={"max_content_width": 100},
    help=(
        "MarkSentry -- secure, local-first document-to-Markdown conversion.\n\n"
        "Author: Sunil Gentyala  |  https://github.com/sunilgentyala/marksentry"
    ),
)
@click.version_option("1.0.0", prog_name="marksentry")
def cli() -> None:
    pass


# ------------------------------------------------------------------
# convert command
# ------------------------------------------------------------------

@cli.command(name="convert")
@click.argument("files", nargs=-1, type=click.Path(exists=True), required=True)
@click.option("-o", "--output", "output_path", default=None,
              help="Write output to this file (single-file input only).")
@click.option("--output-dir", default=None,
              help="Write each converted file to this directory.")
@click.option("--mask-pii", is_flag=True, default=False,
              help="Mask PII (SSNs, emails, private keys, etc.) before output.")
@click.option("--pii-patterns", default=None,
              help="Comma-separated PII pattern names to mask (default: all).")
@click.option("--no-tables", is_flag=True, default=False,
              help="Disable table reconstruction.")
@click.option("--no-math", is_flag=True, default=False,
              help="Disable LaTeX math conversion.")
@click.option("--no-multi-column", is_flag=True, default=False,
              help="Disable multi-column layout reconstruction.")
@click.option("--column-gap", default=18.0, show_default=True,
              help="Minimum horizontal gap (pt) to detect as a column separator.")
@click.option("--page-breaks", is_flag=True, default=False,
              help="Emit --- separators between PDF pages.")
@click.option("--no-strip-macros", is_flag=True, default=False,
              help="Skip macro stripping from OOXML files.")
@click.option("--no-ssrf-check", is_flag=True, default=False,
              help="Skip embedded-URL SSRF scanning.")
@click.option("--allowed-base", default=None,
              help="Restrict file access to this directory tree.")
@click.option("-v", "--verbose", is_flag=True, default=False)
def cmd_convert(
    files: tuple[str, ...],
    output_path: str | None,
    output_dir: str | None,
    mask_pii: bool,
    pii_patterns: str | None,
    no_tables: bool,
    no_math: bool,
    no_multi_column: bool,
    column_gap: float,
    page_breaks: bool,
    no_strip_macros: bool,
    no_ssrf_check: bool,
    allowed_base: str | None,
    verbose: bool,
) -> None:
    """Convert one or more documents to Markdown."""
    _setup_logging(verbose)

    if output_path and len(files) > 1:
        console.print("[red]--output cannot be used with multiple input files.[/red]")
        raise click.Abort()

    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)

    pii_list = [p.strip() for p in pii_patterns.split(",")] if pii_patterns else None
    base = Path(allowed_base) if allowed_base else None

    exit_code = 0

    for file_path in files:
        src = Path(file_path)
        dest: Path | None = None

        if output_path:
            dest = Path(output_path)
        elif output_dir:
            dest = Path(output_dir) / src.with_suffix(".md").name

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task(f"Converting {src.name}...", total=None)

            try:
                result = convert(
                    src,
                    mask_pii=mask_pii,
                    pii_patterns=pii_list,
                    detect_tables=not no_tables,
                    detect_math=not no_math,
                    multi_column=not no_multi_column,
                    min_column_gap=column_gap,
                    include_page_breaks=page_breaks,
                    allowed_base=base,
                    strip_macros=not no_strip_macros,
                    check_ssrf=not no_ssrf_check,
                )
                progress.update(task, completed=1)

            except SanitizationError as exc:
                progress.stop()
                console.print(f"[bold red]Security error:[/bold red] {exc}")
                exit_code = 2
                continue
            except Exception as exc:
                progress.stop()
                console.print(f"[bold red]Error converting '{src.name}':[/bold red] {exc}")
                if verbose:
                    console.print_exception()
                exit_code = 1
                continue

        # Output
        if dest:
            dest.write_text(result.markdown, encoding="utf-8")
            console.print(f"[green]Wrote[/green] {dest}  ({result.page_count} pages)")
        else:
            # Print to stdout so it can be piped
            click.echo(result.markdown)

        for warning in result.warnings:
            console.print(f"  [yellow]Warning:[/yellow] {warning}")

        if result.metadata:
            _print_metadata_table(result.metadata, src.name)

    sys.exit(exit_code)


# ------------------------------------------------------------------
# audit-pii command
# ------------------------------------------------------------------

@cli.command(name="audit-pii")
@click.argument("files", nargs=-1, type=click.Path(exists=True), required=True)
@click.option("-v", "--verbose", is_flag=True, default=False)
def cmd_audit_pii(files: tuple[str, ...], verbose: bool) -> None:
    """Scan documents for PII without modifying output (dry run)."""
    _setup_logging(verbose)

    from marksentry.core.pii_filter import audit_pii

    for file_path in files:
        src = Path(file_path)
        console.print(f"\n[bold]Auditing:[/bold] {src.name}")

        try:
            result = convert(src, mask_pii=False)
        except (SanitizationError, RuntimeError) as exc:
            console.print(f"  [red]Error:[/red] {exc}")
            continue

        found = audit_pii(result.markdown)
        if not found:
            console.print("  [green]No PII detected.[/green]")
        else:
            table = Table(title="PII Audit Report", show_lines=True)
            table.add_column("Pattern", style="bold red")
            table.add_column("Count", justify="right")
            table.add_column("Sample (truncated)")
            for pattern, matches in found.items():
                sample = matches[0][:40] + ("..." if len(matches[0]) > 40 else "")
                table.add_row(pattern, str(len(matches)), sample)
            console.print(table)


# ------------------------------------------------------------------
# info command
# ------------------------------------------------------------------

@cli.command(name="info")
@click.argument("files", nargs=-1, type=click.Path(exists=True), required=True)
def cmd_info(files: tuple[str, ...]) -> None:
    """Display document metadata without performing conversion."""
    from marksentry.core.sanitizer import sanitize

    for file_path in files:
        src = Path(file_path)
        try:
            san = sanitize(src)
        except SanitizationError as exc:
            console.print(f"[red]{src.name}: {exc}[/red]")
            continue

        table = Table(title=f"Info: {src.name}", show_header=False)
        table.add_column("Key", style="bold cyan", width=20)
        table.add_column("Value")
        table.add_row("Detected type", san.detected_type)
        table.add_row("Safe path", str(san.safe_path))
        table.add_row("Macros stripped", str(san.macros_stripped))
        table.add_row("SSRF URLs blocked", str(len(san.ssrf_urls_found)))
        table.add_row("File size", f"{src.stat().st_size:,} bytes")
        for w in san.warnings:
            table.add_row("Warning", w)
        console.print(table)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_metadata_table(metadata: dict[str, str], filename: str) -> None:
    if not metadata:
        return
    table = Table(title=f"Metadata: {filename}", show_header=False, box=None)
    table.add_column("Key", style="dim", width=18)
    table.add_column("Value")
    for k, v in metadata.items():
        table.add_row(k, v)
    console.print(table)


if __name__ == "__main__":
    cli()
