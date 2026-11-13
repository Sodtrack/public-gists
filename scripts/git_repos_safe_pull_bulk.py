#!/usr/bin/env python3
"""Fetch every Git repository under a directory, and fast-forward its long-lived branches.

Usage:
    scripts/git_repos_safe_pull_bulk.py ~/src
    scripts/git_repos_safe_pull_bulk.py --dry-run ~/src

A repository is a direct subdirectory of the base directory, hidden ones included, that holds a
`.git` directory or file. For each one, in name order:

    1. `git fetch origin`. A repository with no `origin` remote is skipped
    2. For each local long-lived branch (main, master, dev, develop, uat, UAT) that tracks the
        branch of the same name on `origin`, and is behind it with no commits of its own,
        whichever branch is checked out:
        - checked out in a worktree, the repository's own or a linked one:
            `git merge --ff-only --no-overwrite-ignore` in that worktree
        - checked out nowhere: `git fetch . refs/remotes/origin/<branch>:refs/heads/<branch>`,
            which moves the branch and touches no working tree
            - A branch that a worktree is rebasing or bisecting is checked out nowhere, but Git
                refuses it: that branch is skipped

A long-lived branch that exists only on `origin` is not created.

While it runs, it prints a progress line per repository on stderr. At the end, on stdout, it
prints one row per long-lived branch, or one per repository that has none, the output of the Git
command behind each `failed` row, and a count per result. As `git branch` does, a row marks
with `*` the branch checked out in the repository itself, and with `+` one checked out in a
linked worktree.

It exits 1 if a row is `failed`, 0 otherwise.

Why neither update can lose work:

    - Per `man git-merge`:
        - `--ff-only` never creates a merge commit, and refuses a branch that has diverged
        - Git stops, changing nothing, when an uncommitted change is in a file the merge would
            update; any other uncommitted change stays as it is
        - `--no-overwrite-ignore` stops it from overwriting an ignored file, such as a local
            `.env`, that the incoming commits start tracking
    - Per `man git-fetch` and `man git-push`, a refspec without `+` updates a branch only by
        fast-forward
    - `git fetch` refuses to update a branch checked out in any worktree, or being rebased or
        bisected in one, so the branch it moves has no working tree to fall out of step with
        - Tested with Git 2.48.1; `man git-fetch` documents only the current branch, at
            `--update-head-ok`

`--dry-run` neither fetches nor merges: it reports against each repository's `origin/*` refs as
they were after its last fetch. As it attempts no update, it reports a branch being rebased or
bisected as `would merge`.
"""
import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

LONG_LIVED_BRANCHES = ("main", "master", "dev", "develop", "uat", "UAT")
REMOTE = "origin"
# Report order: the results that need attention come last, next to the summary
RESULTS = ("skipped", "up to date", "would merge", "merged", "ahead", "diverged", "failed")
# What `git for-each-ref` prints of each local branch, 1 field per item, separated by NUL
BRANCH_FIELDS = (
    "%(HEAD)",  # `*` for the branch checked out in the repository itself
    "%(refname:lstrip=2)",
    "%(objectname:short)",
    "%(upstream)",  # empty when the branch tracks none
    "%(upstream:track)",  # `[gone]` once the upstream branch is deleted, and pruned
    "%(worktreepath)",  # the worktree that has the branch checked out, the repository included
)
MERGE = ("merge", "--ff-only", "--no-overwrite-ignore", "--no-stat")
# Moves a branch from a ref of the same repository, and does nothing else: no tags, no
# FETCH_HEAD (the fetch from origin wrote it), no submodule fetch, no maintenance
LOCAL_FETCH = (
    "fetch",
    "--no-tags",
    "--no-write-fetch-head",
    "--no-recurse-submodules",
    "--no-auto-maintenance",
    ".",
)
# How `LOCAL_FETCH` refuses a branch, which it is only given when no worktree has it checked
# out: one being rebased or bisected. Git words it so with its messages untranslated
REFUSAL = re.compile(r"refusing to fetch into branch '[^']*' checked out at '(.*)'")
UNTRANSLATED = {"LC_ALL": "C"}

Git = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass
class Branch:
    name: str
    commit: str  # abbreviated
    upstream: str  # such as `refs/remotes/origin/main`
    gone: bool
    worktree: Path | None  # where it is checked out
    marker: str  # as `git branch` marks it: `*` in the repository itself, `+` in a linked worktree

    @classmethod
    def parse(cls, line: str) -> "Branch":
        head, name, commit, upstream, track, worktree = line.split("\0")
        marker = "*" if head == "*" else "+" if worktree else ""
        return cls(
            name, commit, upstream, track == "[gone]", Path(worktree) if worktree else None, marker
        )


@dataclass
class Outcome:
    repository: str
    branch: str = ""
    result: str = ""
    detail: str = ""
    failure: str = ""  # output of the Git command that failed
    marker: str = ""  # that of the branch

    def finish(self, result: str, detail: str = "") -> "Outcome":
        self.result = result
        self.detail = detail
        return self


def git(
    directory: Path, *args: str, env: dict[str, str] | None = None
) -> "subprocess.CompletedProcess[str]":
    """Runs Git in `directory`, with `env` added to the environment."""
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
    )


def output(completed: "subprocess.CompletedProcess[str]") -> str | None:
    return completed.stdout.strip() if completed.returncode == 0 else None


def plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def find_repositories(base_dir: Path) -> list[Path]:
    return sorted(
        (path for path in base_dir.iterdir() if path.is_dir() and (path / ".git").exists()),
        key=lambda path: path.name,
    )


def skip_reason(branch: Branch) -> str | None:
    upstream = branch.upstream.removeprefix("refs/remotes/")
    if not branch.upstream:
        return "tracks no branch"
    if branch.upstream != f"refs/remotes/{REMOTE}/{branch.name}":
        return f"tracks {upstream}"
    if branch.gone:
        return f"{upstream} is gone"
    return None


def compare(upstream: str, ahead: int, behind: int) -> tuple[str, str]:
    if ahead and behind:
        return "diverged", f"{ahead} ahead, {behind} behind {upstream}"
    if ahead:
        return "ahead", f"{ahead} ahead of {upstream}"
    if behind:
        return "behind", f"{behind} behind {upstream}"
    return "up to date", ""


def failed(outcome: Outcome, completed: "subprocess.CompletedProcess[str]") -> Outcome:
    command = " ".join(["git", *completed.args[3:]])  # without `-C <directory>`
    outcome.failure = "\n".join(
        text.strip() for text in (completed.stdout, completed.stderr) if text.strip()
    )
    return outcome.finish("failed", command)


def fast_forward(repository: Path, branch: Branch, dry_run: bool, run: Git) -> Outcome:
    outcome = Outcome(repository.name, branch.name, marker=branch.marker)
    reason = skip_reason(branch)
    if reason is not None:
        return outcome.finish("skipped", reason)
    ref = f"refs/heads/{branch.name}"
    counts = run(repository, "rev-list", "--left-right", "--count", f"{ref}...{branch.upstream}")
    if counts.returncode != 0:
        return failed(outcome, counts)
    ahead, behind = (int(count) for count in counts.stdout.split())
    upstream = branch.upstream.removeprefix("refs/remotes/")
    state, detail = compare(upstream, ahead, behind)
    if state != "behind":
        return outcome.finish(state, detail)

    if dry_run:
        if branch.worktree is not None:
            status = run(branch.worktree, "status", "--porcelain", "--untracked-files=no")
            changed = len(status.stdout.splitlines())
            if changed:
                detail += f", {plural(changed, 'file')} with uncommitted changes"
        return outcome.finish("would merge", detail)
    if branch.worktree is None:
        update = run(repository, *LOCAL_FETCH, f"{branch.upstream}:{ref}", env=UNTRANSLATED)
        if update.returncode != 0 and (refusal := REFUSAL.search(update.stderr)):
            return outcome.finish("skipped", f"being rebased or bisected at {refusal[1]}")
    else:
        update = run(branch.worktree, *MERGE, branch.upstream)
    if update.returncode != 0:
        return failed(outcome, update)
    after = output(run(repository, "rev-parse", "--short", ref))
    detail = f"{plural(behind, 'commit')} from {upstream}, {branch.commit}..{after}"
    return outcome.finish("merged", detail)


def process(repository: Path, dry_run: bool, run: Git = git) -> list[Outcome]:
    outcome = Outcome(repository.name)
    if output(run(repository, "remote", "get-url", REMOTE)) is None:
        return [outcome.finish("skipped", f"no {REMOTE} remote")]
    if not dry_run:
        fetch = run(repository, "fetch", REMOTE)
        if fetch.returncode != 0:
            return [failed(outcome, fetch)]

    listing = run(
        repository, "for-each-ref", f"--format={'%00'.join(BRANCH_FIELDS)}", "refs/heads/"
    )
    if listing.returncode != 0:
        return [failed(outcome, listing)]
    branches = (Branch.parse(line) for line in listing.stdout.splitlines())
    long_lived = [branch for branch in branches if branch.name in LONG_LIVED_BRANCHES]
    if not long_lived:
        return [outcome.finish("skipped", "no long-lived branch")]
    return [fast_forward(repository, branch, dry_run, run) for branch in long_lived]


def render_report(outcomes: list[Outcome]) -> str:
    ordered = sorted(outcomes, key=lambda o: (RESULTS.index(o.result), o.repository, o.branch))
    rows = [("repository", "branch", "result", "detail")]
    rows += [
        (o.repository, f"{o.marker:1} {o.branch}" if o.branch else "", o.result, o.detail)
        for o in ordered
    ]
    widths = [max(len(row[column]) for row in rows) for column in range(3)]
    lines = [
        "  ".join([*(cell.ljust(width) for cell, width in zip(row, widths)), row[3]]).rstrip()
        for row in rows
    ]
    for outcome in ordered:
        if outcome.failure:
            where = " ".join(filter(None, (outcome.repository, outcome.branch)))
            lines.append(f"--- {where}: {outcome.detail}")
            lines.extend(f"    {line}" for line in outcome.failure.splitlines())
    counts = Counter(outcome.result for outcome in outcomes)
    summary = ", ".join(f"{counts[result]} {result}" for result in RESULTS if counts[result])
    lines.append(f"--- {summary or 'no repositories'}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "base_dir",
        type=lambda value: Path(value).expanduser(),
        help="the directory whose direct subdirectories are the repositories",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="neither fetch nor merge, and report against the refs of the last fetch",
    )
    args = parser.parse_args()
    if not args.base_dir.is_dir():
        parser.error(f"not a directory: {args.base_dir}")

    repositories = find_repositories(args.base_dir)
    width = len(str(len(repositories)))
    outcomes = []
    for number, repository in enumerate(repositories, start=1):
        progress = f"[{number:>{width}}/{len(repositories)}] {repository.name}"
        print(progress, file=sys.stderr, flush=True)
        outcomes.extend(process(repository, args.dry_run))
    sys.stdout.write(render_report(outcomes))
    return 1 if any(outcome.result == "failed" for outcome in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
