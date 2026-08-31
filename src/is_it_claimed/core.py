"""Work out whether a GitHub issue is already being worked on.

A claim hides in four places, and looking in only some of them is how two
people end up writing the same patch:

1. ``assignee``            — the explicit signal, and the least used.
2. a **comment** saying so — "I'll take this", "working on it", …
3. a **cross-referenced PR** — someone opened a PR that mentions the issue but
   never commented on the issue itself. This is the one people miss: GitHub
   shows it in the issue's timeline, not in its comments, and it is invisible
   to ``gh issue view``.
4. a **fork with a matching branch** — work in progress, no PR yet.

Only the GitHub REST API is used, through :mod:`urllib`, so the package has no
runtime dependencies.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = ["Signal", "Verdict", "check", "parse_target", "GitHubError"]

_API = "https://api.github.com"

# Phrases people actually use to claim an issue. Deliberately conservative:
# "can I work on this?" is a question, not a claim, and matching it would tell
# you an issue is taken when it is merely being discussed.
_CLAIM_PATTERNS = [
    r"\bi(?:'| a)?m (?:currently )?working on (?:this|it)\b",
    r"\bi(?:'ll| will) (?:take|work on|pick (?:this )?up|have a go at) (?:this|it)\b",
    r"\bi(?:'d| would) like to (?:work on|take|tackle) (?:this|it)\b",
    r"\b(?:taking|claiming) (?:this|it)\b",
    r"\blet me (?:take|try) (?:this|it)\b",
    r"\bon it\b",
    r"\bassign (?:this )?to me\b",
    r"\bplease assign me\b",
    r"\bpr (?:is )?(?:incoming|coming|up shortly|on the way)\b",
    r"\bworking on a (?:fix|patch|pr)\b",
]
_CLAIM_RE = re.compile("|".join(_CLAIM_PATTERNS), re.IGNORECASE)

# "owner/repo#123", a full issue URL, or "owner/repo 123".
_TARGET_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:github\.com/)?(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)"
    r"(?:/(?:issues|pull)/|#|\s+)(?P<number>\d+)/?$"
)


class GitHubError(RuntimeError):
    """The API could not be reached, or refused the request."""


@dataclass
class Signal:
    """One piece of evidence that an issue is (or is not) spoken for."""

    kind: str  # assignee | comment | cross_referenced_pr | fork_branch
    detail: str
    url: str | None = None
    actor: str | None = None
    weight: int = 1  # how strongly this implies the issue is taken

    def __str__(self) -> str:
        who = f"@{self.actor} " if self.actor else ""
        tail = f"  {self.url}" if self.url else ""
        return f"{who}{self.detail}{tail}"


@dataclass
class Verdict:
    """The answer, and everything it was based on."""

    target: str
    state: str  # open | closed
    title: str
    url: str
    signals: list[Signal] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    partial: list[str] = field(default_factory=list)
    is_pull_request: bool = False

    @property
    def score(self) -> int:
        return sum(s.weight for s in self.signals)

    @property
    def claimed(self) -> bool:
        return self.score > 0

    @property
    def notes(self) -> list[Signal]:
        """Zero-weight findings: worth showing, not evidence of a claim."""
        return [s for s in self.signals if s.weight == 0]

    @property
    def confidence(self) -> str:
        """How sure we are, given that different signals carry different weight.

        A cross-referenced PR is near-proof. A fork branch alone is a hint —
        someone may have started and abandoned it months ago.
        """
        if not self.signals:
            return "none"
        if self.score >= 3:
            return "high"
        if self.score == 2:
            return "medium"
        return "low"

    @property
    def summary(self) -> str:
        if self.is_pull_request:
            return "IS A PULL REQUEST"
        if self.state == "closed":
            return "CLOSED"
        return "CLAIMED" if self.claimed else "AVAILABLE"


def parse_target(raw: str) -> tuple[str, str, int]:
    """Parse ``owner/repo#123``, a GitHub URL, or ``owner/repo 123``."""
    match = _TARGET_RE.match(raw.strip())
    if not match:
        raise ValueError(
            f"could not parse {raw!r} — expected owner/repo#123 or a github.com issue URL"
        )
    return match["owner"], match["repo"], int(match["number"])


def _token() -> str | None:
    """A token from the environment, or the one the gh CLI already stores.

    Unauthenticated callers get 60 requests/hour, which one check can exhaust,
    so reusing gh's credentials when they exist saves the user a setup step.
    """
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(var)
        if value:
            return value
    config = os.path.expanduser("~/.config/gh/hosts.yml")
    try:
        with open(config, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("oauth_token:"):
                    return stripped.split(":", 1)[1].strip()
    except OSError:
        pass

    # On macOS (and with a keyring on Linux) gh keeps the token in the system
    # credential store, so hosts.yml has none. Asking gh itself works whatever
    # backend it chose — without this, a user with `gh auth login` still gets
    # the 60/hour anonymous limit and no idea why.
    try:
        completed = subprocess.run(  # noqa: S603
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = completed.stdout.strip()
    return token or None


def _get(path: str, *, accept: str = "application/vnd.github+json") -> Any:
    """GET a REST path and return parsed JSON."""
    request = urllib.request.Request(f"{_API}{path}", headers={"Accept": accept})
    request.add_header("User-Agent", "is-it-claimed")
    token = _token()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise GitHubError(f"not found: {path}") from exc
        if exc.code in (401, 403):
            hint = (
                " (rate limited — set GITHUB_TOKEN or run `gh auth login`)"
                if not token
                else ""
            )
            raise GitHubError(f"refused ({exc.code}){hint}") from exc
        raise GitHubError(f"GitHub returned {exc.code} for {path}") from exc
    except urllib.error.URLError as exc:
        raise GitHubError(f"could not reach GitHub: {exc.reason}") from exc


def _issue(owner: str, repo: str, number: int) -> dict:
    return _get(f"/repos/{owner}/{repo}/issues/{number}")


def _assignee_signals(issue: dict) -> list[Signal]:
    return [
        Signal(
            kind="assignee",
            detail="is assigned to this issue",
            actor=user.get("login"),
            weight=3,
        )
        for user in issue.get("assignees") or []
    ]


def _comment_signals(
    owner: str, repo: str, number: int, *, stale_days: int = 90
) -> list[Signal]:
    """Comments in which somebody said they would take the issue.

    A claim decays like any other. "I'll take this" said eight months ago, with
    nothing since, is not a live claim — the same reasoning applied to abandoned
    pull requests, and for the same reason: treating it as current keeps people
    away from work that is in practice free again.
    """
    comments = _get(f"/repos/{owner}/{repo}/issues/{number}/comments?per_page=100")
    signals = []
    for comment in comments:
        body = comment.get("body") or ""
        if not _CLAIM_RE.search(body):
            continue
        snippet = " ".join(body.split())[:90]
        age = _days_since(comment.get("created_at"))
        if age is not None and age >= stale_days:
            detail = f'claimed it {age} days ago and has not followed up: "{snippet}"'
            weight = 1
        else:
            detail = f'claimed it in a comment: "{snippet}"'
            weight = 2
        signals.append(
            Signal(
                kind="comment",
                detail=detail,
                url=comment.get("html_url"),
                actor=(comment.get("user") or {}).get("login"),
                weight=weight,
            )
        )
    return signals



def _days_since(timestamp: str | None) -> int | None:
    """Whole days between an ISO-8601 GitHub timestamp and now, or None."""
    if not timestamp:
        return None
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, (datetime.now(timezone.utc) - moment).days)


def _cross_reference_signals(
    owner: str, repo: str, number: int, *, issue_state: str = "open", stale_days: int = 90
) -> list[Signal]:
    """PRs that reference this issue — the signal `gh issue view` does not show.

    A cross-reference is created by *any* mention of the issue number, not only
    by a pull request that fixes it. Telling the two apart matters, because
    reporting a passing mention as a claim sends people away from work that is
    genuinely free.

    The discriminator is cheap: **a merged PR that did not close the issue was
    not a fix for it.** If the issue is still open, that merge referenced it in
    passing, and the issue is if anything more likely to be available, not less.
    """
    events = _get(f"/repos/{owner}/{repo}/issues/{number}/timeline?per_page=100")
    signals = []
    for event in events:
        if event.get("event") != "cross-referenced":
            continue
        source = (event.get("source") or {}).get("issue") or {}
        if not source.get("pull_request"):
            continue  # another issue referencing this one is not a claim
        state = source.get("state", "?")
        merged = (source.get("pull_request") or {}).get("merged_at")
        draft = bool(source.get("draft"))
        idle = _days_since(source.get("updated_at"))

        if merged and issue_state == "open":
            # Merged, yet this issue is still open: it mentioned the issue, it
            # did not resolve it. Barely evidence of anything.
            label, weight = "mentioned this in a merged PR (which did not close it)", 0
        elif merged:
            label, weight = "merged a PR for this", 4
        elif state == "open":
            label, weight = "has an OPEN PR for this", 4
            # An open PR nobody has touched for months is not a live claim. The
            # work may exist, but the person has moved on, and treating it the
            # same as this morning's PR sends people away from issues that are
            # in practice free again.
            if idle is not None and idle >= stale_days:
                label = f"has an open but STALE PR ({idle} days idle)"
                weight = 1
            if draft:
                # A draft is explicitly "not ready for review" — weaker than a
                # PR its author has put up for merging.
                label += " [draft]"
                weight = max(1, weight - 1)
        else:
            label, weight = "opened a PR for this (since closed)", 1
        signals.append(
            Signal(
                kind="cross_referenced_pr",
                detail=f"{label} — {source.get('title', '')[:60]}",
                url=source.get("html_url"),
                actor=(event.get("actor") or {}).get("login"),
                weight=weight,
            )
        )
    return signals



# Dotted version numbers inside a branch name ("release-2.1.10") contain digits
# that are not issue references. They are removed before looking for the issue
# number, or issue 1 matches every 2.1.x release branch.
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


def _branch_names_issue(name: str, number: int) -> bool:
    """True when a branch name refers to this issue number.

    Matches the shapes people actually use — ``fix/123``, ``issue-123``,
    ``gh123``, ``123-short-description`` — while refusing digits that are part
    of a version string or of a longer number.
    """
    stripped = _VERSION_RE.sub(" ", name)
    return re.search(rf"(?:^|[^0-9]){number}(?:[^0-9]|$)", stripped) is not None


def _fork_branch_signals(owner: str, repo: str, number: int, limit: int = 30) -> list[Signal]:
    """Forks with a branch naming this issue — work started, no PR yet.

    Only the most recently pushed forks are examined; a repository can have
    thousands, and an old fork tells you nothing.
    """
    forks = _get(f"/repos/{owner}/{repo}/forks?sort=newest&per_page={limit}")
    signals = []
    for fork in forks:
        login = (fork.get("owner") or {}).get("login")
        if not login:
            continue
        try:
            branches = _get(f"/repos/{login}/{repo}/branches?per_page=100")
        except GitHubError:
            continue  # fork may be private, renamed or deleted mid-scan
        for branch in branches:
            name = branch.get("name", "")
            if _branch_names_issue(name, number):
                signals.append(
                    Signal(
                        kind="fork_branch",
                        detail=f"has a branch named '{name}' on their fork",
                        url=f"https://github.com/{login}/{repo}/tree/{name}",
                        actor=login,
                        weight=1,
                    )
                )
                break
    return signals


def check(
    target: str,
    *,
    include_forks: bool = False,
    fork_limit: int = 30,
    stale_days: int = 90,
) -> Verdict:
    """Decide whether ``target`` is already spoken for.

    Args:
        target: ``owner/repo#123``, a github.com issue URL, or ``owner/repo 123``.
        include_forks: also scan recent forks for a branch naming the issue.
            Off by default — it costs one request per fork.
        fork_limit: how many recent forks to scan when ``include_forks``.
        stale_days: an open PR untouched for this long is treated as a weak
            signal rather than a live claim.

    Returns:
        A :class:`Verdict`. Checks that fail individually are recorded in
        ``partial`` rather than aborting: a rate limit on the fork scan should
        not throw away a definitive answer already found in the timeline.
    """
    owner, repo, number = parse_target(target)
    issue = _issue(owner, repo, number)

    verdict = Verdict(
        target=f"{owner}/{repo}#{number}",
        state=issue.get("state", "?"),
        title=issue.get("title", ""),
        url=issue.get("html_url", ""),
        is_pull_request=issue.get("pull_request") is not None,
    )
    if verdict.is_pull_request:
        # GitHub's issues endpoint returns pull requests too. Analysing one for
        # "claims" is meaningless — it is already somebody's work.
        return verdict

    verdict.signals.extend(_assignee_signals(issue))
    verdict.checked.append("assignee")

    for name, fetch in (
        ("comments", lambda: _comment_signals(owner, repo, number, stale_days=stale_days)),
        ("linked PRs", lambda: _cross_reference_signals(
            owner, repo, number, issue_state=verdict.state, stale_days=stale_days
        )),
    ):
        try:
            verdict.signals.extend(fetch())
            verdict.checked.append(name)
        except GitHubError as exc:
            verdict.partial.append(f"{name}: {exc}")

    if include_forks:
        try:
            verdict.signals.extend(_fork_branch_signals(owner, repo, number, fork_limit))
            verdict.checked.append("fork branches")
        except GitHubError as exc:
            verdict.partial.append(f"fork branches: {exc}")

    return verdict
