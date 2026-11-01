const DEFAULTS = {
  enabled: true, port: 8790, ttlMs: 60000,
  showPanel: true, showPill: true, collapsed: false, pos: null, debug: false
};
let S = Object.assign({}, DEFAULTS);

const $ = (id) => document.getElementById(id);

function fmtNum(n) {
  if (n == null) return '—';
  if (n >= 10000) return (n / 10000).toFixed(1) + ' 万';
  return String(n);
}

function setMsg(text, kind) {
  const el = $('msg');
  el.textContent = text || '';
  el.className = 'msg' + (kind ? ' ' + kind : '');
}

function setStatus(state, text) {
  const dot = $('dot');
  dot.className = 'dot' + (state ? ' ' + state : '');
  $('statusText').textContent = text;
  $('hint').classList.toggle('show', state === 'err');
}

function paintToggles() {
  $('swEnabled').classList.toggle('on', !!S.enabled);
  $('swPanel').classList.toggle('on', !!S.showPanel);
  $('swPill').classList.toggle('on', !!S.showPill);
  $('port').value = S.port || 8790;
}

function send(msg) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(msg, (res) => {
        if (chrome.runtime.lastError) {
          resolve({ ok: false, code: 'context', message: chrome.runtime.lastError.message });
          return;
        }
        resolve(res || { ok: false, code: 'empty', message: '无响应' });
      });
    } catch (e) { resolve({ ok: false, code: 'context', message: String(e) }); }
  });
}

async function save(patch) {
  S = Object.assign({}, S, patch);
  paintToggles();
  await send({ type: 'setSettings', patch });
}

async function health(showMsg) {
  if (showMsg) setMsg('检测中…');
  const r = await send({ type: 'health' });
  if (r.ok && r.data) {
    setStatus('ok', '本地服务已连接 · ' + (r.data.version || ''));
    $('sAuthors').textContent = fmtNum(r.data.authors);
    $('sFiles').textContent = fmtNum(r.data.files);
    $('sFresh').textContent = (r.data.scanned_at || '—').slice(5, 16) || '—';
    setMsg(showMsg ? '连接正常，索引 ' + (r.data.build_ms || 0) + ' ms' : '', 'ok');
  } else {
    setStatus('err', '本地服务未连接');
    $('sAuthors').textContent = '—';
    $('sFiles').textContent = '—';
    $('sFresh').textContent = '—';
    setMsg(r.message || '连不上 127.0.0.1:' + (S.port || 8790), 'err');
  }
}

async function broadcastRefresh() {
  try {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const t = tabs && tabs[0];
      if (t && t.id != null) chrome.tabs.sendMessage(t.id, { type: 'refresh' }, () => void chrome.runtime.lastError);
    });
  } catch (e) { /* ignore */ }
}

document.addEventListener('DOMContentLoaded', async () => {
  const got = await chrome.storage.local.get('settings');
  S = Object.assign({}, DEFAULTS, (got && got.settings) || {});
  paintToggles();
  health(false);

  $('btnTest').addEventListener('click', () => health(true));

  $('swEnabled').addEventListener('click', () => save({ enabled: !S.enabled }));
  $('swPanel').addEventListener('click', () => save({ showPanel: !S.showPanel }));
  $('swPill').addEventListener('click', () => save({ showPill: !S.showPill }));

  $('btnPort').addEventListener('click', async () => {
    const p = parseInt($('port').value, 10);
    if (!p || p < 1024 || p > 65535) { setMsg('端口需在 1024–65535 之间', 'err'); return; }
    await save({ port: p });
    setMsg('已保存端口 ' + p + '，正在重新检测…');
    setTimeout(() => health(true), 120);
  });

  $('btnRefresh').addEventListener('click', async () => {
    setMsg('正在清除缓存…');
    await send({ type: 'clearCache' });
    broadcastRefresh();
    setMsg('缓存已清除，页面已刷新查询', 'ok');
    setTimeout(() => health(false), 200);
  });

  $('btnLib').addEventListener('click', () => {
    chrome.tabs.create({ url: 'http://127.0.0.1:8723/' });
  });
});
