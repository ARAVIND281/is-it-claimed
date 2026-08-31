"""Check many issues at once, from a GitHub search query or an explicit list.

Checking one issue answers "can I take this?". The question people actually
have is "of these forty, which can I take?" — and answering it by hand is the
tedium this package exists to remove.

Each issue costs two or three API calls, so a forty-issue sweep is a hundred
requests. Two things keep that workable: a small thread pool, and stopping
early when the rate limit is nearly gone rather than emitting a wall of
identical 403s.
"""

from __future__ import annotations

import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from is_it_claimed.core import GitHubError, Verdict, _get, check

__all__ = ["BatchResult", "search_issues", "check_many"]

# Below this many remaining requests, stop and report rather than burning the
# rest of the user's hourly quota on a sweep they can restart later.
_RATE_FLOOR = 15


@dataclass
class BatchResult:
    """Outcome of checking several issues."""

    verdicts: list[Verdict] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    stopped_early: str | None = None

    @property
    def available(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.summary == "AVAILABLE"]

    @property
    def claimed(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.summary == "CLAIMED"]


def rate_remaining() -> int | None:
    """Requests left this hour, or None when the endpoint is unavailable."""
    try:
        data = _get("/rate_limit")
    except GitHubError:
        return None
    try:
        return int(data["resources"]["core"]["remaining"])
    except (KeyError, TypeError, ValueError):
        return None


def search_issues(query: str, *, limit: int = 40) -> list[str]:
    """Run a GitHub issue search and return ``owner/repo#number`` targets.

    ``is:issue`` and ``is:open`` are added when absent — a search that returns
    pull requests or closed issues wastes the sweep on things nobody can claim.
    """
    lowered = query.lower()
    parts = [query]
    if "is:issue" not in lowered and "type:issue" not in lowered:
        parts.append("is:issue")
    if "is:open" not in lowered and "state:" not in lowered:
        parts.append("is:open")
    full = " ".join(parts)

    per_page = min(limit, 100)
    encoded = urllib.parse.quote(full)
    data = _get(f"/search/issues?q={encoded}&per_page={per_page}&sort=created&order=desc")

    targets = []
    for item in data.get("items", [])[:limit]:
        url = item.get("html_url", "")
        # https://github.com/owner/repo/issues/123 -> owner/repo#123
        bits = url.split("/")
        if len(bits) >= 7:
            targets.append(f"{bits[3]}/{bits[4]}#{bits[6]}")
    return targets


def check_many(
    targets: list[str],
    *,
    include_forks: bool = False,
    workers: int = 6,
    respect_rate_limit: bool = True,
) -> BatchResult:
    """Check every target, concurrently.

    A failure on one issue is recorded and the sweep continues — one deleted
    repository should not discard thirty-nine good answers.
    """
    result = BatchResult()
    if not targets:
        return result

    if respect_rate_limit:
        remaining = rate_remaining()
        # Each issue costs ~3 calls; refuse to start a sweep that cannot finish.
        if remaining is not None and remaining < len(targets) * 3 + _RATE_FLOOR:
            affordable = max(0, (remaining - _RATE_FLOOR) // 3)
            if affordable == 0:
                result.stopped_early = (
                    f"rate limit too low to start ({remaining} requests left) — "
                    "set GITHUB_TOKEN or run `gh auth login`"
                )
                return result
            result.stopped_early = (
                f"only {remaining} requests left this hour; checked the first "
                f"{affordable} of {len(targets)}"
            )
            targets = targets[:affordable]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(check, target, include_forks=include_forks): target
            for target in targets
        }
        for future in as_completed(futures):
            target = futures[future]
            try:
                result.verdicts.append(future.result())
            except (GitHubError, ValueError) as exc:
                result.errors.append((target, str(exc)))

    # Deterministic output: the caller sees the same order every run.
    result.verdicts.sort(key=lambda v: v.target)
    result.errors.sort()
    return result
