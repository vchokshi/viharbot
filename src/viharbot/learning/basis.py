"""How well a piece of evidence is known. A leaf module: it imports nothing from the package, so the
trainee side (`ada.py`, `lesson.py`) can use it without reaching any instructor or GitHub code."""

from enum import StrEnum


class Basis(StrEnum):
    VERIFIED_OBSERVATION = "verified_observation"
    AUTHOR_STATEMENT = "author_statement"
    SAGE_ADVISORY = "sage_advisory"
    KEPLER_HANDOFF = "kepler_handoff"
    INSTRUCTOR_GUIDANCE = "instructor_guidance"
    UNSUPPORTED = "unsupported"
