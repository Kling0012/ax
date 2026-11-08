'use strict';
const $ = id => document.getElementById(id);
const states = {};
function numberAll() { document.querySelectorAll('[data-number]').forEach((node, i) => { node.dataset.number = String(i + 1); }); }
function text(tag, value, cls = '') { const e = document.createElement(tag); e.textContent = value; e.className = cls; e.dataset.number = ''; return e; }
function rows(id, entries, empty) { const root = $(id); root.replaceChildren(); if (!entries.length) root.append(text('p', empty, 'empty')); for (const [title, detail] of entries) { const item = document.createElement('div'); item.className = 'item'; item.append(text('strong', title), text('p', detail)); root.append(item); } numberAll(); }
function collection(data, key) { const value = (data.data || data)[key]; if (!Array.isArray(value)) throw Error('invalid'); return value; }
async function get(url) { const response = await fetch(url, {credentials:'same-origin', cache:'no-store'}); if (!response.ok) throw Error('unavailable'); const data = await response.json(); if (data.ok === false || data.error || data.status === 'unavailable') throw Error('unavailable'); return data; }
async function load(key, url, success, fail) { try { const data = await get(url); success(data); states[key] = true; } catch (_) { states[key] = false; fail(); } updateOverall(); numberAll(); }
function updateOverall() {
  const done = ['health','todos','jobs','slots','notify','approvals','plugins'].every(k => k in states);
  const bad = ['health','todos','jobs','slots','notify','approvals','plugins'].some(k => states[k] === false);
  $('overall').textContent = !done ? '確認中' : states.health === false ? '停止・接続不可' : bad || states.attention ? '注意' : '正常';
  $('overall-detail').textContent = !done ? '各サービスの応答を確認しています。' : states.health === false ? 'HQ の健康状態を取得できません。' : bad ? '取得できない項目があります。各区画を確認してください。' : states.attention ? '承認待ちまたはプラグインの状態を確認してください。' : '各項目を取得しました。';
}
function refresh() {
  for (const key of Object.keys(states)) delete states[key]; updateOverall();
  load('approvals','/v1/hq/approval-snapshot', d => { const items = collection(d,'approvals'); states.approvalAttention = items.some(x => x.state === 'pending_user'); states.attention = states.approvalAttention || states.pluginAttention; $('approval-status').textContent = items.length ? items.map(x => `${x.destination || '承認'}: ${x.state === 'expired' ? '期限切れ' : '承認待ち'}`).join(' / ') : '承認待ちはありません'; }, () => { $('approval-status').textContent = '取得できませんでした'; });
  load('plugins','/v1/hq/plugins', d => { const items = collection(d,'plugins'); states.pluginAttention = items.some(x => x.status !== 'registered' || !['run','collect_only'].includes(x.mode)); states.attention = states.approvalAttention || states.pluginAttention; for (const [filter, detail, badge] of [[x => x.kind === 'timer', 'timer-plugins', 'timer-status'], [x => x.sending, 'sending-plugins', 'sending-status']]) { const list = items.filter(filter); $(detail).textContent = list.length ? list.map(x => `${x.id}: ${x.status} / ${x.mode}`).join(' · ') : '対象なし'; $(badge).textContent = `${list.length} 件`; } }, () => { for (const id of ['timer-plugins','sending-plugins']) $(id).textContent = '取得できませんでした'; for (const id of ['timer-status','sending-status']) $(id).textContent = '不明'; });
  load('todos','/v1/todos', d => { const items = collection(d,'todos'); rows('todos',items.map(x => [x.title || x.body || x.id || 'ToDo', x.state || '状態不明']), '表示する ToDo はありません。'); $('todo-status').textContent = '取得済み'; }, () => { rows('todos',[], '取得できませんでした。'); $('todo-status').textContent = '取得不可'; });
  load('jobs','/v1/pool/jobs', d => { const jobs = collection(d,'jobs').filter(x => ['running','launching','failed','timed_out','lost'].includes(x.state || x.status)); rows('jobs', jobs.map(x => [x.title || x.id || x.job_id || 'ジョブ', `状態: ${x.state || x.status || '不明'}${x.error ? ' / ' + (typeof x.error === 'string' ? x.error : JSON.stringify(x.error)) : ''}`]), '実行中・失敗のジョブはありません。'); $('jobs-status').textContent = '取得済み'; }, () => { rows('jobs',[], '取得できませんでした。'); $('jobs-status').textContent = '取得不可'; });
  load('slots','/v1/pool/slots', d => { const value = d.data || d; $('slots').textContent = value.in_use == null ? '席の詳細は確認できません' : `使用中: ${typeof value.in_use === 'object' ? JSON.stringify(value.in_use) : value.in_use}`; }, () => { $('slots').textContent = '取得できませんでした'; });
  load('notify','/v1/notify/status', d => { $('notify').textContent = d.guard_verifiable === true ? '通知の検査が可能' : d.guard_verifiable === false ? '通知の検査ができません' : '通知状態を取得しました'; }, () => { $('notify').textContent = '取得できませんでした'; });
  load('health','/healthz', d => { if (d.ok !== true) throw Error('unavailable'); $('health-status').textContent = d.ok === true ? '応答あり' : '状態不明'; rows('health', [['HQ', d.ok === true ? '応答あり' : '状態不明'], ['拡張', Array.isArray(d.ext_failed) && d.ext_failed.length ? '読み込み失敗: ' + d.ext_failed.join(', ') : '読み込み失敗の報告なし']], ''); }, () => { $('health-status').textContent = '取得不可'; rows('health',[], '取得できませんでした。'); });
}
function toggleNumbers() { const active = document.body.classList.toggle('show-numbers'); $('numbers').setAttribute('aria-pressed', String(active)); }
$('numbers').addEventListener('click', toggleNumbers); $('refresh').addEventListener('click', refresh);
document.addEventListener('keydown', e => { if (e.altKey && !e.ctrlKey && !e.metaKey && e.key.toLowerCase() === 'n') { e.preventDefault(); toggleNumbers(); } });
numberAll(); refresh();
