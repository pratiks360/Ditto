"""Picking the resume up from a path in the environment.

Runs at every start, so a resume you keep editing stays in step — but it must
not rewrite the store when nothing changed, and a stale path must not stop the
service from starting.
"""

import pytest

from jobkb import ingest
from jobkb.config import settings

PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
TEXT = "ALEX RIVERA\nSolution Architect at Northwind Trading FZE since Sept 2025."


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(settings, "resume_path", None)
    monkeypatch.setattr(settings, "resume_file_path", None)
    return monkeypatch


def test_nothing_configured_does_nothing(kb, env):
    assert ingest.sync_from_disk(kb) == []


def test_a_text_path_is_imported(kb, env, tmp_path):
    src = tmp_path / "resume.txt"
    src.write_text(TEXT, encoding="utf-8")
    env.setattr(settings, "resume_path", src)

    notes = ingest.sync_from_disk(kb)
    kb.load()
    assert any("imported resume text" in n for n in notes)
    assert "Northwind" in kb.resume_text()


def test_an_unchanged_file_is_not_rewritten(kb, env, tmp_path):
    src = tmp_path / "resume.txt"
    src.write_text(TEXT, encoding="utf-8")
    env.setattr(settings, "resume_path", src)

    ingest.sync_from_disk(kb)
    kb.load()
    stamp = kb.by_type("Resume")[0].timestamp

    notes = ingest.sync_from_disk(kb)
    kb.load()
    assert any("already current" in n for n in notes)
    assert kb.by_type("Resume")[0].timestamp == stamp, "an unchanged resume must not churn"


def test_an_edited_resume_is_picked_up(kb, env, tmp_path):
    src = tmp_path / "resume.txt"
    src.write_text(TEXT, encoding="utf-8")
    env.setattr(settings, "resume_path", src)
    ingest.sync_from_disk(kb)

    src.write_text(TEXT + "\nNew line about Kafka.", encoding="utf-8")
    ingest.sync_from_disk(kb)
    kb.load()
    assert "Kafka" in kb.resume_text()


def test_a_document_path_is_attached(kb, env, tmp_path):
    src = tmp_path / "Alex Rivera Resume.pdf"
    src.write_bytes(PDF)
    env.setattr(settings, "resume_file_path", src)

    ingest.sync_from_disk(kb)
    kb.load()
    data, filename, mime = ingest.read_resume_file(kb)
    assert data == PDF
    assert filename == "Alex Rivera Resume.pdf"
    assert mime == "application/pdf"


def test_pointing_the_text_variable_at_a_pdf_attaches_it(kb, env, tmp_path):
    """The obvious mistake. Storing PDF bytes as "resume text" would poison
    every drafted answer, so treat it as the attachment instead."""
    src = tmp_path / "resume.pdf"
    src.write_bytes(PDF)
    env.setattr(settings, "resume_path", src)

    notes = ingest.sync_from_disk(kb)
    kb.load()
    assert any("not a text file" in n for n in notes)
    assert ingest.read_resume_file(kb)[0] == PDF
    assert "%PDF" not in kb.resume_text()


def test_a_missing_path_is_reported_not_raised(kb, env, tmp_path):
    """A stale environment variable must not stop the service from starting."""
    env.setattr(settings, "resume_path", tmp_path / "gone.txt")
    env.setattr(settings, "resume_file_path", tmp_path / "gone.pdf")

    notes = ingest.sync_from_disk(kb)
    assert len(notes) == 2
    assert all("no such file" in n for n in notes)


def test_both_paths_together(kb, env, tmp_path):
    txt = tmp_path / "resume.txt"
    txt.write_text(TEXT, encoding="utf-8")
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(PDF)
    env.setattr(settings, "resume_path", txt)
    env.setattr(settings, "resume_file_path", pdf)

    ingest.sync_from_disk(kb)
    kb.load()
    assert "Northwind" in kb.resume_text()
    assert ingest.read_resume_file(kb)[0] == PDF
