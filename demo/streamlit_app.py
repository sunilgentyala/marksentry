"""
MarkSentry live demo -- upload a PDF/DOCX/ZIP, see the sanitized Markdown output,
security warnings, and PII scan results in the browser.

Run locally:
    pip install -e ".[dev]" streamlit
    streamlit run demo/streamlit_app.py

Deploy (free):
    Push this repo to GitHub, then create an app at https://share.streamlit.io
    pointing at demo/streamlit_app.py -- no separate hosting needed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import streamlit as st

from marksentry import convert
from marksentry.core.pii_filter import audit_pii

st.set_page_config(
    page_title="MarkSentry Demo",
    page_icon="🛡️",
    layout="wide",
)

st.title("🛡️ MarkSentry")
st.caption(
    "Secure, local-first document-to-Markdown conversion. "
    "Every file below is sanitized against SSRF, path traversal, macros, and zip bombs "
    "before a single byte is parsed -- entirely in this session, nothing leaves the server."
)

with st.sidebar:
    st.header("Options")
    mask_pii = st.checkbox("Mask PII in output", value=False)
    detect_tables = st.checkbox("Reconstruct tables", value=True)
    detect_math = st.checkbox("Convert math to LaTeX", value=True)
    multi_column = st.checkbox("Multi-column layout reconstruction", value=True)
    st.divider()
    st.markdown(
        "**Source:** [github.com/sunilgentyala/marksentry]"
        "(https://github.com/sunilgentyala/marksentry)\n\n"
        "**MCP server:** ships in this repo -- see the README's "
        "[Claude Code MCP Integration](https://github.com/sunilgentyala/marksentry#claude-code-mcp-integration) "
        "section to use it directly inside Claude Code."
    )

uploaded = st.file_uploader(
    "Drop a PDF, DOCX, or ZIP",
    type=["pdf", "docx", "zip"],
    help="Processed in-memory for this session only. Nothing is stored.",
)

if uploaded is not None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / uploaded.name
        tmp_path.write_bytes(uploaded.getvalue())

        with st.spinner("Sanitizing and converting..."):
            try:
                result = convert(
                    tmp_path,
                    mask_pii=mask_pii,
                    detect_tables=detect_tables,
                    detect_math=detect_math,
                    multi_column=multi_column,
                    allowed_base=Path(tmp),
                )
            except Exception as exc:  # noqa: BLE001 -- surface any sanitizer/parser error to the UI
                st.error(f"Conversion blocked or failed: {exc}")
                st.stop()

        if result.warnings:
            with st.expander(f"⚠️ {len(result.warnings)} warning(s) from the sanitizer", expanded=True):
                for warning in result.warnings:
                    st.warning(warning)

        pii_hits = audit_pii(result.markdown)
        col_left, col_right = st.columns([3, 1])

        with col_right:
            st.subheader("PII scan")
            if pii_hits:
                for category, matches in pii_hits.items():
                    st.markdown(f"**{category}**: {len(matches)} hit(s)")
                if not mask_pii:
                    st.info("Enable \"Mask PII in output\" in the sidebar to redact these before export.")
            else:
                st.success("No PII patterns detected.")

            st.subheader("Metadata")
            st.json(result.metadata)

        with col_left:
            tab_rendered, tab_raw = st.tabs(["Rendered", "Raw Markdown"])
            with tab_rendered:
                st.markdown(result.markdown)
            with tab_raw:
                st.code(result.markdown, language="markdown")

            st.download_button(
                "Download .md",
                data=result.markdown,
                file_name=f"{tmp_path.stem}.md",
                mime="text/markdown",
            )
else:
    st.info("Upload a file above to see MarkSentry convert it live.")
    st.markdown(
        "No file handy? Try any multi-column academic PDF or a DOCX with an embedded "
        "equation -- that's the case most converters get wrong."
    )
