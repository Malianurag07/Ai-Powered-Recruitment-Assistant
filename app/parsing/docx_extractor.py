"""Word (.docx) -> raw text. Same contract as pdf_extractor: never raises, returns ExtractionResult."""
import io

from docx import Document

from app.config import MAX_FILE_MB, MIN_TEXT_CHARS
from app.parsing.pdf_extractor import ExtractionResult


def _visible_text(paragraph) -> tuple[str, str]:
    """(visible, hidden) text of a body paragraph. Runs marked hidden or coloured pure white are left out (keyword stuffing).
    Paragraphs without such runs take the normal path, so hyperlinks and other content are untouched."""
    def gone(run):
        rgb = run.font.color.rgb if run.font.color is not None and run.font.color.type is not None else None
        return bool(run.font.hidden) or (rgb is not None and str(rgb).upper() == "FFFFFF")
    bad = [r for r in paragraph.runs if r.text.strip() and gone(r)]
    if not bad:
        return paragraph.text.strip(), ""
    return "".join(r.text for r in paragraph.runs if not gone(r)).strip(), " ".join(r.text for r in bad)


def extract_docx(data: bytes, filename: str) -> ExtractionResult:
    if len(data) > MAX_FILE_MB * 1024 * 1024:
        return ExtractionResult(filename, "too_large", message=f"File exceeds {MAX_FILE_MB} MB.")
    if not data.startswith(b"PK"):  # .docx is a zip archive; zips start with "PK"
        return ExtractionResult(filename, "corrupt", message="File is not a valid .docx (legacy .doc is unsupported).")
    try:
        doc = Document(io.BytesIO(data))
    except Exception as exc:
        return ExtractionResult(filename, "corrupt", message=f"Cannot open .docx: {exc}")

    lines, hidden = [], []
    # Headers and footers often hold the name, email and phone; they are not part of the body.
    for section in doc.sections:
        for part in (section.header, section.footer):
            for p in part.paragraphs:
                t = p.text.strip()
                if t and t not in lines:
                    lines.append(t)
    # iter_inner_content yields paragraphs AND tables in document order (many resumes use tables for layout).
    for block in doc.iter_inner_content():
        if hasattr(block, "rows"):  # a table
            for row in block.rows:
                cells = []
                for cell in row.cells:
                    t = cell.text.strip()
                    if t and t not in cells:  # merged cells repeat their text
                        cells.append(t)
                if cells:
                    lines.append("  |  ".join(cells))
        elif block.text.strip():
            visible, gone = _visible_text(block)
            if gone:
                hidden.append(gone)
            if visible:
                lines.append(visible)
    # A link shown only as a label ("Portfolio") hides its address; add targets the visible text lacks (same idea as the PDF reader).
    body = "\n".join(lines).lower()
    links = [r.target_ref.removeprefix("mailto:").removeprefix("tel:") for r in doc.part.rels.values()
             if r.reltype.endswith("/hyperlink") and r.is_external and r.target_ref]
    missing = [u for u in dict.fromkeys(links) if u.split("://")[-1].removeprefix("www.").rstrip("/").lower() not in body]
    if missing:
        lines.append("Links: " + "  ".join(missing))
    text = "\n".join(lines)

    if len(text) < MIN_TEXT_CHARS:
        return ExtractionResult(filename, "empty_or_scanned", text=text, message="Document has little or no text.")
    return ExtractionResult(filename, "ok", text=text, pages=0, hidden_text=" ".join(hidden).strip())
