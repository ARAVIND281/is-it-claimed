"""Tests for sweeping many issues at once. No network requests are made."""

from __future__ import annotations

import json

import pytest

from is_it_claimed import batch, core
from is_it_claimed.cli import main


# --------------------------------------------------------------------------
# search query handling
# --------------------------------------------------------------------------
def _search_capture(monkeypatch, items=None):
    """Capture the path search_issues builds, and return canned items."""
    seen = {}

    def fake_get(path, **_):
        seen["path"] = path
        return {"items": items or []}

    monkeypatch.setattr(batch, "_get", fake_get)
    return seen


def test_search_adds_is_issue_and_is_open(monkeypatch):
    """A search returning PRs or closed issues wastes the whole sweep."""
    seen = _search_capture(monkeypatch)
    batch.search_issues("label:bug")
    assert "is%3Aissue" in seen["path"]
    assert "is%3Aopen" in seen["path"]


def test_search_does_not_duplicate_qualifiers_the_user_supplied(monkeypatch):
    seen = _search_capture(monkeypatch)
    batch.search_issues("label:bug is:issue is:open")
    assert seen["path"].count("is%3Aissue") == 1
    assert seen["path"].count("is%3Aopen") == 1


def test_search_respects_an_explicit_state_qualifier(monkeypatch):
    """`state:closed` is a deliberate choice; do not override it with is:open."""
    seen = _search_capture(monkeypatch)
    batch.search_issues("label:bug state:closed")
    assert "is%3Aopen" not in seen["path"]


def test_search_converts_urls_to_targets(monkeypatch):
    _search_capture(monkeypatch, items=[
        {"html_url": "https://github.com/psf/black/issues/4421"},
        {"html_url": "https://github.com/pallets/flask/issues/5502"},
    ])
    assert batch.search_issues("anything") == ["psf/black#4421", "pallets/flask#5502"]


def test_search_honours_the_limit(monkeypatch):
    _search_capture(monkeypatch, items=[
        {"html_url": f"https://github.com/o/r/issues/{n}"} for n in range(50)
    ])
    assert len(batch.search_issues("anything", limit=5)) == 5


# --------------------------------------------------------------------------
# sweeping
# --------------------------------------------------------------------------
def _stub_checks(monkeypatch, mapping, *, rate=10_000):
    """Map target -> "available" | "claimed" | an exception to raise."""

    def fake_check(target, **_):
        outcome = mapping[target]
        if isinstance(outcome, Exception):
            raise outcome
        verdict = core.Verdict(target=target, state="open", title="t", url="u")
        if outcome == "claimed":
            verdict.signals.append(
                core.Signal(kind="cross_referenced_pr", detail="has an OPEN PR", weight=4)
            )
        return verdict

    monkeypatch.setattr(batch, "check", fake_check)
    monkeypatch.setattr(batch, "rate_remaining", lambda: rate)


def test_a_sweep_separates_available_from_claimed(monkeypatch):
    _stub_checks(monkeypatch, {"o/r#1": "available", "o/r#2": "claimed", "o/r#3": "available"})
    result = batch.check_many(["o/r#1", "o/r#2", "o/r#3"])
    assert [v.target for v in result.available] == ["o/r#1", "o/r#3"]
    assert [v.target for v in result.claimed] == ["o/r#2"]


def test_results_are_ordered_deterministically(monkeypatch):
    """Concurrency must not make the output shuffle between runs."""
    targets = [f"o/r#{n}" for n in range(1, 9)]
    _stub_checks(monkeypatch, dict.fromkeys(targets, "available"))
    first = [v.target for v in batch.check_many(targets).verdicts]
    second = [v.target for v in batch.check_many(targets).verdicts]
    assert first == second == sorted(targets)


def test_one_failure_does_not_discard_the_rest(monkeypatch):
    """A deleted repo in the middle of a sweep must not lose the other answers."""
    _stub_checks(monkeypatch, {
        "o/r#1": "available",
        "o/gone#2": core.GitHubError("not found"),
        "o/r#3": "claimed",
    })
    result = batch.check_many(["o/r#1", "o/gone#2", "o/r#3"])
    assert len(result.verdicts) == 2
    assert result.errors == [("o/gone#2", "not found")]


def test_an_empty_target_list_is_not_an_error(monkeypatch):
    result = batch.check_many([])
    assert result.verdicts == [] and result.errors == []


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------
def test_a_sweep_that_cannot_finish_is_trimmed_not_attempted(monkeypatch):
    """Each issue costs ~3 calls. With 40 left, check what fits and say so."""
    targets = [f"o/r#{n}" for n in range(1, 21)]
    _stub_checks(monkeypatch, dict.fromkeys(targets, "available"), rate=40)
    result = batch.check_many(targets)
    assert len(result.verdicts) == 8  # (40 - 15 floor) // 3
    assert "only 40 requests left" in result.stopped_early


def test_a_hopeless_rate_limit_refuses_up_front(monkeypatch):
    targets = ["o/r#1", "o/r#2"]
    _stub_checks(monkeypatch, dict.fromkeys(targets, "available"), rate=5)
    result = batch.check_many(targets)
    assert result.verdicts == []
    assert "too low to start" in result.stopped_early
    assert "GITHUB_TOKEN" in result.stopped_early


def test_rate_checking_can_be_switched_off(monkeypatch):
    targets = ["o/r#1"]
    _stub_checks(monkeypatch, dict.fromkeys(targets, "available"), rate=1)
    result = batch.check_many(targets, respect_rate_limit=False)
    assert len(result.verdicts) == 1
    assert result.stopped_early is None


def test_an_unavailable_rate_endpoint_does_not_block_the_sweep(monkeypatch):
    """If /rate_limit cannot be read, proceed rather than refusing to work."""
    _stub_checks(monkeypatch, {"o/r#1": "available"}, rate=None)
    assert len(batch.check_many(["o/r#1"]).verdicts) == 1


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------
def test_cli_rejects_targets_and_search_together(capsys):
    with pytest.raises(SystemExit):
        main(["o/r#1", "--search", "label:bug"])
    assert "not both" in capsys.readouterr().err


def test_cli_requires_something_to_check(capsys):
    with pytest.raises(SystemExit):
        main([])
    assert "--search" in capsys.readouterr().err


def test_cli_sweeps_several_targets(monkeypatch, capsys):
    _stub_checks(monkeypatch, {"o/r#1": "available", "o/r#2": "claimed"})
    assert main(["o/r#1", "o/r#2", "--no-colour"]) == 0
    out = capsys.readouterr().out
    assert "2 checked, 1 available, 1 claimed" in out


def test_cli_exit_1_when_a_sweep_finds_nothing_available(monkeypatch, capsys):
    _stub_checks(monkeypatch, {"o/r#1": "claimed", "o/r#2": "claimed"})
    assert main(["o/r#1", "o/r#2", "--no-colour"]) == 1


def test_available_only_hides_the_claimed_ones(monkeypatch, capsys):
    _stub_checks(monkeypatch, {"o/r#1": "available", "o/r#2": "claimed"})
    main(["o/r#1", "o/r#2", "--available-only", "--no-colour"])
    out = capsys.readouterr().out
    assert "o/r#1" in out
    assert "o/r#2" not in out


def test_a_sweep_says_why_each_claimed_issue_is_taken(monkeypatch, capsys):
    """Knowing it is claimed is half the answer; by whom and how is the rest."""
    _stub_checks(monkeypatch, {"o/r#1": "claimed", "o/r#2": "claimed"})
    main(["o/r#1", "o/r#2", "--no-colour"])
    assert "has an OPEN PR" in capsys.readouterr().out


def test_cli_search_path_runs_a_sweep(monkeypatch, capsys):
    monkeypatch.setattr(batch, "search_issues", lambda q, limit=40: ["o/r#1"])
    monkeypatch.setattr("is_it_claimed.cli.search_issues", lambda q, limit=40: ["o/r#1"])
    _stub_checks(monkeypatch, {"o/r#1": "available"})
    assert main(["--search", "label:bug", "--no-colour"]) == 0
    assert "1 checked, 1 available" in capsys.readouterr().out


def test_cli_reports_an_empty_search_rather_than_pretending_success(monkeypatch, capsys):
    monkeypatch.setattr("is_it_claimed.cli.search_issues", lambda q, limit=40: [])
    assert main(["--search", "label:nope"]) == 1
    assert "no issues matched" in capsys.readouterr().err


def test_batch_json_is_machine_readable(monkeypatch, capsys):
    _stub_checks(monkeypatch, {"o/r#1": "available", "o/r#2": "claimed"})
    main(["o/r#1", "o/r#2", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["checked"] == 2
    assert payload["available"] == 1
    assert len(payload["results"]) == 2
