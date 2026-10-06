"""OCR for scanned PDFs: render each page to an image and have a vision model transcribe it.

Only used when a PDF has no text layer at all (see document_extractor); a PDF with text never comes here. The reader is a function
`jpeg bytes -> text` so tests can pass a fake and never call the API.
"""
import re
from typing import Callable

import pymupdf

from app import config

Reader = Callable[[bytes], str]


def default_reader() -> Reader | None:
    """The real vision reader, or None when OCR is switched off, the key is missing, or everything must stay on this machine."""
    if not config.OCR_ENABLED or config.LLM_MODE == "local" or not config.GEMINI_API_KEY:
        return None
    from app.llm import client
    return client.ocr_image


def _clean(text: str) -> str:
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text.strip(), flags=re.I)        # models sometimes wrap the answer in a code fence
    return text.strip()


def ocr_pdf(data: bytes, read_page: Reader, max_pages: int | None = None) -> tuple[str, int, int]:
    """(text, pages_read, total_pages). Raises whatever the reader raises if the very first page fails; later failures keep what was read."""
    max_pages = max_pages or config.OCR_MAX_PAGES
    doc = pymupdf.open(stream=data, filetype="pdf")
    total = len(doc)
    parts: list[str] = []
    for page in list(doc)[:max_pages]:
        jpeg = page.get_pixmap(dpi=config.OCR_DPI).tobytes("jpeg")
        try:
            parts.append(_clean(read_page(jpeg)))
        except Exception:
            if not parts:
                raise
            break
    return "\n\n".join(p for p in parts if p), len(parts), total
