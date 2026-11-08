"""Seat page and confirmation forms. No untrusted HTML is inserted."""
from __future__ import annotations

from urllib.parse import quote

from core.hq.approval.render import esc, fields, form, shell
from .service import FACTOR_OPERATIONS


def seat_page(seats, approvals, factor, page):
    data = seats.get("data", seats)
    columns = (("label", "ラベル"), ("via", "受け口"), ("device", "端末"), ("tier", "段"),
               ("expires_at", "期限"), ("last_seen_at", "最終使用"), ("state", "状態"), ("key_fp", "公開鍵の指紋"))
    rows = '<table><thead><tr>' + ''.join('<th>' + label + '</th>' for _, label in columns) + '</tr></thead><tbody>'
    for seat in data.get("seats", []):
        rows += '<tr>' + ''.join('<td>' + esc(seat.get(key)) + '</td>' for key, _ in columns) + '</tr>'
    rows += '</tbody></table>'
    waiting = approvals.get("data", approvals).get("approvals", [])
    pending = '<section><h2>承認待ち</h2>' + ''.join('<p><a href="/approvals/' + quote(a['approval_id'], safe='') + '">' +
               esc(a['destination']) + ' ' + esc(a['approval_id']) + '</a></p>' for a in waiting if a['state'] == 'pending_user') + '</section>'
    # The response envelope for denials and locks is not fixed in S-22.10.
    # Display its non-secret summary via the explicit presenter at integration.
    summary = '<h2>接続の状態</h2><pre>' + esc(data.get("summary", "状態の応答は未接続")) + '</pre>'
    status = '<h2>第二要素</h2><pre>' + esc(factor) + '</pre>'
    tier = '<label>段<select name="tier"><option>read</option><option>operate</option><option>irreversible</option></select></label>'
    choices = ''.join('<option>' + x + '</option>' for x in ('seat.enroll_start', 'seat.tier', 'seat.revoke', 'seat.lock', 'seat.unlock'))
    controls = ('<label>操作<select name="destination">' + choices + '</select></label>'
                '<label>ラベル<input name="label" maxlength="200"></label>'
                '<label>席の ID<input name="seat_id" maxlength="32"></label>' + tier +
                '<label>期限の延長<input name="expires_at" placeholder="ISO8601"></label>'
                '<label>ロックの範囲<select name="scope"><option>ssh</option><option>tailnet</option><option>all</option></select></label>')
    actions = form('/v1/hq/seats/prepare', page, '内容を確認する', extra=controls)
    ssh = '<section><h2>SSH の席の登録</h2><p>公開鍵の貼り付けは登録の API 項目が未確定のため未接続。</p><button disabled>SSH の席を登録する</button></section>'
    warnings = ''.join('<p role="alert">' + esc(w['kind']) + ' を検知した。24 時間表示する。</p>' for w in factor['warnings'])
    return shell('司令塔の接続', warnings + rows + summary + pending + status + actions + ssh)


def confirmation_page(saved, page):
    return shell('席の操作の確認', '<h2>' + esc(saved['destination']) + '</h2><pre>' + esc(saved['payload']) + '</pre>' +
                 form('/v1/hq/seats/apply', page, '実行する', factor=saved['destination'] in FACTOR_OPERATIONS))


def completed_page(destination, display):
    details = ''
    if display.enrollment_code:
        details += '<h2>登録コード</h2><p>一度だけ表示する。記録や通知には出さない。</p><pre id="enrollment-code">' + esc(display.enrollment_code) + '</pre>'
    if display.key_fp:
        details += '<h2>公開鍵の指紋</h2><pre>' + esc(display.key_fp) + '</pre>'
    return shell('席の操作の結果', '<p>' + esc(destination) + ' を入口へ送った。</p>' + details)
