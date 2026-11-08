'use strict';
const $ = id => document.getElementById(id);
const formatter = new Intl.DateTimeFormat('ja-JP', {timeZone:'Asia/Tokyo', year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hourCycle:'h23'});
function date(value) { const d = new Date(value); return value && !Number.isNaN(d.valueOf()) ? formatter.format(d) : '時刻不明'; }
function numberAll() { document.querySelectorAll('[data-number]').forEach((node, i) => {node.dataset.number = String(i + 1);}); }
function line(text, cls) { const el = document.createElement('p'); el.className = cls; el.dataset.number = ''; el.textContent = text; return el; }
function item(title, meta) { const el = document.createElement('div'); el.className = 'item'; el.append(line(title, 'item-title'), line(meta, 'item-meta')); return el; }
function show(id, rows, empty) { const root = $(id); root.replaceChildren(...(rows.length ? rows : [line(empty, 'empty')])); numberAll(); }
async function load(id, url, convert) {
  const state = $(id === 'schedules' ? 'schedule-state' : id === 'todos' ? 'todo-state' : id + '-state');
  state.classList.remove('error'); state.textContent = '取得中';
  try {
    const response = await fetch(url, {credentials:'same-origin', cache:'no-store'});
    if (!response.ok) throw new Error('unavailable');
    const data = await response.json();
    if (data.ok === false || data.error || data.status === 'unavailable') throw new Error('unavailable');
    const rows = convert(data);
    if (!Array.isArray(rows)) throw new Error('invalid');
    show(id, rows, '表示する項目はありません。');
    state.textContent = data.sample || data.status === 'sample' ? '見本' : '取得済み';
  } catch (_) {
    state.textContent = '取得できませんでした'; state.classList.add('error');
    show(id, [line('接続先を確認して、もう一度更新してください。', 'empty')], '');
  }
}
function array(data, key) { const value = (data.data || data)[key]; if (!Array.isArray(value)) throw new Error('invalid'); return value; }
function refresh() {
  load('schedules', '/v1/schedules?state=planned', data => array(data, 'schedules').map(x => item(x.body || x.id || '予定', `${date(x.start_at)} 〜 ${date(x.end_at)} JST`)));
  load('todos', '/v1/todos', data => array(data, 'todos').map(x => item(x.title || x.body || x.id || 'ToDo', x.state || '状態不明')));
  load('pool', '/v1/pool/jobs', data => array(data, 'jobs').map(x => item(x.title || x.id || x.job_id || '作業',
    `状態: ${x.state || x.status || '不明'} / 検証: ${x.verify?.rc ?? '未実施'} / エラー: ${x.error || x.report?.human_action || 'なし'}`)));
  load('slots', '/v1/pool/slots', data => [item('使用中', String(data.in_use ?? '不明'))]);
  load('selections', '/v1/pool/selections', data => array(data, 'selections').map(x => {
    const doc = x.doc || x.selection || x;
    return item(doc.selected || doc.model || x.job_id || '選択', doc.reason || '理由なし');
  }));
}
function toggleNumbers() { const active = document.body.classList.toggle('show-numbers'); $('numbers').setAttribute('aria-pressed', String(active)); }
$('numbers').addEventListener('click', toggleNumbers);
$('refresh').addEventListener('click', refresh);
document.addEventListener('keydown', e => { if (e.altKey && !e.ctrlKey && !e.metaKey && e.key.toLowerCase() === 'n') { e.preventDefault(); toggleNumbers(); } });
numberAll(); refresh();
