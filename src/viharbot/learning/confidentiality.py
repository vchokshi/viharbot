"""Leak checks for text bound for trainees or a model (issues #141, #143, #144).

`ConfidentialIndex` (instructor_access.py) fingerprints whole lines, so any change to line breaks,
case, markup or wording defeats it. `ContentGuard` extends it, keeping that check and adding:

- **Normalisation:** Unicode NFKC, zero-width and control characters removed, case folded, markup and
  punctuation dropped, so reformatted, re-wrapped, bulleted or re-cased copies still match.
- **Shingles:** every run of `SHINGLE_WORDS` consecutive words of confidential text is hashed; a run
  of that length in candidate text is a copy even when it is embedded in other prose.
- **Sentence overlap:** a candidate sentence that contains most of the content words of a
  confidential sentence (stop words dropped, light stemming) is flagged as a possible paraphrase.
- **Markers and credentials:** phrases that mark instructor-only material (answer key, rubric, grading
  criteria, defect catalogue, the instructor repository's name) and credential shapes.

Everything held is a hash; the guard never stores confidential text and is never persisted. Findings
report a kind and a count, never the matched text.

Limitation, deliberately plain: this cannot catch a faithful paraphrase that shares few words with the
original, a translation, or a summary of an answer key's meaning. It lowers the chance of an accidental
leak and is one layer. A human must still review anything bound for Ada (#144); nothing is published
on the guard's say-so.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from .instructor_access import ConfidentialIndex, ConfidentialLeak

SHINGLE_WORDS = 6
MIN_SENTENCE_CONTENT_WORDS = 6
PARAPHRASE_THRESHOLD = 0.7
_STOP = frozenset(
    "a an the and or but if of to in on at by for with from as is are was were be been being it its this "
    "that these those which who whom what when where why how not no do does did done have has had will "
    "would can could should may might must than then so such into over under about after before".split())
_ZERO_WIDTH = re.compile(r"[​-‏⁠﻿­]")
_NONWORD = re.compile(r"[^0-9a-z]+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_MARKERS = re.compile(
    r"answer[\s_-]*key|model[\s_-]*answer|expected[\s_-]*answer|correct[\s_-]*answer|grading[\s_-]*(?:key|criteria|rubric|notes)|"
    r"scoring[\s_-]*rubric|\brubric\b|intentional[\s_-]*defect|defect[\s_-]*catalog|seeded[\s_-]*(?:bug|defect)|"
    r"viharbot-onboarding-instructor|instructor[\s_-]*only|points[\s_-]*awarded|marking[\s_-]*scheme|"
    r"do not share with (?:students|trainees)", re.I)
_CREDENTIALS = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{12,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|Bearer\s+[A-Za-z0-9._~+/=-]{16,}|sk-[A-Za-z0-9]{20,}|"
    r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*\S{8,}", re.I)


@dataclass(frozen=True)
class Finding:
    kind: str  # exact_line | shingle | paraphrase | marker | credential
    count: int = 1


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in _ZERO_WIDTH.sub("", text) if unicodedata.category(ch)[0] != "C" or ch in "\n\t")
    return text.casefold()


def _words(text: str) -> list[str]:
    return [w for w in _NONWORD.sub(" ", normalise(text)).split() if w]


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "ly", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def _content(sentence: str) -> frozenset[str]:
    """Hashed content-word stems, so what the guard keeps cannot be read back as words."""
    return frozenset(_h(_stem(w)) for w in _words(sentence) if w not in _STOP and len(w) > 2)


def _shingles(text: str) -> set[str]:
    words = _words(text)
    return {_h(" ".join(words[i:i + SHINGLE_WORDS])) for i in range(len(words) - SHINGLE_WORDS + 1)}


def _sentence_sets(text: str) -> list[frozenset[str]]:
    sets = (_content(s) for s in _SENTENCE.split(normalise(text)))
    return [s for s in sets if len(s) >= MIN_SENTENCE_CONTENT_WORDS]


def has_credential(text: str) -> bool:
    return bool(_CREDENTIALS.search(text))


class ContentGuard(ConfidentialIndex):
    """A `ConfidentialIndex` that also catches reformatted and lightly reworded copies."""

    def __init__(self) -> None:
        super().__init__()
        self._shingles: set[str] = set()
        self._sentences: list[frozenset[str]] = []

    def add(self, text: str) -> None:
        super().add(text)
        self._shingles |= _shingles(text)
        self._sentences.extend(_sentence_sets(text))

    @property
    def empty(self) -> bool:
        return not (self._hashes or self._shingles or self._sentences)

    def check(self, text: str) -> list[Finding]:
        found: list[Finding] = []
        if super().leaks(text):
            found.append(Finding("exact_line"))
        hits = len(_shingles(text) & self._shingles)
        if hits:
            found.append(Finding("shingle", hits))
        if self._sentences:
            overlaps = 0
            for candidate in _sentence_sets(text):
                for secret in self._sentences:
                    if len(candidate & secret) / len(secret) >= PARAPHRASE_THRESHOLD:
                        overlaps += 1
                        break
            if overlaps:
                found.append(Finding("paraphrase", overlaps))
        markers = _MARKERS.findall(normalise(text))
        if markers:
            found.append(Finding("marker", len(markers)))
        if has_credential(text):
            found.append(Finding("credential"))
        return found

    def leaks(self, text: str) -> bool:  # keeps the ConfidentialIndex contract
        return bool(self.check(text))

    def assert_trainee_safe(self, text: str) -> None:
        found = self.check(text)
        if found:
            raise ConfidentialLeak("text failed the confidentiality checks: "
                                   + ", ".join(sorted({f.kind for f in found})))


def check_text(guard: ContentGuard | None, text: str) -> list[Finding]:
    """Findings for `text`, using a fresh marker/credential-only guard when none was loaded."""
    return (guard or ContentGuard()).check(text)
