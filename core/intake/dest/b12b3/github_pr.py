"""Read pull request state using the existing gh authentication."""
from __future__ import annotations

import json
import re
import subprocess


def resolve_pr(repo, number, *, timeout=10):
    """Return a verified PR snapshot, or None when it cannot be obtained."""
    if (not isinstance(repo, str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo)
            or type(number) is not int or number < 1):
        return None
    try:
        result = subprocess.run(
            ['gh', 'api', '--method', 'GET', f'repos/{repo}/pulls/{number}'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout,
            check=True,
        )
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            return None
        head = data.get('head')
        base = data.get('base')
        if not isinstance(head, dict) or not isinstance(base, dict):
            return None
        base_repo = base.get('repo')
        if not isinstance(base_repo, dict):
            return None
        actual_repo = base_repo.get('full_name')
        sha = head.get('sha')
        merged = data.get('merged')
        state = data.get('state')
        if (not isinstance(actual_repo, str) or actual_repo.lower() != repo.lower()
                or type(data.get('number')) is not int or data['number'] != number
                or not isinstance(sha, str) or not re.fullmatch(r'[0-9a-fA-F]{40}', sha)
                or type(merged) is not bool or state not in ('open', 'closed')
                or (merged and state != 'closed')):
            return None
        return {'issue_repo': actual_repo, 'number': number, 'head_sha': sha.lower(),
                'merged': merged, 'state': state}
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
        return None
