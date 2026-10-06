"""PDF -> raw text. Never raises for bad input; returns an ExtractionResult with a status."""
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app.config import MAX_FILE_MB, MIN_TEXT_CHARS


@dataclass
class ExtractionResult:
    filename: str
    status: str          # ok | empty_or_scanned | corrupt | encrypted | unsupported | too_large
    text: str = ""
    pages: int = 0
    message: str = ""
    hidden_text: str = ""     # text a human cannot see (white-on-white, microscopic, transparent) that was left out
    ocr: bool = False         # True when the text was read from page images because the PDF had no text layer

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _is_invisible(span: dict, fills: list) -> bool:
    """Would a human reading the page fail to see this text? White text is fine on a coloured banner, so it only counts as
    hidden when no coloured shape sits behind it. Microscopic and fully transparent text is always hidden."""
    if not span["text"].strip():
        return False
    if span.get("alpha", 255) == 0 or span["size"] < 2.0:
        return True
    c = span["color"]
    near_white = all(((c >> s) & 0xFF) >= 0xF0 for s in (16, 8, 0))
    return near_white and not any(pymupdf.Rect(span["bbox"]).intersects(f) for f in fills)


def _visible_page_text(page) -> tuple[str, str]:
    """(visible text, hidden text). Normal pages take the fast path unchanged; only pages with hidden spans are rebuilt."""
    fills = [d["rect"] for d in page.get_drawings() if d.get("fill") and tuple(d["fill"]) != (1.0, 1.0, 1.0)]
    d = page.get_text("dict", sort=True)
    hidden = [s["text"] for b in d["blocks"] if b["type"] == 0 for l in b["lines"] for s in l["spans"] if _is_invisible(s, fills)]
    if not hidden:
        blocks = page.get_text("blocks", sort=True)
        return "\n".join(b[4].strip() for b in blocks if b[6] == 0 and b[4].strip()), ""
    parts = []
    for b in d["blocks"]:
        if b["type"] != 0:
            continue
        lines = ["".join(s["text"] for s in l["spans"] if not _is_invisible(s, fills)).strip() for l in b["lines"]]
        block = "\n".join(x for x in lines if x)
        if block:
            parts.append(block)
    return "\n".join(parts), " ".join(hidden)


def extract_text(data: bytes, filename: str) -> ExtractionResult:
    """Extract text from PDF bytes, handling multi-column layouts and bad files."""
    if not filename.lower().endswith(".pdf"):
        return ExtractionResult(filename, "unsupported", message="Only .pdf files are supported.")
    if len(data) > MAX_FILE_MB * 1024 * 1024:
        return ExtractionResult(filename, "too_large", message=f"File exceeds {MAX_FILE_MB} MB.")
    if not data.startswith(b"%PDF"):
        return ExtractionResult(filename, "corrupt", message="File is not a valid PDF.")

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        return ExtractionResult(filename, "corrupt", message=f"Cannot open PDF: {exc}")

    if doc.needs_pass:
        return ExtractionResult(filename, "encrypted", message="PDF is password protected.")

    page_texts, hidden_parts = [], []
    for page in doc:
        # Sorted text blocks keep two-column layouts mostly readable; invisible text is dropped (see _visible_page_text).
        body, hidden_here = _visible_page_text(page)
        if hidden_here:
            hidden_parts.append(hidden_here)
        # Contact details are often only a hyperlink ("Email me" -> mailto:...). Add link targets that the visible text lacks.
        links = [l["uri"].removeprefix("mailto:").removeprefix("tel:") for l in page.get_links() if l.get("uri")]
        hidden = [u for u in dict.fromkeys(links) if u and u not in body]
        page_texts.append(body + ("\nLinks: " + "  ".join(hidden) if hidden else ""))
    text = "\n\n".join(page_texts).strip()

    if len(text) < MIN_TEXT_CHARS and doc.is_repaired:      # MuPDF had to patch a broken file and still found no text
        return ExtractionResult(filename, "corrupt", pages=len(doc), message="The PDF is damaged or incomplete. Re-export it and try again.")
    if len(text) < MIN_TEXT_CHARS:
        return ExtractionResult(
            filename, "empty_or_scanned", text=text, pages=len(doc),
            message="Little or no selectable text; likely a scanned image (OCR not supported).",
        )
    return ExtractionResult(filename, "ok", text=text, pages=len(doc), hidden_text=" ".join(hidden_parts).strip())


def extract_from_path(path: str | Path) -> ExtractionResult:
    path = Path(path)
    return extract_text(path.read_bytes(), path.name)
