"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys

from is_it_claimed import __version__
from is_it_claimed.core import GitHubError, Verdict, check

_COLOUR = {
    "AVAILABLE": "\033[32m",
    "CLAIMED": "\033[31m",
    "CLOSED": "\033[33m",
    "IS A PULL REQUEST": "\033[33m",
}
_OFF = "\033[0m"


def _render(verdict: Verdict, *, colour: bool) -> str:
    tint = _COLOUR.get(verdict.summary, "") if colour else ""
    off = _OFF if colour and tint else ""
    lines = [f"{tint}{verdict.summary}{off}"]
    if verdict.summary == "CLAIMED":
        lines[0] += f" ({verdict.confidence} confidence)"
    lines.append(f"  {verdict.target}  {verdict.title[:70]}")
    lines.append("")

    if verdict.is_pull_request:
        lines.append("  that is a pull request, not an issue — nothing to claim")
        return "\n".join(lines)

    if verdict.signals:
        for signal in sorted(verdict.signals, key=lambda s: -s.weight):
            lines.append(f"  ! {signal}")
    else:
        checked = ", ".join(verdict.checked) or "nothing"
        lines.append(f"  no claim found in: {checked}")

    for note in verdict.partial:
        lines.append(f"  ? could not check {note}")

    if verdict.state == "closed":
        lines.append("  ! the issue itself is closed")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="is-it-claimed",
        description="Check whether a GitHub issue is already being worked on.",
        epilog="exit codes: 0 available, 1 claimed, 2 error",
    )
    parser.add_argument("target", help="owner/repo#123, or a github.com issue URL")
    parser.add_argument(
        "--forks",
        action="store_true",
        help="also scan recent forks for a branch naming the issue (slower)",
    )
    parser.add_argument(
        "--fork-limit", type=int, default=30, metavar="N",
        help="how many recent forks to scan with --forks (default 30)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-colour", action="store_true", help="disable colour")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    try:
        verdict = check(args.target, include_forks=args.forks, fork_limit=args.fork_limit)
    except (ValueError, GitHubError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "target": verdict.target,
            "summary": verdict.summary,
            "claimed": verdict.claimed,
            "confidence": verdict.confidence,
            "state": verdict.state,
            "title": verdict.title,
            "url": verdict.url,
            "checked": verdict.checked,
            "partial": verdict.partial,
            "signals": [
                {"kind": s.kind, "actor": s.actor, "detail": s.detail, "url": s.url}
                for s in verdict.signals
            ],
        }, indent=2))
    else:
        colour = not args.no_colour and sys.stdout.isatty()
        print(_render(verdict, colour=colour))

    # Closed counts as "do not start": an issue with no claim signals is still
    # not something to pick up once it is closed.
    return 1 if (verdict.claimed or verdict.state == "closed" or verdict.is_pull_request) else 0


if __name__ == "__main__":
    raise SystemExit(main())
