"""Single entry point for uploads: routes by file extension to the right extractor."""
from pathlib import Path

from dataclasses import replace

from app.config import MIN_TEXT_CHARS
from app.parsing.docx_extractor import extract_docx
from app.parsing.ocr import Reader, ocr_pdf
from app.parsing.pdf_extractor import ExtractionResult, extract_text as extract_pdf

SUPPORTED = (".pdf", ".docx")


def extract_document(data: bytes, filename: str, ocr: Reader | None = None) -> ExtractionResult:
    """Read a resume or job description. `ocr` (a page-image reader) is used only for a PDF that has no text layer; every other
    file goes through the normal path and never touches it."""
    name = filename.lower()
    if name.endswith(".pdf"):
        result = extract_pdf(data, filename)
        if result.status == "empty_or_scanned" and ocr is not None:
            return _try_ocr(data, result, ocr)
        return result
    if name.endswith(".docx"):
        return extract_docx(data, filename)
    return ExtractionResult(filename, "unsupported", message="Supported formats: PDF, DOCX.")


def _try_ocr(data: bytes, original: ExtractionResult, ocr: Reader) -> ExtractionResult:
    try:
        text, read, total = ocr_pdf(data, ocr)
    except Exception as exc:
        return replace(original, message=f"{original.message} OCR was tried and failed: {str(exc)[:120]}")
    if len(text) < MIN_TEXT_CHARS:
        return replace(original, message=f"{original.message} OCR found no readable text.")
    note = "Scanned PDF: text read by OCR" + (f" (only the first {read} of {total} pages)" if read < total else "") + "."
    return replace(original, status="ok", text=text, pages=total, message=note, ocr=True)


def extract_from_path(path: str | Path) -> ExtractionResult:
    path = Path(path)
    return extract_document(path.read_bytes(), path.name)
