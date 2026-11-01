/* ============================================================================
   标注台（2026-10-31 c75）
   ----------------------------------------------------------------------------
   给「界面想改但说不清哪儿」用的：左边 iframe 里是真实应用（能点、能滚），
   用户在页面上框一下 / 画个箭头 / 涂两笔 / 写一句话，每条自动编号，并记下
   「命中哪块区域 + CSS 选择器 + 矩形坐标」，最后拼成一份 markdown 存到
   data/annotations/界面意见_*.md —— 我（AI）直接读文件就能照着改。

   三个关键设计（改代码前先看这三条）：
   1. canvas 和编号层是**插进 iframe 的 body** 的，不是盖在 iframe 外面：
      这样标注跟着页面滚动/缩放走，还能用 elementsFromPoint 反查真实元素。
   2. 绘制模式下在 iframe 的 window 上**捕获阶段** stopImmediatePropagation，
      应用自己的点击（点开作者、切视图）一个都收不到；浏览模式完全不插手。
   3. 坐标一律存**页面坐标**（client + 当时 scroll），滚动时不重算数据，只换
      绘制偏移；页面重绘（改星标会重建 #detailTop）时靠 MutationObserver 提示。
   ========================================================================== */
'use strict';

/* 绘制模式要拦掉的事件：不拦 wheel —— 标注时还得能滚页面 */
var ANNOT_BLOCK = ['pointerdown', 'pointermove', 'pointerup', 'pointercancel',
    'mousedown', 'mouseup', 'click', 'dblclick', 'contextmenu', 'dragstart'];

/* 区域表：**先子后父**（closest 按顺序找第一个命中的）。
   顺序错了就会把「数据条」认成「作者信息区」——因为 .detail-stats-wrap 在 .detail-info 里面，
   谁排在前面谁先命中，所以子元素必须写在祖先前面。 */
var ANNOT_REGIONS = [
    ['账号状态卡', '.detail-card.c-status'],
    ['作者等级卡', '.detail-card.c-level'],
    ['更新频率卡', '.detail-card.c-freq'],
    ['标签卡', '.detail-card.c-tags'],
    ['备注卡', '.detail-card.c-note'],
    ['封面图', '.detail-cover'],
    ['作者名 / 别名', '.detail-name'],
    ['字段行（抖音号/UID/SEC_UID）', '.detail-fields'],
    ['简介', '.detail-sign'],
    ['操作按钮组', '.detail-actions'],
    ['历史记录浮层', '.hist-pop'],
    ['大小号行', '.detail-alts-inline'],
    ['数据条（作品/抖音/最后下载）', '.detail-stats-wrap'],
    ['作者信息区', '.detail-info'],
    ['信息卡网格', '.detail-grid'],
    ['详情头部整块', '.detail-header'],
    ['返回栏', '.detail-bar'],
    ['作品筛选栏', '.wall-head'],
    ['单个作品卡', '.wall-card'],
    ['作品区', '#workRail'],
    ['详情左栏', '.work-wall'],
    ['右侧播放 / 预览区', '#playerStage'],
    ['作者卡封面', '.author-cover'],
    ['作者卡名字', '.author-name'],
    ['作者卡信息行（作品数/最新）', '.author-meta'],
    ['作者卡标签', '.author-tags'],
    ['作者卡', '.author-card'],
    ['作者网格', '#authorGrid'],
    ['视图标题栏', '.view-header'],
    ['左侧边栏', '.sidebar'],
    ['顶栏', '.topbar']
];

var annotFrame = null, annotDoc = null, annotWin = null;
var annotCanvas = null, annotCtx = null, annotLayer = null;
var annotTool = null;          // null(浏览) | 'rect' | 'arrow' | 'pen' | 'text'
var annotItems = [];           // 见 annotNewItem() 的字段注释
var annotDrag = null;          // {tool, scroller, points:[{x,y}] 页面坐标}
var annotFlashNo = 0;          // 正在闪烁的条目号（1 起）
var annotRafPending = false;
var annotEditingText = false;  // 文字工具正在输入：此时不拦事件，否则打不了字
var annotReady = false;
var annotStale = false;        // 页面重绘过（坐标可能对不上）

/* ============================== 启动 ============================== */

document.addEventListener('DOMContentLoaded', annotBoot);

function annotBoot() {
    annotFrame = document.getElementById('annotFrame');
    annotBindPanel();

    annotFrame.addEventListener('load', annotAttach);
    /* 首次打开时 load 可能已经过（浏览���缓存），手动补一次 */
    try {
        if (annotFrame.contentDocument && annotFrame.contentDocument.body) annotAttach();
    } catch (e) { /* 跨域才会抛，本地不会 */ }

    /* 6 秒还没装上 canvas，说明 iframe 根本没起来（服务没跑 / 端口不对） */
    setTimeout(function () {
        if (!annotReady) annotShowMask('内嵌页面还没起来。先确认服务在跑（双击 start-browser.bat 或用「重新扫描」那个窗口），再点重试。');
    }, 6000);
}

function annotAttach() {
    try {
        annotDoc = annotFrame.contentDocument;
        annotWin = annotFrame.contentWindow;
        if (!annotDoc || !annotDoc.body) throw new Error('文档是空的');
    } catch (e) {
        annotShowMask('读不到内嵌页面：' + e.message);
        return;
    }
    /* 已经装过（load 触发两次 / 手动补调）就直接复用 */
    if (annotDoc.getElementById('__annotCanvas')) {
        annotReady = true;
        annotHideMask();
        return;
    }
    /* 嵌进来的页面里那个「✏️ 标注」按钮要藏掉，否则能在标注台里再点出第二层标注台 */
    try {
        var nb = annotDoc.getElementById('annotateBtn');
        if (nb) nb.style.display = 'none';
    } catch (e) { /* 老版本 index.html 没这个按钮，无所谓 */ }

    annotInstallCanvas();
    annotBindInnerEvents();
    annotBindParentFallback();
    annotWatchScroll();
    annotWatchRerender();
    annotSetTool(null);
    annotReady = true;
    annotHideMask();
    annotRenderList();
}

function annotShowMask(msg) {
    var m = document.getElementById('frameMask');
    if (!m) return;
    var t = document.getElementById('maskMsg');
    if (t && msg) t.textContent = msg;
    m.classList.remove('hidden');
}

function annotHideMask() {
    var m = document.getElementById('frameMask');
    if (m) m.classList.add('hidden');
}

/* ============================== 画布与样式 ============================== */

function annotInstallCanvas() {
    /* 样式一次性插进 iframe 的 head：canvas / 编号层 / 输入框 都在 iframe 文档里，
       所以这些规则也必须写在 iframe 文档里才能生效 */
    var st = annotDoc.createElement('style');
    st.id = '__annotStyle';
    st.textContent = [
        'html.__annot-drawing, html.__annot-drawing * { cursor: crosshair !important; }',
        '#__annotCanvas { position: fixed; left: 0; top: 0; pointer-events: none; z-index: 2147483000; }',
        '#__annotLayer { position: fixed; left: 0; top: 0; pointer-events: none; z-index: 2147483001; }',
        '.annot-badge { position: fixed; min-width: 18px; height: 18px; padding: 0 3px; border-radius: 9px;',
        '  background: #fe2c55; color: #fff; text-align: center; pointer-events: none;',
        '  font: 700 11px/18px system-ui, -apple-system, "Microsoft YaHei", sans-serif;',
        '  box-shadow: 0 2px 6px rgba(0,0,0,.28); }',
        '.annot-badge.flash { animation: __annotFlash .45s ease-in-out 2; }',
        '@keyframes __annotFlash { 0%,100% { transform: scale(1); } 50% { transform: scale(1.45); background:#1d4ed8; } }',
        '#__annotText { position: fixed; z-index: 2147483002; min-width: 150px; padding: 4px 6px;',
        '  font: 500 13.5px/1.4 system-ui, -apple-system, "Microsoft YaHei", sans-serif; color: #111;',
        '  border: 2px solid #fe2c55; border-radius: 7px; background: #fff; outline: none;',
        '  box-shadow: 0 8px 22px rgba(0,0,0,.18); }'
    ].join('\n');
    annotDoc.head.appendChild(st);

    annotCanvas = annotDoc.createElement('canvas');
    annotCanvas.id = '__annotCanvas';
    /* 挂 body 而不是 #detailTop：后者被 innerHTML 整体重建，画布会被冲掉 */
    annotDoc.body.appendChild(annotCanvas);
    annotCtx = annotCanvas.getContext('2d');

    annotLayer = annotDoc.createElement('div');
    annotLayer.id = '__annotLayer';
    annotDoc.body.appendChild(annotLayer);

    annotFitCanvas();
    /* 只用 window.resize，别用 ResizeObserver：
       RO 里改 canvas 尺寸会触发新一轮尺寸变化 → 「ResizeObserver loop completed with
       undelivered notifications」，应用自己的 window.onerror 会把它当致命错误弹红条。
       iframe 变宽/变窄（含收起右栏）时 window.resize 一次就够。 */
    annotWin.addEventListener('resize', annotFitCanvas);
}

/* 按 DPR 放大物理像素，再用 setTransform 归一到 CSS 像素：笔画在高分屏才不发虚 */
function annotFitCanvas() {
    if (!annotCanvas) return;
    var w = annotWin.innerWidth, h = annotWin.innerHeight;
    var dpr = annotWin.devicePixelRatio || 1;
    /* 尺寸没变就别重设（重设会清空画布再重画，滚动时白闪一下） */
    if (w === annotCanvas.__w && h === annotCanvas.__h && dpr === annotCanvas.__dpr) return;
    annotCanvas.__w = w;
    annotCanvas.__h = h;
    annotCanvas.__dpr = dpr;
    annotCanvas.width = Math.round(w * dpr);
    annotCanvas.height = Math.round(h * dpr);
    annotCanvas.style.width = w + 'px';
    annotCanvas.style.height = h + 'px';
    annotCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    annotDrawAll();
}

function annotSetTool(tool) {
    annotTool = tool || null;
    /* 用属性选择器找按钮：data-tool="none" 也算「浏览」 */
    var btns = document.querySelectorAll('.annot-tools .tb[data-tool]');
    for (var i = 0; i < btns.length; i++) {
        var t = btns[i].getAttribute('data-tool');
        var on = (tool ? t === tool : t === 'none');
        btns[i].classList.toggle('on', on);
    }
    if (annotDoc) {
        annotDoc.documentElement.classList.toggle('__annot-drawing', !!annotTool);
    }
}

/* ============================== 坐标系 ============================== */

/* 滚动容器：详情页滚 #wallScroll、列表页滚 .content（app.js 里就是这么定的） */
function annotScrollerKey() {
    var dv = annotDoc.getElementById('detailView');
    if (dv && !dv.classList.contains('hidden') && annotDoc.getElementById('wallScroll')) return 'wallScroll';
    return 'content';
}

function annotScrollerEl(key) {
    if (key === 'wallScroll') return annotDoc.getElementById('wallScroll') || annotDoc.scrollingElement;
    return annotDoc.querySelector('.content') || annotDoc.scrollingElement;
}

function annotPagePos(clientX, clientY, key) {
    var sc = annotScrollerEl(key);
    return { x: clientX + (sc ? sc.scrollLeft : 0), y: clientY + (sc ? sc.scrollTop : 0) };
}

function annotClientPos(p, key) {
    var sc = annotScrollerEl(key);
    return { x: p.x - (sc ? sc.scrollLeft : 0), y: p.y - (sc ? sc.scrollTop : 0) };
}

/* ============================== 绘制 ============================== */

function annotScheduleDraw() {
    if (annotRafPending) return;
    annotRafPending = true;
    (annotWin.requestAnimationFrame || annotWin.setTimeout)(function () {
        annotRafPending = false;
        annotDrawAll();
    }, 16);
}

function annotDrawAll() {
    if (!annotCtx) return;
    annotCtx.clearRect(0, 0, annotWin.innerWidth, annotWin.innerHeight);
    for (var i = 0; i < annotItems.length; i++) {
        annotDrawOne(annotItems[i], i + 1, false);
    }
    if (annotDrag) {
        annotDrawOne({ tool: annotDrag.tool, points: annotDrag.points, scroller: annotDrag.scroller }, 0, true);
    }
    annotPlaceBadges();
}

function annotRoundRect(ctx, x, y, w, h, r) {
    var rr = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + rr, y);
    ctx.arcTo(x + w, y, x + w, y + h, rr);
    ctx.arcTo(x + w, y + h, x, y + h, rr);
    ctx.arcTo(x, y + h, x, y, rr);
    ctx.arcTo(x, y, x + w, y, rr);
    ctx.closePath();
}

function annotDrawOne(it, no, preview) {
    var ctx = annotCtx;
    var pts = it.points || [];
    if (!pts.length) return;
    var flash = !preview && no === annotFlashNo;
    var color = preview ? 'rgba(254,44,85,.6)' : (flash ? '#1d4ed8' : '#fe2c55');
    var fill = preview ? 'rgba(254,44,85,.06)' : 'rgba(254,44,85,.10)';

    var p0 = annotClientPos(pts[0], it.scroller);
    var pn = annotClientPos(pts[pts.length - 1], it.scroller);

    ctx.save();
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = flash ? 3.2 : (preview ? 2 : 2.2);
    if (preview) { ctx.setLineDash([5, 4]); }

    if (it.tool === 'rect') {
        var x = Math.min(p0.x, pn.x), y = Math.min(p0.y, pn.y);
        var w = Math.abs(pn.x - p0.x), h = Math.abs(pn.y - p0.y);
        ctx.fillStyle = fill;
        annotRoundRect(ctx, x, y, w, h, 4);
        ctx.fill();
        ctx.stroke();
    } else if (it.tool === 'arrow') {
        annotDrawArrow(ctx, p0, pn);
    } else if (it.tool === 'pen') {
        ctx.beginPath();
        for (var i = 0; i < pts.length; i++) {
            var p = annotClientPos(pts[i], it.scroller);
            if (i === 0) ctx.moveTo(p.x, p.y);
            else ctx.lineTo(p.x, p.y);
        }
        ctx.stroke();
    } else if (it.tool === 'text') {
        var txt = it.text || it.note || '';
        ctx.font = '600 13.5px system-ui, -apple-system, "Microsoft YaHei", sans-serif';
        var tw = ctx.measureText(txt).width;
        var bx = p0.x, by = p0.y;
        ctx.fillStyle = 'rgba(255,255,255,.94)';
        annotRoundRect(ctx, bx - 4, by - 15, tw + 10, 22, 6);
        ctx.fill();
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.2;
        ctx.stroke();
        ctx.fillStyle = color;
        ctx.textBaseline = 'middle';
        ctx.fillText(txt, bx + 1, by - 3);
    }
    ctx.restore();
}

function annotDrawArrow(ctx, a, b) {
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
    var ang = Math.atan2(b.y - a.y, b.x - a.x);
    var len = 11;
    ctx.beginPath();
    ctx.moveTo(b.x, b.y);
    ctx.lineTo(b.x - len * Math.cos(ang - 0.42), b.y - len * Math.sin(ang - 0.42));
    ctx.lineTo(b.x - len * Math.cos(ang + 0.42), b.y - len * Math.sin(ang + 0.42));
    ctx.closePath();
    ctx.fill();
}

/* 编号用 DOM 徽标：canvas 上写字高 DPR 会糊，也没法做闪烁动画 */
function annotCityNum(n) {
    return (n >= 1 && n <= 20) ? String.fromCharCode(0x2460 + n - 1) : '(' + n + ')';
}

function annotPlaceBadges() {
    if (!annotLayer) return;
    annotLayer.innerHTML = '';
    for (var i = 0; i < annotItems.length; i++) {
        var it = annotItems[i];
        var r = it.rect || { x: 0, y: 0, w: 0, h: 0 };
        var p = annotClientPos({ x: r.x, y: r.y }, it.scroller);
        var b = annotDoc.createElement('span');
        b.className = 'annot-badge' + ((i + 1) === annotFlashNo ? ' flash' : '');
        b.textContent = annotCityNum(i + 1);
        b.style.left = Math.round(p.x - 9) + 'px';
        b.style.top = Math.round(p.y - 9) + 'px';
        annotLayer.appendChild(b);
    }
}

function annotFlash(no) {
    annotFlashNo = no;
    annotDrawAll();
    setTimeout(function () {
        if (annotFlashNo === no) { annotFlashNo = 0; annotDrawAll(); }
    }, 900);
}

/* ============================== 事件接管 ============================== */

function annotBindInnerEvents() {
    ANNOT_BLOCK.forEach(function (t) {
        annotWin.addEventListener(t, annotGuard, true);
    });
    annotWin.addEventListener('keydown', annotHotkey, true);
}

function annotGuard(ev) {
    if (!annotTool || annotEditingText) return;    // 浏览模式：一根手指都不伸
    ev.preventDefault();
    ev.stopImmediatePropagation();
    if (ev.type === 'pointerdown') annotOnDown(ev);
    else if (ev.type === 'pointermove') annotOnMove(ev);
    else if (ev.type === 'pointerup' || ev.type === 'pointercancel') annotOnUp();
}

function annotHotkey(ev) {
    if (annotEditingText) return;
    if (ev.key === 'Escape') {
        annotSetTool(null);
        ev.preventDefault();
        ev.stopImmediatePropagation();
    } else if ((ev.ctrlKey || ev.metaKey) && String(ev.key).toLowerCase() === 'z') {
        annotUndo();
        ev.preventDefault();
        ev.stopImmediatePropagation();
    }
}

/* 指针从 iframe 拖到右侧面板上时，iframe 收不到事件了，靠父窗口接着喂 */
function annotBindParentFallback() {
    window.addEventListener('pointermove', function (ev) {
        if (!annotDrag) return;
        var r = annotFrame.getBoundingClientRect();
        annotOnMove({
            clientX: Math.max(0, Math.min(ev.clientX - r.left, annotWin.innerWidth)),
            clientY: Math.max(0, Math.min(ev.clientY - r.top, annotWin.innerHeight))
        });
    }, true);
    window.addEventListener('pointerup', function () {
        if (annotDrag) annotOnUp();
    }, true);
    window.addEventListener('blur', function () {
        if (annotDrag) annotOnUp();
    });
}

function annotOnDown(ev) {
    if (ev.button && ev.button !== 0) return;
    if (annotTool === 'text') {
        annotOpenTextInput(ev.clientX, ev.clientY);
        return;
    }
    var key = annotScrollerKey();
    annotDrag = {
        tool: annotTool,
        scroller: key,
        points: [annotPagePos(ev.clientX, ev.clientY, key)]
    };
    annotScheduleDraw();
}

function annotOnMove(ev) {
    if (!annotDrag) return;
    annotDrag.points.push(annotPagePos(ev.clientX, ev.clientY, annotDrag.scroller));
    annotScheduleDraw();
}

function annotOnUp() {
    var d = annotDrag;
    annotDrag = null;
    if (!d || !d.points.length) { annotDrawAll(); return; }

    var xs = d.points.map(function (p) { return p.x; });
    var ys = d.points.map(function (p) { return p.y; });
    var rect = {
        x: Math.min.apply(null, xs), y: Math.min.apply(null, ys),
        w: Math.max.apply(null, xs) - Math.min.apply(null, xs),
        h: Math.max.apply(null, ys) - Math.min.apply(null, ys)
    };
    /* 手抖点一下就生成一条会很烦：小于 4px 且不是画笔（画笔有点）就丢掉 */
    if (rect.w < 4 && rect.h < 4 && d.tool !== 'pen') { annotDrawAll(); return; }

    var hitClient = annotClientPos({ x: rect.x + rect.w / 2, y: rect.y + rect.h / 2 }, d.scroller);
    var hit = annotHitTest(hitClient.x, hitClient.y);
    var region = annotRegionOf(hit);

    annotItems.push(annotNewItem({
        tool: d.tool,
        points: d.points,
        rect: rect,
        scroller: d.scroller,
        region: region ? region.name : annotFallbackLabel(hit),
        selector: annotSelectorFor(hit, region)
    }));
    annotDrawAll();
    annotRenderList();
    annotFocusLastNote();
    annotStatus('已记 ' + annotItems.length + ' 条 · 继续拖可继续标 · Esc 退出');
}

/* ============================== 反查元素 ============================== */

function annotHitTest(x, y) {
    var list = (annotDoc.elementsFromPoint ? annotDoc.elementsFromPoint(x, y) : [annotDoc.elementFromPoint(x, y)]) || [];
    for (var i = 0; i < list.length; i++) {
        var el = list[i];
        if (!el || el.id === '__annotCanvas' || el.id === '__annotLayer' || el.id === '__annotText') continue;
        if (el.closest && el.closest('#__annotLayer')) continue;
        return el;
    }
    return annotDoc.body;
}

function annotRegionOf(el) {
    if (!el || !el.closest) return null;
    for (var i = 0; i < ANNOT_REGIONS.length; i++) {
        var name = ANNOT_REGIONS[i][0], sel = ANNOT_REGIONS[i][1];
        if (el.closest(sel)) return { name: name, sel: sel };
    }
    return null;
}

function annotFallbackLabel(el) {
    if (!el) return '未知区域';
    var cls = (el.className || '').toString().trim().split(/\s+/)[0];
    var txt = (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 16);
    return (el.tagName || 'div').toLowerCase() + (cls ? '.' + cls : '') + (txt ? '「' + txt + '」' : '');
}

function annotSelectorOf(el) {
    if (!el) return '';
    if (el.id && el.id.indexOf('__annot') !== 0) return '#' + el.id;
    var uid = el.getAttribute && el.getAttribute('data-uid');
    if (uid) return '.author-card[data-uid="' + uid + '"]';
    var idx = el.getAttribute && el.getAttribute('data-idx');
    if (idx !== null && idx !== undefined && idx !== '') {
        return '#workRail .wall-card[data-idx="' + idx + '"]';
    }
    var parts = [], n = el, depth = 0;
    while (n && n.nodeType === 1 && n !== annotDoc.body && depth < 4) {
        if (n.id && n.id.indexOf('__annot') !== 0) { parts.unshift('#' + n.id); break; }
        var s = n.tagName.toLowerCase();
        var cls = (n.className || '').toString().trim().split(/\s+/)
            .filter(function (c) { return c && c.indexOf('__annot') !== 0; }).slice(0, 2);
        if (cls.length) s += '.' + cls.join('.');
        parts.unshift(s);
        n = n.parentElement;
        depth++;
    }
    return parts.join(' > ');
}

/* 有了区域就优先用「区域选择器 + 唯一标识」：
   落到 .author-card 时带 data-uid，落到作品卡时带 data-idx —— 我拿到就能直接定位到那一张，
   而不是只知道「某个作者卡」。 */
function annotSelectorFor(hit, region) {
    var el = hit;
    if (region && hit && hit.closest) {
        var m = hit.closest(region.sel);
        if (m) el = m;
    }
    if (!el) return region ? region.sel : '';
    var uid = el.getAttribute && el.getAttribute('data-uid');
    if (uid) return '.author-card[data-uid="' + uid + '"]';
    var idx = el.getAttribute && el.getAttribute('data-idx');
    if (idx !== null && idx !== undefined && idx !== '' && el.classList.contains('wall-card')) {
        return '#workRail .wall-card[data-idx="' + idx + '"]';
    }
    if (region) return region.sel;
    return annotSelectorOf(el);
}

/* ============================== 文字工具 ============================== */

function annotOpenTextInput(cx, cy) {
    annotCloseTextInput();
    var key = annotScrollerKey();
    var input = annotDoc.createElement('input');
    input.id = '__annotText';
    input.type = 'text';
    input.placeholder = '写一句话，回车确认';
    input.style.left = Math.max(0, cx - 2) + 'px';
    input.style.top = Math.max(0, cy - 12) + 'px';
    annotDoc.body.appendChild(input);
    annotEditingText = true;
    setTimeout(function () { input.focus(); }, 0);

    var done = false;
    function commit() {
        if (done) return;
        done = true;
        var txt = String(input.value || '').trim();
        var pos = annotPagePos(cx, cy, key);
        annotCloseTextInput();
        if (!txt) return;
        var hit = annotHitTest(cx, cy);
        var region = annotRegionOf(hit);
        annotItems.push(annotNewItem({
            tool: 'text',
            points: [pos],                       // 文字只有一个点
            rect: { x: pos.x, y: pos.y, w: 1, h: 1 },
            scroller: key,
            region: region ? region.name : annotFallbackLabel(hit),
            selector: annotSelectorFor(hit, region),
            text: txt
        }));
        annotDrawAll();
        annotRenderList();
        annotStatus('已记 ' + annotItems.length + ' 条');
    }

    input.addEventListener('keydown', function (ev) {
        ev.stopPropagation();
        if (ev.key === 'Enter') { ev.preventDefault(); commit(); }
        else if (ev.key === 'Escape') { ev.preventDefault(); done = true; annotCloseTextInput(); }
    });
    input.addEventListener('blur', commit);
}

function annotCloseTextInput() {
    annotEditingText = false;
    var old = annotDoc.getElementById('__annotText');
    if (old && old.parentNode) old.parentNode.removeChild(old);
}

/* ============================== 条目 / 列表 ============================== */

/* 条目结构：
   { tool, points:[{x,y}]（页面坐标）, rect:{x,y,w,h}（页面坐标）, scroller:'wallScroll'|'content',
     region:'数据条（作品/抖音/最后下载）', selector:'.detail-stats-wrap', note:'用户写的话', text:'文字工具打的字' } */
function annotNewItem(o) {
    return {
        tool: o.tool,
        points: o.points,
        rect: o.rect,
        scroller: o.scroller,
        region: o.region || '',
        selector: o.selector || '',
        note: o.note || '',
        text: o.text || ''
    };
}

var ANNOT_TOOL_LABEL = { rect: '框选', arrow: '箭头', pen: '画笔', text: '文字' };

/* 只做 HTML 转义：区域名/选择器里有引号和 < > 时不能把列表拼坏 */
function annotEsc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function annotRenderList() {
    var wrap = document.getElementById('annotList');
    var cnt = document.getElementById('annotCount');
    if (!wrap) return;
    if (cnt) cnt.textContent = annotItems.length ? annotItems.length + ' 条' : '还没有标注';

    if (!annotItems.length) {
        wrap.innerHTML = '<div class="empty">左边拖一下就有了。<br>每条会自动认出是「哪一块」，' +
            '比如<code>作者名</code>、<code>数据条</code>、<code>标签卡</code>。</div>';
        return;
    }

    wrap.innerHTML = annotItems.map(function (it, i) {
        return '<div class="item" data-i="' + i + '">' +
            '<div class="item-head" title="点一下在左边闪一下">' +
            '<span class="badge">' + annotCityNum(i + 1) + '</span>' +
            '<span class="rg">' + annotEsc(it.region || '（未识别）') + '</span>' +
            '<span class="sel" title="' + annotEsc(it.selector || '') + '">' + annotEsc(it.selector || '') + '</span>' +
            '<button class="del" title="删掉这条">×</button>' +
            '</div>' +
            '<textarea data-i="' + i + '" placeholder="想变成什么样？（例如：字号小一点 / 想跟' +
            annotCityNum(i) + ' 一样高 / 这块不要了）">' + annotEsc(it.note || '') + '</textarea>' +
            '</div>';
    }).join('');

    /* 事件用委托绑定，列表重绘不用重新挂 */
    wrap.onclick = function (ev) {
        var head = ev.target.closest('.item-head');
        if (head && !ev.target.closest('.del')) {
            var box = head.parentNode;
            annotFlash(parseInt(box.getAttribute('data-i'), 10) + 1);
        }
        if (ev.target.closest('.del')) {
            var item = ev.target.closest('.item');
            var idx = parseInt(item.getAttribute('data-i'), 10);
            annotItems.splice(idx, 1);
            annotDrawAll();
            annotRenderList();
        }
    };
    wrap.oninput = function (ev) {
        if (ev.target.tagName !== 'TEXTAREA') return;
        var idx = parseInt(ev.target.getAttribute('data-i'), 10);
        if (annotItems[idx]) annotItems[idx].note = ev.target.value;
    };
}

function annotFocusLastNote() {
    var wrap = document.getElementById('annotList');
    if (!wrap) return;
    var tas = wrap.querySelectorAll('textarea');
    if (!tas.length) return;
    var last = tas[tas.length - 1];
    last.focus();
    last.scrollIntoView({ block: 'nearest' });
}

function annotUndo() {
    if (!annotItems.length) { annotStatus('没有可撤销的'); return; }
    annotItems.pop();
    annotDrawAll();
    annotRenderList();
    annotStatus('已撤销，剩 ' + annotItems.length + ' 条');
}

function annotClear() {
    if (!annotItems.length) return;
    if (!window.confirm('清空全部 ' + annotItems.length + ' 条标注？')) return;
    annotItems = [];
    annotDrawAll();
    annotRenderList();
    annotStatus('');
}

/* ============================== 面板按钮 ============================== */

function annotBindPanel() {
    var tools = document.querySelectorAll('.annot-tools .tb[data-tool]');
    for (var i = 0; i < tools.length; i++) {
        tools[i].addEventListener('click', function () {
            var t = this.getAttribute('data-tool');
            annotSetTool(t === 'none' ? null : t);
        });
    }
    document.getElementById('undoBtn').addEventListener('click', annotUndo);
    document.getElementById('clearBtn').addEventListener('click', annotClear);
    document.getElementById('saveBtn').addEventListener('click', annotSave);
    document.getElementById('copyBtn').addEventListener('click', annotCopy);
    document.getElementById('dlBtn').addEventListener('click', function () {
        annotDownload(annotBuildMarkdown());
    });
    document.getElementById('maskRetry').addEventListener('click', function () {
        annotHideMask();
        annotFrame.src = '/?t=' + Date.now();
    });
    document.getElementById('maskOpen').addEventListener('click', function () {
        window.open('/', '_blank');
    });
    /* 收起 / 展开右栏：收起后 iframe 变宽，页面排版跟用户平常看到的一致 */
    var panel = document.getElementById('annotPanel');
    var showBtn = document.getElementById('panelShow');
    document.getElementById('panelFold').addEventListener('click', function () {
        panel.classList.add('collapsed');
        showBtn.classList.add('on');
    });
    showBtn.addEventListener('click', function () {
        panel.classList.remove('collapsed');
        showBtn.classList.remove('on');
    });
    document.addEventListener('keydown', function (ev) {
        /* 焦点在输入框里时 Ctrl+Z 是「撤销打字」，别抢 */
        var tag = (ev.target && ev.target.tagName || '').toLowerCase();
        if (tag === 'textarea' || tag === 'input') return;
        if (ev.key === 'Escape') annotSetTool(null);
        if ((ev.ctrlKey || ev.metaKey) && String(ev.key).toLowerCase() === 'z') { ev.preventDefault(); annotUndo(); }
    });
}

function annotStatus(msg, kind) {
    var el = document.getElementById('saveStatus');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'status' + (kind === 'ok' ? ' ok' : (kind === 'err' ? ' err' : ''));
}

/* ============================== 清单 / 保存 ============================== */

function annotBuildMarkdown() {
    var now = new Date();
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    var stamp = now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate()) +
        ' ' + pad(now.getHours()) + ':' + pad(now.getMinutes()) + ':' + pad(now.getSeconds());

    var lines = [];
    lines.push('# 界面标注清单 · ' + stamp);
    lines.push('');
    lines.push('- 被标注页面：' + (annotWin ? annotWin.location.href : 'http://127.0.0.1:8723/') + '（标注台内嵌）');
    lines.push('- 内嵌视口：' + (annotWin ? annotWin.innerWidth : 0) + ' × ' + (annotWin ? annotWin.innerHeight : 0) +
        ' · DPR ' + (annotWin ? (annotWin.devicePixelRatio || 1) : 1) +
        '（比平常窗口窄，断点和字号可能和用户平时看到的不同）');
    lines.push('- 条数：' + annotItems.length);
    if (annotStale) lines.push('- 注意：标注完成后页面重绘过，个别坐标可能对不上，以「选择器」为准');
    lines.push('');
    if (!annotItems.length) {
        lines.push('（还没有标注）');
        return lines.join('\n');
    }

    annotItems.forEach(function (it, i) {
        var num = annotCityNum(i + 1);
        lines.push('## ' + num + ' ' + (ANNOT_TOOL_LABEL[it.tool] || it.tool) + ' · ' + (it.region || '未识别区域'));
        lines.push('- 选择器：`' + (it.selector || '（无）') + '`');
        lines.push('- 位置：x=' + Math.round(it.rect.x) + ' y=' + Math.round(it.rect.y) +
            ' 宽 ' + Math.round(it.rect.w) + ' 高 ' + Math.round(it.rect.h) + '（页面坐标）');
        var note = (it.note || '').trim();
        if (it.tool === 'text' && !note) note = (it.text || '').trim();
        lines.push('- 备注：' + (note || '（用户没写，看位置判断）'));
        lines.push('');
    });
    return lines.join('\n').trim() + '\n';
}

async function annotCopy() {
    if (!annotItems.length) { annotStatus('还没有标注，先在左边框一个', 'err'); return; }
    var text = annotBuildMarkdown();
    try {
        await navigator.clipboard.writeText(text);
        annotStatus('已复制到剪贴板，直接粘给我就行', 'ok');
        return;
    } catch (e) { /* 走到下面降级 */ }
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0;left:-9999px';
    document.body.appendChild(ta);
    ta.select();
    var okFlag = false;
    try { okFlag = document.execCommand('copy'); } catch (e) { okFlag = false; }
    document.body.removeChild(ta);
    if (okFlag) { annotStatus('已复制到剪贴板，直接粘给我就行', 'ok'); return; }
    annotManualCopy(text);
}

/* 剪贴板被拒时的最后一条路：把全文摊在一个大框里，让用户自己 Ctrl+C */
function annotManualCopy(text) {
    var mask = document.createElement('div');
    mask.style.cssText = 'position:fixed;inset:0;background:rgba(17,24,39,.55);z-index:99;' +
        'display:flex;align-items:center;justify-content:center;padding:40px';
    var box = document.createElement('div');
    box.style.cssText = 'width:min(720px,100%);max-height:80vh;display:flex;flex-direction:column;' +
        'gap:8px;background:#fff;border-radius:12px;padding:14px';
    box.innerHTML = '<div style="font-size:13px;font-weight:600">剪贴板被浏览器拒了，按 Ctrl+C 复制下面这段</div>';
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'flex:1;min-height:300px;font:12px/1.5 Consolas,monospace;border:1px solid #e5e8ee;' +
        'border-radius:8px;padding:8px';
    var close = document.createElement('button');
    close.textContent = '复制好了，关闭';
    close.style.cssText = 'align-self:flex-end;height:30px;padding:0 14px;border:0;border-radius:7px;' +
        'background:#fe2c55;color:#fff;cursor:pointer;font:600 13px/1 inherit';
    close.addEventListener('click', function () { document.body.removeChild(mask); });
    box.appendChild(ta);
    box.appendChild(close);
    mask.appendChild(box);
    document.body.appendChild(mask);
    ta.focus();
    ta.select();
}

async function annotSave() {
    if (!annotItems.length) { annotStatus('还没有标注，先在左边框一个', 'err'); return; }
    var btn = document.getElementById('saveBtn');
    btn.disabled = true;
    annotStatus('保存中…');
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, 8000);
    try {
        var resp = await fetch('/api/annotations', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            signal: ctrl.signal,
            body: JSON.stringify({
                text: annotBuildMarkdown(),
                meta: {
                    url: annotWin ? annotWin.location.href : '',
                    viewport: annotWin ? { w: annotWin.innerWidth, h: annotWin.innerHeight, dpr: annotWin.devicePixelRatio || 1 } : {}
                },
                items: annotItems.map(function (it) {
                    return {
                        tool: it.tool,
                        region: it.region,
                        selector: it.selector,
                        note: it.note || it.text || '',
                        rect: it.rect,
                        scroller: it.scroller,
                        points: it.points
                    };
                })
            })
        });
        clearTimeout(timer);
        var j = await resp.json();
        if (!j || !j.ok) throw new Error((j && j.error) || ('HTTP ' + resp.status));
        annotStatus('已保存 ' + j.count + ' 条 → ' + j.dir + '\\' + j.file + '（跟我说一声「存好了」我就去读）', 'ok');
    } catch (e) {
        clearTimeout(timer);
        annotStatus('保存失败：' + (e && e.message ? e.message : e) + '　可先用「下载 .md」发给我', 'err');
    } finally {
        btn.disabled = false;
    }
}

function annotDownload(text) {
    var blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
    var a = document.createElement('a');
    var d = new Date();
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    a.download = '界面意见_' + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + '-' +
        pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds()) + '.md';
    a.href = URL.createObjectURL(blob);
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
    annotStatus('已下载，把文件拖进对话发我就行', 'ok');
}

/* ============================== 监听 ============================== */

function annotWatchScroll() {
    var sc1 = annotDoc.getElementById('wallScroll');
    var sc2 = annotDoc.querySelector('.content');
    if (sc1) sc1.addEventListener('scroll', annotScheduleDraw, { passive: true });
    if (sc2) sc2.addEventListener('scroll', annotScheduleDraw, { passive: true });
    annotWin.addEventListener('scroll', annotScheduleDraw, true);
}

/* 改星标/状态/等级/标签都会整体重建 #detailTop，旧坐标就对不上了 —— 提示一下重框 */
function annotWatchRerender() {
    if (!annotWin.MutationObserver) return;
    var targets = [annotDoc.getElementById('detailTop'), annotDoc.getElementById('workRail')];
    var obs = new annotWin.MutationObserver(function () {
        if (!annotItems.length) return;
        annotStale = true;
        var tip = document.getElementById('staleTip');
        if (tip) tip.classList.remove('hidden');
        annotScheduleDraw();
    });
    targets.forEach(function (t) {
        if (t) obs.observe(t, { childList: true, subtree: true });
    });
}

/* ============================== 探针（验收脚本用） ============================== */
function annotateReady() { return true; }

/* 供 shot2.py 之类的脚本直接读，别在页面里用 */
window.__annotDebug = function () {
    return {
        ready: annotReady,
        tool: annotTool,
        items: annotItems.map(function (it) {
            return { tool: it.tool, region: it.region, selector: it.selector, rect: it.rect, note: it.note };
        })
    };
};
