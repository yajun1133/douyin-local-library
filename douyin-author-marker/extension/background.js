/* 后台 Service Worker
 * 所有对 127.0.0.1 的请求都走这里：扩展后台有 host_permissions，
 * 不受页面 https 混合内容和 CORS 的限制。
 * 同时做一层内存缓存（默认 60 秒），避免反复查同一个作者。
 */
const DEFAULTS = {
  enabled: true,
  port: 8790,
  ttlMs: 60000,
  showPanel: true,
  showPill: true,
  collapsed: false,
  pos: null,
  debug: false
};

const MAX_CACHE = 500;
const CACHE = new Map(); // key -> {t, data}

async function getSettings() {
  try {
    const got = await chrome.storage.local.get('settings');
    return Object.assign({}, DEFAULTS, (got && got.settings) || {});
  } catch (e) {
    return Object.assign({}, DEFAULTS);
  }
}

function cacheGet(key, ttl) {
  const e = CACHE.get(key);
  if (!e) return null;
  if (Date.now() - e.t > ttl) { CACHE.delete(key); return null; }
  CACHE.delete(key); // LRU：命中后挪到末尾
  CACHE.set(key, e);
  return e;
}

function cacheSet(key, data) {
  if (CACHE.size >= MAX_CACHE) {
    const first = CACHE.keys().next().value;
    if (first !== undefined) CACHE.delete(first);
  }
  CACHE.set(key, { t: Date.now(), data });
}

async function apiGet(pathInfo, timeoutMs) {
  const s = await getSettings();
  const base = 'http://127.0.0.1:' + (Number(s.port) || 8790);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs || 3500);
  try {
    const res = await fetch(base + pathInfo, {
      method: 'GET',
      cache: 'no-store',
      signal: ctrl.signal,
      headers: { 'Accept': 'application/json' }
    });
    if (!res.ok) return { ok: false, code: 'http', message: 'HTTP ' + res.status };
    const data = await res.json();
    return { ok: true, data };
  } catch (e) {
    const timeout = e && e.name === 'AbortError';
    return {
      ok: false,
      code: timeout ? 'timeout' : 'unreachable',
      message: timeout ? '本地服务响应超时' : '连不上本地服务（127.0.0.1:' + base.split(':').pop() + '）'
    };
  } finally {
    clearTimeout(timer);
  }
}

async function apiPost(pathInfo, body, timeoutMs) {
  const s = await getSettings();
  const base = 'http://127.0.0.1:' + (Number(s.port) || 8790);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs || 3500);
  try {
    const res = await fetch(base + pathInfo, {
      method: 'POST',
      cache: 'no-store',
      signal: ctrl.signal,
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(body || {})
    });
    if (!res.ok) return { ok: false, code: 'http', message: 'HTTP ' + res.status };
    const data = await res.json();
    return { ok: true, data };
  } catch (e) {
    const timeout = e && e.name === 'AbortError';
    return {
      ok: false,
      code: timeout ? 'timeout' : 'unreachable',
      message: timeout ? '本地服务响应超时' : '连不上本地服务（127.0.0.1:' + base.split(':').pop() + '）'
    };
  } finally {
    clearTimeout(timer);
  }
}

function qs(obj) {
  const p = new URLSearchParams();
  for (const k of Object.keys(obj)) {
    const v = obj[k];
    if (v !== undefined && v !== null && String(v) !== '') p.set(k, String(v));
  }
  return p.toString();
}

async function doLookup(q, meta, force) {
  const s = await getSettings();
  // meta 会影响「本地文件大小 vs 抖音原片」这类对比结果，所以缓存键里带上关键指纹
  const fp = meta ? [meta.aweme_id, meta.size || 0, meta.create_time || 0].join(':') : '';
  const key = [q.sec_uid || '', q.uid || '', q.nickname || '', q.aweme_id || '', fp].join('|');
  if (!force) {
    const hit = cacheGet(key, Number(s.ttlMs) || 60000);
    if (hit) return { ok: true, data: hit.data, cached: true };
  }
  let r = await apiPost('/lookup', { query: q, meta: meta || null }, 4000);
  // 老版本服务只认 GET /lookup：POST 失败（404/405）就退回 GET，保证混版本也能用
  if (!r.ok && r.code === 'http') {
    r = await apiGet('/lookup?' + qs(q), 3500);
  }
  if (!r.ok) return r;
  if (r.data && r.data.ok === false) {
    return { ok: false, code: 'badjson', message: r.data.error || '本地服务返回异常' };
  }
  cacheSet(key, r.data);
  return { ok: true, data: r.data, cached: false };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      switch (msg && msg.type) {
        case 'lookup': {
          const s = await getSettings();
          if (!s.enabled) { sendResponse({ ok: false, code: 'disabled', message: '插件已关闭' }); return; }
          sendResponse(await doLookup(msg.query || {}, msg.meta || null, !!msg.force));
          return;
        }
        case 'queueAdd': {
          // 建档可能要等后台查 UID，给足 35 秒
          sendResponse(await apiPost('/queue/add', msg.payload || {}, 35000));
          return;
        }
        case 'health': {
          const r = await apiGet('/health', 2000);
          sendResponse(r.ok ? { ok: true, data: r.data } : r);
          return;
        }
        case 'getSettings': {
          sendResponse({ ok: true, data: await getSettings() });
          return;
        }
        case 'setSettings': {
          const cur = await getSettings();
          const next = Object.assign({}, cur, msg.patch || {});
          await chrome.storage.local.set({ settings: next });
          if (msg.patch && (msg.patch.port !== undefined || msg.patch.enabled !== undefined ||
              msg.patch.ttlMs !== undefined)) CACHE.clear();
          sendResponse({ ok: true, data: next });
          return;
        }
        case 'clearCache': {
          CACHE.clear();
          sendResponse({ ok: true, data: { size: 0 } });
          return;
        }
        case 'cacheSize': {
          sendResponse({ ok: true, data: { size: CACHE.size } });
          return;
        }
        default:
          sendResponse({ ok: false, code: 'unknown', message: '未知消息' });
      }
    } catch (e) {
      sendResponse({ ok: false, code: 'error', message: String((e && e.message) || e) });
    }
  })();
  return true; // 异步响应
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get('settings').then((got) => {
    if (!got || !got.settings) chrome.storage.local.set({ settings: Object.assign({}, DEFAULTS) });
  }).catch(() => {});
});
