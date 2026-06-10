"""
Math Conversion Utilities.

Handles two source formats:

1. OMML (Office Math Markup Language) -- the XML-based equation format
   embedded in DOCX files via <m:oMath> elements.  Converts to LaTeX
   using a recursive descent parser over the element tree.

2. Heuristic PDF math reconstruction -- pdfminer delivers characters
   with precise coordinates and font information.  Subscripts and
   superscripts are identified by vertical displacement, and common
   Unicode math symbols are mapped to their LaTeX equivalents.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lxml.etree import _Element  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# Unicode math symbol → LaTeX mapping
# ---------------------------------------------------------------------------

UNICODE_TO_LATEX: dict[str, str] = {
    # Greek lowercase
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta",
    "ε": r"\epsilon", "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta",
    "ι": r"\iota", "κ": r"\kappa", "λ": r"\lambda", "μ": r"\mu",
    "ν": r"\nu", "ξ": r"\xi", "π": r"\pi", "ρ": r"\rho",
    "σ": r"\sigma", "τ": r"\tau", "υ": r"\upsilon", "φ": r"\phi",
    "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega",
    # Greek uppercase
    "Α": "A", "Β": "B", "Γ": r"\Gamma", "Δ": r"\Delta",
    "Ε": "E", "Ζ": "Z", "Η": "H", "Θ": r"\Theta",
    "Λ": r"\Lambda", "Ξ": r"\Xi", "Π": r"\Pi", "Σ": r"\Sigma",
    "Υ": r"\Upsilon", "Φ": r"\Phi", "Ψ": r"\Psi", "Ω": r"\Omega",
    # Operators and relations
    "±": r"\pm", "∓": r"\mp", "×": r"\times", "÷": r"\div",
    "·": r"\cdot", "∘": r"\circ", "∗": r"*",
    "≤": r"\leq", "≥": r"\geq", "≠": r"\neq", "≈": r"\approx",
    "≡": r"\equiv", "∼": r"\sim", "∝": r"\propto",
    "∞": r"\infty", "∅": r"\emptyset",
    "∈": r"\in", "∉": r"\notin", "⊂": r"\subset", "⊃": r"\supset",
    "⊆": r"\subseteq", "⊇": r"\supseteq", "∪": r"\cup", "∩": r"\cap",
    "∧": r"\wedge", "∨": r"\vee", "¬": r"\neg",
    "→": r"\to", "←": r"\leftarrow", "↔": r"\leftrightarrow",
    "⇒": r"\Rightarrow", "⇐": r"\Leftarrow", "⇔": r"\Leftrightarrow",
    "↑": r"\uparrow", "↓": r"\downarrow",
    "∂": r"\partial", "∇": r"\nabla",
    "∫": r"\int", "∬": r"\iint", "∭": r"\iiint", "∮": r"\oint",
    "∑": r"\sum", "∏": r"\prod", "√": r"\sqrt",
    "⌈": r"\lceil", "⌉": r"\rceil", "⌊": r"\lfloor", "⌋": r"\rfloor",
    "〈": r"\langle", "〉": r"\rangle", "⟨": r"\langle", "⟩": r"\rangle",
    "…": r"\ldots", "⋯": r"\cdots", "⋮": r"\vdots", "⋱": r"\ddots",
    "ℝ": r"\mathbb{R}", "ℕ": r"\mathbb{N}", "ℤ": r"\mathbb{Z}",
    "ℚ": r"\mathbb{Q}", "ℂ": r"\mathbb{C}", "ℙ": r"\mathbb{P}",
    "†": r"\dagger", "‡": r"\ddagger", "♦": r"\diamond",
    # Superscript / subscript digits and letters (Unicode combining forms)
    "⁰": "^{0}", "¹": "^{1}", "²": "^{2}", "³": "^{3}", "⁴": "^{4}",
    "⁵": "^{5}", "⁶": "^{6}", "⁷": "^{7}", "⁸": "^{8}", "⁹": "^{9}",
    "₀": "_{0}", "₁": "_{1}", "₂": "_{2}", "₃": "_{3}", "₄": "_{4}",
    "₅": "_{5}", "₆": "_{6}", "₇": "_{7}", "₈": "_{8}", "₉": "_{9}",
    # Miscellaneous
    "°": r"^{\circ}", "′": "'", "″": "''",
}


def unicode_to_latex(text: str) -> str:
    """Replace Unicode math characters with LaTeX equivalents."""
    result: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in UNICODE_TO_LATEX:
            result.append(UNICODE_TO_LATEX[ch])
        else:
            result.append(ch)
        i += 1
    return "".join(result)


# ---------------------------------------------------------------------------
# OMML → LaTeX  (recursive XML descent)
# ---------------------------------------------------------------------------

OMML_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def _tag(local: str) -> str:
    return f"{{{OMML_NS}}}{local}"


def omml_to_latex(element: "_Element") -> str:
    """
    Convert an <m:oMath> element (or any sub-element) to a LaTeX string.

    Handles the most common OMML constructs:
      m:f   fraction          →  \frac{num}{den}
      m:rad radical           →  \sqrt[n]{base}  or \sqrt{base}
      m:sSup superscript      →  base^{exp}
      m:sSub subscript        →  base_{sub}
      m:sSubSup sub+superscript → base_{sub}^{sup}
      m:nary n-ary (∫, ∑, ∏) →  \int_{lo}^{hi} ... etc.
      m:func function         →  \funcName{arg}
      m:eqArr equation array  →  \begin{aligned}...\end{aligned}
      m:m matrix              →  \begin{pmatrix}...\end{pmatrix}
      m:d delimiters          →  \left( ... \right)
      m:r run (leaf text)     →  literal characters or mapped symbols
    """
    tag = element.tag

    if tag == _tag("oMath") or tag == _tag("oMathPara"):
        return " ".join(omml_to_latex(c) for c in element)

    if tag == _tag("r"):   # math run — leaf node
        return _omml_run_text(element)

    if tag == _tag("f"):   # fraction
        num_el = element.find(_tag("num"))
        den_el = element.find(_tag("den"))
        num = omml_to_latex(num_el) if num_el is not None else ""
        den = omml_to_latex(den_el) if den_el is not None else ""
        return rf"\frac{{{num}}}{{{den}}}"

    if tag == _tag("rad"):  # radical
        deg_el = element.find(_tag("deg"))
        base_el = element.find(_tag("e"))
        base = omml_to_latex(base_el) if base_el is not None else ""
        deg_el_e = None if deg_el is None else deg_el.find(_tag("r"))
        if deg_el is not None and deg_el_e is not None:
            deg = _omml_run_text(deg_el_e).strip()
            if deg and deg != "2":
                return rf"\sqrt[{deg}]{{{base}}}"
        return rf"\sqrt{{{base}}}"

    if tag == _tag("sSup"):  # superscript
        base_el = element.find(_tag("e"))
        sup_el = element.find(_tag("sup"))
        base = omml_to_latex(base_el) if base_el is not None else ""
        sup = omml_to_latex(sup_el) if sup_el is not None else ""
        return rf"{base}^{{{sup}}}"

    if tag == _tag("sSub"):  # subscript
        base_el = element.find(_tag("e"))
        sub_el = element.find(_tag("sub"))
        base = omml_to_latex(base_el) if base_el is not None else ""
        sub = omml_to_latex(sub_el) if sub_el is not None else ""
        return rf"{base}_{{{sub}}}"

    if tag == _tag("sSubSup"):  # sub + superscript
        base_el = element.find(_tag("e"))
        sub_el = element.find(_tag("sub"))
        sup_el = element.find(_tag("sup"))
        base = omml_to_latex(base_el) if base_el is not None else ""
        sub = omml_to_latex(sub_el) if sub_el is not None else ""
        sup = omml_to_latex(sup_el) if sup_el is not None else ""
        return rf"{base}_{{{sub}}}^{{{sup}}}"

    if tag == _tag("nary"):  # n-ary operator (integral, sum, product)
        return _omml_nary(element)

    if tag == _tag("func"):  # named function
        fname_el = element.find(_tag("fName"))
        arg_el = element.find(_tag("e"))
        fname = omml_to_latex(fname_el) if fname_el is not None else ""
        arg = omml_to_latex(arg_el) if arg_el is not None else ""
        return rf"\{fname.strip()}{{{arg}}}"

    if tag == _tag("d"):  # delimiter (brackets, parens, etc.)
        return _omml_delimiter(element)

    if tag == _tag("eqArr"):  # equation array (aligned)
        rows = element.findall(_tag("e"))
        row_strs = [omml_to_latex(r) for r in rows]
        return r"\begin{aligned}" + r" \\ ".join(row_strs) + r"\end{aligned}"

    if tag == _tag("m"):  # matrix
        return _omml_matrix(element)

    if tag == _tag("limLow"):  # lower limit
        base_el = element.find(_tag("e"))
        lim_el = element.find(_tag("lim"))
        base = omml_to_latex(base_el) if base_el is not None else ""
        lim = omml_to_latex(lim_el) if lim_el is not None else ""
        return rf"{base}_{{{lim}}}"

    if tag == _tag("limUpp"):  # upper limit
        base_el = element.find(_tag("e"))
        lim_el = element.find(_tag("lim"))
        base = omml_to_latex(base_el) if base_el is not None else ""
        lim = omml_to_latex(lim_el) if lim_el is not None else ""
        return rf"{base}^{{{lim}}}"

    if tag == _tag("acc"):  # accent (hat, bar, etc.)
        return _omml_accent(element)

    # Generic container fallback — recurse into children
    return " ".join(omml_to_latex(c) for c in element)


def _omml_run_text(run: "_Element") -> str:
    t_el = run.find(_tag("t"))
    if t_el is None or t_el.text is None:
        return ""
    return unicode_to_latex(t_el.text)


def _omml_nary(element: "_Element") -> str:
    # Determine the operator character from m:naryPr > m:chr
    pr_el = element.find(_tag("naryPr"))
    chr_el = pr_el.find(_tag("chr")) if pr_el is not None else None
    op_char = chr_el.get(_tag("val"), "∫") if chr_el is not None else "∫"
    op_latex = UNICODE_TO_LATEX.get(op_char, rf"\{op_char}")

    sub_el = element.find(_tag("sub"))
    sup_el = element.find(_tag("sup"))
    body_el = element.find(_tag("e"))

    sub_str = omml_to_latex(sub_el) if sub_el is not None else ""
    sup_str = omml_to_latex(sup_el) if sup_el is not None else ""
    body_str = omml_to_latex(body_el) if body_el is not None else ""

    result = op_latex
    if sub_str:
        result += rf"_{{{sub_str}}}"
    if sup_str:
        result += rf"^{{{sup_str}}}"
    result += f" {body_str}"
    return result


def _omml_delimiter(element: "_Element") -> str:
    pr_el = element.find(_tag("dPr"))
    beg_chr_el = pr_el.find(_tag("begChr")) if pr_el is not None else None
    end_chr_el = pr_el.find(_tag("endChr")) if pr_el is not None else None

    beg = (beg_chr_el.get(_tag("val"), "(") if beg_chr_el is not None else "(")
    end = (end_chr_el.get(_tag("val"), ")") if end_chr_el is not None else ")")

    DELIM_MAP = {
        "(": r"\left(", ")": r"\right)",
        "[": r"\left[", "]": r"\right]",
        "{": r"\left\{", "}": r"\right\}",
        "|": r"\left|", "‖": r"\left\|",
        "⌈": r"\left\lceil", "⌉": r"\right\rceil",
        "⌊": r"\left\lfloor", "⌋": r"\right\rfloor",
    }

    latex_beg = DELIM_MAP.get(beg, rf"\left{beg}")
    latex_end = DELIM_MAP.get(end, rf"\right{end}")

    contents_els = element.findall(_tag("e"))
    inner = " ".join(omml_to_latex(c) for c in contents_els)
    return f"{latex_beg} {inner} {latex_end}"


def _omml_matrix(element: "_Element") -> str:
    rows_els = element.findall(_tag("mr"))
    rows: list[str] = []
    for row_el in rows_els:
        cells = row_el.findall(_tag("e"))
        row_str = " & ".join(omml_to_latex(c) for c in cells)
        rows.append(row_str)
    body = r" \\ ".join(rows)
    return rf"\begin{{pmatrix}} {body} \end{{pmatrix}}"


_ACCENT_MAP: dict[str, str] = {
    "̂": r"\hat", "̄": r"\bar", "̃": r"\tilde", "̇": r"\dot",
    "̈": r"\ddot", "⃗": r"\vec", "̌": r"\check", "̀": r"\grave",
    "́": r"\acute", "̆": r"\breve",
}


def _omml_accent(element: "_Element") -> str:
    pr_el = element.find(_tag("accPr"))
    chr_el = pr_el.find(_tag("chr")) if pr_el is not None else None
    acc_char = chr_el.get(_tag("val"), "̂") if chr_el is not None else "̂"
    latex_acc = _ACCENT_MAP.get(acc_char, r"\hat")

    base_el = element.find(_tag("e"))
    base = omml_to_latex(base_el) if base_el is not None else ""
    return rf"{latex_acc}{{{base}}}"


# ---------------------------------------------------------------------------
# Heuristic inline math detection for plain-text / PDF streams
# ---------------------------------------------------------------------------

_INLINE_MATH_TRIGGERS: re.Pattern[str] = re.compile(
    r"""(?x)
    (?:                          # any of:
      [α-ωΑ-Ω]                  #   Greek letter
      | [∫∑∏√∂∇∞±×÷≤≥≠≈≡∈⊂⊆∪∩→←↔⇒⇐⇔]  # math operators
      | [₀-₉⁰-⁹]               #   sub/superscript digits
      | [℀-⅏]          #   letterlike symbols (ℝ, ℕ, etc.)
      | [∀-⋿]          #   mathematical operators block
    )
    """,
    re.UNICODE,
)


def text_contains_math(text: str) -> bool:
    """Quick test: does this text fragment look like it contains inline math?"""
    return bool(_INLINE_MATH_TRIGGERS.search(text))


def wrap_inline_math(text: str) -> str:
    """
    Heuristic: if a short text segment looks like a standalone formula
    (contains math symbols and is under 80 chars), wrap it in ``$...$``.
    """
    if not text_contains_math(text):
        return text
    converted = unicode_to_latex(text.strip())
    if len(text.strip()) <= 80 and not text.strip().startswith("$"):
        return f"${converted}$"
    return converted
