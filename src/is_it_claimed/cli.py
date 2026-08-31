"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys

from is_it_claimed import __version__
from is_it_claimed.batch import BatchResult, check_many, search_issues
from is_it_claimed.core import GitHubError, Verdict, check

_COLOUR = {
    "AVAILABLE": "\033[32m",
    "CLAIMED": "\033[31m",
    "CLOSED": "\033[33m",
    "IS A PULL REQUEST": "\033[33m",
}
_OFF = "\033[0m"
_DIM = "\033[2m"


def _tint(text: str, summary: str, *, colour: bool) -> str:
    if not colour:
        return text
    shade = _COLOUR.get(summary, "")
    return f"{shade}{text}{_OFF}" if shade else text


def _render(verdict: Verdict, *, colour: bool) -> str:
    lines = [_tint(verdict.summary, verdict.summary, colour=colour)]
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


def _render_batch(result: BatchResult, *, colour: bool, available_only: bool) -> str:
    """One line per issue — a sweep is for scanning, not for reading in depth."""
    lines = []
    shown = result.available if available_only else result.verdicts

    counts = (
        f"{len(result.verdicts)} checked, "
        f"{len(result.available)} available, "
        f"{len(result.claimed)} claimed"
    )
    if result.errors:
        counts += f", {len(result.errors)} failed"
    lines.append(counts)
    if result.stopped_early:
        lines.append(f"  ! {result.stopped_early}")
    lines.append("")

    if not shown:
        lines.append("  nothing available" if available_only else "  nothing to show")
    for verdict in shown:
        label = _tint(f"{verdict.summary:<9}", verdict.summary, colour=colour)
        lines.append(f"  {label} {verdict.target:<32} {verdict.title[:52]}")
        # Why it is taken matters more than that it is taken.
        if verdict.summary == "CLAIMED" and not available_only:
            top = max(verdict.signals, key=lambda s: s.weight)
            note = f"            {top}"
            lines.append(f"{_DIM}{note}{_OFF}" if colour else note)

    for target, message in result.errors:
        lines.append(f"  {'ERROR':<9} {target:<32} {message[:52]}")
    return "\n".join(lines)


def _verdict_payload(verdict: Verdict) -> dict:
    return {
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
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="is-it-claimed",
        description="Check whether a GitHub issue is already being worked on.",
        epilog=(
            "exit codes: 0 available (or at least one available, in a sweep), "
            "1 claimed / none available, 2 error"
        ),
    )
    parser.add_argument(
        "targets",
        nargs="*",
        metavar="TARGET",
        help="owner/repo#123, or a github.com issue URL. Several may be given.",
    )
    parser.add_argument(
        "--search",
        metavar="QUERY",
        help=(
            "check every issue matching a GitHub search query, e.g. "
            "'repo:psf/black label:\"good first issue\" no:assignee'. "
            "is:issue and is:open are added when absent."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=40, metavar="N",
        help="most issues to take from --search (default 40)",
    )
    parser.add_argument(
        "--available-only", action="store_true",
        help="in a sweep, list only the issues nobody has claimed",
    )
    parser.add_argument(
        "--workers", type=int, default=6, metavar="N",
        help="concurrent checks during a sweep (default 6)",
    )
    parser.add_argument(
        "--forks", action="store_true",
        help="also scan recent forks for a branch naming the issue (slower)",
    )
    parser.add_argument(
        "--fork-limit", type=int, default=30, metavar="N",
        help="how many recent forks to scan with --forks (default 30)",
    )
    parser.add_argument(
        "--stale-days", type=int, default=90, metavar="N",
        help=(
            "an open PR untouched for N days counts as a weak signal, not a "
            "live claim (default 90)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-colour", action="store_true", help="disable colour")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _run_single(args, colour: bool) -> int:
    verdict = check(
        args.targets[0],
        include_forks=args.forks,
        fork_limit=args.fork_limit,
        stale_days=args.stale_days,
    )
    if args.json:
        print(json.dumps(_verdict_payload(verdict), indent=2))
    else:
        print(_render(verdict, colour=colour))
    # Closed counts as "do not start": an issue with no claim signals is still
    # not something to pick up once it is closed.
    return 1 if (verdict.claimed or verdict.state == "closed" or verdict.is_pull_request) else 0


def _run_batch(args, targets: list[str], colour: bool) -> int:
    result = check_many(
        targets,
        include_forks=args.forks,
        workers=max(1, args.workers),
        stale_days=args.stale_days,
    )
    if args.json:
        print(json.dumps({
            "checked": len(result.verdicts),
            "available": len(result.available),
            "claimed": len(result.claimed),
            "stopped_early": result.stopped_early,
            "errors": [{"target": t, "error": e} for t, e in result.errors],
            "results": [_verdict_payload(v) for v in result.verdicts],
        }, indent=2))
    else:
        print(_render_batch(result, colour=colour, available_only=args.available_only))
    # In a sweep the useful question is "is there anything for me here?"
    return 0 if result.available else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.targets and not args.search:
        parser.error("give a TARGET, or use --search")
    if args.targets and args.search:
        parser.error("use TARGETs or --search, not both")

    colour = not args.no_colour and sys.stdout.isatty()

    try:
        if args.search:
            targets = search_issues(args.search, limit=args.limit)
            if not targets:
                print("no issues matched that search", file=sys.stderr)
                return 1
            return _run_batch(args, targets, colour)
        if len(args.targets) == 1:
            return _run_single(args, colour)
        return _run_batch(args, args.targets, colour)
    except (ValueError, GitHubError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
