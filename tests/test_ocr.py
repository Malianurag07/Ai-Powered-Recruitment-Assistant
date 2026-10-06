"""OCR for scanned resumes: only when a PDF has no text layer, never for normal files; the reader is injected so no API is called."""
import json

import pymupdf
import pytest

from app.llm.client import LLMError
from app.parsing.document_extractor import extract_document
from app.parsing.ocr import default_reader, ocr_pdf
from app.services import candidate_service as svc
from conftest import JD_TEXT, _judge, make_resume_pdf

SCANNED = ("Ananya Iyer\nananya.iyer@example.com | 9876501234\nEDUCATION: M.Sc. Data Science, 2025\n"
           "TECHNICAL SKILLS: Python, Pandas, Scikit-learn, SQL, Deep Learning\nPROJECTS: Sentiment analysis of tweets; house price prediction.")


def image_only_pdf(pages=1) -> bytes:
    """A PDF whose pages are pictures with no text layer, like a scan."""
    src = pymupdf.open()
    src.new_page().insert_text((60, 80), "scan", fontsize=30)
    png = src[0].get_pixmap(dpi=40).tobytes("png")
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page().insert_image(pymupdf.Rect(50, 50, 300, 300), stream=png)
    return doc.tobytes()


class Reader:
    def __init__(self, text=SCANNED, fail=False):
        self.calls, self.text, self.fail = 0, text, fail

    def __call__(self, jpeg: bytes) -> str:
        self.calls += 1
        assert jpeg[:2] == b"\xff\xd8"                    # a real JPEG image is what gets sent
        if self.fail:
            raise LLMError("vision model busy")
        return self.text


def test_scanned_pdf_is_read_by_ocr_when_it_has_no_text_layer():
    reader = Reader()
    r = extract_document(image_only_pdf(), "scan.pdf", ocr=reader)
    assert reader.calls == 1 and r.ok and r.ocr is True and "ananya.iyer@example.com" in r.text and "OCR" in r.message


def test_ocr_is_never_used_when_the_pdf_has_text():
    reader = Reader()
    r = extract_document(make_resume_pdf("A B", "a@b.co", "9000000000", ["Python"]), "normal.pdf", ocr=reader)
    assert r.ok and r.ocr is False and reader.calls == 0


def test_without_a_reader_a_scan_is_still_rejected_as_before():
    r = extract_document(image_only_pdf(), "scan.pdf")
    assert r.status == "empty_or_scanned" and r.ocr is False


def test_ocr_failure_keeps_the_old_rejection_and_says_why():
    r = extract_document(image_only_pdf(), "scan.pdf", ocr=Reader(fail=True))
    assert r.status == "empty_or_scanned" and "OCR" in r.message and "busy" in r.message


def test_ocr_that_returns_almost_nothing_is_not_accepted():
    r = extract_document(image_only_pdf(), "scan.pdf", ocr=Reader(text="  \n "))
    assert r.status == "empty_or_scanned" and r.ocr is False


def test_only_the_first_pages_are_read_and_the_recruiter_is_told():
    reader = Reader()
    text, read, total = ocr_pdf(image_only_pdf(pages=5), reader, max_pages=3)
    assert reader.calls == 3 and read == 3 and total == 5
    r = extract_document(image_only_pdf(pages=5), "long.pdf", ocr=Reader())
    assert r.ok and "first 3 of 5 pages" in r.message


def test_markdown_fences_are_stripped_from_the_model_answer():
    text, _, _ = ocr_pdf(image_only_pdf(), Reader(text="```text\n" + SCANNED + "\n```"), max_pages=1)
    assert text.startswith("Ananya Iyer") and "```" not in text


def test_default_reader_is_off_in_tests_and_in_local_mode(monkeypatch):
    assert default_reader() is None                        # OCR_ENABLED=0 in the test environment
    from app import config
    monkeypatch.setattr(config, "OCR_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(config, "LLM_MODE", "local")
    assert default_reader() is None
    monkeypatch.setattr(config, "LLM_MODE", "cloud")
    assert callable(default_reader())


def test_full_pipeline_scores_a_scanned_resume_and_flags_it(pool):
    conn, job_id = pool
    profile = {"name": "Ananya Scan", "email": "ananya.iyer@example.com", "phone": "9876501234", "skills": ["Python", "SQL"], "experience_years": 0}
    o = svc.process_resume(conn, image_only_pdf(), "scan.pdf", job_id, ocr_llm=Reader(), extract_llm=lambda s, u: json.dumps(profile),
                           verify_llm=_judge, canon_llm=_judge, score_llm=_judge)
    assert o.status in ("stored", "conflict_pending") and o.ocr_used is True and "OCR" in o.message
    plain = svc.process_resume(conn, make_resume_pdf("P Q", "p@q.co", "9000000010", ["Python"]), "p.pdf", job_id, ocr_llm=Reader(),
                               extract_llm=lambda s, u: json.dumps({**profile, "email": "p@q.co", "phone": "9000000010"}),
                               verify_llm=_judge, canon_llm=_judge, score_llm=_judge)
    assert plain.ocr_used is False
