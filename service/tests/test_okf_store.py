"""OKF round-tripping, and the guarantee that a pointer cannot be forged."""

from jobkb import okf
from jobkb.okf import Record, T_ANSWER, T_EXPERIENCE


def test_roundtrip_preserves_everything(tmp_path):
    rec = Record(
        path="answers/why-us.md", type=T_ANSWER,
        title="Why do you want to work here?",
        description="Reusable motivation answer.",
        resource="https://careers.acme.com/jobs/1",
        tags=["motivation", "opinion"],
        aliases=["What attracts you to this position?"],
        seen_on=[{"site": "careers.acme.com", "date": "2026-08-11"}],
        body="Your streaming work matches my Kafka background.",
    )
    okf.write_file(tmp_path, rec)
    back = okf.read_file(tmp_path, "answers/why-us.md")

    assert back.type == T_ANSWER
    assert back.title == rec.title
    assert back.aliases == rec.aliases
    assert back.tags == rec.tags
    assert back.seen_on == rec.seen_on
    assert back.body == rec.body
    assert back.timestamp                      # stamped on write


def test_file_is_readable_okf(tmp_path):
    """Frontmatter first, `type` present, body human-readable — a person or
    another OKF tool has to be able to read this."""
    rec = Record(path="profile/experience/x.md", type=T_EXPERIENCE, title="Consultant",
                 fields={"company": "Capgemini", "startDate": "Sep 2018"})
    path = okf.write_file(tmp_path, rec)
    text = path.read_text(encoding="utf-8")

    assert text.startswith("---\n")
    assert "type: Work Experience" in text
    # Data records mirror their frontmatter into a table so the file reads well.
    assert "| company | Capgemini |" in text


def test_body_only_markdown_is_still_loaded(tmp_path):
    (tmp_path / "note.md").write_text("# A plain note\n\ntext", encoding="utf-8")
    recs = okf.walk(tmp_path)
    assert len(recs) == 1 and recs[0].title == "A plain note"


def test_index_md_is_skipped_as_knowledge(tmp_path):
    okf.write_file(tmp_path, Record(path="answers/a.md", type=T_ANSWER, title="Q", body="A"))
    okf.write_index_md(tmp_path, "answers", "Answers")
    assert [r.path for r in okf.walk(tmp_path)] == ["answers/a.md"]


def test_pointers_resolve_to_stored_text(kb):
    assert kb.resolve("profile/personal.md#email") == "alex.rivera@example.com"
    assert kb.resolve("profile/experience/northwind-solution-architect.md#company") \
        == "Northwind Trading FZE"
    assert kb.resolve("profile/skills.md#skills") == "Kafka, Python, AWS"
    assert kb.resolve("profile/custom.md#notice_period") == "30 days"
    assert kb.resolve("answers/why-us.md").startswith("Your streaming work")


def test_a_model_cannot_inject_a_value(kb):
    """Routing returns pointers, never text. Anything that is not a real pointer
    resolves to nothing, so invented content cannot reach a form field."""
    for forged in [
        "Northwind Trading FZ",                        # a plausible-looking value
        "profile/personal.md#email = other@x.com",  # a value smuggled into a pointer
        "profile/personal.md#middleName",           # a field that does not exist
        "profile/experience/nope.md#company",       # a record that does not exist
        "answers/../../../etc/passwd",
        "none", "generate", "", "   ",
    ]:
        assert kb.resolve(forged) is None, forged


def test_experience_is_newest_first(kb):
    order = [r.fields["company"] for r in kb.experience()]
    assert order == ["Northwind Trading FZE", "Capgemini"]


def test_reworded_question_becomes_an_alias_not_a_second_record(kb):
    before = len(kb.answers())
    kb.upsert_answer("Why do you want to work here at Acme?", "Updated answer.")
    after = kb.answers()

    assert len(after) == before, "a reworded question must not create a new file"
    rec = kb.get("answers/why-us.md")
    assert "Why do you want to work here at Acme?" in rec.aliases
    assert rec.body == "Updated answer."


def test_unrelated_question_creates_its_own_record(kb):
    before = len(kb.answers())
    kb.upsert_answer("What is your notice period?", "30 days.")
    assert len(kb.answers()) == before + 1
