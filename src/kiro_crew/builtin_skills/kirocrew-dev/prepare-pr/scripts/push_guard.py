#!/usr/bin/env python3
"""push_guard.py - pre-push stale-base guard for the prepare-pr skill.

Verifies that the current HEAD is safe to force-push by checking:
1. The fetch of origin/<base> succeeds (fail closed on network error).
2. The merge-base of HEAD and freshly-fetched origin/<base> is an ancestor of
   origin/<base> (i.e. we diverged from the real upstream, not from a stale
   local trunk that carried unshipped integration commits).
3. The number of commits HEAD is ahead of origin/<base> is plausibly small
   (default threshold: 5 commits for a single-commit PR workflow; configurable
   via --max-ahead).
4. None of the ahead-commits are patch-equivalent to commits already on
   origin/<base> (replayed history from a stale fork point → refuse).

This prevents the catastrophic failure mode where a worktree branched from a
local integration trunk (kiki-trunk) carries 100+ unshipped commits that get
force-pushed to the remote feature branch, clobbering upstream work.

Portable: stdlib only; shells out to git via argument lists.

Usage:  python3 push_guard.py [--base <branch>] [--max-ahead <N>]
Exit:   0 SAFE | 40 REFUSED (stale base detected) | 2 environment error
"""

import argparse
import subprocess
import sys

# Single source of truth for the default max-ahead threshold.  Shared by
# preflight.py (which imports this constant) so the two scripts cannot drift.
DEFAULT_MAX_AHEAD = 5


def run(args):
    """Run a command; return (returncode, stdout, stderr) as stripped text."""
    try:
        p = subprocess.run(args, capture_output=True, text=True)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


def err(msg):
    sys.stderr.write(msg + "\n")


def _resolve_base(base_arg):
    """Resolve the base branch name from arg, symbolic ref, or default."""
    base = base_arg
    if not base:
        sym = run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])[1]
        if sym.startswith("origin/"):
            base = sym[len("origin/") :]
        if not base:
            base = "main"
    return base


def _fetch_base(base):
    """Fetch origin/<base>; return 0 on success or 40 on failure."""
    print("Fetching origin/{} ...".format(base))
    fetch_rc, _, fetch_err = run(["git", "fetch", "--quiet", "origin", base])
    if fetch_rc != 0:
        err(
            "REFUSED: git fetch origin {} failed. Cannot verify merge-base "
            "freshness — refusing to push on a potentially stale ref.\n"
            "  error: {}".format(base, fetch_err[:300])
        )
        return 40
    return 0


def _check_single_on_base(base):
    """Post-squash structural guard: assert HEAD~1 == origin/<base>.

    After a squash, the single commit should sit directly on the freshly
    fetched origin/<base>.  If HEAD~1 != origin/<base>, the squash landed
    on a stale ref or the branch carries unexpected history.

    Returns: 0 safe, 40 refused.
    """
    rc, head_parent, _ = run(["git", "rev-parse", "HEAD~1"])
    if rc != 0:
        err(
            "REFUSED: cannot resolve HEAD~1. The branch may have no parent "
            "commit (single root commit with no base)."
        )
        return 40

    rc, origin_base_sha, _ = run(["git", "rev-parse", "origin/{}".format(base)])
    if rc != 0:
        err("REFUSED: cannot resolve origin/{}.".format(base))
        return 40

    head_sha = run(["git", "rev-parse", "HEAD"])[1][:12]

    print("HEAD~1:          " + head_parent[:12])
    print("origin/{}:     {}".format(base, origin_base_sha[:12]))
    print("HEAD:            " + head_sha)

    if head_parent != origin_base_sha:
        err(
            "REFUSED: HEAD~1 ({}) != origin/{} ({}). "
            "The squashed commit does not sit directly on the freshly fetched "
            "remote base — either the squash landed on a stale ref or the "
            "branch carries unexpected history.\n"
            "  To fix: re-squash onto the fresh origin/{} "
            "(git reset --soft origin/{} && git commit).".format(
                head_parent[:12], base, origin_base_sha[:12], base, base
            )
        )
        return 40

    print("STATUS: SAFE TO PUSH (single commit on base)")
    return 0


def _check_pre_squash(base, max_ahead):
    """Pre-squash guard: merge-base ancestry, commit count, replayed commits.

    Returns: 0 safe, 40 refused.
    """
    # Compute merge-base of HEAD and freshly-fetched origin/<base>.
    rc, merge_base, _ = run(["git", "merge-base", "HEAD", "origin/{}".format(base)])
    if rc != 0 or not merge_base:
        err(
            "REFUSED: cannot compute merge-base between HEAD and origin/{}. "
            "The branch may have no common history with the remote base.".format(base)
        )
        return 40

    # Verify the merge-base is an ancestor of origin/<base>.
    rc, _, _ = run(["git", "merge-base", "--is-ancestor", merge_base, "origin/{}".format(base)])
    if rc != 0:
        err(
            "REFUSED: merge-base {} is NOT an ancestor of origin/{}. "
            "This means HEAD diverged from a ref that is not on the remote "
            "base branch — likely a stale local trunk. Rebase onto the fresh "
            "origin/{} first.".format(merge_base[:12], base, base)
        )
        return 40

    # Count commits HEAD is ahead of origin/<base>.
    rc, count_str, _ = run(["git", "rev-list", "--count", "origin/{}..HEAD".format(base)])
    if rc != 0:
        err("REFUSED: cannot count commits ahead of origin/{}.".format(base))
        return 40

    try:
        ahead = int(count_str)
    except ValueError:
        err("REFUSED: unexpected rev-list output: {}".format(count_str))
        return 40

    origin_base_sha = run(["git", "rev-parse", "origin/{}".format(base)])[1][:12]
    head_sha = run(["git", "rev-parse", "HEAD"])[1][:12]

    print("merge-base:      " + merge_base[:12])
    print("origin/{}:     {}".format(base, origin_base_sha))
    print("HEAD:            " + head_sha)
    print("commits ahead:   {}".format(ahead))
    print("max allowed:     {}".format(max_ahead))

    if ahead > max_ahead:
        err(
            "REFUSED: HEAD is {} commits ahead of origin/{} (max allowed: {}). "
            "This is far too many for a squashed single-commit PR — the branch "
            "likely carries unshipped local integration commits that would "
            "clobber upstream work if force-pushed.\n"
            "  To fix: git rebase origin/{} to rebase only your changes onto "
            "the fresh remote base, or reset to origin/{} and cherry-pick your "
            "commit.".format(ahead, base, max_ahead, base, base)
        )
        return 40

    # Detect replayed commits via git cherry.
    # `git cherry origin/<base> HEAD` lists ahead-commits; lines starting with
    # "-" are patch-equivalent to commits already on origin/<base> (replayed
    # history from a stale fork point).
    rc, cherry_out, _ = run(["git", "cherry", "origin/{}".format(base), "HEAD"])
    if rc == 0 and cherry_out:
        replayed = [
            line.split(None, 1)[1][:12] for line in cherry_out.splitlines() if line.startswith("- ")
        ]
        if replayed:
            err(
                "REFUSED: {} ahead-commit(s) are patch-equivalent to commits "
                "already on origin/{} — the branch replays upstream history "
                "from a stale fork point.\n"
                "  replayed: {}\n"
                "  To fix: rebase onto a fresh origin/{} so only novel changes "
                "remain, or cherry-pick your original commits onto "
                "origin/{}.".format(len(replayed), base, ", ".join(replayed), base, base)
            )
            return 40

    print("STATUS: SAFE TO PUSH")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Pre-push stale-base guard")
    parser.add_argument(
        "--base",
        default="",
        help="Base branch name (without origin/ prefix). "
        "Auto-detected from PR or origin/HEAD if omitted.",
    )
    parser.add_argument(
        "--max-ahead",
        type=int,
        default=DEFAULT_MAX_AHEAD,
        help="Maximum commits HEAD may be ahead of origin/<base> (default: {}).".format(
            DEFAULT_MAX_AHEAD
        ),
    )
    parser.add_argument(
        "--require-single-on-base",
        action="store_true",
        default=False,
        help="Post-squash mode: assert HEAD~1 == origin/<base> after a fresh "
        "fetch (the single squashed commit sits directly on the remote base).",
    )
    args = parser.parse_args()

    # Must be in a git repo.
    if run(["git", "rev-parse", "--is-inside-work-tree"])[0] != 0:
        err("ERROR: not inside a git repository.")
        return 2

    base = _resolve_base(args.base)

    # Fetch origin/<base> — MUST succeed (fail closed) for both modes.
    fetch_result = _fetch_base(base)
    if fetch_result != 0:
        return fetch_result

    if args.require_single_on_base:
        return _check_single_on_base(base)
    else:
        return _check_pre_squash(base, args.max_ahead)


if __name__ == "__main__":
    sys.exit(main())
