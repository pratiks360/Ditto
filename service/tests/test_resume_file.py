"""The original resume document, kept for attaching to upload fields.

Bytes live next to the markdown, not inside it: a PDF base64'd into frontmatter
would make the bundle unreadable, and readable files are the point of OKF.
"""

import pytest

from jobkb import ingest

PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


def test_the_document_is_stored_beside_its_markdown(kb):
    rec = ingest.save_resume_file(kb, PDF, "resume.pdf", "application/pdf")

    on_disk = kb.root / "resume" / "resume.pdf"
    assert on_disk.is_file()
    assert on_disk.read_bytes() == PDF, "the file must be byte-identical"
    assert rec.fields["mime"] == "application/pdf"
    assert rec.fields["bytes"] == len(PDF)

    # The markdown stays readable — no base64 blob in it.
    text = (kb.root / rec.path).read_text(encoding="utf-8")
    assert "JVBER" not in text and len(text) < 800


def test_it_reads_back_unchanged(kb):
    ingest.save_resume_file(kb, PDF, "cv.pdf", "application/pdf")
    kb.load()
    data, filename, mime = ingest.read_resume_file(kb)
    assert data == PDF
    assert filename == "cv.pdf"
    assert mime == "application/pdf"


def test_no_stored_document_reads_as_none(kb):
    assert ingest.read_resume_file(kb) is None


def test_a_path_in_the_filename_cannot_escape_the_bundle(kb):
    """The filename arrives over HTTP; it must not be able to write anywhere."""
    rec = ingest.save_resume_file(kb, PDF, "../../../../Windows/evil.pdf", "application/pdf")

    assert rec.fields["filename"] == "evil.pdf"
    assert (kb.root / "resume" / "evil.pdf").is_file()
    assert not (kb.root.parent / "evil.pdf").exists()


def test_replacing_it_leaves_one_record(kb):
    ingest.save_resume_file(kb, PDF, "old.pdf", "application/pdf")
    ingest.save_resume_file(kb, PDF + b"more", "new.pdf", "application/pdf")
    kb.load()

    data, filename, _ = ingest.read_resume_file(kb)
    assert filename == "new.pdf"
    assert data == PDF + b"more"


def test_the_text_resume_and_the_file_coexist(kb):
    """They do different jobs: the text grounds drafted answers, the file gets
    uploaded. Storing one must not clear the other."""
    ingest.save_resume(kb, "Solution Architect at Northwind.", "alex")
    ingest.save_resume_file(kb, PDF, "alex.pdf", "application/pdf")
    kb.load()

    assert "Solution Architect" in kb.resume_text()
    assert ingest.read_resume_file(kb)[1] == "alex.pdf"
