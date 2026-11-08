"""Exercise intake with a gh executable that never contacts GitHub."""
import json

import pytest

from core.intake.dest.b12b3.change import ChangeError, ChangeLedger
from core.intake.dest.b12b3.github_pr import resolve_pr


@pytest.fixture
def gh(tmp_path, monkeypatch):
    executable = tmp_path / 'gh'
    executable.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$GH_CALLS"
case "$GH_MODE" in
  fail) exit 1 ;;
  timeout) exec sleep 2 ;;
  bad) printf '{broken' ;;
  *) printf '%s' "$GH_RESPONSE" ;;
esac
''')
    executable.chmod(0o755)
    calls = tmp_path / 'calls'
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setenv('GH_CALLS', str(calls))
    monkeypatch.setenv('GH_MODE', 'ok')
    monkeypatch.setenv('GH_RESPONSE', json.dumps({'number': 42, 'head': {'sha': 'a' * 40},
        'base': {'repo': {'full_name': 'owner/repo'}}, 'merged': False, 'state': 'open'}))
    return calls


def request(tmp_path, timeout=1):
    ledger = ChangeLedger(tmp_path / 'ledger.db', ['owner/repo'],
                          resolve_pr=lambda repo, number: resolve_pr(repo, number, timeout=timeout))
    return ledger.request('request-1', {'issue_repo': 'owner/repo', 'kind': 'pr',
        'number': 42, 'dry_run': False}, 'owner')


def assert_read_only(calls):
    assert calls.read_text().splitlines() == ['api --method GET repos/owner/repo/pulls/42']


def test_intake_records_current_sha(tmp_path, gh):
    assert request(tmp_path)['head_sha'] == 'a' * 40
    assert_read_only(gh)


@pytest.mark.parametrize('mode', ['fail', 'timeout', 'bad'])
def test_unavailable_state_is_rejected(tmp_path, gh, monkeypatch, mode):
    monkeypatch.setenv('GH_MODE', mode)
    # Use an absolute sleep path because PATH contains only the fake gh.
    if mode == 'timeout':
        gh_script = tmp_path / 'gh'
        gh_script.write_text(gh_script.read_text().replace('exec sleep 2', 'exec /bin/sleep 2'))
    with pytest.raises(ChangeError) as error:
        request(tmp_path, timeout=0.1)
    assert error.value.code == 'pr_state_unavailable'
    assert_read_only(gh)


@pytest.mark.parametrize('field,value', [('head', {}), ('merged', 'false'),
    ('state', 'unknown'), ('number', 43), ('base', {'repo': {'full_name': 'other/repo'}})])
def test_wrong_response_shape_is_rejected(tmp_path, gh, monkeypatch, field, value):
    data = {'number': 42, 'head': {'sha': 'a' * 40},
            'base': {'repo': {'full_name': 'owner/repo'}}, 'merged': False, 'state': 'open'}
    data[field] = value
    monkeypatch.setenv('GH_RESPONSE', json.dumps(data))
    with pytest.raises(ChangeError) as error:
        request(tmp_path)
    assert error.value.code == 'pr_state_unavailable'
    assert_read_only(gh)
