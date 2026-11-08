"""Acceptance ports. Only intake owns the configured integration checkout."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


class MissingBackend:
    def snapshot(self, branch):
        raise NotImplementedError("F-18.3: integration checkout and verify-ax-ci binding required")


class GitBackend:
    """Configured checkout of ax.git, with a separate work.git staging remote.

    verify_job(runner, command, checkout, sha) must return True only when the
    runner's completed job passed. It must never run CI inside the intake process.
    The caller serializes snapshot/verification/merge using Engine.lock.
    """
    def __init__(self, checkout, staging, verify_job, *, run=subprocess.run):
        self.checkout, self.staging = Path(checkout), str(staging)
        self.verify_job, self.run = verify_job, run

    def git(self, *args):
        result = self.run(["git", "-C", str(self.checkout), *args], check=True,
                          capture_output=True, text=True, timeout=300)
        return result.stdout.strip()

    def snapshot(self, branch):
        if not re.fullmatch(r"(?:core|subloop)/[a-z0-9][a-z0-9_/-]*", branch) or ".." in branch or "//" in branch:
            raise ValueError("invalid acceptance branch")
        self.git("check-ref-format", "refs/heads/" + branch)
        if self.git("status", "--porcelain"):
            raise ValueError("integration checkout is dirty")
        self.git("fetch", "--no-tags", self.staging, "refs/heads/" + branch)
        sha = self.git("rev-parse", "--verify", "FETCH_HEAD^{commit}")
        self.git("switch", "--detach", sha)
        return sha

    def verify(self, sha):
        return self.verify_job("verify-ax-ci", "tools/ci.sh", self.checkout, sha) is True

    def merge(self, sha):
        # A concurrent update of ax.git/main makes the non-force push fail.
        self.git("switch", "main")
        self.git("merge", "--ff-only", sha)
        if self.git("rev-parse", "HEAD") != sha:
            raise ValueError("main is not the verified commit")
        self.git("push", "local", "main:main")
        return sha

    def tag(self, sha):
        name = "accepted/" + sha
        existing = self.run(["git", "-C", str(self.checkout), "rev-parse", "--verify", "refs/tags/" + name],
                            capture_output=True, text=True, timeout=30)
        if existing.returncode == 0:
            if existing.stdout.strip() != sha:
                raise ValueError("accepted tag mismatch")
        else:
            self.git("tag", name, sha)
        self.git("push", "local", "refs/tags/" + name)
