"""Tests for the prepare-pr push_guard.py stale-base detection.

Verifies that the push_guard script (src/kiro_crew/builtin_skills/kirocrew-dev/
prepare-pr/scripts/push_guard.py) correctly refuses to push when:
- The branch has no common history with origin/<base> (orphan / disconnected)
- The commit count exceeds --max-ahead (implausibly many commits for a PR)
- The fetch of origin/<base> fails (network error → fail closed)

And allows push when the branch is a normal single-commit PR (1 commit ahead
of a fresh origin/<base> with shared history).

Regression test for the 2026-07-31 clobber incident: a force-push from a
worktree branched off kiki-trunk carried 114 duplicate commits.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Resolve the push_guard.py script path relative to the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
PUSH_GUARD = str(
    REPO_ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "prepare-pr"
    / "scripts"
    / "push_guard.py"
)


def _run_push_guard(cwd: str, extra_args: list[str] | None = None) -> tuple[int, str, str]:
    """Run push_guard.py in the given directory; return (rc, stdout, stderr)."""
    args = [sys.executable, PUSH_GUARD, "--base", "main"]
    if extra_args:
        args.extend(extra_args)
    proc = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _git(cwd: str, *args: str) -> str:
    """Run a git command in cwd; raise on failure."""
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def repo_pair(tmp_path):
    """Create a local 'origin' bare repo and a working clone.

    Returns (clone_dir, origin_dir) where origin_dir is a bare repo and
    clone_dir has 'origin' pointing at origin_dir.
    """
    origin_dir = str(tmp_path / "origin.git")
    clone_dir = str(tmp_path / "work")

    # Create a bare origin with one commit on main.
    os.makedirs(origin_dir)
    _git(origin_dir, "init", "--bare")
    _git(origin_dir, "symbolic-ref", "HEAD", "refs/heads/main")

    # Clone it.
    _git(str(tmp_path), "clone", origin_dir, "work")
    _git(clone_dir, "checkout", "-b", "main")

    # Create an initial commit on main.
    Path(clone_dir, "README.md").write_text("initial\n")
    _git(clone_dir, "add", "README.md")
    _git(clone_dir, "commit", "-m", "initial commit")
    _git(clone_dir, "push", "-u", "origin", "main")

    return clone_dir, origin_dir


class TestPushGuardSafe:
    """Normal single-commit PR: push_guard exits 0 (safe)."""

    def test_single_commit_ahead(self, repo_pair):
        clone_dir, _ = repo_pair

        # Create a feature branch with one commit ahead of origin/main.
        _git(clone_dir, "checkout", "-b", "feature/my-fix")
        Path(clone_dir, "fix.py").write_text("# fix\n")
        _git(clone_dir, "add", "fix.py")
        _git(clone_dir, "commit", "-m", "fix: the bug")

        rc, stdout, stderr = _run_push_guard(clone_dir)
        assert rc == 0, f"Expected safe (0), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "SAFE TO PUSH" in stdout

    def test_max_ahead_at_threshold(self, repo_pair):
        """Exactly at --max-ahead=3 should pass."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/multi")
        for i in range(3):
            Path(clone_dir, f"file{i}.py").write_text(f"# {i}\n")
            _git(clone_dir, "add", f"file{i}.py")
            _git(clone_dir, "commit", "-m", f"commit {i}")

        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "3"])
        assert rc == 0, f"Expected safe (0), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "SAFE TO PUSH" in stdout


class TestPushGuardRefused:
    """push_guard exits 40 (refused) when the branch is unsafe to push."""

    def test_too_many_commits_ahead(self, repo_pair):
        """Branch with 6 commits and --max-ahead=5 → refused."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/bloated")
        for i in range(6):
            Path(clone_dir, f"file{i}.py").write_text(f"# {i}\n")
            _git(clone_dir, "add", f"file{i}.py")
            _git(clone_dir, "commit", "-m", f"commit {i}")

        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "6 commits ahead" in stderr

    def test_orphan_branch_no_common_history(self, repo_pair):
        """Orphan branch with no common history with origin/main → refused."""
        clone_dir, origin_dir = repo_pair

        # Advance origin/main with a new commit via a second clone.
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")
        Path(work2, "upstream.txt").write_text("upstream change\n")
        _git(work2, "add", "upstream.txt")
        _git(work2, "commit", "-m", "upstream: new feature")
        _git(work2, "push", "origin", "main")

        # In the original clone, create an orphan branch — no shared ancestry
        # with origin/main at all.
        _git(clone_dir, "checkout", "--orphan", "stale-trunk")
        Path(clone_dir, "stale.txt").write_text("stale\n")
        _git(clone_dir, "add", "stale.txt")
        _git(clone_dir, "commit", "-m", "stale trunk commit")

        # git merge-base will fail (no common ancestor) → refused.
        rc, stdout, stderr = _run_push_guard(clone_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr

    def test_fetch_failure_refuses(self, tmp_path):
        """When origin doesn't have the base branch, fetch fails → refused."""
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # Set origin to a non-existent path so fetch always fails.
        _git(repo_dir, "remote", "add", "origin", "/nonexistent/repo.git")

        rc, stdout, stderr = _run_push_guard(repo_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "fetch" in stderr.lower()

    def test_stale_base_clobber_scenario(self, repo_pair):
        """Reproduce the exact clobber pattern: many commits from a local trunk
        that aren't on the remote."""
        clone_dir, origin_dir = repo_pair

        # Simulate kiki-trunk: advance local main with 10 "integration" commits
        # that never get pushed to origin.
        _git(clone_dir, "checkout", "main")
        for i in range(10):
            Path(clone_dir, f"integration{i}.py").write_text(f"# int {i}\n")
            _git(clone_dir, "add", f"integration{i}.py")
            _git(clone_dir, "commit", "-m", f"feat(integration): commit {i}")

        # Branch from the stale local main (as kiki does from kiki-trunk).
        _git(clone_dir, "checkout", "-b", "feature/pr-fix")
        Path(clone_dir, "fix.py").write_text("# fix\n")
        _git(clone_dir, "add", "fix.py")
        _git(clone_dir, "commit", "-m", "fix: the issue")

        # Now this branch is 11 commits ahead of origin/main (10 integration +
        # 1 actual fix). The push_guard MUST refuse.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "11 commits ahead" in stderr


class TestPushGuardEdgeCases:
    """Edge cases and error handling."""

    def test_not_a_git_repo(self, tmp_path):
        """Running outside a git repo → exit 2."""
        rc, stdout, stderr = _run_push_guard(str(tmp_path))
        assert rc == 2

    def test_custom_max_ahead(self, repo_pair):
        """--max-ahead=1 catches even 2 commits."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/small")
        for i in range(2):
            Path(clone_dir, f"f{i}.py").write_text(f"# {i}\n")
            _git(clone_dir, "add", f"f{i}.py")
            _git(clone_dir, "commit", "-m", f"commit {i}")

        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "1"])
        assert rc == 40
        assert "2 commits ahead" in stderr


class TestPushGuardReplayedCommits:
    """Replayed-commit detection via git cherry / patch-id equivalence."""

    def test_replayed_commits_refused(self, repo_pair):
        """Branch with commits that are patch-equivalent to upstream → refused.

        This is the exact incident pattern: a branch forked from a stale local
        trunk that carried cherry-picks of already-merged upstream commits.
        With <=5 total commits the count check alone passes, but the cherry
        check catches replayed (patch-equivalent) history.
        """
        clone_dir, origin_dir = repo_pair

        # Create a feature branch from the current main (stale fork point —
        # before the upstream commit we'll push next).
        _git(clone_dir, "checkout", "-b", "feature/replay")

        # Make a commit on the feature branch with specific content.
        Path(clone_dir, "shared_fix.py").write_text("# shared fix\n")
        _git(clone_dir, "add", "shared_fix.py")
        _git(clone_dir, "commit", "-m", "fix: shared bugfix (local)")

        # Now push the SAME patch (different commit message → different SHA,
        # same patch-id) to origin/main via a second clone.
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")
        Path(work2, "shared_fix.py").write_text("# shared fix\n")
        _git(work2, "add", "shared_fix.py")
        _git(work2, "commit", "-m", "fix: shared bugfix (upstream)")
        _git(work2, "push", "origin", "main")

        # Add one novel commit so we have 2 total (well under --max-ahead=5).
        Path(clone_dir, "novel.py").write_text("# novel work\n")
        _git(clone_dir, "add", "novel.py")
        _git(clone_dir, "commit", "-m", "feat: novel work")

        # The branch is 2 commits ahead (count OK) but one is replayed
        # (patch-equivalent to the commit now on origin/main).
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "patch-equivalent" in stderr
        assert "1 ahead-commit" in stderr

    def test_novel_commits_pass(self, repo_pair):
        """Branch with only genuinely novel commits (no upstream equivalents) → safe."""
        clone_dir, _ = repo_pair

        # Create a feature branch with novel work only.
        _git(clone_dir, "checkout", "-b", "feature/novel")
        for i in range(3):
            Path(clone_dir, f"novel{i}.py").write_text(f"# novel {i}\n")
            _git(clone_dir, "add", f"novel{i}.py")
            _git(clone_dir, "commit", "-m", f"feat: novel commit {i}")

        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 0, f"Expected safe (0), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "SAFE TO PUSH" in stdout

    def test_multiple_replayed_commits_refused(self, repo_pair):
        """Multiple replayed commits are all reported in the error message."""
        clone_dir, origin_dir = repo_pair

        # Create a feature branch from the current main (stale fork point).
        _git(clone_dir, "checkout", "-b", "feature/multi-replay")

        # Make two commits with specific patches on the feature branch.
        Path(clone_dir, "up1.py").write_text("# up1\n")
        _git(clone_dir, "add", "up1.py")
        _git(clone_dir, "commit", "-m", "fix: first shared (local)")
        Path(clone_dir, "up2.py").write_text("# up2\n")
        _git(clone_dir, "add", "up2.py")
        _git(clone_dir, "commit", "-m", "fix: second shared (local)")

        # Push the SAME two patches (different messages → different SHAs, same
        # patch-ids) to origin/main via a second clone.
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")
        Path(work2, "up1.py").write_text("# up1\n")
        _git(work2, "add", "up1.py")
        _git(work2, "commit", "-m", "fix: first shared (upstream)")
        Path(work2, "up2.py").write_text("# up2\n")
        _git(work2, "add", "up2.py")
        _git(work2, "commit", "-m", "fix: second shared (upstream)")
        _git(work2, "push", "origin", "main")

        # 2 commits ahead, both replayed (patch-equivalent to origin/main).
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "2 ahead-commit" in stderr
        assert "patch-equivalent" in stderr


class TestPushGuardRequireSingleOnBase:
    """Post-squash structural guard: --require-single-on-base mode."""

    def test_single_commit_on_base_passes(self, repo_pair):
        """A properly squashed branch (HEAD~1 == origin/main) → safe."""
        clone_dir, _ = repo_pair

        # Create a feature branch with one commit directly on origin/main.
        _git(clone_dir, "checkout", "-b", "feature/squashed")
        Path(clone_dir, "squashed.py").write_text("# squashed\n")
        _git(clone_dir, "add", "squashed.py")
        _git(clone_dir, "commit", "-m", "feat: squashed commit")

        # HEAD~1 should equal origin/main exactly.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 0, f"Expected safe (0), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "SAFE TO PUSH" in stdout

    def test_multiple_commits_refused(self, repo_pair):
        """Branch with 2+ commits (HEAD~1 != origin/main) → refused."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/not-squashed")
        Path(clone_dir, "a.py").write_text("# a\n")
        _git(clone_dir, "add", "a.py")
        _git(clone_dir, "commit", "-m", "first commit")
        Path(clone_dir, "b.py").write_text("# b\n")
        _git(clone_dir, "add", "b.py")
        _git(clone_dir, "commit", "-m", "second commit")

        # HEAD~1 is the first commit, not origin/main.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "does not sit directly" in stderr

    def test_stale_base_after_squash_refused(self, repo_pair):
        """Squashed onto a stale origin/main (before upstream advanced) → refused."""
        clone_dir, origin_dir = repo_pair

        # Create and squash a feature branch onto origin/main.
        _git(clone_dir, "checkout", "-b", "feature/stale-squash")
        Path(clone_dir, "fix.py").write_text("# fix\n")
        _git(clone_dir, "add", "fix.py")
        _git(clone_dir, "commit", "-m", "fix: the bug")

        # Now advance origin/main AFTER the squash — simulating base movement
        # between squash and push.
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")
        Path(work2, "upstream.txt").write_text("upstream advance\n")
        _git(work2, "add", "upstream.txt")
        _git(work2, "commit", "-m", "feat: upstream advance")
        _git(work2, "push", "origin", "main")

        # Now HEAD~1 points at the OLD origin/main, but a fresh fetch will
        # update origin/main → HEAD~1 != origin/main → refused.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "does not sit directly" in stderr

    def test_fetch_failure_refuses(self, tmp_path):
        """When fetch fails in --require-single-on-base mode → refused."""
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")
        _git(repo_dir, "remote", "add", "origin", "/nonexistent/repo.git")

        rc, stdout, stderr = _run_push_guard(repo_dir, ["--require-single-on-base"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "fetch" in stderr.lower()
