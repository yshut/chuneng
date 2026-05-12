"""PDF parsing helpers.

Primary path uses PyMuPDF4LLM to preserve reading order and table-like Markdown.
pdfplumber remains as a compatibility fallback and for table extraction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class ParsedPdf:
    text: str = ""
    pages: list[dict] = field(default_factory=list)
    tables: list[pd.DataFrame] = field(default_factory=list)
    primary_engine: str = "none"
    fallback_engine: str = "none"


def parse_pdf(path: str | Path, *, ocr_language: str = "chi_sim+eng") -> ParsedPdf:
    """Parse a PDF into Markdown-ish text, page text and tables.

    PyMuPDF4LLM is better than plain text extraction for LLM/RAG because it keeps
    layout order and Markdown tables when possible. pdfplumber is still used to
    extract DataFrames and as a fallback for environments without PyMuPDF4LLM.
    """

    pdf_path = Path(path)
    parsed = ParsedPdf()

    markdown = _parse_with_pymupdf4llm(pdf_path, ocr_language=ocr_language)
    if markdown:
        parsed.text = markdown
        parsed.primary_engine = "pymupdf4llm"

    plumber_text, pages, tables = _parse_with_pdfplumber(pdf_path)
    parsed.pages = pages
    parsed.tables = tables

    if not parsed.text and plumber_text:
        parsed.text = plumber_text
        parsed.primary_engine = "pdfplumber"
    elif parsed.text and plumber_text:
        parsed.fallback_engine = "pdfplumber"

    return parsed


def _parse_with_pymupdf4llm(path: Path, *, ocr_language: str) -> str:
    try:
        import pymupdf4llm
    except ImportError:
        logger.info("pymupdf4llm 未安装，跳过 PDF Markdown 解析: %s", path.name)
        return ""

    base_kwargs = {
        "page_separators": True,
        "show_progress": False,
        "force_text": True,
    }
    try:
        text = pymupdf4llm.to_markdown(str(path), **base_kwargs)
        if not _clean_text(text):
            text = pymupdf4llm.to_markdown(
                str(path),
                **base_kwargs,
                use_ocr=True,
                ocr_language=ocr_language,
            )
    except TypeError:
        # Older PyMuPDF4LLM builds do not expose layout/OCR kwargs.
        text = pymupdf4llm.to_markdown(str(path), **base_kwargs)
    except Exception as e:
        logger.warning("PyMuPDF4LLM 解析 PDF 失败 %s: %s", path.name, e)
        return ""

    return _clean_text(text)


def _parse_with_pdfplumber(path: Path) -> tuple[str, list[dict], list[pd.DataFrame]]:
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber 未安装，无法兜底解析 PDF: %s", path.name)
        return "", [], []

    pages: list[dict] = []
    all_text_parts: list[str] = []
    all_tables: list[pd.DataFrame] = []

    try:
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages):
                page_text = page.extract_text() or ""
                page_tables = page.extract_tables()
                dfs: list[pd.DataFrame] = []

                for tbl in page_tables:
                    df = _table_to_dataframe(tbl)
                    if df is None:
                        continue
                    dfs.append(df)
                    all_tables.append(df)

                pages.append({
                    "page": i + 1,
                    "text": page_text,
                    "tables": dfs,
                })
                all_text_parts.append(page_text)
    except Exception as e:
        logger.warning("pdfplumber 解析 PDF 失败 %s: %s", path.name, e)
        return "", pages, all_tables

    return _clean_text("\n\n".join(all_text_parts)), pages, all_tables


def _table_to_dataframe(table: list[list] | None) -> pd.DataFrame | None:
    if not table or len(table) < 2:
        return None

    header = [str(cell or "").strip() for cell in table[0]]
    rows = [[str(cell or "").strip() for cell in row] for row in table[1:]]
    if not any(header) or not rows:
        return None

    width = len(header)
    normalized_rows = []
    for row in rows:
        normalized_rows.append((row + [""] * width)[:width])

    try:
        return pd.DataFrame(normalized_rows, columns=header)
    except Exception:
        return None


def _clean_text(text: str | None) -> str:
    if not text:
        return ""
    return str(text).replace("\r\n", "\n").strip()
