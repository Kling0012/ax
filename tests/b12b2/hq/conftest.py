from __future__ import annotations

import pytest
from starlette.applications import Starlette

from core.hq.approval.pages import Pages
from core.hq.approval.private import PrivateStore
from core.hq.approval.qr import svg
from core.hq.approval.service import FactorService, Identity
from core.hq.ext.b12b2 import BrowserPolicy, register
from tests.fakes.b12b2.hq.parts import FakeIntake, fake_checker, registration_display


@pytest.fixture
def world(tmp_path):
    hq = tmp_path / 'hq'
    hq.mkdir(mode=0o700)
    store = PrivateStore(hq / 'private')
    now = [1_800_000_000.0]
    events, notifications = [], []
    factor = FactorService(store, clock=lambda: now[0], checker=fake_checker,
                           emit=events.append, notify=notifications.append, qr_renderer=svg)
    identity = Identity('opaque-user', 'session-a', True)
    factor.enroll(identity)
    intake = FakeIntake()
    app = Starlette()
    app.state.hq_approval_intake = intake
    app.state.hq_approval_factor = factor
    app.state.hq_approval_identity = lambda request: identity
    app.state.hq_approval_browser_policy = BrowserPolicy(frozenset({'hq.test'}), frozenset({'https://hq.test'}))
    app.state.hq_seat_registration_display = registration_display
    register(app)
    app.state.hq_approval.pages = Pages(clock=lambda: now[0])
    return {'app': app, 'factor': factor, 'store': store, 'intake': intake, 'identity': identity,
            'now': now, 'events': events, 'notifications': notifications, 'root': tmp_path}
