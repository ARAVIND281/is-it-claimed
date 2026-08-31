"""Unit tests. Every GitHub call is stubbed — the suite makes no network requests."""

from __future__ import annotations

import pytest

from is_it_claimed import core
from is_it_claimed.cli import main


# --------------------------------------------------------------------------
# target parsing
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    [
        "owner/repo#123",
        "owner/repo 123",
        "https://github.com/owner/repo/issues/123",
        "http://github.com/owner/repo/issues/123",
        "github.com/owner/repo/issues/123",
        "https://github.com/owner/repo/issues/123/",
        "https://github.com/owner/repo/pull/123",
    ],
)
def test_parse_target_accepts_the_forms_people_actually_paste(raw):
    assert core.parse_target(raw) == ("owner", "repo", 123)


def test_parse_target_keeps_dots_and_dashes_in_names():
    assert core.parse_target("my-org/my.repo#7") == ("my-org", "my.repo", 7)


@pytest.mark.parametrize("raw", ["", "owner/repo", "not a target", "owner#123", "/repo#1"])
def test_parse_target_rejects_nonsense(raw):
    with pytest.raises(ValueError, match="could not parse"):
        core.parse_target(raw)


# --------------------------------------------------------------------------
# claim-phrase matching
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "body",
    [
        "I'll take this",
        "I am working on this",
        "I'm working on it",
        "Taking this one",
        "I'd like to work on this",
        "assign this to me please",
        "on it!",
        "PR incoming",
        "Working on a fix now",
        "Let me take this",
    ],
)
def test_claim_phrases_are_recognised(body):
    assert core._CLAIM_RE.search(body)


@pytest.mark.parametrize(
    "body",
    [
        "Can I work on this?",           # a question, not a claim
        "Is anyone working on this?",    # asking, not claiming
        "This is a good first issue",
        "I think the fix is in parser.py",
        "Thanks for reporting",
        "+1",
    ],
)
def test_discussion_is_not_mistaken_for_a_claim(body):
    """Over-matching is the expensive failure: it tells you an issue is taken
    when it is merely being discussed, and you skip work you could have done."""
    assert core._CLAIM_RE.search(body) is None


# --------------------------------------------------------------------------
# signal gathering
# --------------------------------------------------------------------------
def _issue(state="open", assignees=(), title="An issue"):
    return {
        "state": state,
        "title": title,
        "html_url": "https://github.com/o/r/issues/1",
        "assignees": [{"login": a} for a in assignees],
    }


def _crossref(actor, number, *, is_pr=True, state="open", merged=False):
    source = {
        "state": state,
        "title": f"PR {number}",
        "html_url": f"https://github.com/o/r/pull/{number}",
    }
    if is_pr:
        source["pull_request"] = {"merged_at": "2026-01-01T00:00:00Z" if merged else None}
    return {"event": "cross-referenced", "actor": {"login": actor}, "source": {"issue": source}}


def _routes(monkeypatch, *, issue=None, comments=(), timeline=(), forks=(), branches=None):
    """Stub core._get by matching on the path."""
    branches = branches or {}

    def fake_get(path, **_):
        if "/comments" in path:
            return list(comments)
        if "/timeline" in path:
            return list(timeline)
        if "/forks" in path:
            return list(forks)
        if "/branches" in path:
            owner = path.split("/repos/")[1].split("/")[0]
            return [{"name": n} for n in branches.get(owner, [])]
        return issue if issue is not None else _issue()

    monkeypatch.setattr(core, "_get", fake_get)


def test_an_untouched_issue_is_available(monkeypatch):
    _routes(monkeypatch)
    verdict = core.check("o/r#1")
    assert verdict.summary == "AVAILABLE"
    assert verdict.claimed is False
    assert verdict.confidence == "none"
    assert verdict.checked == ["assignee", "comments", "linked PRs"]


def test_an_open_cross_referenced_pr_is_the_signal_gh_issue_view_misses(monkeypatch):
    """The regression this tool exists for: no assignee, no comment, but a PR
    referencing the issue already exists."""
    _routes(monkeypatch, timeline=[_crossref("someone", 607)])
    verdict = core.check("o/r#1")
    assert verdict.summary == "CLAIMED"
    assert verdict.confidence == "high"
    kinds = [s.kind for s in verdict.signals]
    assert kinds == ["cross_referenced_pr"]
    assert verdict.signals[0].actor == "someone"


def test_an_assignee_alone_claims_it(monkeypatch):
    _routes(monkeypatch, issue=_issue(assignees=["dev"]))
    verdict = core.check("o/r#1")
    assert verdict.summary == "CLAIMED"
    assert [s.kind for s in verdict.signals] == ["assignee"]


def test_a_claiming_comment_is_found(monkeypatch):
    _routes(
        monkeypatch,
        comments=[{"body": "I'll take this", "user": {"login": "dev"}, "html_url": "u"}],
    )
    verdict = core.check("o/r#1")
    assert verdict.summary == "CLAIMED"
    assert verdict.signals[0].kind == "comment"


def test_a_question_in_the_comments_leaves_it_available(monkeypatch):
    _routes(
        monkeypatch,
        comments=[{"body": "Can I work on this?", "user": {"login": "dev"}, "html_url": "u"}],
    )
    assert core.check("o/r#1").summary == "AVAILABLE"


def test_an_issue_referencing_another_issue_is_not_a_claim(monkeypatch):
    """A cross-reference from an issue means someone linked it, not that work
    exists. Only a pull_request counts."""
    _routes(monkeypatch, timeline=[_crossref("someone", 99, is_pr=False)])
    assert core.check("o/r#1").summary == "AVAILABLE"


def test_a_closed_pr_is_weaker_than_an_open_one(monkeypatch):
    """Someone tried and stopped — worth seeing, not a blocker."""
    _routes(monkeypatch, timeline=[_crossref("someone", 5, state="closed")])
    verdict = core.check("o/r#1")
    assert verdict.confidence == "low"
    assert "since closed" in verdict.signals[0].detail


def test_a_merged_pr_is_reported_as_merged(monkeypatch):
    _routes(monkeypatch, timeline=[_crossref("someone", 5, state="closed", merged=True)])
    assert "merged a PR" in core.check("o/r#1").signals[0].detail


def test_signals_accumulate_into_confidence(monkeypatch):
    _routes(
        monkeypatch,
        comments=[{"body": "on it", "user": {"login": "dev"}, "html_url": "u"}],
        issue=_issue(assignees=["dev"]),
    )
    verdict = core.check("o/r#1")
    assert verdict.score == 5  # assignee 3 + comment 2
    assert verdict.confidence == "high"


# --------------------------------------------------------------------------
# fork scanning
# --------------------------------------------------------------------------
def test_fork_branch_naming_the_issue_is_a_weak_signal(monkeypatch):
    _routes(
        monkeypatch,
        forks=[{"owner": {"login": "dev"}}],
        branches={"dev": ["fix/issue-1"]},
    )
    verdict = core.check("o/r#1", include_forks=True)
    assert [s.kind for s in verdict.signals] == ["fork_branch"]
    assert verdict.confidence == "low"


def test_fork_branches_are_not_scanned_unless_asked(monkeypatch):
    _routes(monkeypatch, forks=[{"owner": {"login": "dev"}}], branches={"dev": ["fix/issue-1"]})
    verdict = core.check("o/r#1")
    assert verdict.signals == []
    assert "fork branches" not in verdict.checked


def test_a_branch_merely_containing_the_digit_does_not_match(monkeypatch):
    """Issue 1 must not match 'release-2.1.10'."""
    _routes(
        monkeypatch,
        forks=[{"owner": {"login": "dev"}}],
        branches={"dev": ["release-2.1.10", "feature-11"]},
    )
    assert core.check("o/r#1", include_forks=True).signals == []


# --------------------------------------------------------------------------
# partial failure
# --------------------------------------------------------------------------
def test_one_failing_check_does_not_discard_the_others(monkeypatch):
    """A rate limit on comments must not throw away a definitive timeline answer."""

    def fake_get(path, **_):
        if "/comments" in path:
            raise core.GitHubError("refused (403)")
        if "/timeline" in path:
            return [_crossref("someone", 607)]
        return _issue()

    monkeypatch.setattr(core, "_get", fake_get)
    verdict = core.check("o/r#1")
    assert verdict.summary == "CLAIMED"
    assert verdict.partial == ["comments: refused (403)"]
    assert "linked PRs" in verdict.checked


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def test_cli_exits_0_when_available(monkeypatch, capsys):
    _routes(monkeypatch)
    assert main(["o/r#1", "--no-colour"]) == 0
    assert "AVAILABLE" in capsys.readouterr().out


def test_cli_exits_1_when_claimed(monkeypatch, capsys):
    _routes(monkeypatch, timeline=[_crossref("someone", 607)])
    assert main(["o/r#1", "--no-colour"]) == 1
    assert "CLAIMED" in capsys.readouterr().out


def test_cli_exits_1_on_a_closed_issue_with_no_claim(monkeypatch, capsys):
    """Regression: this returned 0, telling you to go ahead on a closed issue."""
    _routes(monkeypatch, issue=_issue(state="closed"))
    assert main(["o/r#1", "--no-colour"]) == 1
    out = capsys.readouterr().out
    assert "CLOSED" in out
    assert "confidence" not in out  # confidence describes a claim, not closure


def test_cli_exits_2_on_a_bad_target(capsys):
    assert main(["nonsense", "--no-colour"]) == 2
    assert "error" in capsys.readouterr().err


def test_cli_exits_2_when_github_is_unreachable(monkeypatch, capsys):
    def boom(*_a, **_k):
        raise core.GitHubError("could not reach GitHub: timed out")

    monkeypatch.setattr(core, "_get", boom)
    assert main(["o/r#1", "--no-colour"]) == 2


def test_json_output_is_machine_readable(monkeypatch, capsys):
    import json

    _routes(monkeypatch, timeline=[_crossref("someone", 607)])
    main(["o/r#1", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["claimed"] is True
    assert payload["confidence"] == "high"
    assert payload["signals"][0]["kind"] == "cross_referenced_pr"
    assert payload["signals"][0]["actor"] == "someone"


# --------------------------------------------------------------------------
# pull requests are not claimable
# --------------------------------------------------------------------------
def test_a_pull_request_target_is_reported_as_such(monkeypatch):
    """GitHub's issues endpoint also returns PRs. Analysing one for claims is
    meaningless — it is already somebody's work."""
    issue = _issue(title="a PR")
    issue["pull_request"] = {"merged_at": None}
    _routes(monkeypatch, issue=issue)
    verdict = core.check("o/r#1")
    assert verdict.is_pull_request is True
    assert verdict.summary == "IS A PULL REQUEST"
    assert verdict.signals == []          # no pointless timeline scan
    assert verdict.checked == []


def test_cli_exits_1_and_explains_for_a_pull_request(monkeypatch, capsys):
    issue = _issue(title="a PR")
    issue["pull_request"] = {"merged_at": None}
    _routes(monkeypatch, issue=issue)
    assert main(["o/r#1", "--no-colour"]) == 1
    assert "not an issue" in capsys.readouterr().out
