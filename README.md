# MarkSentry

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/badge/execution-100%25%20local-brightgreen" alt="Local-first">
  <img src="https://img.shields.io/badge/MCP-Claude%20Code-blueviolet?logo=anthropic" alt="Claude Code MCP">
  <img src="https://img.shields.io/badge/zero--trust-input%20sanitizer-red" alt="Zero-trust">
</p>

> **Secure, local-first document-to-Markdown conversion.**
> Zero cloud dependencies. Zero-trust input sanitization. Correct multi-column layout. Full LaTeX math support.

Built to address the layout bugs, mathematical omissions, and critical security vulnerabilities (SSRF, path traversal, cloud-API lock-in) found in existing conversion utilities.

---

## Table of Contents

- [Why MarkSentry?](#why-marksentry)
- [Architecture](#architecture)
- [Installation](#installation)
- [Quick Start](#quick-start)
  - [CLI](#cli)
  - [Python SDK](#python-sdk)
- [Claude Code MCP Integration](#claude-code-mcp-integration)
- [Feature Deep Dives](#feature-deep-dives)
  - [Zero-Trust Input Sanitizer](#zero-trust-input-sanitizer)
  - [Multi-Column Layout Processor](#multi-column-layout-processor)
  - [Table Reconstruction](#table-reconstruction)
  - [LaTeX Math Output](#latex-math-output)
  - [PII Masking](#pii-masking)
- [CLI Reference](#cli-reference)
- [Development](#development)
- [License](#license)
- [Author](#author-and-attribution)

---

## Why MarkSentry?

| Capability | MarkSentry | Microsoft MarkItDown | Typical Python converters |
|---|:---:|:---:|:---:|
| Path traversal prevention | ✅ full jail + null-byte checks | ❌ | ❌ |
| SSRF mitigation | ✅ embedded URI scanner, RFC-1918 block | ❌ | ❌ |
| VBA macro stripping | ✅ OOXML rewrite before parse | ❌ | ❌ |
| Zip bomb detection | ✅ ratio + nesting depth | ❌ | ❌ |
| Multi-column PDF layout | ✅ gap-analysis algorithm | ❌ reads across columns | ❌ |
| LaTeX math output | ✅ OMML + Unicode to LaTeX | ❌ omits equations | ❌ omits equations |
| GFM table reconstruction | ✅ coordinate-aligned grid | Partial | Partial |
| PII masking (pre-RAG) | ✅ 10 pattern categories | ❌ | ❌ |
| 100% local execution | ✅ zero network calls | ❌ calls Azure OCR | Varies |
| Magic-byte validation | ✅ extension + header check | ❌ | ❌ |

---

## Architecture

```
                  User-supplied file
                        |
              ┌─────────▼──────────┐
              │  Zero-Trust        │  sanitizer.py
              │  Input Sanitizer   │  - path traversal jail
              │                    │  - SSRF URI scan
              │                    │  - macro stripping
              │                    │  - zip-bomb detection
              └─────────┬──────────┘
                        |
          ┌─────────────┼─────────────┐
          |             |             |
   ┌──────▼──────┐ ┌────▼────┐ ┌─────▼──────┐
   │  PDF Parser │ │  DOCX   │ │  ZIP       │
   │             │ │  Parser │ │  Dispatcher│
   │ pdfminer    │ │ python- │ └─────┬──────┘
   │ + BBox grid │ │ docx    │       | recurse
   └──────┬──────┘ └────┬────┘       |
          |             |            |
          └──────┬───────────────────┘
                 |
     ┌───────────▼──────────────┐
     │  Multi-Column Layout     │  layout.py
     │  Processor               │  - gap analysis
     │                          │  - column assignment
     │                          │  - reading-order sort
     └───────────┬──────────────┘
                 |
     ┌───────────▼──────────────┐
     │  Table + Math Core       │  pdf_parser.py / math_converter.py
     │                          │  - GFM table reconstruction
     │                          │  - OMML → LaTeX (DOCX)
     │                          │  - Unicode → LaTeX (PDF)
     └───────────┬──────────────┘
                 |
     ┌───────────▼──────────────┐
     │  PII Masking Filter      │  pii_filter.py  (optional)
     │                          │  - SSN, email, credit card
     │                          │  - private keys, JWT, AWS keys
     └───────────┬──────────────┘
                 |
          Markdown output
```

---

## Installation

**Requirements:** Python 3.10+

```bash
# From source
git clone https://github.com/sunilgentyala/marksentry
cd marksentry
pip install -e .

# From PyPI (once published)
pip install marksentry
```

Optional OCR support (for scanned PDFs):

```bash
pip install marksentry[ocr]
# Also requires: sudo apt install tesseract-ocr  (or brew install tesseract)
```

---

## Quick Start

### CLI

```bash
# Convert a PDF, output to file
marksentry convert paper.pdf --output paper.md

# Convert with PII masking enabled
marksentry convert hr_report.pdf --mask-pii --output clean.md

# Convert multiple files into an output directory
marksentry convert *.pdf --output-dir ./converted/

# Convert a DOCX with LaTeX math, no table detection
marksentry convert thesis.docx --no-tables --output thesis.md

# Convert a ZIP archive containing mixed documents
marksentry convert bundle.zip --output-dir ./extracted/

# Dry-run PII audit (no output written)
marksentry audit-pii confidential.pdf

# Show document metadata and security scan results
marksentry info suspicious.docx
```

### Python SDK

```python
from marksentry import convert

# Simple conversion
result = convert("research_paper.pdf")
print(result.markdown)

# With all security + compliance features
result = convert(
    "internal_report.pdf",
    mask_pii=True,
    pii_patterns=["SSN", "EMAIL", "CREDIT_CARD"],
    multi_column=True,
    detect_math=True,
    allowed_base="/safe/data/directory",   # path jail
    strip_macros=True,
    check_ssrf=True,
)

for warning in result.warnings:
    print(f"[WARNING] {warning}")

with open("output.md", "w") as f:
    f.write(result.markdown)
```

---

## Claude Code MCP Integration

MarkSentry ships a [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server so
Claude Code can convert documents **before** sending their content to the model. This eliminates
the token cost of reading raw binary files: a 20-page IEEE paper that would consume 15,000+
tokens as raw bytes arrives as ~2,000 tokens of clean Markdown.

### Why it saves tokens

| Without MCP | With MCP |
|---|---|
| Claude reads raw PDF bytes or you paste text manually | Claude calls `convert_to_markdown`, receives clean Markdown |
| Every page consumes image or binary tokens | Only the extracted text is sent |
| Tables and math arrive garbled or are skipped | Tables are GFM-formatted, equations are LaTeX |
| PII may reach the model | Optional `mask_pii=True` redacts before conversion |

### Setup

**1. Install MarkSentry and the MCP package:**

```bash
git clone https://github.com/sunilgentyala/marksentry
cd marksentry
pip install -e .
pip install mcp
```

**2. Register with Claude Code:**

```bash
# macOS / Linux
claude mcp add --scope user marksentry python /path/to/marksentry/marksentry_mcp.py

# Windows — use the full path to your Python executable to avoid shell stubs
claude mcp add --scope user marksentry "C:\Python\python.exe" "C:\path\to\marksentry\marksentry_mcp.py"
```

> **Windows note:** Using just `python` can resolve to the Windows Store stub that has no
> packages installed. Run `where python` and pass the full path returned.

**3. Verify:**

```bash
claude mcp get marksentry
# Status: Connected
```

Restart Claude Code after registration for the tools to load into the session.

### MCP Tools

| Tool | Purpose |
|---|---|
| `convert_to_markdown` | Primary tool. Converts PDF, DOCX, or ZIP to clean Markdown. Use whenever a document path is shared. |
| `audit_pii` | Pre-flight PII scan. Reports pattern types and hit counts without producing full output. |
| `document_info` | Metadata preflight. Returns file type, size, macro status, and any SSRF-risk URLs. |

### Parameters for `convert_to_markdown`

| Parameter | Default | Description |
|---|:---:|---|
| `path` | required | Absolute or relative path to the document |
| `mask_pii` | `false` | Replace detected PII with `[REDACTED:TYPE]` placeholders |
| `include_page_breaks` | `false` | Emit `---` between pages in PDF output |

### Usage examples

Once the MCP server is connected, just share a file path in your prompt:

```
"Summarise this paper: /home/user/papers/research.pdf"
  → Claude calls convert_to_markdown, receives Markdown, answers from text

"Does this DOCX contain any PII? /home/user/docs/report.docx"
  → Claude calls audit_pii first, reports findings before converting

"What type of file is this and is it safe to convert? /tmp/unknown.zip"
  → Claude calls document_info for a fast preflight check
```

---

## Feature Deep Dives

### Zero-Trust Input Sanitizer

Every file passes through the sanitizer before reaching any parser:

| Check | Detail |
|---|---|
| **Path traversal** | Resolves symlinks, rejects `../`, null bytes, UNC paths, and `file://`/`http://` schemes |
| **Path jail** | Pass `allowed_base` to restrict access to a directory tree; any escape raises `SanitizationError` |
| **Magic-byte validation** | Compares file header bytes against the expected signature; rejects spoofed extensions |
| **SSRF mitigation** | Scans XML/relationship/HTML entries in OOXML archives; blocks RFC-1918, loopback, link-local, and `file://` URLs |
| **Macro stripping** | Detects `vbaProject.bin` and macro content-type declarations; rewrites the archive without them |
| **Zip bomb detection** | Enforces a max uncompressed:compressed ratio (default 100x) and nesting depth (default 3) |
| **File size ceiling** | Configurable hard limit (default 256 MiB) to prevent memory exhaustion |

### Multi-Column Layout Processor

Academic papers commonly use 2- or 3-column layouts. Naive parsers read text horizontally,
producing garbled output:

```
Introduction  Methodology  Results
We studied...  We applied...  Our findings...
```

MarkSentry uses a gap-analysis algorithm:

1. Build a 1-point-resolution coverage histogram across the page width
2. Identify contiguous uncovered regions wider than the minimum gap threshold (default 18pt)
3. Declare each gap a column separator
4. Assign every text block to the column it overlaps most
5. Sort within each column top-to-bottom
6. Interleave full-width blocks (titles, section headers, captions) at their correct vertical position

The result is correct sequential flow through each column before moving to the next.

### Table Reconstruction

The `TableDetector` clusters text blocks by shared y-bands (rows) and x-positions (columns),
identifies grid regions where at least 3 rows each contain 2+ aligned cells, and emits
standard GitHub-Flavored Markdown pipe tables:

```markdown
| Method     | Precision | Recall | F1   |
|------------|-----------|--------|------|
| Baseline   | 0.82      | 0.79   | 0.80 |
| MarkSentry | 0.94      | 0.91   | 0.92 |
```

### LaTeX Math Output

**PDF documents:** Unicode mathematical symbols (Greek letters, operators, set notation,
integrals, sums) are mapped to LaTeX equivalents. Inline-length formulas use `$...$`;
display-length formulas use `$$...$$`.

**DOCX documents:** Office Math Markup Language (OMML) equations are converted via a
recursive XML descent parser:

| OMML element | LaTeX output |
|---|---|
| `m:f` (fraction) | `\frac{num}{den}` |
| `m:rad` (radical) | `\sqrt{x}` or `\sqrt[n]{x}` |
| `m:sSup` (superscript) | `x^{n}` |
| `m:sSub` (subscript) | `x_{i}` |
| `m:nary` (integral, sum, product) | `\int_{a}^{b}`, `\sum_{i=0}^{n}` |
| `m:d` (delimiters) | `\left( \right)`, `\left[ \right]` |
| `m:m` (matrix) | `\begin{pmatrix}...\end{pmatrix}` |
| `m:eqArr` (equation array) | `\begin{aligned}...\end{aligned}` |

### PII Masking

Ten pattern categories, all with validation (credit cards are Luhn-checked to eliminate
false positives):

| Category | Example input | Masked output |
|---|---|---|
| `SSN` | `123-45-6789` | `[REDACTED:SSN]` |
| `EMAIL` | `user@corp.com` | `[REDACTED:EMAIL]` |
| `CREDIT_CARD` | `4111 1111 1111 1111` | `[REDACTED:CREDIT_CARD]` |
| `PHONE` | `(555) 867-5309` | `[REDACTED:PHONE]` |
| `PRIVATE_KEY` | `-----BEGIN RSA PRIVATE KEY-----...` | `[REDACTED:PRIVATE_KEY]` |
| `AWS_ACCESS_KEY` | `AKIAIOSFODNN7EXAMPLE` | `[REDACTED:AWS_ACCESS_KEY]` |
| `JWT_TOKEN` | `eyJhbGciOiJIUzI1NiIs...` | `[REDACTED:JWT_TOKEN]` |
| `IPV4` | `192.168.1.100` | `[REDACTED:IPV4]` |
| `PASSWORD_ASSIGNMENT` | `password=s3cr3t!` | `[REDACTED:PASSWORD_ASSIGNMENT]` |
| `HIGH_ENTROPY_HEX` | `a3f2c1d4e5b6a7f8...` | `[REDACTED:HIGH_ENTROPY_HEX]` |

Use `marksentry audit-pii` for a dry run that reports findings without modifying any output.

---

## CLI Reference

```
Usage: marksentry [OPTIONS] COMMAND [ARGS]...

Commands:
  convert    Convert one or more documents to Markdown.
  audit-pii  Scan for PII without modifying output (dry run).
  info       Display document metadata and security scan summary.

marksentry convert [OPTIONS] FILES...

Options:
  -o, --output PATH          Output file (single input only)
  --output-dir DIR           Output directory for batch conversion
  --mask-pii                 Enable PII masking
  --pii-patterns TEXT        Comma-separated pattern names (default: all)
  --no-tables                Disable table reconstruction
  --no-math                  Disable LaTeX math conversion
  --no-multi-column          Disable column layout reconstruction
  --column-gap FLOAT         Column gap threshold in points (default: 18.0)
  --page-breaks              Emit --- between PDF pages
  --no-strip-macros          Skip macro stripping
  --no-ssrf-check            Skip embedded URL scanning
  --allowed-base DIR         Restrict file access to this directory
  -v, --verbose              Enable debug logging
```

---

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests with coverage
pytest

# Type checking
mypy marksentry/

# Linting
ruff check marksentry/
```

---

## License

MIT License. See `LICENSE` for details.

---

## Author and Attribution

**MarkSentry** was designed and built by **Sunil Gentyala**.

Sunil Gentyala is a principal software architect and cybersecurity researcher at HCL America
Inc., with expertise in secure systems design, document intelligence pipelines, and adversarial
robustness. MarkSentry was created to solve real security gaps in existing document conversion
tooling -- in particular the complete absence of input sanitization, SSRF defenses, and correct
multi-column layout handling in tools like Microsoft MarkItDown -- and to provide a
production-grade, offline-first foundation for RAG data pipelines that must handle sensitive
documents safely.

Contact: sunil.gentyala@ieee.org
GitHub: [github.com/sunilgentyala](https://github.com/sunilgentyala)
