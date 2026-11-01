/* 内容脚本（隔离世界）
 * 职责：识别当前视频作者 → 让后台查本地库 → 在页面上画一张小卡片（可选加一个昵称角标）
 * 设计原则：不修改抖音自身 DOM 结构（只做「额外插入一个兄弟节点」），不阻塞页面，
 *          识别不到就安静待着，绝不影响观看。
 */
(() => {
  'use strict';
  if (window.__dyLocalBadgeLoaded) return;
  window.__dyLocalBadgeLoaded = true;

  const DEFAULTS = {
    enabled: true, port: 8790, ttlMs: 60000,
    showPanel: true, showPill: true, collapsed: false, pos: null, debug: false
  };
  let S = Object.assign({}, DEFAULTS);

  const st = {
    key: '', ctx: null, data: null, error: null,
    loading: false, seq: 0, missStreak: 0, retryAt: 0,
    toast: '', toastErr: false, toastTimer: 0
  };
  const hookMap = new Map();
  let hookLatest = null, hookAt = 0, hookRouter = null;
  let host = null, shadow = null, pill = null, pillSec = '';
  let timer = null, mo = null, lastTick = 0, mutGuard = 0;

  // ------------------------------------------------------------------ 工具
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const pad = (n) => String(n).padStart(2, '0');

  function fmtFull(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
      pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function fmtShort(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    const md = pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
      pad(d.getHours()) + ':' + pad(d.getMinutes());
    // 不是今年就把年份写出来，否则 25-12-29 和 26-12-29 看起来一模一样
    return d.getFullYear() === new Date().getFullYear()
      ? md : String(d.getFullYear()).slice(2) + '-' + md;
  }
  function rel(ts) {
    if (!ts) return '';
    const s = Date.now() / 1000 - ts;
    if (s < 0) return '刚刚';
    if (s < 60) return '刚刚';
    if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
    if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
    if (s < 86400 * 30) return Math.floor(s / 86400) + ' 天前';
    if (s < 86400 * 365) return Math.floor(s / 86400 / 30) + ' 个月前';
    return Math.floor(s / 86400 / 365) + ' 年前';
  }
  function fmtBytes(b) {
    if (!b) return '—';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0, n = b;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (n >= 100 ? n.toFixed(0) : n.toFixed(1)) + ' ' + u[i];
  }
  function fmtCount(n) {
    if (!n) return '0';
    if (n >= 1e8) return (n / 1e8).toFixed(1).replace(/\.0$/, '') + ' 亿';
    if (n >= 1e4) return (n / 1e4).toFixed(1).replace(/\.0$/, '') + ' 万';
    return String(n);
  }
  function fmtDur(ms) {
    if (!ms) return '';
    const s = Math.round(ms / 1000);
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }
  function fmtFullTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function daysAgo(ts) {
    if (!ts) return 0;
    return Math.floor((Date.now() / 1000 - ts) / 86400);
  }

  // -------------------------------------------------------------- 作者识别
  function activeVideoEl() {
    const cx = innerWidth / 2, cy = innerHeight / 2;
    let best = null, bestScore = -Infinity;
    document.querySelectorAll('video').forEach((v) => {
      const r = v.getBoundingClientRect();
      if (r.width < 120 || r.height < 120) return;
      const ix = Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0));
      const iy = Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
      const area = ix * iy;
      if (area < 20000) return;
      const d = Math.hypot(r.left + r.width / 2 - cx, r.top + r.height / 2 - cy);
      const centered = (r.left <= cx && r.right >= cx && r.top <= cy && r.bottom >= cy) ? 1 : 0;
      const score = area / 1000 - d * 3 + centered * 2000 + (v.paused ? 0 : 800);
      if (score > bestScore) { bestScore = score; best = v; }
    });
    return best;
  }

  const BAD_TEXT = /^(关注|已关注|互相关注|粉丝|获赞|作品|私信|举报)$/;

  function anchorSec(a) {
    const href = a.getAttribute('href') || '';
    let m = href.match(/\/user\/(MS4wLjAB[A-Za-z0-9_-]+)/);
    if (!m) m = href.match(/sec_uid=(MS4wLjAB[A-Za-z0-9_-]+)/);
    return m ? m[1] : '';
  }

  function visibleAnchors() {
    const out = [];
    document.querySelectorAll('a[href*="MS4wLjAB"]').forEach((a) => {
      const sec = anchorSec(a);
      if (!sec) return;
      const r = a.getBoundingClientRect();
      if (r.width < 4 || r.height < 4) return;
      if (r.bottom < -40 || r.top > innerHeight + 40) return;
      let txt = (a.innerText || a.textContent || '').trim().replace(/\s+/g, ' ');
      if (txt.length > 40) txt = txt.slice(0, 40);
      out.push({ el: a, sec_uid: sec, text: BAD_TEXT.test(txt) ? '' : txt, r });
    });
    return out;
  }

  function pickAnchorNear(vidEl, secHint) {
    const list = visibleAnchors();
    if (!list.length) return null;
    if (secHint) {
      const same = list.find((x) => x.sec_uid === secHint);
      if (same) return same;
    }
    if (!vidEl) return list[0];
    const vr = vidEl.getBoundingClientRect();
    const cx = vr.left + vr.width / 2, cy = vr.top + vr.height / 2;
    let best = null, bestD = Infinity;
    for (const it of list) {
      const d = Math.hypot(it.r.left + it.r.width / 2 - cx, it.r.top + it.r.height / 2 - cy);
      if (d < bestD) { bestD = d; best = it; }
    }
    return best;
  }

  /* 有些页面（短剧/合集/新版详情）作者名不是 <a href="/user/...">，而是纯文本节点。
     按抖音自己的 data-e2e 标记取，取不到就返回空，绝不去猜。 */
  function textAuthorName() {
    const sels = ['[data-e2e="video-author-name"]', '[data-e2e="user-name"]',
      '[data-e2e="author-name"]', '[data-e2e="douyin-account"]',
      '[data-e2e="detail-video-author"]',
      '[data-e2e*="author"]', '[data-e2e*="user-name"]'];
    try {
      for (let i = 0; i < sels.length; i++) {
        const el = document.querySelector(sels[i]);
        if (!el) continue;
        let t = String(el.innerText || el.textContent || '').trim().split('\n')[0].trim();
        t = t.replace(/^@/, '').trim();
        if (t && t.length <= 30) return t;
      }
    } catch (e) { /* ignore */ }
    return '';
  }

  /* 页面 HTML 里的 hydration 数据：抖音是 SSR + 客户端接管，
     直接打开视频链接时那些接口请求发生在 page-hook 装上之前，抓不到。
     这里在「包含当前作品 ID」的那段 <script> 里就地找 author，比较准。 */
  const psCache = new Map();
  let psLastTry = 0;

  function fromPageScripts(awemeId) {
    if (!awemeId) return null;
    const hit = psCache.get(awemeId);
    if (hit) return hit;
    const now = Date.now();
    if (now - psLastTry < 2500) return null;   // 别每 450ms 扫一遍
    psLastTry = now;
    try {
      const list = document.querySelectorAll('script');
      for (let i = 0; i < list.length && i < 80; i++) {
        const t = list[i].textContent || '';
        if (t.length < 400 || t.length > 4000000) continue;
        const at = t.indexOf(awemeId);
        if (at < 0) continue;
        // author 通常就在 aweme 对象里、紧挨着 aweme_id，先往后找再往前找
        const segs = [t.slice(at, at + 3000), t.slice(Math.max(0, at - 3000), at)];
        for (let k = 0; k < segs.length; k++) {
          // 兼容两种形态：正常 JSON 和「被转义过的 JSON 字符串」
          const sm = segs[k].match(/sec_uid\\?"\s*:\s*\\?"(MS4wLjAB[A-Za-z0-9_-]+)/);
          if (!sm) continue;
          const nm = segs[k].match(/nickname\\?"\s*:\s*\\?"([^"\\]{1,40})/);
          const res = { sec_uid: sm[1], nickname: nm ? nm[1] : '' };
          psCache.set(awemeId, res);
          if (psCache.size > 50) psCache.delete(psCache.keys().next().value);
          return res;
        }
      }
    } catch (e) { /* ignore */ }
    return null;
  }

  /* hookLatest 只是「最近一次接口响应」，可能是你上一个视频的。
     拿它之前先在页面上确认这个名字真的出现了，否则宁可不认，绝不能张冠李戴。 */
  let nameChkKey = '', nameChkAt = 0, nameChkRes = false;

  function nameOnPage(nick) {
    const n = String(nick || '').trim();
    if (n.length < 2) return false;
    const now = Date.now();
    if (n === nameChkKey && now - nameChkAt < 3000) return nameChkRes;
    let ok = false;
    try {
      const b = document.body;
      const t = (b && (b.innerText || b.textContent)) || '';
      ok = t.indexOf(n) >= 0;
    } catch (e) { /* ignore */ }
    nameChkKey = n;
    nameChkAt = now;
    nameChkRes = ok;
    return ok;
  }

  /* 只有「确认是同一个作品」时才把 meta 带下去。
     * 页面上没拿到作品 ID 时，hook 里那份是「最近一次接口响应」，
   * 可能来自别的视频（甚至几个月前的）——拿它的发布日期去匹配，
   * 会把一小时前才发的视频说成「已存」。所以只保留作者级信息。 */
  function pickMeta(hookMeta, awemeId, secOk) {
    if (!hookMeta) return null;
    const mid = String(hookMeta.aweme_id || '');
    if (awemeId && mid && mid === String(awemeId)) return hookMeta;
    return (secOk && hookMeta.au) ? { au: hookMeta.au } : null;
  }

  function detect() {
    const path = location.pathname;
    const href = location.href;
    // 抖音大量场景是 /?modal_id=7xxxxxxxx 或 /discover?modal_id=...，视频 ID 只在 query 里
    const awemeId = (path.match(/\/(?:video|note)\/(\d{15,25})/) || [])[1]
      || (href.match(/[?&#](?:modal_id|aweme_id|vid)=(\d{15,25})/) || [])[1]
      || '';
    const profM = path.match(/^\/user\/(MS4wLjAB[A-Za-z0-9_-]+)/);
    const hook = awemeId ? (hookMap.get(awemeId) || null)
      : (hookLatest && Date.now() - hookAt < 180000 ? hookLatest : null);

    if (profM) {
      const sec = profM[1];
      let nick = '';
      if (hookRouter && hookRouter.path === path && hookRouter.user &&
          hookRouter.user.sec_uid === sec) nick = hookRouter.user.nickname;
      if (!nick && hook && hook.sec_uid === sec) nick = hook.nickname;
      return { sec_uid: sec, uid: '', nickname: nick, aweme_id: '', profile: true, src: 'url',
        meta: pickMeta(hook && hook.meta, '', !!(hook && hook.sec_uid === sec)) };
    }

    const vid = activeVideoEl();
    const hint = hook && hook.sec_uid ? hook.sec_uid : '';
    const anc = pickAnchorNear(vid, hint);

    if (hook && awemeId && hook.aweme_id === awemeId) {
      return { sec_uid: hook.sec_uid, uid: hook.uid || '', nickname: hook.nickname || '',
        aweme_id: awemeId, src: 'net', meta: hook.meta || null };
    }
    if (anc) {
      return { sec_uid: anc.sec_uid, uid: '',
        nickname: (hook && hook.sec_uid === anc.sec_uid && hook.nickname) || anc.text || '',
        aweme_id: awemeId, src: 'dom',
        meta: pickMeta(hook && hook.meta, awemeId, !!(hook && hook.sec_uid === anc.sec_uid)) };
    }
    // 有 awemeId 时 hook 是按 ID 精确取的，可信；
    // 没有 awemeId 时 hook 只是「最近一次响应」，必须先在页面上看到这个名字才敢用
    if (hook && (awemeId || nameOnPage(hook.nickname))) {
      return { sec_uid: hook.sec_uid, uid: hook.uid || '', nickname: hook.nickname || '',
        aweme_id: awemeId || hook.aweme_id || '', src: 'net',
        meta: pickMeta(hook.meta, awemeId || hook.aweme_id || '', true) };
    }
    // 页面 hydration 数据（直接打开视频链接时最管用）
    const ps = fromPageScripts(awemeId);
    if (ps) {
      return { sec_uid: ps.sec_uid, uid: '', nickname: ps.nickname || '',
        aweme_id: awemeId, src: 'page' };
    }
    // 最后的兜底：只有作品 ID（后端能反查作者）或只有页面上的作者名（按昵称精确匹配）
    const tn = textAuthorName();
    if (awemeId || tn) {
      return { sec_uid: '', uid: '', nickname: tn || '', aweme_id: awemeId,
        src: tn ? 'dom-name' : 'url' };
    }
    return null;
  }

  // ---------------------------------------------------------------- 查询
  function sendToBg(msg) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(msg, (res) => {
          if (chrome.runtime.lastError) { resolve({ ok: false, code: 'context', message: chrome.runtime.lastError.message }); return; }
          resolve(res || { ok: false, code: 'empty', message: '后台无响应' });
        });
      } catch (e) {
        resolve({ ok: false, code: 'context', message: String(e && e.message || e) });
      }
    });
  }

  async function lookup(ctx, force) {
    const mySeq = ++st.seq;
    st.loading = true;
    st.error = null;
    if (force) { st.data = null; }
    render();

    const query = {
      sec_uid: ctx.sec_uid || '', uid: ctx.uid || '',
      nickname: ctx.nickname || '', aweme_id: ctx.aweme_id || ''
    };
    const res = await sendToBg({ type: 'lookup', query, meta: ctx.meta || null, force: !!force });
    if (mySeq !== st.seq) return; // 已经切到别的作者，丢弃旧结果
    st.loading = false;

    if (res.ok) {
      st.data = res.data;
      st.missStreak = 0;
      st.retryAt = 0;
      if (res.data.status === 'hit') ensurePill();
      else removePill();
    } else {
      st.data = null;
      st.error = res;
      removePill();
      if (res.code === 'unreachable' || res.code === 'timeout') {
        st.missStreak++;
        st.retryAt = Date.now() + Math.min(15000 * st.missStreak, 60000);
      }
    }
    render();
  }

  // ---------------------------------------------------------------- 渲染
  function ensureHost() {
    if (host && host.isConnected) return;
    host = document.createElement('div');
    host.id = 'dy-local-author-host';
    host.style.cssText = 'all:initial;position:fixed;left:0;top:0;width:0;height:0;z-index:2147483000;pointer-events:none;';
    shadow = host.attachShadow({ mode: 'open' });
    shadow.innerHTML = [
      '<style>',
      '*{box-sizing:border-box;margin:0;padding:0}',
      '.wrap{position:fixed;right:16px;top:104px;width:276px;pointer-events:auto;',
      ' font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;',
      ' font-size:12px;line-height:1.5;color:#e9ecf1}',
      '.card{background:rgba(22,24,30,.93);border:1px solid rgba(255,255,255,.10);border-radius:12px;',
      ' box-shadow:0 10px 30px rgba(0,0,0,.4);overflow:hidden;backdrop-filter:blur(10px)}',
      '.hd{display:flex;align-items:center;gap:6px;padding:7px 8px 7px 11px;cursor:grab;',
      ' background:rgba(255,255,255,.035);user-select:none}',
      '.hd:active{cursor:grabbing}',
      '.dot{width:7px;height:7px;border-radius:50%;background:#4b5261;flex:none}',
      '.dot.ok{background:#2ecc71;box-shadow:0 0 7px rgba(46,204,113,.75)}',
      '.dot.warn{background:#f5a623}.dot.err{background:#ef5350}',
      '.ttl{font-size:12px;font-weight:650;color:#d5dae3;letter-spacing:.2px}',
      '.sp{flex:1}',
      'button{all:unset;cursor:pointer;width:20px;height:20px;display:flex;align-items:center;',
      ' justify-content:center;border-radius:5px;color:#98a0b0;font-size:13px;line-height:1}',
      'button:hover{background:rgba(255,255,255,.1);color:#fff}',
      '.bd{padding:4px 0 0}',
      '.who{display:flex;align-items:center;gap:5px;padding:2px 11px 6px}',
      '.nick{font-size:14px;font-weight:650;color:#fff;max-width:168px;white-space:nowrap;',
      ' overflow:hidden;text-overflow:ellipsis}',
      '.did{font-size:11px;color:#8b93a3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:96px}',
      '.tag{font-size:10px;padding:0 5px;border-radius:5px;background:rgba(255,255,255,.1);color:#b9c0cc;flex:none}',
      '.grid{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;padding:0 11px 9px}',
      '.k{color:#8b93a3;font-size:11px}',
      '.v{color:#e9ecf1;font-size:12px;font-variant-numeric:tabular-nums}',
      '.v b{color:#ffd479;font-weight:650}.v i{font-style:normal;color:#7f8798;font-size:10.5px;margin-left:4px}',
      '.list{border-top:1px solid rgba(255,255,255,.07);padding:7px 11px 8px;display:flex;flex-direction:column;gap:4px}',
      '.lt{color:#6f7787;font-size:10px;margin-bottom:1px}',
      '.item{display:flex;gap:6px;font-size:11px;color:#b9c0cc;min-width:0}',
      '.item .d{color:#ffd479;flex:none;font-variant-numeric:tabular-nums}',
      '.item .n{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}',
      '.vid{margin:0 11px 9px;padding:5px 8px;border-radius:7px;font-size:11px;display:flex;gap:5px;align-items:center}',
      '.vid.ok{background:rgba(46,204,113,.13);color:#83e0aa;border:1px solid rgba(46,204,113,.26)}',
      '.vid.no{background:rgba(255,255,255,.045);color:#98a0b0;border:1px solid rgba(255,255,255,.07)}',
      '.ft{padding:6px 11px 8px;color:#69707f;font-size:10px;border-top:1px solid rgba(255,255,255,.07);',
      ' display:flex;align-items:center;gap:6px}',
      '.ft .sp{flex:1}',
      '.msg{padding:12px 12px 13px;font-size:11.5px;line-height:1.65;color:#c3c9d4}',
      '.msg b{color:#fff;font-weight:600}',
      '.msg .sub{color:#868e9d;font-size:10.5px;margin-top:4px}',
      '.acts{display:flex;gap:6px;margin-top:9px;flex-wrap:wrap}',
      '.acts button{all:unset;cursor:pointer;font-size:11px;padding:3px 9px;border-radius:6px;',
      ' background:rgba(255,255,255,.09);color:#dfe3ea}',
      '.acts button:hover{background:rgba(255,255,255,.17)}',
      '.sk{padding:11px 12px 13px;color:#8b93a3;font-size:11.5px}',
      '.bar{height:3px;border-radius:2px;background:rgba(255,255,255,.1);overflow:hidden;margin-top:7px}',
      '.bar i{display:block;height:100%;width:40%;background:linear-gradient(90deg,transparent,#ffd479,transparent);',
      ' animation:mv 1.1s linear infinite}',
      '@keyframes mv{from{transform:translateX(-100%)}to{transform:translateX(320%)}}',
      '.mini{width:38px;height:38px;border-radius:19px;background:rgba(22,24,30,.94);',
      ' border:1px solid rgba(255,255,255,.12);box-shadow:0 6px 18px rgba(0,0,0,.4);',
      ' display:flex;flex-direction:column;align-items:center;justify-content:center;cursor:pointer;color:#ffd479}',
      '.mini b{font-size:11px;font-weight:700;line-height:1.1}',
      '.mini span{font-size:8px;color:#8b93a3;letter-spacing:.5px}',
      /* ---- 横栏：收起态 / 未收录态，同高 34px ---- */
      '.hbar{display:flex;align-items:center;gap:6px;height:34px;padding:0 8px 0 11px;',
      ' border-radius:10px;background:rgba(22,24,30,.94);border:1px solid rgba(255,255,255,.10);',
      ' box-shadow:0 8px 22px rgba(0,0,0,.38);backdrop-filter:blur(10px);',
      ' max-width:min(760px,calc(100vw - 32px));overflow:hidden;white-space:nowrap;',
      ' font-size:12px;color:#e9ecf1;cursor:grab;user-select:none}',
      '.hbar:active{cursor:grabbing}',
      '.bnick{font-weight:650;color:#fff;max-width:170px;overflow:hidden;text-overflow:ellipsis;flex:none}',
      '.bsep{color:#454b58;flex:none}',
      '.bnum{color:#b9c0cc;flex:none}.bnum b{color:#ffd479;font-weight:650}',
      '.btime{color:#eef1f6;font-weight:600;flex:none;font-variant-numeric:tabular-nums}',
      '.brel{color:#8b93a3;font-size:10.5px;flex:none;font-variant-numeric:tabular-nums}',
      '.bvid{color:#7ee2a8;font-size:11px;flex:none}',
      '.bmiss{color:#c9a06a;flex:none}',
      '.bbtn{all:unset;cursor:pointer;flex:none;font-size:11px;padding:3px 8px;border-radius:6px;',
      ' background:rgba(255,255,255,.09);color:#dfe3ea}',
      '.bbtn:hover{background:rgba(255,255,255,.17)}',
      '.bico{all:unset;cursor:pointer;flex:none;width:20px;height:20px;display:flex;align-items:center;',
      ' justify-content:center;border-radius:5px;color:#98a0b0;font-size:12px;line-height:1}',
      '.bico:hover{background:rgba(255,255,255,.1);color:#fff}',
      '.hbtn{all:unset;cursor:pointer;flex:none;font-size:10px;padding:1px 6px;',
      ' border-radius:5px;background:rgba(255,255,255,.08);color:#a9b1bf}',
      '.hbtn:hover{background:rgba(255,255,255,.16);color:#fff}',
      '.tag.lv{background:rgba(168,132,255,.18);color:#c9b4ff}',
      '.tag.fq{background:rgba(255,212,121,.14);color:#e8c477}',
      '.relv{color:#e9ecf1;font-weight:600}',
      '.tchip.ali{background:rgba(255,255,255,.05);color:#9aa3b2}',
      '.tag.st{background:rgba(46,204,113,.14);color:#83e0aa}',
      '.hbts{margin-left:auto;display:flex;align-items:center;gap:5px;flex:none}',
      '.hbtn.q{background:rgba(46,204,113,.16);color:#8ee6b3;border:1px solid rgba(46,204,113,.28)}',
      '.hbtn.q:hover{background:rgba(46,204,113,.28);color:#fff}',
      '.bbtn.q{background:rgba(46,204,113,.16);color:#8ee6b3;border:1px solid rgba(46,204,113,.28)}',
      '.bbtn.q:hover{background:rgba(46,204,113,.28);color:#fff}',
      '.bbtn.q[disabled]{opacity:.45;cursor:default}',
      '.tagrow{display:flex;flex-wrap:wrap;gap:4px;padding:1px 11px 6px}',
      '.tchip{font-size:10px;padding:1px 6px;border-radius:5px;background:rgba(255,255,255,.08);color:#b9c0cc}',
      /* 第二行：左边标签、右边已下载数量 */
      '.row2{display:flex;align-items:center;gap:6px;padding:2px 11px 7px}',
      '.tagwrap{display:flex;gap:4px;flex:1;min-width:0;overflow:hidden}',
      '.cnt{flex:none;margin-left:auto;font-size:10.5px;color:#8b93a3;white-space:nowrap;',
      ' font-variant-numeric:tabular-nums}',
      '.cnt b{color:#ffd479;font-weight:650;font-size:11.5px;margin:0 1px}',
      '.toast{margin:0 11px 8px;padding:4px 8px;border-radius:6px;font-size:10.5px;',
      ' background:rgba(46,204,113,.14);color:#8ee6b3;border:1px solid rgba(46,204,113,.25)}',
      '.toast.err{background:rgba(239,83,80,.14);color:#ffb4b4;border-color:rgba(239,83,80,.28)}',
      '.asub{padding:0 11px 7px;color:#7f8798;font-size:10.5px;white-space:nowrap;overflow:hidden;',
      ' text-overflow:ellipsis}',
      '.vdivider{display:flex;align-items:center;gap:6px;padding:6px 11px 4px;color:#6f7787;font-size:10px;',
      ' border-top:1px solid rgba(255,255,255,.07)}',
      '.vdivider::after{content:"";flex:1;height:1px;background:rgba(255,255,255,.05)}',
      '.tags{padding:0 11px 7px;color:#8b93a3;font-size:10.5px;white-space:nowrap;overflow:hidden;',
      ' text-overflow:ellipsis}',
      '.lag{margin:0 11px 7px;padding:4px 8px;border-radius:6px;font-size:10.5px;',
      ' background:rgba(255,255,255,.045);color:#98a0b0;border:1px solid rgba(255,255,255,.06)}',
      '.lag.warn{background:rgba(245,166,35,.10);color:#f0c079;border-color:rgba(245,166,35,.22)}',
      '</style>',
      '<div class="wrap"></div>'
    ].join('');
    (document.body || document.documentElement).appendChild(host);
    shadow.addEventListener('click', onClick);
    shadow.addEventListener('mousedown', onDown);
  }

  function wrapEl() { return shadow ? shadow.querySelector('.wrap') : null; }

  function applyPos(el) {
    if (S.pos && typeof S.pos.left === 'number') {
      el.style.left = Math.max(0, Math.min(S.pos.left, innerWidth - 60)) + 'px';
      el.style.top = Math.max(0, Math.min(S.pos.top, innerHeight - 60)) + 'px';
      el.style.right = 'auto';
    } else {
      el.style.left = ''; el.style.top = ''; el.style.right = '16px';
      el.style.top = '104px';
    }
  }

  function statusDot() {
    if (st.loading) return 'warn';
    if (st.error) return 'err';
    if (st.data) {
      if (st.data.status === 'hit') return 'ok';
      return 'warn';
    }
    return '';
  }

  /* 横栏：收起态（命中）与未收录态共用，高度固定 34px，尽量少挡视频 */
  function barHtml() {
    const d = st.data || {};
    const ctx = st.ctx || {};
    const dot = statusDot();
    const hit = d.status === 'hit';
    const a = d.author || {};

    if (!hit) {
      const aa = d.author || {};
      const nm = aa.nickname || ctx.nickname || '';
      const unknown = !(aa.sec_uid || aa.uid || ctx.sec_uid || ctx.uid || ctx.nickname);
      const canPush = !!(aa.sec_uid || aa.uid || ctx.sec_uid || ctx.uid);
      // 悬停可以看到到底走到了哪一步，方便定位为啥认不出
      const diag = '识别来源：' + (ctx.src || '无') +
        (ctx.aweme_id ? ' · 作品ID ' + ctx.aweme_id : ' · 没拿到作品ID') +
        (ctx.nickname ? ' · 昵称 ' + ctx.nickname : '');
      return '<div class="hbar" title="' + esc(diag) + '">' +
        '<span class="dot ' + dot + '"></span>' +
        '<span class="bnick" title="' + esc(nm) + '">' + esc(nm || '未知作者') + '</span>' +
        '<span class="bsep">·</span>' +
        '<span class="bmiss">' + (unknown ? '认不出作者' : '本地库未收录') + '</span>' +
        '<span class="sp"></span>' +
        '<button class="bbtn q" data-act="queue-add"' + (canPush ? '' : ' disabled') +
        ' title="推送到待下载区（库里没有会自动新建作者主页）">＋待下载</button>' +
        '<button class="bbtn" data-act="retry">再查</button>' +
        '<button class="bbtn" data-act="openlib">本地库</button>' +
        '</div>';
    }

    const vid = (d.video && d.video.downloaded) ? '<span class="bvid">✔ 本视频已存</span>' : '';
    return '<div class="hbar" data-act="expand" title="点击展开详情 · 可拖动">' +
      '<span class="dot ' + dot + '"></span>' +
      '<span class="bnick" title="' + esc(a.nickname) + '">' + esc(a.nickname || '（未填昵称）') + '</span>' +
      '<span class="bsep">·</span>' +
      '<span class="bnum">已存 <b>' + (a.file_count || 0) + '</b></span>' +
      '<span class="bsep">·</span>' +
      '<span class="btime">最后下载 ' + (a.last_download ? rel(a.last_download) : '—') + '</span>' +
      (a.last_download ? '<span class="brel">' + fmtShort(a.last_download) + '</span>' : '') +
      vid +
      '<span class="sp"></span>' +
      '<button class="bbtn q" data-act="queue-add" title="推送到管理后台的待下载区">＋下载</button>' +
      '<button class="bbtn" data-act="open-author" title="打开本地库里的这位作者">主页 ↗</button>' +
      '<button class="bico" data-act="refresh" title="重新查询（跳过缓存）">↻</button>' +
      '<button class="bico" data-act="expand" title="展开详情">⌄</button>' +
      '</div>';
  }

  function cardHtml() {
    const d = st.data;
    const ctx = st.ctx || {};
    const dot = statusDot();

    const head = '<div class="hd"><span class="dot ' + dot + '"></span>' +
      '<span class="ttl">本地库</span><span class="sp"></span>' +
      '<button data-act="refresh" title="重新查询（跳过缓存）">↻</button>' +
      (d ? '<button data-act="collapse" title="收起成横栏">–</button>' : '') + '</div>';

    let body = '';

    if (st.loading && !d) {
      body = '<div class="sk">正在查本地库…<div class="bar"><i></i></div></div>';
    } else if (st.error) {
      const unreachable = st.error.code === 'unreachable' || st.error.code === 'timeout';
      body = '<div class="msg">' +
        (unreachable
          ? '<b>本地服务没有运行</b><div class="sub">双击「start-all.bat」或「local-service\\start-service.bat」后会自动恢复。</div>'
          : '<b>查询失败</b><div class="sub">' + esc(st.error.message || '未知错误') + '</div>') +
        '<div class="acts"><button data-act="retry">重试</button>' +
        (unreachable ? '<button data-act="howto">怎么启动？</button>' : '') +
        '</div></div>';
    } else if (!d) {
      body = '<div class="sk">识别中…<div class="bar"><i></i></div></div>';
    } else if (d.status === 'hit') {
      const a = d.author || {};
      const m = ctx.meta || null;
      const au = (m && m.au) || {};
      // 更新频率：后台存 day / 3天 / 周 / 月，这里翻译成一眼能懂的短标
      const fqText = (function (v) {
        const t = String(v || '').trim();
        if (!t) return '';
        if (t === 'day') return '日更';
        if (/更$/.test(t)) return t;
        return t + '更';
      })(a.update_freq);
      const badges = (a.level ? '<span class="tag lv">' + esc(a.level) + '</span>' : '') +
        (a.status ? '<span class="tag st">' + esc(a.status) + '</span>' : '') +
        (fqText ? '<span class="tag fq" title="更新频率">' + esc(fqText) + '</span>' : '') +
        (a.starred ? '<span class="tag">★</span>' : '');
      const allChips = (a.tags || []).slice(0, 6).map(function (t) {
        return '<span class="tchip">' + esc(t) + '</span>';
      }).join('') + (a.alias ? '<span class="tchip ali" title="别名">又名 ' + esc(a.alias) + '</span>' : '');
      const tagRow = '<div class="row2">' +
        '<div class="tagwrap" title="' +
          esc([a.level, a.status, fqText, a.starred ? '星标' : ''].filter(Boolean).join(' · ') +
              ((a.tags && a.tags.length) ? '　标签：' + a.tags.join(' ') : '') +
              (a.alias ? '　又名 ' + a.alias : '')) + '">' +
        badges + allChips +
        '</div>' +
        '<span class="cnt" title="已下载 ' + (a.file_count || 0) + ' 个作品' +
        (a.bytes ? '，共 ' + fmtBytes(a.bytes) : '') + '">' +
        '<b>' + (a.file_count || 0) + '</b> 个' +
        (a.bytes ? ' · ' + fmtBytes(a.bytes) : '') + '</span>' +
        '</div>';

      // 作者侧的抖音实时数据（跟着同一次接口响应一起来）
      const subBits = [];
      if (au.follower) subBits.push('粉丝 ' + fmtCount(au.follower));
      if (au.aweme_count) subBits.push('抖音作品 ' + fmtCount(au.aweme_count));
      if (au.ip) subBits.push('IP ' + au.ip);
      if (au.verify) subBits.push(au.verify);
      const aSub = subBits.length
        ? '<div class="asub" title="' + esc(au.signature || '') + '">' + esc(subBits.join(' · ')) + '</div>' : '';

      // 数量/体积已经挪到第二行右侧，这里只留「最后下载」
      let rows = '';
      rows += '<div class="k">最后下载</div><div class="v"><span class="relv">' +
        rel(a.last_download) + '</span><i>' + fmtShort(a.last_download) + '</i></div>';

      // 左边是**作品发布日**（后端已按发布日倒序），右边是标题（去掉日期前缀和扩展名）
      const list = (a.recent || []).slice(0, 3).map(function (w) {
        const dd = (w.date || '').slice(5) || fmtShort(w.mtime);
        const t = String(w.name || '')
          .replace(/^\d{4}-\d{2}-\d{2}-/, '')
          .replace(/\.[A-Za-z0-9]+$/, '');
        return '<div class="item"><span class="d">' + esc(dd) + '</span>' +
          '<span class="n" title="' + esc(w.name) + '">' + esc(t) + '</span></div>';
      }).join('');

      let vline = '';
      if (d.video && d.video.aweme_id) {
        vline = d.video.downloaded
          ? '<div class="vid ok" title="' + esc(d.video.name || '') + '">✔ 本视频已存 · ' +
            fmtShort(d.video.mtime) +
            (d.video.how === 'desc' ? '（按文案匹配）' : '') + '</div>'
          : '<div class="vid no">· 本视频未在库中（作者其他作品已存）</div>';
      }

      // ---- 落后几天：抖音最新发布 vs 本地最新一条作品的日期 ----
      let lag = '';
      if (d.remote && d.remote.pub) {
        const dates = (a.recent || []).map(function (w) {
          const mm = String(w.name || '').match(/^(\d{4}-\d{2}-\d{2})/);
          return mm ? mm[1] : '';
        }).filter(Boolean).sort();
        const localLatest = dates.length ? dates[dates.length - 1] : '';
        if (localLatest) {
          const dd = Math.round((Date.parse(d.remote.pub) - Date.parse(localLatest)) / 86400000);
          lag = dd > 0
            ? '<div class="lag warn">抖音最新 ' + esc(d.remote.pub) + ' · 本地最新 ' + esc(localLatest) +
              ' · 落后 ' + dd + ' 天' + (d.remote.crawl ? '（' + esc(d.remote.crawl) + ' 查）' : '') + '</div>'
            : '<div class="lag">抖音最新 ' + esc(d.remote.pub) + ' · 本地已跟上' +
              (d.remote.crawl ? '（' + esc(d.remote.crawl) + ' 查）' : '') + '</div>';
        }
      }

      const toast = st.toast ? '<div class="toast' + (st.toastErr ? ' err' : '') + '">' +
        esc(st.toast) + '</div>' : '';
      const amb = d.matched_by === 'nickname_ambiguous'
        ? '<div class="vid no">· 昵称匹配到 ' + (d.candidates || []).length + ' 位同名，展示的是库存最多的一位</div>' : '';

      body = '<div class="who"><span class="nick" title="' + esc(a.nickname) + '">' +
        esc(a.nickname || '（未填昵称）') + '</span>' +
        '<span class="hbts">' +
        '<button class="hbtn q" data-act="queue-add" title="推送到管理后台的待下载区">＋待下载</button>' +
        '<button class="hbtn" data-act="open-author" title="打开本地库里的这位作者">主页 ↗</button>' +
        '</span></div>' +
        tagRow + aSub +
        '<div class="grid">' + rows + '</div>' +
        toast + lag + vline + amb +
        (list ? '<div class="list">' + list + '</div>' : '') +
        '<div class="ft"><span>数据更新 ' + esc((d.meta && d.meta.scanned_at || '').slice(5, 16)) +
        '</span><span class="sp"></span><span>仅本机读取</span></div>';
    } else {
      const nm = (d.author && d.author.nickname) || ctx.nickname || '';
      body = '<div class="msg"><b>本地库未收录</b>' +
        (nm ? '<div class="sub">作者：' + esc(nm) + '</div>' : '') +
        '<div class="sub">该作者还没有下载记录（或已改名且未同步）。</div>' +
        '<div class="acts"><button data-act="retry">再查一次</button>' +
        '<button data-act="openlib">打开本地库</button></div></div>';
    }

    return '<div class="card">' + head + '<div class="bd">' + body + '</div></div>';
  }

  /* 未收录 → 横栏；命中且已收起 → 横栏；其余 → 卡片 */
  function buildHtml() {
    const d = st.data;
    if (d && d.status !== 'hit') return { bar: true, html: barHtml() };
    if (d && d.status === 'hit' && S.collapsed) return { bar: true, html: barHtml() };
    return { bar: false, html: cardHtml() };
  }

  function render() {
    if (!S.enabled) { teardown(); return; }
    ensureHost();
    const w = wrapEl();
    if (!w) return;
    if (!S.showPanel) { w.innerHTML = ''; w.style.display = 'none'; return; }
    const out = buildHtml();
    w.style.display = '';
    w.style.width = out.bar ? 'auto' : '276px';
    w.innerHTML = out.html;
    applyPos(w);
  }

  function clearPanel() {
    const w = wrapEl();
    if (w) { w.innerHTML = ''; w.style.display = 'none'; }
    removePill();
  }

  // ------------------------------------------------------------ 昵称角标
  function findAnchorBySec(sec) {
    if (!sec) return null;
    const list = visibleAnchors().filter((x) => x.sec_uid === sec);
    if (!list.length) return null;
    const v = activeVideoEl();
    if (!v) return list[0].el;
    const vr = v.getBoundingClientRect();
    const cx = vr.left + vr.width / 2, cy = vr.top + vr.height / 2;
    list.sort((a, b) => {
      const da = Math.hypot(a.r.left - cx, a.r.top - cy);
      const db = Math.hypot(b.r.left - cx, b.r.top - cy);
      return da - db;
    });
    return list[0].el;
  }

  /* 在作者头像右上角找位置：往上找 4 层，挑一个「方的、圆角」的小 img */
  function findAvatar(anchor) {
    const imgs = [];
    let node = anchor;
    for (let up = 0; up < 4 && node; up++) {
      if (node.querySelectorAll) {
        node.querySelectorAll('img').forEach(function (i) { imgs.push(i); });
      }
      node = node.parentElement;
    }
    for (let i = 0; i < imgs.length; i++) {
      const img = imgs[i];
      const r = img.getBoundingClientRect();
      if (r.width < 22 || r.width > 96) continue;
      if (Math.abs(r.width - r.height) > 10) continue;
      let cs = null;
      try { cs = getComputedStyle(img); } catch (e) { continue; }
      const rad = parseFloat(cs.borderRadius) || 0;
      let prad = 0;
      try { prad = parseFloat(getComputedStyle(img.parentElement).borderRadius) || 0; } catch (e) { }
      if (rad >= r.width * 0.3 || prad >= r.width * 0.3) return img;
    }
    return null;
  }

  function ensurePill() {
    if (!S.showPill || !st.data || st.data.status !== 'hit') { removePill(); return; }
    const a = st.data.author || {};
    const sec = a.sec_uid || (st.ctx && st.ctx.sec_uid) || '';
    if (!sec) return;
    if (pill && pill.isConnected && pillSec === sec) return;
    removePill();
    const anchor = findAnchorBySec(sec);
    if (!anchor || !anchor.parentElement) return;
    const n = a.file_count || 0;
    const label = n >= 1000 ? (Math.round(n / 100) / 10) + 'k' : String(n);

    // ① 首选：头像右上角一个小圆点
    const img = findAvatar(anchor);
    if (img && img.parentElement) {
      const host = img.parentElement;
      let clip = false;
      try {
        const hcs = getComputedStyle(host);
        clip = hcs.overflow === 'hidden';
        if (hcs.position === 'static') host.style.position = 'relative';
      } catch (e) { /* ignore */ }
      const dot = document.createElement('span');
      dot.textContent = label;
      dot.title = '本地已存 ' + n + ' 个作品';
      dot.style.cssText = 'position:absolute;' + (clip ? 'top:0;right:0;' : 'top:-2px;right:-2px;') +
        'z-index:9;min-width:17px;height:17px;padding:0 3px;border-radius:9px;' +
        'background:rgba(20,22,28,.95);border:1px solid rgba(255,212,121,.9);color:#ffd479;' +
        'font-size:10px;font-weight:700;line-height:15px;text-align:center;box-sizing:border-box;' +
        'pointer-events:none;font-family:inherit;';
      try { host.appendChild(dot); pill = dot; pillSec = sec; return; } catch (e) { /* fallthrough */ }
    }

    // ② 兜底：昵称后面的小胶囊
    const el = document.createElement('span');
    el.textContent = '已存 ' + n;
    el.style.cssText = 'display:inline-block;margin-left:6px;padding:0 6px;height:17px;line-height:17px;' +
      'border-radius:9px;background:rgba(255,212,121,.16);color:#ffd479;' +
      'border:1px solid rgba(255,212,121,.4);font-size:11px;font-weight:600;' +
      'vertical-align:middle;pointer-events:none;white-space:nowrap;font-family:inherit;';
    try {
      anchor.insertAdjacentElement('afterend', el);
      pill = el; pillSec = sec;
    } catch (e) { /* ignore */ }
  }

  function removePill() {
    try { if (pill && pill.isConnected) pill.remove(); } catch (e) { /* ignore */ }
    pill = null; pillSec = '';
  }

  // --------------------------------------------------------------- 交互
  let drag = null;

  function onDown(e) {
    if (!e.target || !e.target.closest) return;
    if (e.target.closest('button')) return;          // 按钮不当拖拽把手
    const handle = e.target.closest('.hd') || e.target.closest('.hbar');
    if (!handle) return;
    const w = wrapEl();
    if (!w) return;
    const r = w.getBoundingClientRect();
    drag = { dx: e.clientX - r.left, dy: e.clientY - r.top, moved: false };
    e.preventDefault();
    document.addEventListener('mousemove', onMove, true);
    document.addEventListener('mouseup', onUp, true);
  }

  function onMove(e) {
    if (!drag) return;
    const w = wrapEl();
    if (!w) return;
    const r = w.getBoundingClientRect();
    let left = e.clientX - drag.dx;
    let top = e.clientY - drag.dy;
    left = Math.max(2, Math.min(left, innerWidth - r.width - 2));
    top = Math.max(2, Math.min(top, innerHeight - 60));
    drag.moved = true;
    w.style.left = left + 'px';
    w.style.top = top + 'px';
    w.style.right = 'auto';
    drag.left = left; drag.top = top;
  }

  let lastDragTs = 0;

  function onUp() {
    document.removeEventListener('mousemove', onMove, true);
    document.removeEventListener('mouseup', onUp, true);
    if (drag && drag.moved && typeof drag.left === 'number') {
      lastDragTs = Date.now();
      S.pos = { left: Math.round(drag.left), top: Math.round(drag.top) };
      saveSettings({ pos: S.pos });
    }
    drag = null;
  }

  function onClick(e) {
    const btn = e.target && e.target.closest ? e.target.closest('[data-act]') : null;
    if (!btn) return;
    if (Date.now() - lastDragTs < 260) return;       // 拖完松手别误触
    const act = btn.getAttribute('data-act');
    if (act === 'collapse') { S.collapsed = true; saveSettings({ collapsed: true }); render(); return; }
    if (act === 'expand') { S.collapsed = false; saveSettings({ collapsed: false }); render(); return; }
    if (act === 'refresh' || act === 'retry') {
      // 「刷新」要真的重新识别一次，而不是拿旧结果再查一遍（页面可能刚把作者名渲染出来）
      psCache.clear();
      psLastTry = 0;
      let fresh = null;
      try { fresh = detect(); } catch (e) { fresh = null; }
      if (fresh) {
        st.ctx = fresh;
        st.key = [fresh.sec_uid, fresh.uid, fresh.nickname,
          fresh.aweme_id, fresh.profile ? 'p' : ''].join('|');
      }
      if (st.ctx) lookup(st.ctx, true);
      return;
    }
    if (act === 'queue-add') {
      const a = (st.data && st.data.author) || {};
      const c = st.ctx || {};
      const m = c.meta || null;
      const au = (m && m.au) || {};
      const payload = {
        uid: a.uid || c.uid || '',
        sec_uid: a.sec_uid || c.sec_uid || '',
        nickname: a.nickname || c.nickname || '',
        douyin_id: a.douyin_id || '',
        tags: a.tags || [], level: a.level || '', status: a.status || '',
        follower: au.follower || 0, signature: au.signature || ''
      };
      const inBar = !!(btn.closest && btn.closest('.hbar'));
      if (!payload.uid && !payload.sec_uid) {
        if (inBar) {
          btn.textContent = '认不出作者';
          setTimeout(function () { if (btn.isConnected) btn.textContent = '＋待下载'; }, 2200);
        } else {
          st.toastErr = true;
          st.toast = '认不出这位作者，先点进他的主页再推';
          render();
        }
        return;
      }
      const oldTxt = btn.textContent;
      btn.textContent = '…';
      sendToBg({ type: 'queueAdd', payload }).then(function (res) {
        const d = (res && res.data) || {};
        const okAdd = !!(res && res.ok);
        if (inBar) {
          // 横栏没有提示区，直接把按钮文字换成结果，过几秒复原
          btn.textContent = okAdd ? (d.duplicated ? '已在队列' : '已加入 ✓') : '失败';
          setTimeout(function () { if (btn.isConnected) btn.textContent = oldTxt; }, 2600);
          return;
        }
        if (okAdd) {
          st.toastErr = false;
          st.toast = d.duplicated ? '已经在待下载区了'
            : ('已加入待下载区' + (d.item && d.item.created ? '，并新建了作者主页' : '')
               + (d.item && d.item.note && !d.item.created ? '（' + d.item.note + '）' : ''));
        } else {
          st.toastErr = true;
          st.toast = '加入失败：' + ((res && res.message) || '未知错误');
        }
        render();
        clearTimeout(st.toastTimer);
        st.toastTimer = setTimeout(function () { st.toast = ''; render(); }, 3200);
      });
      return;
    }
    if (act === 'open-author') {
      const a = (st.data && st.data.author) || {};
      const uid = a.uid || (st.ctx && st.ctx.uid) || '';
      if (uid) window.open('http://127.0.0.1:8723/#author=' + encodeURIComponent(uid), '_blank');
      else window.open('http://127.0.0.1:8723/', '_blank');
      return;
    }
    if (act === 'copy') {
      const nm = (st.ctx && st.ctx.nickname) || '';
      try { navigator.clipboard.writeText(nm); btn.textContent = '已复制'; } catch (err) { /* ignore */ }
      return;
    }
    if (act === 'openlib') { window.open('http://127.0.0.1:8723/', '_blank'); return; }
    if (act === 'howto') {
      alert('启动本地服务（任选一种）：\n\n' +
        '① 一起启动（推荐）：双击项目根目录的 start-all.bat\n\n' +
        '② 单独启动：\nlocal-service\\start-service.bat\nbrowser\\start-browser.bat\n\n' +
        '起来后这里会自动恢复，不用刷新页面。');
      return;
    }
  }

  let saveTimer = null;
  function saveSettings(patch) {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      sendToBg({ type: 'setSettings', patch });
    }, 220);
  }

  // --------------------------------------------------------------- 主循环
  /* ---- 作品卡片右下角的发布时间 ----
   * 数据全部来自页面自己发过的接口响应（page-hook 旁听），不额外请求、不动登录态。
   * 只在「有缩略图、尺寸像卡片」的链接上加，主播放器不会命中。 */
  let lastDeco = 0;
  let decoDirty = true;   // 只有「拿到新的 create_time」或「换了页面」才需要重扫，空闲时一次都不跑

  function fmtGridTime(ts) {
    const d = new Date(ts * 1000);
    const mm = pad(d.getMonth() + 1) + '-' + pad(d.getDate());
    return (d.getFullYear() === new Date().getFullYear()) ? mm : (String(d.getFullYear()).slice(2) + '-' + mm);
  }

  function decorateWorks() {
    if (!hookMap.size) return;
    const links = document.querySelectorAll('a[href*="/video/"], a[href*="/note/"]');
    let guard = 0;
    for (let i = 0; i < links.length && guard < 240; i++) {
      const a = links[i];
      const mm = (a.getAttribute('href') || '').match(/\/(?:video|note)\/(\d{15,25})/);
      if (!mm) continue;
      const id = mm[1];
      const hit = hookMap.get(id);
      if (!hit || !hit.meta || !hit.meta.create_time) continue;
      if (a.dataset.dyTime === id) continue;
      const img = a.querySelector('img');
      if (!img) continue;
      const r = a.getBoundingClientRect();
      if (r.width < 70 || r.width > 460 || r.height < 70 || r.height > 700) continue;
      guard++;
      let pos = '';
      try { pos = getComputedStyle(a).position; } catch (e) { }
      if (pos === 'static') a.style.position = 'relative';
      const badge = document.createElement('span');
      badge.className = 'dy-time-badge';
      badge.textContent = fmtGridTime(hit.meta.create_time);
      badge.title = '发布时间 ' + fmtFull(hit.meta.create_time);
      badge.style.cssText = 'position:absolute;right:5px;bottom:5px;z-index:3;pointer-events:none;' +
        'background:rgba(0,0,0,.62);color:#fff;font-size:11px;line-height:16px;padding:0 5px;' +
        'border-radius:3px;font-family:inherit;letter-spacing:.2px;';
      try { a.appendChild(badge); a.dataset.dyTime = id; } catch (e) { /* ignore */ }
    }
  }

  let lastPath = '';

  function tick() {
    if (!S.enabled) return;
    lastTick = Date.now();

    if (decoDirty) {
      const tn = Date.now();
      if (tn - lastDeco > 600) {
        lastDeco = tn;
        decoDirty = false;
        try { decorateWorks(); } catch (e) { /* ignore */ }
      }
    }

    // 地址一变（切视频/换页）先立刻收掉旧卡片，绝不把上一条的作者留在屏幕上
    const p = location.pathname + location.search;
    if (p !== lastPath) {
      lastPath = p;
      decoDirty = true;
      if (st.key) {
        st.key = ''; st.ctx = null; st.data = null; st.error = null;
        clearPanel();
      }
    }
    let ctx = null;
    try { ctx = detect(); } catch (e) { ctx = null; }

    if (!ctx || (!ctx.sec_uid && !ctx.nickname && !ctx.aweme_id)) {
      // 完全没线索：显示一条「认不出」的横栏，别让面板凭空消失（也别去查库）
      if (st.key !== '__none__') {
        st.key = '__none__';
        st.ctx = { sec_uid: '', uid: '', nickname: '', aweme_id: '', src: '' };
        st.data = { ok: true, status: 'miss', matched_by: 'none', candidates: [], meta: null };
        st.error = null;
        render();
      }
      return;
    }
    const key = [ctx.sec_uid, ctx.uid, ctx.nickname, ctx.aweme_id, ctx.profile ? 'p' : ''].join('|');
    if (key === st.key) {
      ensurePill();
      // 服务未连接时，到点自动重试
      if (st.error && st.retryAt && Date.now() > st.retryAt && !st.loading) {
        st.retryAt = 0;
        lookup(st.ctx, true);
      }
      return;
    }
    st.key = key;
    st.ctx = ctx;
    st.data = null;
    st.error = null;
    lookup(ctx, false);
  }

  function schedule(delay) {
    const now = Date.now();
    const gap = delay || 220;
    if (now - mutGuard < gap) return;
    mutGuard = now;
    setTimeout(() => { mutGuard = 0; tick(); }, gap);
  }

  function start() {
    stop();
    ensureHost();
    tick();
    timer = setInterval(tick, 450);
    try {
      mo = new MutationObserver(() => schedule(220));
      mo.observe(document.documentElement, { childList: true, subtree: true });
    } catch (e) { /* ignore */ }
    try {
      window.addEventListener('scroll', onScroll, { passive: true, capture: true });
      window.addEventListener('wheel', onScroll, { passive: true, capture: true });
      window.addEventListener('popstate', () => schedule(80));
    } catch (e) { /* ignore */ }
    setTimeout(() => { try { window.postMessage({ __dyLocalAsk: 1 }, '*'); } catch (e) {} }, 800);
  }

  let scrollGuard = 0;
  function onScroll() {
    const now = Date.now();
    if (now - scrollGuard < 160) return;
    scrollGuard = now;
    schedule(120);
  }

  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
    if (mo) { try { mo.disconnect(); } catch (e) {} mo = null; }
    try {
      window.removeEventListener('scroll', onScroll, { capture: true });
      window.removeEventListener('wheel', onScroll, { capture: true });
    } catch (e) { /* ignore */ }
  }

  function teardown() {
    stop();
    removePill();
    if (host && host.isConnected) { try { host.remove(); } catch (e) {} }
    host = null; shadow = null;
    st.key = ''; st.ctx = null; st.data = null; st.error = null;
  }

  // ------------------------------------------------------- 页面钩子数据接收
  window.addEventListener('message', (ev) => {
    if (ev.source !== window || !ev.data) return;
    const p = ev.data.__dyLocalHook ? ev.data.payload : null;
    if (!p) return;
    if (p.src === 'router') {
      hookRouter = { path: location.pathname, user: { sec_uid: p.sec_uid, nickname: p.nickname } };
      schedule(300);
      return;
    }
    if (!p.aweme_id && !p.sec_uid) return;
    hookLatest = p; hookAt = Date.now();
    if (p.aweme_id) {
      hookMap.set(p.aweme_id, p);
      decoDirty = true;
      if (hookMap.size > 200) hookMap.delete(hookMap.keys().next().value);
    }
    const cur = (location.pathname.match(/\/(?:video|note)\/(\d{15,25})/) || [])[1] || '';
    if (p.aweme_id && cur && p.aweme_id === cur) schedule(120);
  });

  chrome.storage.onChanged.addListener((ch, area) => {
    if (area !== 'local' || !ch.settings) return;
    const wasEnabled = S.enabled;
    S = Object.assign({}, DEFAULTS, ch.settings.newValue || {});
    if (!S.enabled) { teardown(); return; }
    if (!wasEnabled) { start(); return; }
    render();
    if (S.showPill && st.data && st.data.status === 'hit') ensurePill(); else if (!S.showPill) removePill();
  });

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg && msg.type === 'refresh') {
      if (st.ctx) lookup(st.ctx, true);
      sendResponse({ ok: true });
    }
    if (msg && msg.type === 'ping') sendResponse({ ok: true, key: st.key });
  });

  // 抖音是 SPA：地址变化靠定时器 + 观察器双保险（1 秒一次，开销极小）
  (async () => {
    try {
      const got = await chrome.storage.local.get('settings');
      S = Object.assign({}, DEFAULTS, (got && got.settings) || {});
    } catch (e) { /* 用默认值 */ }
    if (S.enabled) start();
  })();
})();
