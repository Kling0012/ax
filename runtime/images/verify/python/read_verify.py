"""Validation primitives for research reports against a fixed input tree.

The caller must independently supply the expected revision and a trusted snapshot.
This checks evidence references and structure, not the truth of conclusions.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def verify_report(report: object, snapshot: Path, expected_revision: str) -> list[str]:
    errors = []
    if not isinstance(expected_revision, str) or not _REVISION.fullmatch(expected_revision):
        return ["invalid_expected_revision"]
    if not isinstance(report, dict) or set(report) != {"type", "revision", "findings"}:
        return ["invalid_report"]
    if report["type"] != "ax.research-report.v1":
        errors.append("invalid_type")
    if report["revision"] != expected_revision:
        errors.append("revision_mismatch")
    findings = report["findings"]
    if not isinstance(findings, list) or not findings:
        return errors + ["missing_findings"]
    root = Path(snapshot).resolve(strict=True)
    for finding in findings:
        if (not isinstance(finding, dict) or set(finding) != {"statement", "citations"}
                or not isinstance(finding["statement"], str) or not finding["statement"].strip()
                or not isinstance(finding["citations"], list) or not finding["citations"]):
            errors.append("invalid_finding")
            continue
        for citation in finding["citations"]:
            if not isinstance(citation, dict) or set(citation) != {"path", "line_start", "line_end"}:
                errors.append("invalid_citation")
                continue
            name, start, end = citation["path"], citation["line_start"], citation["line_end"]
            if (not isinstance(name, str) or not name or "\\" in name
                    or PurePosixPath(name).is_absolute() or any(p in (".", "..") for p in name.split("/"))
                    or type(start) is not int or type(end) is not int or start < 1 or end < start):
                errors.append("invalid_citation")
                continue
            try:
                path = root.joinpath(*name.split("/"))
                if any(p.is_symlink() for p in [path, *list(path.parents)[:len(name.split('/')) - 1]]):
                    raise ValueError("symlink")
                if not path.is_file() or not path.resolve(strict=True).is_relative_to(root):
                    raise ValueError("outside snapshot")
                with path.open("rb") as source:
                    count = sum(1 for _ in source)
                if end > count:
                    raise ValueError("line missing")
            except (OSError, ValueError):
                errors.append("citation_not_found")
    return errors


def main():
    import argparse
    import json
    import hashlib
    parser = argparse.ArgumentParser()
    parser.add_argument('--revision', required=True)
    parser.add_argument('--report', default='/work/research-report.json')
    parser.add_argument('--snapshot', default='/target')
    args = parser.parse_args()
    try:
        with open(args.report, 'rb') as stream:
            raw = stream.read(1048577)
        if len(raw) > 1048576:
            raise ValueError('report too large')
        report = json.loads(raw)
        errors = verify_report(report, Path(args.snapshot), args.revision)
    except (OSError, ValueError, TypeError):
        errors = ['report_unavailable']
    if errors:
        print('research verification failed: ' + ', '.join(errors))
        return 1
    print('research-report-sha256: ' + hashlib.sha256(raw).hexdigest())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
