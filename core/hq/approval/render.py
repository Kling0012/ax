"""HTML is server-rendered from saved intake content; all values are escaped."""
from __future__ import annotations

import base64
import html
import json
from urllib.parse import quote


def esc(value) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, indent=2)
    return html.escape(str(value if value is not None else ""), quote=True)


def fields(page):
    return ''.join(f'<input type="hidden" name="{k}" value="{esc(page[k])}">' for k in ("page_id", "csrf_token"))


def form(action, page, label, *, factor=False, extra=""):
    code = ('<label>第二要素のコード<input name="code" inputmode="numeric" pattern="[0-9]{6}" '
            'maxlength="6" autocomplete="one-time-code" required></label>') if factor else ""
    return (f'<form method="post" action="{esc(action)}">{fields(page)}{extra}{code}'
            f'<button type="submit">{esc(label)}</button></form>')


def shell(title, body):
    return (f'<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)}</title><link rel="stylesheet" href="/v1/hq/b12b2/ui.css">'
            '<script src="/v1/hq/b12b2/ui.js" defer></script></head><body><main>'
            '<nav><a href="/seats">司令塔の接続</a> <a href="/approval/settings">承認の設定</a></nav>'
            f'<h1>{esc(title)}</h1>{body}<p>Alt+N で番号を表示する。</p></main></body></html>')


def approval_page(saved, page):
    payload = saved["payload"]
    explanation = saved.get("explanation", {}).get("text", payload.get("reason", ""))
    fixed = {"行き先": saved["destination"], "依頼した席": saved.get("requested_by"),
             "接続": saved.get("caller"), "期限": saved["expires_at"], "状態": saved["state"],
             "digest": saved["digest"][:8]}
    summary = {k: v for k, v in payload.items() if k not in {"reason", "go_md_text"}}
    go_full = ""
    if saved["destination"] == "go.confirm":
        values = saved.get("derived") if payload["kind"] == "ratify" else payload
        if not isinstance(values, dict):
            raise ValueError("saved_go_derived_missing")
        fixed.update({"kind": payload["kind"], "範囲": values["scope"], "範囲外": values["excluded"],
                      "変更の番号": values["changes"], "go_md_sha256": payload.get("go_md_sha256", "")[:12]})
        if payload["kind"] == "ratify":
            go_full = '<section><h2>保存済み go の全文</h2><pre id="go-md-text">' + esc(payload["go_md_text"]) + '</pre></section>'
    fixed_html = '<dl id="fixed-fields">' + ''.join(f'<dt>{esc(k)}</dt><dd>{esc(v)}</dd>' for k, v in fixed.items()) + '</dl>'
    reason = '<fieldset id="untrusted-reason"><legend>司令塔の説明（未検証）</legend><pre>' + esc(explanation) + '</pre></fieldset>'
    path = '/v1/hq/approvals/' + quote(saved["approval_id"], safe="")
    actions = ""
    if saved["state"] == "pending_user":
        actions = form(path + '/grant', page, '承認', factor=True) + form(path + '/deny', page, '却下', factor=True)
    return shell('戻しにくい操作の承認', fixed_html + '<h2>保存済みの操作</h2><pre>' + esc(summary) + '</pre>' + go_full + reason + actions)


def factor_page(status, page):
    warnings = ''.join('<p role="alert">' + esc(w["kind"]) + ' のホスト操作を検知した。24 時間表示する。</p>' for w in status['warnings'])
    label = '第二要素を取り替える' if status['enrolled'] else '第二要素を登録する'
    return shell('承認の設定', warnings + '<p>登録済み: ' + esc(status['enrolled']) + '</p>' +
                 form('/v1/hq/approval/enroll', page, label, factor=status['enrolled']))


def qr_page(result):
    # A trusted QR renderer's SVG is shown as an image, never parsed into DOM.
    encoded = base64.b64encode(result['qr_svg'].encode('utf-8')).decode('ascii')
    return shell('第二要素の登録', '<p>認証アプリで QR を読む。この画面は再表示できない。</p>' +
                 f'<img alt="承認用 TOTP の QR" src="data:image/svg+xml;base64,{encoded}">')


CSS = '''body{font:1rem/1.6 system-ui,sans-serif;margin:0;background:#f7f8fa;color:#18212c}main{max-width:72rem;margin:auto;padding:1.5rem}nav{display:flex;gap:1rem}form,fieldset,section{margin:1rem 0;padding:1rem;border:1px solid #8995a3;border-radius:.4rem}label{display:block;margin:.5rem 0}input,select,textarea,button{font:inherit;max-width:100%;box-sizing:border-box}textarea{width:100%;min-height:5rem}button{margin:.5rem;padding:.4rem .8rem}pre{white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid #8995a3;padding:1rem;font-family:monospace}table{border-collapse:collapse;width:100%;display:block;overflow:auto}td,th{padding:.4rem;border:1px solid #8995a3}dt{font-weight:bold}dd{margin-bottom:.5rem;overflow-wrap:anywhere}.ax-number{display:none;font:small monospace;color:#612800;background:#fff1c9;margin-right:.3rem}body.ax-numbered .ax-number{display:inline}'''

JS = '''document.addEventListener("keydown",(event)=>{if(event.altKey&&event.code==="KeyN"){event.preventDefault();if(!document.querySelector(".ax-number")){let n=0;document.querySelectorAll("h1,h2,p,dt,dd,legend,label,button,a,th,td,pre").forEach((el)=>{const badge=document.createElement("span");badge.className="ax-number";badge.setAttribute("aria-hidden","true");badge.textContent=String(++n);el.prepend(badge);});}document.body.classList.toggle("ax-numbered");}});'''
