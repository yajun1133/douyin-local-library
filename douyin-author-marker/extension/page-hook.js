/* 页面世界脚本（MAIN world）
 * 只做一件事：旁听抖音自己的接口返回，把「作品ID + 作者 sec_uid/uid/昵称」取出来
 * 通过 window.postMessage 交给隔离世界的 content.js。
 * 全程 try/catch 包裹，任何异常都不会影响抖音本身。
 */
(() => {
  'use strict';
  const MAX_NODES = 6000;
  const seen = new Set();

  function post(payload) {
    try { window.postMessage({ __dyLocalHook: 1, payload }, '*'); } catch (e) { /* ignore */ }
  }

  /* 从一条 aweme 里刮出所有「一次查询就能白拿」的信息。
   * 全部字段都做存在性判断，抖音改结构也不会炸，缺了就空着。 */
  function num(v) { return typeof v === 'number' && isFinite(v) ? v : 0; }

  function pickAddr(v) {
    // play_addr 有时是对象，有时是数组；数组里挑码率最高的那个
    if (!v) return null;
    if (Array.isArray(v)) {
      let best = null;
      for (const it of v) {
        if (!it) continue;
        const s = num(it.data_size);
        if (!best || s > num(best.data_size)) best = it;
      }
      return best;
    }
    return v;
  }

  function metaOf(aweme, a) {
    const st = aweme.statistics || aweme.stats || {};
    const v = aweme.video || {};
    const addr = pickAddr(v.play_addr) || {};
    const tags = [];
    const te = aweme.text_extra || aweme.textExtra || [];
    for (const t of te) {
      const n = t && (t.hashtag_name || t.hashtagName);
      if (n && tags.length < 6) tags.push(String(n));
    }
    const imgs = aweme.images || (aweme.image_post_info && aweme.image_post_info.images) || [];
    const music = aweme.music || {};
    const poi = aweme.poi_info || aweme.poiInfo || {};
    const m = {
      aweme_id: String(aweme.aweme_id || ''),
      desc: String(aweme.desc || '').slice(0, 200),
      create_time: num(aweme.create_time),
      media_type: num(aweme.media_type),
      is_top: !!aweme.is_top,
      dur: num(v.duration),                 // 毫秒
      w: num(addr.width) || num(v.width),
      h: num(addr.height) || num(v.height),
      size: num(addr.data_size),            // 抖音原片字节数（默认清晰度）
      digg: num(st.digg_count),
      comment: num(st.comment_count),
      collect: num(st.collect_count),
      share: num(st.share_count),
      play: num(st.play_count),
      music: String(music.title || '').slice(0, 40),
      tags,
      poi: String(poi.poi_name || '').slice(0, 30),
      imgs: Array.isArray(imgs) ? imgs.length : 0,
      au: {
        follower: num(a.follower_count),
        total_fav: num(a.total_favorited),
        aweme_count: num(a.aweme_count),
        ip: String(a.ip_location || '').slice(0, 20),
        verify: String(a.custom_verify || a.enterprise_verify_reason || '').slice(0, 30),
        signature: String(a.signature || '').slice(0, 80)
      }
    };
    return m;
  }

  function walk(node, depth, out, budget) {
    if (!node || typeof node !== 'object' || depth > 6 || budget.n > MAX_NODES) return;
    budget.n++;
    if (Array.isArray(node)) {
      for (let i = 0; i < node.length && i < 60; i++) walk(node[i], depth + 1, out, budget);
      return;
    }
    const a = node.author || node.authorInfo || node.author_info;
    const id = node.aweme_id || node.awemeId || node.awemeIdStr || node.group_id;
    if (a && typeof a === 'object' && id && (a.sec_uid || a.secUid || a.uid)) {
      out.push({
        aweme_id: String(id),
        sec_uid: a.sec_uid || a.secUid || '',
        uid: String(a.uid || a.uid_str || ''),
        nickname: a.nickname || a.nickName || '',
        unique_id: a.unique_id || a.short_id || '',
        desc: String(node.desc || '').slice(0, 120),
        meta: metaOf(node, a)
      });
      return;
    }
    const keys = Object.keys(node);
    for (let i = 0; i < keys.length && i < 40; i++) {
      const k = keys[i];
      if (k === 'author' || k === 'stats' || k === 'statistics') continue;
      const v = node[k];
      if (v && typeof v === 'object') walk(v, depth + 1, out, budget);
    }
  }

  function handleJson(text, url) {
    if (!text || typeof text !== 'string' || text.length < 40 || text.length > 6e6) return;
    if (text.charCodeAt(0) !== 123 && text.charCodeAt(0) !== 91) return;
    let json;
    try { json = JSON.parse(text); } catch (e) { return; }
    const out = [];
    walk(json, 0, out, { n: 0 });
    for (const it of out) {
      if (!it.sec_uid && !it.uid) continue;
      const key = it.aweme_id + '|' + (it.sec_uid || it.uid);
      if (seen.has(key)) continue;
      if (seen.size > 800) seen.clear();
      seen.add(key);
      it.src = 'net';
      it.t = Date.now();
      post(it);
    }
  }

  const interesting = (u) => typeof u === 'string' && u.indexOf('aweme') >= 0;

  // ---- fetch ----
  try {
    const origFetch = window.fetch;
    if (typeof origFetch === 'function') {
      window.fetch = function () {
        const p = origFetch.apply(this, arguments);
        try {
          const a0 = arguments[0];
          const url = typeof a0 === 'string' ? a0 : (a0 && a0.url) || '';
          if (interesting(url)) {
            p.then((res) => {
              try {
                const ct = (res.headers && res.headers.get('content-type')) || '';
                if (/json|text\/plain/.test(ct) || interesting(url)) {
                  res.clone().text().then((t) => handleJson(t, url)).catch(() => {});
                }
              } catch (e) { /* ignore */ }
            }).catch(() => {});
          }
        } catch (e) { /* ignore */ }
        return p;
      };
    }
  } catch (e) { /* ignore */ }

  // ---- XHR ----
  try {
    const XP = XMLHttpRequest.prototype;
    const origOpen = XP.open;
    const origSend = XP.send;
    XP.open = function (m, u) {
      try { this.__dyUrl = u; } catch (e) { /* ignore */ }
      return origOpen.apply(this, arguments);
    };
    XP.send = function () {
      try {
        if (interesting(this.__dyUrl)) {
          this.addEventListener('load', () => {
            try {
              if (this.responseType === 'json' && this.response) {
                handleJson(JSON.stringify(this.response), this.__dyUrl);
              } else if (!this.responseType || this.responseType === 'text') {
                handleJson(this.responseText, this.__dyUrl);
              }
            } catch (e) { /* ignore */ }
          });
        }
      } catch (e) { /* ignore */ }
      return origSend.apply(this, arguments);
    };
  } catch (e) { /* ignore */ }

  // ---- 兜底：页面内嵌的路由数据（用户主页常见）----
  function routerUser() {
    try {
      const rd = window._ROUTER_DATA || window.__INITIAL_STATE__ || null;
      if (!rd) return null;
      const out = [];
      (function scan(n, d) {
        if (!n || typeof n !== 'object' || d > 5 || out.length > 3) return;
        if (Array.isArray(n)) { for (let i = 0; i < n.length && i < 30; i++) scan(n[i], d + 1); return; }
        const sec = n.sec_uid || n.secUid;
        const nick = n.nickname || n.nickName;
        if (sec && String(sec).indexOf('MS4wLjAB') === 0 && nick) {
          out.push({ sec_uid: String(sec), uid: String(n.uid || n.uid_str || ''), nickname: String(nick) });
          return;
        }
        const keys = Object.keys(n);
        for (let i = 0; i < keys.length && i < 30; i++) {
          const v = n[keys[i]];
          if (v && typeof v === 'object') scan(v, d + 1);
        }
      })(rd, 0);
      return out[0] || null;
    } catch (e) { return null; }
  }

  window.addEventListener('message', (ev) => {
    try {
      if (ev.source !== window || !ev.data || ev.data.__dyLocalAsk !== 1) return;
      const u = routerUser();
      if (u) post(Object.assign({ src: 'router', t: Date.now(), aweme_id: '' }, u));
    } catch (e) { /* ignore */ }
  });
})();
