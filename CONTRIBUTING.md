# Contributing to MarkSentry

Thanks for considering a contribution. MarkSentry exists because existing document converters skip security and layout correctness — contributions that add coverage for a new attack class, a new PII pattern, or a new document quirk are exactly what this project needs.

---

## Getting Set Up

```bash
git clone https://github.com/sunilgentyala/marksentry
cd marksentry
pip install -e ".[dev]"
```

Run the checks before and after your change:

```bash
pytest
mypy marksentry/
ruff check marksentry/
```

---

## Where to Start

Check the [issues](https://github.com/sunilgentyala/marksentry/issues) labeled `good first issue` or `help wanted`. Standing areas of interest:

- **New PII pattern categories** in `marksentry/core/pii_filter.py` — passport numbers, IBAN, driver's license formats, non-US phone formats
- **Additional parsers** — PPTX, RTF, or EPUB support alongside the existing PDF/DOCX/ZIP parsers
- **OCR accuracy** for scanned PDFs (the `[ocr]` extra) — better preprocessing before Tesseract
- **Multi-column edge cases** in `marksentry/core/layout.py` — 3+ column layouts, mixed single/multi-column pages, rotated text blocks
- **MCP tool additions** in `marksentry_mcp.py` — e.g. a `batch_convert` tool, or a `diff_conversion` tool that shows what the sanitizer stripped
- **Benchmark corpus** — a small `tests/fixtures/` set of real-world PDFs/DOCX with known-correct expected Markdown, for regression testing beyond unit tests

---

## What Makes a Good PR

- One parser, filter, or tool per PR
- New PII patterns or security checks ship with a test in `tests/`
- If you touch `sanitizer.py`, explain in the PR description which threat (path traversal, SSRF, macro, zip-bomb) the change affects
- `pytest`, `mypy marksentry/`, and `ruff check marksentry/` all pass clean

---

## What Not to Send

- Features that call out to a cloud API by default — MarkSentry's entire premise is 100% local execution; anything network-facing must be strictly opt-in and clearly documented
- New PII categories without at least one true-positive and one false-positive test case
- Large formatting-only diffs mixed with functional changes (keep them separate PRs)

---

## Questions

Open an issue on GitHub.
