"""Validate CONTRIBUTORS.md and decide trusted-contributor eligibility.

Standard library only, offline except for the optional GitHub identity lookup.

Two rules govern everything here:

* The registry is data. Nothing in it is executed, and a row never authorizes anyone by being
  proposed. Eligibility is read only from a trusted git ref (the protected default branch),
  never from a pull request's copy of the file.
* A check that passes means "the change is well formed". It is not approval. Founder approval
  of registry changes is a repository ruleset and CODEOWNERS matter (see
  docs/governance/contributor-registry-automation.md); Actions cannot enforce it.

Usage:
  contributor_registry.py check --base-file F --head-file F --author-id N --author-login L
                                [--changed-files a,b] [--no-verify]
  contributor_registry.py eligible --git-ref REF --account-id N
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

REGISTRY_PATH = "CONTRIBUTORS.md"
FOUNDER_MARKER = "Founder"
HEADER = ("GitHub username", "GitHub numeric account ID", "Approved on (UTC)")

USERNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
ACCOUNT_ID = re.compile(r"^[1-9][0-9]{0,15}$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ROW_LINK = re.compile(r"^\[@([^\]]+)\]\(https://github\.com/([^)\s]+)\)$")


class IdentityUnavailable(Exception):
    """GitHub could not be asked, so the identity is neither confirmed nor refuted."""


@dataclass(frozen=True)
class Entry:
    username: str
    account_id: int
    approved: str

    @property
    def is_founder(self) -> bool:
        return self.approved == FOUNDER_MARKER


def safe(value: object, limit: int = 60) -> str:
    """Make untrusted text harmless to print: one line, printable ASCII, no backticks."""
    text = "".join(c if c.isascii() and c.isprintable() and c != "`" else "?" for c in str(value))
    return text[:limit] + ("..." if len(text) > limit else "")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse(text: str) -> tuple[list[Entry], list[str]]:
    """Parse the Registry table. Returns the valid entries and every problem found."""
    errors: list[str] = []
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "## Registry")
    except StopIteration:
        return [], ["There is no '## Registry' section."]
    table: list[tuple[int, str]] = []
    for number, line in enumerate(lines[start + 1 :], start=start + 2):
        if line.startswith("## "):
            break
        if line.strip().startswith("|"):
            table.append((number, line))
    if len(table) < 2:
        return [], ["The Registry section has no table."]
    if tuple(_cells(table[0][1])) != HEADER:
        errors.append(f"Line {table[0][0]}: the table header must be exactly: {' | '.join(HEADER)}.")
    entries: list[Entry] = []
    seen_ids: dict[int, int] = {}
    seen_names: dict[str, int] = {}
    for number, line in table[2:]:
        cells = _cells(line)
        if len(cells) != 3:
            errors.append(f"Line {number}: expected 3 columns, found {len(cells)}.")
            continue
        link, raw_id, approved = cells
        match = ROW_LINK.match(link)
        if not match:
            errors.append(f"Line {number}: the username must look like [@name](https://github.com/name).")
            continue
        name = match.group(1)
        row_ok = True
        if match.group(2) != name:
            errors.append(f"Line {number}: the link target does not match the username `{safe(name)}`.")
            row_ok = False
        if not USERNAME.match(name):
            errors.append(f"Line {number}: `{safe(name)}` is not a valid GitHub username.")
            row_ok = False
        if not ACCOUNT_ID.match(raw_id):
            errors.append(
                f"Line {number}: account ID `{safe(raw_id)}` must be a positive whole number "
                "with no leading zeros (find it at https://api.github.com/users/<username>)."
            )
            row_ok = False
        if approved != FOUNDER_MARKER and not _valid_date(approved):
            errors.append(f"Line {number}: 'Approved on' must be YYYY-MM-DD or `{FOUNDER_MARKER}`.")
            row_ok = False
        if not row_ok:
            continue
        account_id = int(raw_id)
        if account_id in seen_ids:
            errors.append(f"Line {number}: account ID {account_id} is already listed on line {seen_ids[account_id]}.")
        if name.lower() in seen_names:
            errors.append(f"Line {number}: username `{safe(name)}` is already listed on line {seen_names[name.lower()]}.")
        seen_ids.setdefault(account_id, number)
        seen_names.setdefault(name.lower(), number)
        entries.append(Entry(name, account_id, approved))
    if not errors and not entries:
        errors.append("The registry must list at least one contributor.")
    return entries, errors


def _valid_date(value: str) -> bool:
    if not ISO_DATE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class Change:
    added: tuple[Entry, ...]
    removed: tuple[Entry, ...]
    modified: tuple[tuple[Entry, Entry], ...]  # (before, after), same account ID

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.modified)


def diff(base: list[Entry], head: list[Entry]) -> Change:
    """Compare by account ID, the authoritative key. A renamed row is a modification."""
    before = {e.account_id: e for e in base}
    after = {e.account_id: e for e in head}
    return Change(
        added=tuple(after[i] for i in after if i not in before),
        removed=tuple(before[i] for i in before if i not in after),
        modified=tuple((before[i], after[i]) for i in before if i in after and before[i] != after[i]),
    )


@dataclass
class Result:
    kind: str = "none"  # none | registration | removal | founder-change | bootstrap | invalid
    author_state: str = "external"  # trusted | registration-request | external
    errors: list[str] = None  # type: ignore[assignment]
    notices: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.errors = self.errors or []
        self.notices = self.notices or []

    @property
    def ok(self) -> bool:
        return not self.errors


def evaluate(
    base_text: str | None,
    head_text: str | None,
    changed_files: set[str],
    author_id: int,
    author_login: str,
) -> Result:
    """Classify a pull request's effect on the registry. Never grants anything.

    `base_text` must come from the protected base branch. `head_text` is the PR's version and
    is only ever compared against it.
    """
    result = Result()
    base: list[Entry] = []
    if base_text is not None:
        base, base_errors = parse(base_text)
        if base_errors:
            result.kind = "invalid"
            result.errors.append("The registry on the base branch is itself invalid; fix it first.")
            result.errors += [f"base: {e}" for e in base_errors]
            return result
    author_is_trusted = any(e.account_id == author_id for e in base)
    author_is_founder = any(e.account_id == author_id and e.is_founder for e in base)
    result.author_state = "trusted" if author_is_trusted else "external"

    touched = REGISTRY_PATH in changed_files
    if not touched and head_text == base_text:
        return result
    if head_text is None:
        result.kind = "invalid"
        result.errors.append(f"{REGISTRY_PATH} must not be deleted.")
        return result
    head, head_errors = parse(head_text)
    if head_errors:
        result.kind = "invalid"
        result.errors += head_errors
        return result
    if base_text is None:
        result.kind = "bootstrap"
        result.notices.append(
            f"{REGISTRY_PATH} does not exist on the base branch. This introduces it. Founder "
            "review is still required; nothing in this file is authoritative until it is merged."
        )
        return result

    change = diff(base, head)
    if change.empty:
        return result
    if author_is_founder:
        result.kind = "founder-change"
        result.notices.append(
            "The author is the founder row on the base branch, so this registry change is "
            "well formed for founder maintenance. It still needs the usual review."
        )
        return result

    problems: list[str] = []
    for before, after in change.modified:
        problems.append(
            f"The existing record for account ID {before.account_id} (`{safe(before.username)}`) "
            "was modified. Existing records can only be changed by the founder."
        )
    if change.added and (change.removed or change.modified):
        problems.append("A registration may only add a row; it must not also remove or edit rows.")
    if len(change.added) > 1:
        problems.append("A registration may add only one row: your own.")
    if change.added and not (change.removed or change.modified) and len(change.added) == 1:
        row = change.added[0]
        if row.account_id != author_id or row.username.lower() != author_login.lower():
            problems.append(
                f"The added row (`{safe(row.username)}`, ID {row.account_id}) is not the pull "
                f"request author (`{safe(author_login)}`, ID {author_id}). You may register only yourself."
            )
        if row.is_founder:
            problems.append(f"`{FOUNDER_MARKER}` cannot be claimed in a registration.")
        extra = changed_files - {REGISTRY_PATH}
        if extra:
            problems.append(f"A registration must change only {REGISTRY_PATH}; it also changes other files.")
        if not problems:
            result.kind = "registration"
            result.author_state = "registration-request"
            result.notices.append(
                "This is a registration request. Passing this check does NOT make the author a "
                "trusted contributor. Trust begins only when the founder approves and merges it."
            )
            return result
    elif change.removed and not (change.added or change.modified):
        result.kind = "removal"
        result.notices.append(
            "This removes trusted-contributor records. It needs founder approval. Removing a row "
            "does not revoke GitHub collaborator or team permissions; that is a separate step."
        )
        return result
    result.kind = "invalid"
    result.errors += problems or ["This registry change is not a recognised registration or removal."]
    return result


FetchUser = Callable[[int], "str | None"]


def fetch_github_login(account_id: int, token: str | None = None) -> str | None:
    """The current login for a numeric account ID, None if no such account.

    Raises IdentityUnavailable when GitHub cannot be asked (network, rate limit, 5xx).
    """
    request = urllib.request.Request(  # noqa: S310 - fixed https URL, integer path component
        f"https://api.github.com/user/{int(account_id)}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "viharbot-contributor-registry"},
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310  # nosec B310
            return json.load(response).get("login")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise IdentityUnavailable(f"GitHub answered HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        raise IdentityUnavailable(type(error).__name__) from None


def verify_identities(entries: list[Entry], fetch_user: FetchUser) -> list[str]:
    """Confirm each entry's username is the current login of its account ID."""
    problems: list[str] = []
    for entry in entries:
        try:
            login = fetch_user(entry.account_id)
        except IdentityUnavailable as error:
            problems.append(
                f"Could not verify account ID {entry.account_id} with GitHub ({safe(error)}). "
                "The check fails rather than assuming the identity is genuine; re-run it."
            )
            continue
        if login is None:
            problems.append(f"GitHub has no account with ID {entry.account_id}.")
        elif login.lower() != entry.username.lower():
            problems.append(
                f"Account ID {entry.account_id} is currently `{safe(login)}` on GitHub, not "
                f"`{safe(entry.username)}`. Fix the username or the ID."
            )
    return problems


def eligibility(base_text: str | None, account_id: int) -> str:
    """'trusted' only if the account ID is in the (trusted-ref) registry, else 'external'."""
    if base_text is None:
        return "external"
    entries, errors = parse(base_text)
    if errors:
        raise ValueError("the registry is invalid; refusing to decide eligibility")
    return "trusted" if any(e.account_id == account_id for e in entries) else "external"


def read_at_ref(ref: str, path: str = REGISTRY_PATH) -> str | None:
    """Read a file from a git ref, not from the working tree. None if it does not exist."""
    shown = subprocess.run(  # noqa: S603  # nosec B603
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True, check=False
    )
    return shown.stdout if shown.returncode == 0 else None


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def render(result: Result, problems: list[str], verified: bool) -> str:
    status = "PASS" if result.ok and not problems else "FAIL"
    lines = [
        "### Contributor registry check",
        "",
        f"* Result: **{status}**",
        f"* Registry change: `{result.kind}`",
        f"* Author's status on the protected base branch: `{result.author_state}`",
        f"* GitHub identity verification: {'done' if verified else 'not run'}",
        "",
        "Passing means the change is well formed. It is not approval and grants no trust.",
        "External contributors may still submit ordinary pull requests.",
    ]
    if result.notices:
        lines += ["", "**Notes**", *[f"* {n}" for n in result.notices]]
    if result.errors or problems:
        lines += ["", "**Fix these**", *[f"* {e}" for e in [*result.errors, *problems]]]
    return "\n".join(lines) + "\n"


def run_check(args: argparse.Namespace, fetch_user: FetchUser | None = None) -> int:
    changed = {f for f in (args.changed_files or "").split(",") if f}
    result = evaluate(_read(args.base_file), _read(args.head_file), changed, args.author_id, args.author_login)
    problems: list[str] = []
    verified = False
    if result.ok and result.kind in {"registration", "founder-change", "bootstrap"} and not args.no_verify:
        head, _ = parse(_read(args.head_file) or "")
        base_text = _read(args.base_file)
        base = parse(base_text)[0] if base_text else []
        changed_entries = [e for e in head if e not in base]
        problems = verify_identities(
            changed_entries,
            fetch_user or (lambda i: fetch_github_login(i, os.environ.get("GITHUB_TOKEN"))),
        )
        verified = True
    report = render(result, problems, verified)
    sys.stdout.write(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report)
    return 0 if result.ok and not problems else 1


def run_eligible(args: argparse.Namespace) -> int:
    try:
        decision = eligibility(read_at_ref(args.git_ref), args.account_id)
    except ValueError as error:
        sys.stderr.write(f"{error}\n")
        return 2
    print(decision)
    return 0 if decision == "trusted" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--base-file", required=True)
    check.add_argument("--head-file", required=True)
    check.add_argument("--author-id", type=int, required=True)
    check.add_argument("--author-login", required=True)
    check.add_argument("--changed-files", default="")
    check.add_argument("--no-verify", action="store_true")
    eligible = sub.add_parser("eligible")
    eligible.add_argument("--git-ref", required=True)
    eligible.add_argument("--account-id", type=int, required=True)
    args = parser.parse_args(argv)
    return run_check(args) if args.command == "check" else run_eligible(args)


if __name__ == "__main__":
    sys.exit(main())
