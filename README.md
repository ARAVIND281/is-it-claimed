# is-it-claimed

Check whether a GitHub issue is already being worked on — **before** you write the patch.

```console
$ is-it-claimed owner/repo#606
CLAIMED (high confidence)
  owner/repo#606  serve backend hand-rolls its own prompt

  ! @contributor has an OPEN PR for this — fix: apply the model's chat template
    https://github.com/owner/repo/pull/607
```

No dependencies. Python 3.9+.

## Why

An issue with no assignee and no comments looks free. It often isn't.

This tool exists because of a specific afternoon: an issue was filed at 16:27, a
pull request fixing it was opened **50 seconds later**, and a second contributor —
who had checked the issue for a claim and found none — started the same work at
16:52 and finished it. Two people wrote the same patch, and a maintainer had to
spend their time choosing between them.

Nothing was done wrong. The claim simply wasn't anywhere the second contributor
looked. GitHub records a referencing PR in the issue's **timeline**, not in its
comments, so `gh issue view` does not show it and neither does the web page's
comment thread.

`is-it-claimed` looks in all four places a claim actually hides.

## Install

```bash
pip install is-it-claimed
# or, without installing:
uvx is-it-claimed owner/repo#123
```

## Use

```bash
is-it-claimed owner/repo#123
is-it-claimed https://github.com/owner/repo/issues/123
is-it-claimed owner/repo#123 --forks     # also scan forks for a matching branch
is-it-claimed owner/repo#123 --json      # machine-readable
```

Exit codes make it scriptable — `0` available, `1` claimed, `2` error:

```bash
is-it-claimed "$ISSUE" || { echo "someone is already on it"; exit 1; }
```

## What it checks

| Signal | Weight | Why it matters |
|---|---|---|
| **Open/merged PR referencing the issue** | 4 | Near-proof. The work exists, not just the intent. **This is the one `gh issue view` doesn't show you.** |
| **Assignee** | 3 | The explicit signal — and the least used. |
| **A comment claiming it** | 2 | "I'll take this", "working on it", … |
| **A fork with a branch naming the issue** | 1 | Work started, no PR yet. Opt-in via `--forks`. |
| Closed PR referencing the issue | 1 | Someone tried and stopped — worth knowing, not a blocker. |

Weights add up into a confidence level, because these signals are not equal. A
fork branch alone is a hint; someone may have abandoned it months ago. An open PR
is as close to certain as this gets.

**Claim phrases are matched conservatively.** "Can I work on this?" is a question,
not a claim, and treating it as one would tell you an issue is taken when it is
merely being discussed.

## Authentication

Optional, but recommended — unauthenticated GitHub allows 60 requests/hour, and
`--forks` can spend that on one check.

The token is picked up from `GITHUB_TOKEN`, `GH_TOKEN`, or the credentials the
`gh` CLI already stores, so if you have `gh auth login` there is nothing to do.

## Partial results

If one check fails — rate limit, a deleted fork — the others still run and the
failure is reported:

```
AVAILABLE
  no claim found in: assignee, comments
  ? could not check fork branches: refused (403)
```

A definitive answer already found in the timeline is not thrown away because a
later, weaker check failed.

## Limits

- A claim made somewhere else entirely — Discord, a mailing list, a maintainer's
  head — is invisible here. `AVAILABLE` means "no public signal", not "nobody is
  working on it".
- `--forks` scans the 30 most recently pushed forks by default. A very popular
  repository has more.
- Claim-phrase matching is English-only.

## Licence

MIT
