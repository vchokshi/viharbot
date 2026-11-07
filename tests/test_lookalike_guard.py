"""Look-alike typos in model aliases and configuration must fail the build.

Why this exists: a model was published on the LiteLLM gateway as `openal/gpt-5.4-mini` (a lowercase
L where the I of `openai` belongs). The two letters are near-identical in most fonts and sit next
to each other on the keyboard, so the typo survived for at least a week, including in the docs, and
deleting it later took a working deployment with it. These checks make that class of mistake fail
in CI instead of being found in production:

1. The literal `openal` may not appear in tracked files, except in the few documents that tell the
   story of the typo.
2. Every model alias configured in Compose, `.env.example` and the Stage spec must be on an explicit
   allowlist. An allowlist catches any misspelling, not only this one. Adding or changing an alias
   means editing APPROVED_ALIASES below, which a reviewer sees.
3. Configuration files are ASCII only, so a Unicode look-alike (a Cyrillic letter that is
   indistinguishable from a Latin one) cannot hide in an alias, a URL or a command.

Static checks on the repository's own files; no network, no model call.
"""

from __future__ import annotations

import difflib
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The aliases the application is configured to use. To add or change one: edit this set, in the same
# change that edits the configuration, after checking it with a one-token request to the gateway
# (GET /v1/models is not a complete list of what the gateway accepts).
APPROVED_ALIASES = {
    "gpt-6-luna",
    "gpt-6.1-sol",
    "gpt-5.4-mini",
    "openai/gpt-5.4",
    "gemini/gemini-3.5-flash",
}
PROVIDERS = ("openai", "gemini")

# Documents that discuss the `openal` typo by name. Nothing else may contain it.
TYPO_STORY = {
    "docs/audits/phase-6.6-release-readiness.md",
    "docs/sage-service.md",
    "tests/test_lookalike_guard.py",
}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pptx", ".woff", ".woff2", ".pdf", ".zip"}

ASCII_ONLY_PATTERNS = ("compose.yaml", ".env.example", "Dockerfile*", "deploy/**/*.yaml", "deploy/**/*.json",
                       ".github/workflows/*.yml", ".github/dependabot.yml", "pyproject.toml", "requirements.txt")


def tracked_files() -> list[Path]:
    listed = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if listed.returncode == 0 and listed.stdout.strip():
        names = listed.stdout.split("\n")
    else:  # not a git checkout: walk the tree, skipping what is never tracked
        skip = {".git", ".venv", "venv", "node_modules", "__pycache__", "data"}
        names = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                 if p.is_file() and not skip & set(p.relative_to(ROOT).parts)]
    return [ROOT / n for n in names if n and (ROOT / n).is_file()]


def alias_problem(alias: str) -> str | None:
    """Why this alias is not acceptable, or None. Pure, so it can be tested with bad examples."""
    if not alias.isascii():
        return "contains a non-ASCII character (a look-alike letter?)"
    if alias in APPROVED_ALIASES:
        return None
    hints = difflib.get_close_matches(alias, APPROVED_ALIASES, n=1, cutoff=0.8)
    prefix = alias.split("/", 1)[0] if "/" in alias else ""
    bad_prefix = prefix and prefix not in PROVIDERS
    reason = "is not an approved alias"
    if bad_prefix:
        close = difflib.get_close_matches(prefix, PROVIDERS, n=1, cutoff=0.7)
        reason = f"has an unknown provider prefix {prefix!r}" + (f" (did you mean {close[0]!r}?)" if close else "")
    elif hints:
        reason += f" (did you mean {hints[0]!r}?)"
    return reason


def configured_aliases() -> list[tuple[str, int, str, str]]:
    """(file, line, variable, alias) for every *_MODEL default in the shipped configuration."""
    found = []
    compose = re.compile(r"^\s*([A-Z_]+_MODEL):\s*\$\{[A-Z_]+_MODEL:-([^}]*)\}")
    env = re.compile(r"^([A-Z_]+_MODEL)=(\S*)$")
    for path, pattern in (("compose.yaml", compose), (".env.example", env)):
        for number, line in enumerate((ROOT / path).read_text().splitlines(), 1):
            match = pattern.match(line)
            if match and match.group(2):
                found.append((path, number, match.group(1), match.group(2)))
    spec = (ROOT / "deploy/digitalocean/app.yaml").read_text().splitlines()
    for number, line in enumerate(spec):
        match = re.match(r"^\s*-\s*key:\s*([A-Z_]+_MODEL)\s*$", line)
        if match and number + 1 < len(spec):
            value = re.match(r"^\s*value:\s*(\S+)\s*$", spec[number + 1])
            if value:
                found.append(("deploy/digitalocean/app.yaml", number + 2, match.group(1), value.group(1)))
    return found


# ---- 1. the literal typo ---------------------------------------------------------------------
def test_the_openal_typo_appears_nowhere_except_where_it_is_explained():
    offenders = []
    for path in tracked_files():
        rel = str(path.relative_to(ROOT))
        if rel in TYPO_STORY or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if re.search(r"openal", line, re.IGNORECASE):
                offenders.append(f"{rel}:{number}")
    assert offenders == [], f"'openal' is a typo for 'openai' (l and i look alike): {offenders}"


# ---- 2. only approved aliases are configured --------------------------------------------------
def test_there_is_configuration_to_check():
    names = {variable for _, _, variable, _ in configured_aliases()}
    assert {"RESEARCH_MODEL", "SECURITY_MODEL", "PRIVACY_MODEL", "SYNTHESIS_MODEL", "CHARLIE_PLANNER_MODEL"} <= names


def test_every_configured_alias_is_on_the_approved_list():
    problems = [f"{f}:{n} {var}={alias!r} {alias_problem(alias)}"
                for f, n, var, alias in configured_aliases() if alias_problem(alias)]
    assert problems == []


def test_the_same_variable_has_the_same_alias_everywhere_it_is_set():
    seen: dict[str, set[str]] = {}
    for _, _, variable, alias in configured_aliases():
        seen.setdefault(variable, set()).add(alias)
    assert {v: a for v, a in seen.items() if len(a) > 1} == {}, "Compose, .env.example and the Stage spec disagree"


@pytest.mark.parametrize("alias,expect", [
    ("gpt-5.4-mini", None),
    ("openai/gpt-5.4", None),
    ("openal/gpt-5.4-mini", "openai"),         # the real typo: lowercase L for I
    ("openal/gpt-5.4", "openai"),
    ("openaI/gpt-5.4", "openai"),              # capital I that looks like l
    ("0penai/gpt-5.4", "openai"),              # zero for o
    ("gemini/gemin1-3.5-flash", "gemini"),     # digit one for i
    ("gpt-5.4-minl", "gpt-5.4-mini"),          # same trick inside a name
    ("openai/gpt-5.4-mini", "not an approved"),  # well formed, but not deliberately approved
    ("opеnai/gpt-5.4", "non-ASCII"),           # Cyrillic е inside "openai"
])
def test_the_detector_catches_look_alike_aliases(alias, expect):
    problem = alias_problem(alias)
    if expect is None:
        assert problem is None
    else:
        assert problem and expect in problem, (alias, problem)


# ---- 3. no Unicode look-alikes in configuration -----------------------------------------------
def test_configuration_files_are_ascii_only():
    offenders = []
    for pattern in ASCII_ONLY_PATTERNS:
        for path in ROOT.glob(pattern):
            if path.is_file():
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    bad = sorted({c for c in line if ord(c) > 127})
                    if bad:
                        offenders.append(f"{path.relative_to(ROOT)}:{number} {[f'U+{ord(c):04X}' for c in bad]}")
    assert offenders == [], f"non-ASCII characters in configuration (possible look-alikes): {offenders}"
