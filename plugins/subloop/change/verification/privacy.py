"""Fixed privacy check; patterns are staged by the trusted preparer, not the PR."""
import shlex

# git grep -q prevents matching private text from reaching stdout/stderr.
SOURCE = '''from pathlib import Path
import subprocess
import sys
p = Path("/work/privacy-patterns")
if not p.is_file():
    sys.exit(2)
patterns = [line for line in p.read_text(encoding="utf-8").splitlines() if line]
if not patterns:
    sys.exit(2)
for pattern in patterns:
    result = subprocess.run(["git", "grep", "-qE", "--", pattern], capture_output=True)
    if result.returncode != 1:
        sys.exit(1 if result.returncode == 0 else 2)
sys.exit(0)
'''


def privacy_command():
    return "python3 -c " + shlex.quote(SOURCE)
