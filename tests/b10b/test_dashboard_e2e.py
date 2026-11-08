"""The dashboard is served through HQ's public HTTP interface."""
from pathlib import Path

from tests.b10b.test_app import app, headers
from tests.fakes.b10b.runtime import call


def test_dashboard_is_served_with_operational_sections(tmp_path):
    application = app(tmp_path, ui_dir=Path(__file__).resolve().parents[2] / "core/hq/ui")
    response = call(application, "GET", "/", headers=headers())
    assert response.status_code == 200
    assert "<title>AXD（AX ダッシュボード）</title>" in response.text
    for heading in ("AXD（AX ダッシュボード）", "全体の状態", "承認待ちと ToDo", "ワーカープール", "プラグイン", "通知と健康状態"):
        assert heading in response.text
    for asset in ("/app.js", "/style.css"):
        assert call(application, "GET", asset, headers=headers()).status_code == 200


def test_dashboard_gets_require_auth_and_do_not_write(tmp_path):
    from core.hq.ext.b12b2 import register

    class Intake:
        def __init__(self):
            self.calls = []
        def approval_snapshot(self, proof):
            self.calls.append('read')
            return {'approvals': [{'approval_id': 'a', 'destination': 'example', 'state': 'pending_user'}]}

    class Control:
        def __init__(self):
            self.calls = []
        def get(self, path):
            self.calls.append(path)
            return {'plugins': [{'id': 'timer', 'kind': 'timer', 'status': 'registered', 'mode': 'run', 'manifest': {'outputs': []}},
                                {'id': 'send', 'kind': 'subloop', 'status': 'registered', 'mode': 'collect_only', 'manifest': {'outputs': ['discord.send']}}]}

    application = app(tmp_path)
    intake, control = Intake(), Control()
    application.state.hq_approval_intake = intake
    application.state.hq_dashboard_control = control
    register(application)
    for path in ('/v1/hq/approval-snapshot', '/v1/hq/plugins'):
        assert call(application, 'GET', path).status_code == 403
    assert intake.calls == [] and control.calls == []
    approvals = call(application, 'GET', '/v1/hq/approval-snapshot', headers=headers())
    plugins = call(application, 'GET', '/v1/hq/plugins', headers=headers())
    assert approvals.status_code == 200 and approvals.json()['approvals'][0]['state'] == 'pending_user'
    assert plugins.status_code == 200 and [(p['id'], p['mode'], p['sending']) for p in plugins.json()['plugins']] == [('timer', 'run', False), ('send', 'collect_only', True)]
    assert intake.calls == ['read'] and control.calls == ['/v1/plugins']
    for path in ('/v1/hq/approval-snapshot', '/v1/hq/plugins'):
        assert call(application, 'POST', path, headers=headers(origin=True), data={}).status_code == 405
