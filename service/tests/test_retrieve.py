"""Catalog construction and the local prefilter."""

from jobkb.retrieve import Retriever, build_catalog, exact_answer_match


def test_catalog_carries_pointers_not_values(kb):
    catalog = build_catalog(kb)
    blob = " ".join(f"{c.pointer} {c.label} {c.text}" for c in catalog)

    assert "alex.rivera@example.com" not in blob
    assert "+971-500000000" not in blob
    assert "streaming work matches" not in blob      # the stored answer text
    assert "profile/personal.md#email" in blob       # the pointer to it


def test_catalog_distinguishes_repeated_blocks(kb):
    catalog = build_catalog(kb)
    pointers = {c.pointer for c in catalog}
    assert "profile/experience/northwind-solution-architect.md#startDate" in pointers
    assert "profile/experience/capgemini-consultant.md#startDate" in pointers

    newest = next(c for c in catalog
                  if c.pointer.endswith("northwind-solution-architect.md#company"))
    assert newest.block == 0 and "most recent" in c_label(newest)


def c_label(c):
    return f"{c.label} {c.text}"


def test_answers_expose_their_aliases_for_matching(kb):
    catalog = build_catalog(kb)
    answer = next(c for c in catalog if c.kind == "answer")
    assert "What attracts you to this position?" in answer.text


def test_prefilter_surfaces_the_right_pointer(kb):
    r = Retriever(build_catalog(kb))
    for label, expected in [
        ("Email address", "profile/personal.md#email"),
        ("Mobile number", "profile/personal.md#phone"),
        ("Notice period", "profile/custom.md#notice_period"),
        ("Key skills", "profile/skills.md#skills"),
    ]:
        top = [c.pointer for c in r.search(label, k=5)]
        assert expected in top, f"{label}: got {top}"


def test_exact_alias_match_is_free_and_finds_reworded_questions(kb):
    """This is the cheap half of retrieval — no model, no embeddings."""
    hit = exact_answer_match(kb, "What attracts you to this position?")
    assert hit and hit[0] == "answers/why-us.md"

    assert exact_answer_match(kb, "Why do you want to work here?")[0] == "answers/why-us.md"
    # And it must not fire on an unrelated question.
    assert exact_answer_match(kb, "What is your current salary?") is None


def test_retriever_survives_an_empty_knowledge_base(tmp_path, monkeypatch):
    from jobkb import store as store_mod
    from jobkb.config import settings

    monkeypatch.setattr(settings, "root", tmp_path)
    empty = store_mod.reset_store(tmp_path)
    r = Retriever(build_catalog(empty))
    assert r.search("Email address") == []
    # An empty catalog says nothing about whether embeddings are installed;
    # reporting "no semantic retrieval" here would be a lie on a fresh install.
    assert r.semantic == r.embedder.available
