#!/usr/bin/env python3
"""
Tests for the fetch and fast-forward of every repository under a directory.

Run from the repository root, on the standard library alone:

    python3 -m unittest discover --start-directory scripts/tests

The module under test is `scripts/git_repos_safe_pull_bulk.py`, loaded by path, so that it does
not depend on `scripts/` being importable.

Git itself never runs: `process` takes a fake in its place, which answers each command from a
table and records it with the directory it runs in, so a test can tell which commands would have
run, and where; the updates of a branch above all.
"""
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "git_repos_safe_pull_bulk.py"

spec = importlib.util.spec_from_file_location("git_repos_safe_pull_bulk", MODULE_PATH)
assert spec is not None and spec.loader is not None
git_repos_safe_pull_bulk = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = git_repos_safe_pull_bulk
spec.loader.exec_module(git_repos_safe_pull_bulk)

Outcome = git_repos_safe_pull_bulk.Outcome
process = git_repos_safe_pull_bulk.process
LONG_LIVED_BRANCHES = git_repos_safe_pull_bulk.LONG_LIVED_BRANCHES

REPOSITORY = Path("/nonexistent/lorem-ipsum")
LINKED_WORKTREE = Path("/nonexistent/lorem-ipsum-uat")

REMOTE_URL = ("remote", "get-url", "origin")
FETCH = ("fetch", "origin")
LIST = (
    "for-each-ref",
    "--format=" + "%00".join(git_repos_safe_pull_bulk.BRANCH_FIELDS),
    "refs/heads/",
)
STATUS = ("status", "--porcelain", "--untracked-files=no")
# The 2 ways to move a branch, spelled out rather than taken from the module, so that a test
# fails if a flag that keeps them safe goes missing
MERGE = ("merge", "--ff-only", "--no-overwrite-ignore", "--no-stat")
LOCAL_FETCH = (
    "fetch",
    "--no-tags",
    "--no-write-fetch-head",
    "--no-recurse-submodules",
    "--no-auto-maintenance",
    ".",
)


def counts(name):
    upstream = f"refs/remotes/origin/{name}"
    return ("rev-list", "--left-right", "--count", f"refs/heads/{name}...{upstream}")


def merge(name):
    return (*MERGE, f"refs/remotes/origin/{name}")


def local_fetch(name):
    return (*LOCAL_FETCH, f"refs/remotes/origin/{name}:refs/heads/{name}")


def short(name):
    return ("rev-parse", "--short", f"refs/heads/{name}")


class FakeGit:
    """Answers each command from `answers`, keyed by its arguments, and records it.

    An answer is `(returncode, stdout, stderr)`, or a list of them consumed in order. A command
    missing from the table fails, as `git` does for a missing remote. A call is recorded as
    `(directory, arguments)`, and what it adds to the environment, under its arguments.
    """

    def __init__(self, answers):
        self.answers = answers
        self.calls = []
        self.env = {}

    def __call__(self, directory, *args, env=None):
        self.calls.append((directory, args))
        self.env[args] = env
        answer = self.answers.get(args, (1, "", ""))
        if isinstance(answer, list):
            answer = answer.pop(0)
        returncode, stdout, stderr = answer
        return subprocess.CompletedProcess(
            ["git", "-C", str(directory), *args], returncode, stdout, stderr
        )


def branch(name="main", ahead=0, behind=0, upstream=None, gone=False, worktree=None):
    """A local branch: its line in the list of branches, and the answers of the commands on it.

    `upstream` defaults to the branch of the same name on `origin`, and `""` tracks no branch.
    `worktree` is where the branch is checked out: `REPOSITORY` for the repository itself.
    """
    if upstream is None:
        upstream = f"refs/remotes/origin/{name}"
    fields = [
        "*" if worktree == REPOSITORY else " ",
        name,
        "1111111",
        upstream,
        "[gone]" if gone else "",
        str(worktree or ""),
    ]
    answers = {
        counts(name): (0, f"{ahead}\t{behind}\n", ""),
        merge(name): (0, "Updating 1111111..2222222\nFast-forward\n", ""),
        local_fetch(name): (0, "", f"From .\n   1111111..2222222  origin/{name} -> {name}\n"),
        short(name): (0, "2222222\n", ""),
    }
    return "\0".join(fields), answers


def repository(*branches):
    """The answers of a repository with an `origin` remote, where every command succeeds."""
    answers = {
        REMOTE_URL: (0, "git@example.com:lorem/ipsum.git\n", ""),
        FETCH: (0, "", ""),
        STATUS: (0, "", ""),
    }
    lines = []
    for line, branch_answers in branches:
        lines.append(line)
        answers.update(branch_answers)
    answers[LIST] = (0, "".join(f"{line}\n" for line in lines), "")
    return answers


def run(answers, dry_run=False):
    fake = FakeGit(answers)
    return process(REPOSITORY, dry_run, run=fake), fake.calls


def updates(calls):
    """The calls that would move a branch."""
    return [
        (directory, args)
        for directory, args in calls
        if args[: len(MERGE)] == MERGE or args[: len(LOCAL_FETCH)] == LOCAL_FETCH
    ]


def row(outcome):
    return outcome.marker, outcome.branch, outcome.result, outcome.detail


class FastForwardTest(unittest.TestCase):
    def test_merges_the_branch_checked_out_in_the_repository(self):
        [outcome], calls = run(repository(branch("main", behind=3, worktree=REPOSITORY)))
        self.assertEqual(
            row(outcome), ("*", "main", "merged", "3 commits from origin/main, 1111111..2222222")
        )
        self.assertEqual(updates(calls), [(REPOSITORY, merge("main"))])
        self.assertLess(calls.index((REPOSITORY, FETCH)), calls.index((REPOSITORY, LIST)))

    def test_merges_a_branch_checked_out_in_a_linked_worktree_in_that_worktree(self):
        [outcome], calls = run(repository(branch("uat", behind=1, worktree=LINKED_WORKTREE)))
        self.assertEqual(
            row(outcome), ("+", "uat", "merged", "1 commit from origin/uat, 1111111..2222222")
        )
        self.assertEqual(updates(calls), [(LINKED_WORKTREE, merge("uat"))])

    def test_moves_a_branch_checked_out_nowhere_without_a_merge(self):
        [outcome], calls = run(repository(branch("dev", behind=2)))
        self.assertEqual(
            row(outcome), ("", "dev", "merged", "2 commits from origin/dev, 1111111..2222222")
        )
        self.assertEqual(updates(calls), [(REPOSITORY, local_fetch("dev"))])

    def test_every_long_lived_branch_whichever_branch_is_checked_out(self):
        answers = repository(
            branch("feat/lorem", behind=3, worktree=REPOSITORY),
            *(branch(name, behind=1) for name in LONG_LIVED_BRANCHES),
        )
        outcomes, calls = run(answers)
        self.assertEqual(
            [(outcome.branch, outcome.result) for outcome in outcomes],
            [(name, "merged") for name in LONG_LIVED_BRANCHES],
        )
        self.assertEqual(
            updates(calls), [(REPOSITORY, local_fetch(name)) for name in LONG_LIVED_BRANCHES]
        )

    def test_a_failed_update_leaves_the_other_branches_to_be_updated(self):
        answers = repository(branch("dev", behind=1), branch("main", behind=1))
        rejection = "From .\n ! [rejected]        origin/dev -> dev  (non-fast-forward)"
        answers[local_fetch("dev")] = (1, "", f"{rejection}\n")
        outcomes, _ = run(answers)
        self.assertEqual(
            [(outcome.branch, outcome.result) for outcome in outcomes],
            [("dev", "failed"), ("main", "merged")],
        )
        self.assertEqual(
            outcomes[0].detail,
            "git fetch --no-tags --no-write-fetch-head --no-recurse-submodules"
            " --no-auto-maintenance . refs/remotes/origin/dev:refs/heads/dev",
        )
        self.assertEqual(outcomes[0].failure, rejection)

    def test_skips_a_branch_being_rebased_or_bisected_and_updates_the_others(self):
        answers = repository(branch("dev", behind=1), branch("main", behind=1))
        answers[local_fetch("dev")] = (
            128,
            "",
            "fatal: refusing to fetch into branch 'refs/heads/dev' checked out at"
            " '/nonexistent/lorem-ipsum'\n",
        )
        outcomes, _ = run(answers)
        self.assertEqual(
            [row(outcome) for outcome in outcomes],
            [
                ("", "dev", "skipped", "being rebased or bisected at /nonexistent/lorem-ipsum"),
                ("", "main", "merged", "1 commit from origin/main, 1111111..2222222"),
            ],
        )
        self.assertEqual(outcomes[0].failure, "")

    def test_reads_the_refusal_with_git_messages_untranslated(self):
        fake = FakeGit(repository(branch("dev", behind=1)))
        process(REPOSITORY, False, run=fake)
        self.assertEqual(fake.env[local_fetch("dev")], {"LC_ALL": "C"})

    def test_a_refused_merge_is_reported_with_its_output(self):
        answers = repository(branch("main", behind=3, worktree=REPOSITORY))
        answers[merge("main")] = (
            128,
            "Updating 1111111..2222222\n",
            "error: Your local changes to the following files would be overwritten by merge:\n"
            "\tlorem.txt\n",
        )
        [outcome], _ = run(answers)
        self.assertEqual(outcome.result, "failed")
        self.assertEqual(
            outcome.detail,
            "git merge --ff-only --no-overwrite-ignore --no-stat refs/remotes/origin/main",
        )
        self.assertEqual(
            outcome.failure,
            "Updating 1111111..2222222\n"
            "error: Your local changes to the following files would be overwritten by merge:\n"
            "\tlorem.txt",
        )


class NoUpdateTest(unittest.TestCase):
    """Every case where no branch must move, and what each one reports instead."""

    def assert_not_updated(self, answers, result, detail, dry_run=False):
        [outcome], calls = run(answers, dry_run)
        self.assertEqual((outcome.result, outcome.detail), (result, detail))
        self.assertEqual(updates(calls), [])
        return calls

    def test_a_repository_with_no_long_lived_branch(self):
        answers = repository(branch("feat/lorem", behind=3, worktree=REPOSITORY))
        calls = self.assert_not_updated(answers, "skipped", "no long-lived branch")
        self.assertIn((REPOSITORY, FETCH), calls)

    def test_a_branch_named_like_a_long_lived_one_in_another_case(self):
        answers = repository(branch("Main", behind=3))
        self.assert_not_updated(answers, "skipped", "no long-lived branch")

    def test_a_long_lived_branch_that_tracks_another_branch(self):
        answers = repository(branch("main", upstream="refs/remotes/origin/dev", behind=3))
        self.assert_not_updated(answers, "skipped", "tracks origin/dev")

    def test_a_long_lived_branch_that_tracks_a_local_branch(self):
        answers = repository(branch("main", upstream="refs/heads/dev", behind=3))
        self.assert_not_updated(answers, "skipped", "tracks refs/heads/dev")

    def test_a_long_lived_branch_that_tracks_nothing(self):
        self.assert_not_updated(repository(branch(upstream="")), "skipped", "tracks no branch")

    def test_a_long_lived_branch_whose_upstream_is_gone(self):
        answers = repository(branch("master", gone=True))
        self.assert_not_updated(answers, "skipped", "origin/master is gone")

    def test_a_repository_without_origin_is_not_fetched(self):
        answers = repository(branch(behind=3))
        del answers[REMOTE_URL]
        calls = self.assert_not_updated(answers, "skipped", "no origin remote")
        self.assertNotIn((REPOSITORY, FETCH), calls)

    def test_a_failed_fetch(self):
        answers = repository(branch(behind=3))
        answers[FETCH] = (128, "", "fatal: Could not read from remote repository.\n")
        self.assert_not_updated(answers, "failed", "git fetch origin")
        [outcome], _ = run(answers)
        self.assertEqual(outcome.failure, "fatal: Could not read from remote repository.")

    def test_a_diverged_branch(self):
        answers = repository(branch(ahead=2, behind=3))
        self.assert_not_updated(answers, "diverged", "2 ahead, 3 behind origin/main")

    def test_a_branch_ahead_of_its_upstream(self):
        self.assert_not_updated(repository(branch(ahead=2)), "ahead", "2 ahead of origin/main")

    def test_an_up_to_date_branch(self):
        self.assert_not_updated(repository(branch()), "up to date", "")


class DryRunTest(unittest.TestCase):
    def test_neither_fetches_nor_updates(self):
        answers = repository(
            branch("main", behind=3, worktree=REPOSITORY), branch("dev", behind=1)
        )
        outcomes, calls = run(answers, dry_run=True)
        self.assertEqual(
            [(outcome.branch, outcome.result, outcome.detail) for outcome in outcomes],
            [
                ("main", "would merge", "3 behind origin/main"),
                ("dev", "would merge", "1 behind origin/dev"),
            ],
        )
        self.assertNotIn((REPOSITORY, FETCH), calls)
        self.assertEqual(updates(calls), [])

    def test_counts_the_files_with_uncommitted_changes_where_the_branch_is_checked_out(self):
        answers = repository(branch("uat", behind=3, worktree=LINKED_WORKTREE))
        answers[STATUS] = (0, " M lorem.txt\nM  ipsum.txt\n", "")
        [outcome], calls = run(answers, dry_run=True)
        self.assertEqual(outcome.detail, "3 behind origin/uat, 2 files with uncommitted changes")
        self.assertIn((LINKED_WORKTREE, STATUS), calls)

    def test_a_branch_checked_out_nowhere_has_no_working_tree_to_look_at(self):
        _, calls = run(repository(branch("dev", behind=3)), dry_run=True)
        self.assertNotIn(STATUS, [args for _, args in calls])


class FindRepositoriesTest(unittest.TestCase):
    def test_finds_direct_subdirectories_with_git_hidden_ones_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "lorem" / ".git").mkdir(parents=True)
            (base / ".ipsum" / ".git").mkdir(parents=True)
            (base / "worktree").mkdir()
            (base / "worktree" / ".git").write_text("gitdir: /nonexistent\n")
            (base / "plain").mkdir()
            (base / "nested" / "dolor" / ".git").mkdir(parents=True)
            (base / "notes.txt").write_text("")
            found = git_repos_safe_pull_bulk.find_repositories(base)
        self.assertEqual([path.name for path in found], [".ipsum", "lorem", "worktree"])


class ReportTest(unittest.TestCase):
    def test_aligns_columns_and_puts_what_needs_attention_last(self):
        merge_uat = "git merge --ff-only --no-overwrite-ignore --no-stat refs/remotes/origin/uat"
        outcomes = [
            Outcome(
                "lorem",
                "main",
                "merged",
                "3 commits from origin/main, 1111111..2222222",
                marker="*",
            ),
            Outcome("lorem", "dev", "merged", "1 commit from origin/dev, 3333333..4444444"),
            Outcome(
                "dolor-sit",
                "",
                "failed",
                "git fetch origin",
                failure="fatal: Could not read from remote repository.",
            ),
            Outcome(
                "lorem",
                "uat",
                "failed",
                merge_uat,
                failure="fatal: Not possible to fast-forward, aborting.",
                marker="+",
            ),
            Outcome("ipsum", "", "skipped", "no long-lived branch"),
            Outcome("amet", "main", "up to date", "", marker="*"),
        ]
        expected = (
            "repository  branch  result      detail\n"
            "ipsum               skipped     no long-lived branch\n"
            "amet        * main  up to date\n"
            "lorem         dev   merged      1 commit from origin/dev, 3333333..4444444\n"
            "lorem       * main  merged      3 commits from origin/main, 1111111..2222222\n"
            "dolor-sit           failed      git fetch origin\n"
            f"lorem       + uat   failed      {merge_uat}\n"
            "--- dolor-sit: git fetch origin\n"
            "    fatal: Could not read from remote repository.\n"
            f"--- lorem uat: {merge_uat}\n"
            "    fatal: Not possible to fast-forward, aborting.\n"
            "--- 1 skipped, 1 up to date, 2 merged, 2 failed\n"
        )
        self.assertEqual(git_repos_safe_pull_bulk.render_report(outcomes), expected)

    def test_an_empty_directory(self):
        self.assertEqual(
            git_repos_safe_pull_bulk.render_report([]),
            "repository  branch  result  detail\n--- no repositories\n",
        )


if __name__ == "__main__":
    unittest.main()
