"""Epic #139, issues #141/#143/#144: leak checks beyond whole-line fingerprints. Offline.

The guard is one layer, not a proof (see confidentiality.py). These tests pin what it does catch:
copies, reformatted copies, embedded copies, light paraphrase, markers and credentials, and what it
openly does not (a faithful paraphrase that shares few words)."""

from __future__ import annotations

import pytest

from viharbot.learning.confidentiality import ContentGuard, normalise
from viharbot.learning.instructor_access import ConfidentialIndex, ConfidentialLeak

SECRET = ("The correct answer to question four is the off-by-one in the loop bound, "
          "because the range excludes its final index and the sentinel is never reached.")


@pytest.fixture
def guard():
    g = ContentGuard()
    g.add(f"# Week one key\n\n{SECRET}\n\nAnother unrelated heading line for padding purposes only.")
    return g


def kinds(guard, text):
    return {f.kind for f in guard.check(text)}


def test_the_existing_whole_line_check_still_works_and_is_inherited(guard):
    assert isinstance(guard, ConfidentialIndex)
    assert guard.leaks(SECRET) and "exact_line" in kinds(guard, SECRET)
    with pytest.raises(ConfidentialLeak):
        guard.assert_trainee_safe(SECRET)


def test_ordinary_trainee_text_passes(guard):
    text = ("Agents are separate services. Each one has its own configuration, and the web service "
            "coordinates them over plain JSON.")
    assert guard.check(text) == []
    guard.assert_trainee_safe(text)


@pytest.mark.parametrize("variant", [
    SECRET.upper(),
    "  ".join(SECRET.split()),  # extra spaces
    SECRET.replace(", ", ",\n"),  # re-wrapped across lines
    "- **" + SECRET + "**",  # markdown bullet and bold
    "> " + SECRET.replace("answer", "ans​wer"),  # zero-width character inside a word
    SECRET.replace("o", "ο", 1).replace("e", "e"),  # a Greek omicron: NFKC leaves it, shingles survive elsewhere
    f"Intro sentence about something else entirely. {SECRET} And a closing remark.",  # embedded in prose
])
def test_reformatted_or_embedded_copies_are_caught(guard, variant):
    assert guard.check(variant), "a reformatted copy must be flagged"


def test_a_lightly_reworded_copy_is_flagged_as_a_possible_paraphrase(guard):
    reworded = ("Question four's right answer is an off-by-one error in the loop's bound: the range leaves out "
                "its last index, so the sentinel is never reached.")
    assert "paraphrase" in kinds(guard, reworded)


def test_a_faithful_paraphrase_with_little_word_overlap_is_not_caught():
    """Stated limitation: the guard is lexical. This is why a human reviews everything bound for Ada."""
    g = ContentGuard()
    g.add(SECRET)
    disguised = "Item 4 hinges on the loop terminating one step early because of how the boundary is defined."
    assert g.check(disguised) == []


def test_normalisation_is_unicode_and_case_aware():
    assert normalise("Ａnswer KEY") == "answer key"  # full-width A and a non-breaking space
    assert "​" not in normalise("a​b")


@pytest.mark.parametrize("text", [
    "See the answer key for details.", "The grading criteria are listed elsewhere.",
    "This is instructor-only material.", "Points awarded: 3.", "Check the rubric first.",
    "Compare with the intentional defect catalog.", "Read vchokshi/viharbot-onboarding-instructor.",
    "model answer: 42"])
def test_instructor_marker_phrases_are_flagged_even_with_no_index_loaded(text):
    assert "marker" in kinds(ContentGuard(), text)


@pytest.mark.parametrize("text", [
    "token ghp_" + "a" * 30, "github_pat_" + "b" * 30, "AKIA" + "C" * 16, "Authorization: Bearer " + "d" * 30,
    "-----BEGIN " + "RSA PRIVATE KEY-----", "=".join(("api_key", "x" * 16)), "sk-" + "e" * 30])
def test_credential_shapes_are_flagged(text):
    assert "credential" in kinds(ContentGuard(), text)


def test_findings_never_contain_the_matched_text(guard):
    with pytest.raises(ConfidentialLeak) as info:
        guard.assert_trainee_safe(SECRET)
    assert "loop bound" not in str(info.value) and "exact_line" in str(info.value)
    assert all(not hasattr(f, "text") for f in guard.check(SECRET))


def test_the_guard_holds_only_hashes_and_is_not_persisted(guard):
    import pickle
    blob = pickle.dumps(guard)
    for word in (b"off-by-one", b"sentinel", b"range", b"index", b"unrelat", b"purpos", b"another"):
        assert word not in blob  # neither the text nor its word stems are kept in the clear


def test_an_empty_guard_still_applies_marker_and_credential_checks_only():
    g = ContentGuard()
    assert g.empty and g.check("The loop bound is off by one at the end of the range.") == []
    assert {f.kind for f in g.check(SECRET)} == {"marker"}  # "correct answer" is a marker; nothing to compare to
