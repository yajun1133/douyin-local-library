// ===== 抖音下载浏览器 - 前端逻辑（按需加载版） =====

// 会被写回 user_data.json 的账号字段（服务端 merge_save_user_data 按字段合并）。
// 加新字段一定要同步加到这里：漏一个的表现是「界面改了、刷新后没了」——
// level（作者等级）就漏过一次，怎么设置都存不住。
const SAVE_FIELDS = [
    'nickname', 'alias', 'douyin_id', 'signature', 'sec_uid', 'status', 'level',
    'update_freq', 'starred', 'tags', 'note', 'cover', 'main_uid', 'alt_accounts', 'history',
];

// 排序字段默认方向（和 server.py 的 SORT_DEFAULT_DIR 对齐）：昵称/UID 正序，
// 数量/日期/等级默认「大的/新的在前」。
const SORT_DEFAULT_DIR = {
    nickname: 'asc', uid: 'asc',
    work_count: 'desc', video_count: 'desc', image_count: 'desc',
    latest: 'desc', earliest: 'desc', level: 'desc', added: 'desc',
};

// 更新正反序按钮的显示（当前生效方向）
function updateSortDirBtn() {
    const btn = document.getElementById('sortDirBtn');
    if (!btn) return;
    const eff = state.sortDir || SORT_DEFAULT_DIR[state.sortBy] || 'asc';
    btn.textContent = eff === 'asc' ? '↑ 正序' : '↓ 倒序';
}

// 封面显示方式：固定在 localStorage 的 coverMode（cover=填充裁切 / stretch=拉伸 / contain=原比例）。
// 2026-10-28：顶部那个「封面：填充」三档切换按钮去掉了 —— 定稿就是「填充」，不再需要切换。
// 这里保留读 localStorage 的能力，是为了尊重之前切换过的用户（值还在就照旧渲染）。

// 刷新当前界面：先把手上的改动落盘 → 重读annotations/统计 → 重拉第一页。
// 用在「数据是别的程序在后台写、页面已经开着」的场景（下载器刚跑完、刚在另一个
// 标签页改过标注），点一下就能同步，不用整页 F5（F5 会丢掉筛选面板的临时输入）。
async function refreshAuthorList() {
    const btn = document.getElementById('refreshBtn');
    if (btn) { btn.disabled = true; btn.textContent = '⟳ 刷新中'; }
    const beforeOffset = state.offset;
    try {
        // ① 本会话还没写完的标注必须先落盘，否则读回来的是旧数据，等于把刚点的星标刷掉
        try { await saveUserData(); } catch (e) {}
        const dirtyLeft = (state.dirtyAccounts && state.dirtyAccounts.size) || 0;
        // ② 重读标注与统计
        const [userResp, metaResp] = await Promise.all([
            fetch('user_data.json').then((r) => (r.ok ? r.json() : null)).catch(() => null),
            fetch('/api/meta').then((r) => (r.ok ? r.json() : null)).catch(() => null),
        ]);
        // 读失败 / 还有脏数据 → 保留内存里的旧状态。刷新绝不能把标注刷没
        // （2026-10-27 那次「537 条 → 4 条」就是空状态被当成真状态用）。
        if (userResp && userResp.accounts && !dirtyLeft) {
            state.userData = userResp;
            // 状态说明也在 user_data 顶层，一起换（别只换 accounts，否则说明栏还是旧的）
            state.statusNotes = (userResp.status_notes && typeof userResp.status_notes === 'object')
                ? userResp.status_notes : {};
        }
        if (metaResp && !dirtyLeft) state.meta = metaResp;
        // ③ 侧边栏计数、标签、年份、缓存体积跟着更新
        renderStats();
        renderCategoryList();
        renderTagCloud();
        renderYearList();
        refreshAltEntryCount();
        refreshStatusCounts();
        refreshCacheSize();
        // ④ 重拉第一页（保留筛选 / 搜索 / 排序）
        await loadAuthors(true);
        const c = document.querySelector('.content');
        if (c) c.scrollTop = 0;
        if (dirtyLeft) showToast('有 ' + dirtyLeft + ' 个改动没写进磁盘，已保留当前数据');
        else showToast(beforeOffset > state.pageSize
            ? '已刷新，回到第一页 · 共 ' + state.total + ' 个'
            : '已刷新 · 共 ' + state.total + ' 个');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '⟳ 刷新'; }
    }
}

const state = {
    userData: null,       // user_data.json 内容（用户标注）
    meta: null,           // /api/meta 聚合信息
    filtered: [],         // 当前筛选后的作者列表（分页加载）
    total: 0,             // 筛选后总数
    currentCategory: 'all',
    currentTag: null,
    currentYear: null,
    currentStatus: null, // 状态列筛选（'注销'/'禁言/封号'/...，null=不过滤）
    currentLevel: null,  // 等级筛选（'夯'/'顶级'/'一般'/'拉'，null=不过滤）
    currentFreq: null,   // 更新频率筛选（'day'/'3天'/'周'/'月'，null=不过滤）
    // 右上角筛选面板（独立于侧边栏，与分类/状态列/标签/年份叠加生效）
    // status/level/freq/cover 都是多选（数组），空数组 = 不限
    // latest：'' | '3d' | '7d' | '14d' | '30d' | '60d' | '365d' | 'custom'（自定义区间）
    // latestFrom / latestTo：自定义区间的起止日期（YYYY-MM-DD，可只填一端）
    // matchMap：每行左侧的圆钮 —— or=＋包含（默认）/ not=－排除（2026-11-05 用户口径）
    filters: {
        sec_uid: '', starred: '', watch: '', latest: '', latestFrom: '', latestTo: '',
        level: [], cover: [], status: [], freq: [],
        matchMap: { level: 'or', cover: 'or', status: 'or', freq: 'or', latest: 'or' },
    },
    searchQuery: '',
    searchScope: '',     // 搜索范围：''(全部) | nickname | note | signature | works
    // 每个账号状态自己写的一段说明（"我怎么理解这个状态"）：{ 状态名: 文本 }
    // 存 user_data.status_notes（顶层，和 accounts 平级），保存时按 key 增量上送
    statusNotes: {},
    dirtyStatusNotes: new Set(),   // 这次会话改过的状态名（只发这几个，防旧快照覆盖）
    workHits: [],        // 作品范围搜索的扁平化结果（每个作品一条，含作者信息）
    sortBy: 'nickname',
    sortDir: '',         // '' = 按字段默认方向；'asc' / 'desc' = 强制正/反序
    batchConflicts: [],   // 批量获取资料时「昵称/抖音号对不上」的待确认清单（存 localStorage）
    batchDone: [],        // 刚处理过的待确认账号（采用/忽略后挪到「已处理」，存 localStorage）
    batchResult: null,    // 最近一次批量的完整结果（冲突/补全/失败明细，点统计条要它）
    batchFilter: 'conflict',  // 批量页当前看哪一块：conflict | filled | failed | all
    offset: 0,
    pageSize: 60,
    loadSeq: 0,          // 列表请求序号：只认最后一次请求的响应（防旧响应覆盖新筛选）
    loading: false,
    done: false,
    currentAuthor: null,
    detailStack: [],      // 详情页内跳转历史：点小号进另一作者时压栈，返回时逐级回退
    listScrollTop: 0,     // 进入作者详情前，作者列表的滚动位置（返回时恢复）
    viewScroll: {},       // 各整页视图离开时的滚动位置（切走记住、切回恢复，不丢进度）
    view: 'authors',      // authors | alts | batch（内容区当前整页视图）
    returnView: 'authors',// 从哪个视图点进作者详情的（返回时回到这里）
    suppressViewTransition: false, // 详情页只是中间一步时，避免先播一次返回动画再播目标页动画
    altGroups: [],        // /api/alt_groups 的结果（一「组」一个文件夹）
    altGroupNames: {},    // 自定义文件夹名：{ 组key: 名字 }
    altOpen: {},          // 文件夹展开状态：{ 组key: false } 表示收起
    // 脏账号跟踪：本会话真正改过的 uid 才会随 /api/save 上送。
    // 背景（2026-10-27）：两个标签页同开时，旧标签页的全量保存会把已解除的
    // 大小号关联按旧快照写回磁盘——表现为「解除了但关联还在」。增量上送后，
    // 未修改的账号永远不会离开本机，旧快照不可能覆盖新数据。
    dirtyAccounts: new Set(),
    // uid -> { 字段名: true }：这次到底改了哪几个字段。
    // 只有这些字段会随保存上送。2026-11-08 之前是整条账号对象一起发，
    // 而 state.userData 是页面打开那一刻的快照 —— 旧标签页随便点一下星标，
    // 就会把当时那整条旧记录写回去，把别处改过的 status / update_freq 一起冲掉，
    // 表现为「状态和更新频率自己变了」。按字段发之后，没改过的字段永远不出本机。
    dirtyFields: {},
    dirtyVersion: {},     // uid -> 改动次数，用来判断「保存期间是否又被改过」
    dirtyMeta: false,     // alt_group_names（文件夹改名）等顶层字段的脏标记
    fillMode: (function () { try { return localStorage.getItem('mediaFill') === '1'; } catch (e) { return false; } })(),
    // 封面显示方式：cover=填充(裁切铺满) / stretch=拉伸变形 / contain=原比例(留白)
    coverMode: (function () { try { return localStorage.getItem('coverMode') || 'cover'; } catch (e) { return 'cover'; } })(),
    // 作者列表显示方式：grid = 网格卡片（默认）/ rows = 每作者一行，右边跟一行作品缩略图
    authorLayout: (function () {
        try { return localStorage.getItem('authorLayout') === 'rows' ? 'rows' : 'grid'; }
        catch (e) { return 'grid'; }
    })(),
    // 详情页视频放完之后怎么办：stop=放完就停（默认）/ next=继续下一个 / repeat=单曲重复 / shuffle=随机
    playMode: (function () {
        try {
            const v = localStorage.getItem('playMode') || '';
            return (v === 'next' || v === 'repeat' || v === 'shuffle') ? v : 'stop';
        } catch (e) { return 'stop'; }
    })(),
    // 不能设置更新频率的账号状态（来自设置：注销/禁言/封号/卖号等）
    updateFreqDisabledStatus: [],
};

// 「每作者一行」一行里铺几个作品缩略图（后端 works 参数上限 40）
const ROW_WORKS = 12;

function markDirty(uid, field) {
    if (!uid) return;
    uid = String(uid);
    state.dirtyAccounts.add(uid);
    if (field) {
        const fs = state.dirtyFields[uid] || (state.dirtyFields[uid] = {});
        fs[field] = true;
    }
    // 版本号：保存请求在途期间又改了同一个账号时，成功后不能把这个脏标记删掉，
    // 否则这期间的改动永远送不出去（并发保存丢改动的老问题）
    state.dirtyVersion[uid] = (state.dirtyVersion[uid] || 0) + 1;
}

// 一次改动涉及多个字段时用（比如批量采用资料会同时改昵称/抖音号/简介/历史）
function markDirtyFields(uid, fields) {
    if (!uid) return;
    uid = String(uid);
    state.dirtyAccounts.add(uid);
    const fs = state.dirtyFields[uid] || (state.dirtyFields[uid] = {});
    (fields || []).forEach((f) => { if (f) fs[f] = true; });
    state.dirtyVersion[uid] = (state.dirtyVersion[uid] || 0) + 1;
}

// ===== 初始化 =====
async function init() {
    try {
        // 只加载轻量的 user_data.json 和 meta，绝不加载 63MB data.json
        // ⚠️ user_data 加载失败必须直接失败，不能吞掉：state.userData 若是空对象，
        // 之后任何一次保存都会把磁盘上的全部标注覆盖没（2026-10-27 踩过：537 条 → 4 条）。
        const [userResp, metaResp] = await Promise.all([
            fetch('user_data.json').then(r => {
                if (!r.ok) throw new Error('user_data.json 加载失败 HTTP ' + r.status);
                return r.json();
            }),
            fetch('/api/meta').then(r => {
                if (!r.ok) throw new Error('meta 加载失败 HTTP ' + r.status);
                return r.json();
            }),
        ]);
        if (!userResp || typeof userResp !== 'object' || !userResp.accounts) {
            throw new Error('user_data.json 内容异常（缺 accounts）');
        }
        state.userData = userResp;
        state.meta = metaResp;
        // 状态说明（顶层字段，老数据没有就是空对象）
        state.statusNotes = (userResp.status_notes && typeof userResp.status_notes === 'object')
            ? userResp.status_notes : {};
        state.dirtyStatusNotes = new Set();

        // 用户设置（更新频率禁用状态 / 路径地址）。加载失败不致命，用默认空列表。
        try {
            const sResp = await fetch('/api/settings').then((r) => (r.ok ? r.json() : null));
            if (sResp && sResp.settings) {
                state.updateFreqDisabledStatus = sResp.settings.update_freq_disabled_status || [];
            }
        } catch (e) { /* 忽略 */ }

        // 大小号「组」自愈：把历史遗留的单向/不一致数据统一归一化成组内全连
        const fixedAlts = normalizeAltGroups();
        if (fixedAlts > 0) {
            console.info('[alt] 已归一化 ' + fixedAlts + ' 条大小号关系');
            saveUserData();
        }

        renderStats();
        renderCategoryList();   // 「全部作者 / 星标」的数量要在首屏就有，不能等第一次点星标
        renderTagCloud();
        renderYearList();
        updateSortDirBtn();     // 正反序按钮显示默认方向
        refreshAltEntryCount();
        refreshStatusCounts();
        refreshCacheSize();
        state.batchConflicts = loadBatchConflicts();
        state.batchDone = loadBatchDone();
        refreshBatchBadge();
        refreshBlacklistCount();
        updateExportBtn();
        bindFilterPanelEvents();
        syncLayoutBtn();        // 恢复上次选的显示方式（网格 / 每作者一行）
        await loadAuthors(true);

        // 注意：不要在这里再覆盖 #statInfo，renderStats() 已写入「共 N 作者 · M 星标」
        renderStats();

        // 支持 #author=<uid> 直接打开某位作者（刷新/收藏也落在同一个作者页）
        const m = location.hash.match(/^#author=(\d+)/);
        if (m) openAuthor(m[1]);
        // 老地址 /watch 现在会跳到 /#watch，在这里接住，落到主界面里的追更视图
        else if (location.hash === '#watch') {
            try { history.replaceState(null, '', location.pathname + location.search); } catch (e) { /* ignore */ }
            openWatchView();
        }

        // 批量获取资料：刷新或重开页面时，接着显示进度 / 补看上次跑完的结果
        fetch('/api/batch_status').then((r) => r.json()).then((s) => {
            applyBatchState(s);
            if (s.running) startBatchPolling();
            else if (s.unread) consumeBatchResult();
        }).catch(() => { /* 接口不在就算了，不影响浏览 */ });
    } catch (e) {
        console.error('加载失败', e);
        document.getElementById('statInfo').textContent = '加载失败，请确认已运行扫描';
        document.getElementById('authorGrid').innerHTML =
            '<div class="loading">数据加载失败：请先双击「start-browser.bat」或运行 scanner.py</div>';
    }
}

// ===== 作者列表（分页加载） =====
async function loadAuthors(reset) {
    // 作品范围搜索：走专门的接口，结果直接是作品卡片，不再列作者
    if (state.searchScope === 'works' && state.searchQuery) {
        return loadWorkHits(reset);
    }
    // 请求序号：快速切分类 / 连续改筛选时，旧请求必须能被丢弃。
    // 旧写法是 `if (state.loading) return`（直接丢掉新请求）——于是旧响应回来时
    // 用旧条件的数据填列表，而标题/高亮已是新条件，界面会一直错到下一次操作。
    if (reset) {
        state.loadSeq = (state.loadSeq || 0) + 1;
        state.offset = 0;
        state.done = false;
        // 不在这里清空 state.filtered：请求失败时保留旧列表，比白屏友好
    } else if (state.loading || state.done) {
        return;                     // 只有「追加下一页」才需要挡并发
    }
    const seq = state.loadSeq || 0;
    state.loading = true;

    const params = new URLSearchParams({
        offset: state.offset,
        // 行模式一屏只有 5~6 行、但每行带 12 张缩略图，一页拉 60 个作者会一次性
        // 铺 700 多张图（懒加载也顶不住）。所以行模式每页 20 个作者，滚到底再续。
        limit: state.authorLayout === 'rows' ? 20 : state.pageSize,
        sort: state.sortBy,
        cat: state.currentCategory,
    });
    // 每作者一行：顺带要该作者最近 N 个作品（缩略图）。网格卡片不需要，省一次拼装
    if (state.authorLayout === 'rows') params.set('works', String(ROW_WORKS));
    if (state.searchQuery) params.set('q', state.searchQuery);
    if (state.searchScope) params.set('scope', state.searchScope);
    if (state.currentTag) params.set('tag', state.currentTag);
    if (state.currentYear) params.set('year', state.currentYear);
    // 账号状态 / 等级 / 频率：侧边栏那列（单选）与筛选面板（多选）取并集。
    // 但侧边栏那列本质是「包含」语义：面板这一行如果是 －（排除），就不能把侧边栏的
    // 值并进去，否则「排除注销」会把侧边栏正选着的「注销」也一起排除，永远筛不到人。
    const mm = state.filters.matchMap || {};
    const mergeSide = (key, sideVal, arr) => {
        const set = new Set();
        if (mm[key] !== 'not' && sideVal) set.add(sideVal);
        (arr || []).forEach((v) => set.add(v));
        return Array.from(set);
    };
    const statusAll = mergeSide('status', state.currentStatus, state.filters.status);
    if (statusAll.length) params.set('status', statusAll.join(','));
    const levelAll = mergeSide('level', state.currentLevel, state.filters.level);
    if (levelAll.length) params.set('level', levelAll.join(','));
    const freqAll = mergeSide('freq', state.currentFreq, state.filters.freq);
    if (freqAll.length) params.set('freq', freqAll.join(','));
    // 每行自己的圆钮：match_x=not 表示这一行是「排除」；不传 = 包含（默认）
    ['level', 'status', 'freq', 'cover', 'latest'].forEach((k) => {
        if (mm[k] === 'not') params.set('match_' + k, 'not');
    });
    // 筛选面板：sec_uid 有/无、星标 是/否、封面（多选）
    if (state.filters.sec_uid) params.set('sec_uid', state.filters.sec_uid);
    if (state.filters.starred) params.set('starred', state.filters.starred);
    // 追更：yes / no（名单以 data/watch.json 为准，后端按 uid 判定）
    if (state.filters.watch) params.set('watch', state.filters.watch);
    const covers = state.filters.cover || [];
    if (covers.length) params.set('cover', covers.join(','));
    // 最后更新时间：预设区间（最近 N 天）或自定义起止日期
    const lat = state.filters.latest;
    if (lat === 'custom') {
        if (state.filters.latestFrom) params.set('latest_after', state.filters.latestFrom);
        if (state.filters.latestTo) params.set('latest_before', state.filters.latestTo);
    } else if (lat) {
        const days = parseInt(lat, 10);
        if (!isNaN(days)) {
            const d = new Date(Date.now() - days * 86400000);
            const iso = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
            params.set('latest_after', iso);
        }
    }
    // 排序方向：有显式 asc/desc 才传，否则用字段默认方向
    if (state.sortDir) params.set('dir', state.sortDir);

    try {
        const resp = await fetch('/api/authors?' + params.toString());
        const data = await resp.json();
        if (seq !== (state.loadSeq || 0)) return;    // 期间又切了筛选，丢掉这次响应
        if (!data || !Array.isArray(data.items)) throw new Error('返回格式异常');
        state.total = data.total || 0;
        updateViewCount();
        if (reset) state.filtered = data.items;
        else state.filtered = state.filtered.concat(data.items);
        state.offset += data.items.length;
        if (state.offset >= state.total) state.done = true;
        renderAuthorGrid();
    } catch (e) {
        if (seq === (state.loadSeq || 0)) {
            // 接口抖动不该弹「脚本出错」全屏红条（那是给真正的编程错误用的）
            console.error('加载作者列表失败', e);
            showToast('列表加载失败，稍后再试');
        }
    } finally {
        if (seq === (state.loadSeq || 0)) state.loading = false;
    }
}

// ===== 作品范围搜索：结果直接是作品卡片，不是作者 =====
async function loadWorkHits(reset) {
    if (reset) {
        state.loadSeq = (state.loadSeq || 0) + 1;
        state.offset = 0;
        state.done = false;
    } else if (state.loading || state.done) {
        return;
    }
    const seq = state.loadSeq || 0;
    state.loading = true;
    const params = new URLSearchParams({
        q: state.searchQuery,
        offset: state.offset,
        limit: state.pageSize,
    });
    try {
        const resp = await fetch('/api/search_works?' + params.toString());
        const data = await resp.json();
        if (seq !== (state.loadSeq || 0)) return;
        if (!data || !Array.isArray(data.items)) throw new Error('返回格式异常');
        state.total = data.total || 0;
        updateViewCount(true);   // 作品态显示「N 个作品」
        if (reset) state.workHits = data.items;
        else state.workHits = state.workHits.concat(data.items);
        state.offset += data.items.length;
        if (state.offset >= state.total) state.done = true;
        renderWorkHits();
    } catch (e) {
        if (seq === (state.loadSeq || 0)) {
            console.error('搜索作品失败', e);
            showToast('作品搜索失败，稍后再试');
        }
    } finally {
        if (seq === (state.loadSeq || 0)) state.loading = false;
    }
}

function renderWorkHits() {
    const grid = document.getElementById('authorGrid');
    // 作品搜索结果永远是卡片网格：撤掉「每作者一行」的两栏布局和右侧播放区，
    // 否则作品卡片会被纵向 flex 拉成一条一条、右边还留一块空播放器
    const wrap = document.getElementById('rowsWrap');
    if (wrap) wrap.classList.remove('rows-mode');
    const player = document.getElementById('rowsPlayer');
    if (player) player.classList.add('hidden');
    rowStopPlay();
    grid.classList.remove('rows-mode');
    // 这时「显示方式」按钮没意义（结果是作品不是作者），藏起来免得点了没反应
    const lb = document.getElementById('layoutBtn');
    if (lb) lb.classList.add('hidden');
    if (state.workHits.length === 0) {
        grid.innerHTML = '<div class="loading">没有匹配的作品</div>';
        document.getElementById('loadMore').classList.add('hidden');
        return;
    }
    grid.innerHTML = state.workHits.map(workHitCardHtml).join('');
    const loadMore = document.getElementById('loadMore');
    if (!state.done) {
        loadMore.classList.remove('hidden');
        loadMore.textContent = `已显示 ${state.workHits.length} / ${state.total}`;
    } else {
        loadMore.classList.add('hidden');
    }
    observeThumbs(grid);
}

function workHitCardHtml(hit) {
    const poster = hit.poster_is_image
        ? mediaUrl(hit.poster_rel)
        : (hit.poster_rel ? '/thumb?path=' + encodeURIComponent(hit.poster_rel) : '');
    const thumb = poster
        ? `<img class="work-hit-thumb" data-src="${poster}" onerror="thumbError(this)" alt="">`
        : `<div class="work-hit-thumb placeholder">🎬</div>`;
    const star = hit.starred ? '⭐' : '';
    const name = hit.alias || hit.nickname || '未命名账号';
    return `
    <div class="work-hit-card" data-uid="${escapeHtml(hit.uid)}" data-wi="${hit.work_index}" onclick="openWorkHit('${onClickArg(hit.uid)}', ${hit.work_index})">
        ${thumb}
        <div class="work-hit-info">
            <div class="work-hit-author">${star}${escapeHtml(name)}</div>
            <div class="work-hit-desc" title="${escapeHtml(hit.desc || '')}">${escapeHtml(hit.desc || '（无描述）')}</div>
            ${hit.date ? `<div class="work-hit-date">${escapeHtml(hit.date)}</div>` : ''}
        </div>
    </div>`;
}

// 点作品卡片 → 打开该作者主页，并定位到命中的那个作品
function openWorkHit(uid, workIndex) {
    openAuthor(uid, workIndex);
}

function renderAuthorGrid() {
    const grid = document.getElementById('authorGrid');
    const wrap = document.getElementById('rowsWrap');
    const player = document.getElementById('rowsPlayer');
    const rowsMode = state.authorLayout === 'rows';
    // 回到作者列表时，把「显示方式」按钮恢复出来（搜作品时被藏过）
    const lb = document.getElementById('layoutBtn');
    if (lb) lb.classList.remove('hidden');
    // 行模式：外层 rows-wrap 变成「左列表 + 右播放区」两栏，网格模式恢复普通块级
    if (wrap) wrap.classList.toggle('rows-mode', rowsMode);
    if (player) player.classList.toggle('hidden', !rowsMode);
    grid.classList.remove('rows-mode');
    if (state.filtered.length === 0) {
        grid.innerHTML = '<div class="loading">没有匹配的作者</div>';
        document.getElementById('loadMore').classList.add('hidden');
        return;
    }
    grid.innerHTML = state.filtered
        .map(rowsMode ? authorRowHtml : authorCardHtml)
        .join('');
    // 行模式的作品缩略图是 data-src 懒加载（一屏几十张，不懒加载会把硬盘读爆）
    if (rowsMode) {
        rowFitStrip(grid);      // 先按宽度定每行放几个（必须在插进 DOM 之后量）
        observeThumbs(grid);
        rowKeepActive();
    } else {
        rowStopPlay();          // 切回网格 / 换筛选：把播放器停掉，别在后台出声
    }

    const loadMore = document.getElementById('loadMore');
    if (!state.done) {
        loadMore.classList.remove('hidden');
        loadMore.textContent = `已显示 ${state.filtered.length} / ${state.total}`;
    } else {
        loadMore.classList.add('hidden');
    }
}

// ===== 显示方式切换：网格卡片 ⇄ 每作者一行 =====
function syncLayoutBtn() {
    const b = document.getElementById('layoutBtn');
    if (!b) return;
    const rows = state.authorLayout === 'rows';
    b.textContent = rows ? '▦ 网格卡片' : '▤ 每作者一行';
    b.classList.toggle('active', rows);
    b.title = rows
        ? '切回网格卡片（一张张作者卡片）'
        : '切换成「每作者一行」：一位作者一条表头，下面铺一行作品，点作品右边直接播';
}

function toggleAuthorLayout() {
    state.authorLayout = state.authorLayout === 'rows' ? 'grid' : 'rows';
    try { localStorage.setItem('authorLayout', state.authorLayout); } catch (e) { /* 隐私模式忽略 */ }
    syncLayoutBtn();
    // 行模式要带作品数据，网格不需要 → 直接重拉第一页（筛选/排序/搜索条件都保留）
    loadAuthors(true);
    const c = document.querySelector('.content');
    if (c) c.scrollTop = 0;
    showToast(state.authorLayout === 'rows' ? '已切换：每作者一行' : '已切换：网格卡片');
}

// 一个作者 = 一条表头 + 下面一行作品卡片（跟「新下载」栏目同一个长相）
function authorRowHtml(a) {
    const chips = [
        levelTagHtml(a.level),
        freqTagHtml(a.update_freq, a.status),
        statusTagHtml(a.status),
        ...sortTags(a.tags || []).map(t => `<span class="mini-tag">${escapeHtml(t)}</span>`),
    ].join('');
    const recent = a.recent || [];
    const n = recent.length;
    // 只渲染前 ROW_WORKS 张；实际显示几张由 rowFitStrip() 按容器宽度决定（不滚动）
    const cards = recent.map((w, ri) => {
        const src = w.is_image ? mediaUrl(w.rel) : '/thumb?path=' + encodeURIComponent(w.rel);
        const files = w.files || 1;
        const tip = [a.nickname || '', w.desc || '（无描述）', w.date || ''].filter(Boolean).join('\n')
            + (files > 1 ? `\n这个作品共 ${files} 个文件` : '')
            + '\n点一下在右边播放';
        return `<div class="ar-card" data-uid="${escapeHtml(a.uid)}" data-ri="${ri}" data-i="${ri}"
                     onclick="rowPlayCard(this)" title="${escapeHtml(tip)}">
            <div class="ar-thumb">
                <img data-src="${src}" alt="" onerror="thumbError(this)">
                ${w.is_image
                    ? `<span class="ar-multi">${files}</span>`
                    : (files > 1 ? `<span class="ar-multi">${files}</span>` : '')}
            </div>
            <div class="ar-time">${escapeHtml(String(w.date || '').slice(5) || '—')}</div>
        </div>`;
    }).join('');

    // 最后下载 / 多久没下载（用户 11-03 标注：表头要有这两个信息）
    const dlTs = Number(a.last_download) || 0;
    const dlDays = dlTs ? Math.floor((Date.now() / 1000 - dlTs) / 86400) : -1;
    const dlHtml = dlTs
        ? `<span class="ar-dl${dlDays > 30 ? ' old' : ''}"
                 title="最后一次下载：${tsText(dlTs, true)}（本地最新文件的写入时间）">⬇ ${daysAgoText(dlTs)}</span>`
        : '<span class="ar-dl none" title="下载记录里还没有这个作者的记录">⬇ 没下载过</span>';

    return `
    <div class="author-row" data-uid="${escapeHtml(a.uid)}">
        <div class="ar-head" onclick="openAuthor('${onClickArg(a.uid)}')" title="点击进入作者详情页">
            <span class="ar-dot"></span>
            <span class="ar-name">${a.starred ? '⭐' : ''}${a.watch ? '<span class="watch-chip" title="在追更名单里">追更</span>' : ''}${nameHtml(a)}</span>
            <span class="ar-chips">${chips}</span>
            <span class="ar-side">${dlHtml}<span class="ar-cnt">${a.work_count} 个作品</span></span>
        </div>
        <div class="ar-strip" data-n="${n}" data-extra="${a.recent_more || 0}"
             data-uid="${escapeHtml(a.uid)}">${cards}
            ${n ? '' : '<div class="ar-empty">本地还没有作品</div>'}
        </div>
    </div>`;
}

// 更新频率徽章：day / 3天 / 周 / 月（没设置就不显示，和状态徽章一个规矩）
function freqTagHtml(freq, status) {
    const v = String(freq || '').trim();
    if (!v) return '';
    // 注销 / 禁言封号 / 卖号 这类「不可能再更新」的账号，历史残留的频率值不显示，
    // 否则卡片上会出现「注销 · 日更」这种自相矛盾的组合（详情页也是同一口径）
    const st = String(status || '').trim();
    if (st && (state.updateFreqDisabledStatus || []).indexOf(st) >= 0) return '';
    // 原样显示 day / 3天 / 周 / 月（用户 2026-11-03：「day 就是 day，不要写成日更」）
    return `<span class="mini-tag freq-tag" title="更新频率：${escapeHtml(v)}">${escapeHtml(v)}</span>`;
}

// ===== 行模式右侧播放区（点左边作品就地播放，不跳作者页） =====
const rowPlay = { uid: '', ri: null, nick: '', rel: '', isImage: false, images: [], cur: 0,
                  date: '', desc: '', files: 1, token: 0 };

function rowStageEl() { return document.getElementById('rowStage'); }
function rowFootEl() { return document.getElementById('rowFoot'); }

function rowSetEmpty(html) {
    const st = rowStageEl();
    if (!st) return;
    st.innerHTML = html;
    const f = rowFootEl();
    if (f) f.innerHTML = '';
}

function rowStopPlay() {
    rowPlay.token = (rowPlay.token || 0) + 1;
    rowPlay.uid = '';
    rowPlay.ri = null;
    rowPlay.rel = '';
    rowPlay.images = [];
    rowPlay.cur = 0;
    const st = rowStageEl();
    if (st && st.querySelector('video')) {
        try { st.querySelector('video').pause(); } catch (e) { /* 忽略 */ }
    }
    if (st) st.innerHTML = '<div class="rp-empty">点左边的作品直接播放</div>';
    const f = rowFootEl();
    if (f) f.innerHTML = '';
    rowClearActive();
}

function rowClearActive() {
    document.querySelectorAll('#authorGrid .ar-card.active')
        .forEach((c) => c.classList.remove('active'));
}

// 重绘列表后把正在播的那张卡重新点亮（按 uid + recent 下标定位，
// 别用 img 的 data-src —— 懒加载成功后那个属性会被删掉，匹配不上）
function rowKeepActive() {
    if (!rowPlay.uid || rowPlay.ri == null) return;
    const card = document.querySelector(
        `#authorGrid .ar-card[data-uid="${CSS.escape(String(rowPlay.uid))}"][data-ri="${rowPlay.ri}"]`);
    if (card) card.classList.add('active');
}

function rowPlayCard(el) {
    const card = el.closest ? el.closest('.ar-card') : el;
    if (!card) return;
    const uid = card.dataset.uid || '';
    const ri = Number(card.dataset.ri);
    const a = (state.filtered || []).find((x) => String(x.uid) === String(uid));
    const w = a && (a.recent || [])[ri];
    if (!w) return;
    rowClearActive();
    card.classList.add('active');
    rowPlay.token = (rowPlay.token || 0) + 1;
    const tk = rowPlay.token;
    Object.assign(rowPlay, {
        uid, ri, nick: (a.alias || a.nickname || ''), rel: w.rel, isImage: !!w.is_image,
        images: (w.images && w.images.length) ? w.images : (w.is_image ? [w.rel] : []),
        cur: 0, date: w.date || '', desc: w.desc || '', files: w.files || 1,
    });
    rowRenderFoot(a, w);
    if (w.is_image) rowRenderImage(tk);
    else rowPlayVideo(w.rel, tk);
}

async function rowPlayVideo(rel, tk) {
    const st = rowStageEl();
    if (!st) return;
    st.innerHTML = '<div class="rp-empty">正在检测视频编码…</div>';
    const info = await probeVideo(rel);
    if (tk !== rowPlay.token) return;

    // 取源顺序**必须和详情页一致**（2026-11-08 用户报「行模式播不了 HEVC」的根因）：
    //   ① probe 里带 url → 以前转过码了，直接用缓存。原来这里只看 playable，缓存过的文件
    //      playable 会变成 true，于是又回去播原片 HEVC → 黑屏，看着就像"不支持 HEVC"。
    //   ② 浏览器自己解得动（H.264，或装了系统 HEVC 扩展的 HEVC）→ 播原片，原画质零等待。
    //   ③ 解不动 → 转码成 H.264，转完以后走 ① 秒开。
    const cached = info.url || '';
    const needTranscode = !cached && info.playable === false && !canPlayHevc();
    const playedNative = !cached && !needTranscode && info.playable === false;
    let src = cached || mediaUrl(rel);

    if (needTranscode) {
        st.innerHTML = '<div class="rp-empty">这个视频浏览器放不了，正在转码…<br><span id="rpPct">0%</span></div>';
        const r = await transcodeVideo(rel, (pct) => {
            const el = document.getElementById('rpPct');
            if (el) el.textContent = pct >= 0 ? pct + '%' : '处理中…';
        }, () => tk !== rowPlay.token);
        if (tk !== rowPlay.token) return;
        if (!r.ok) {
            st.innerHTML = `<div class="rp-empty">转码失败：${escapeHtml(r.error || '未知原因')}<br>
                <span style="font-size:12px">可以直接打开作者页用原文件播放</span></div>`;
            return;
        }
        src = r.url;
    }
    if (tk !== rowPlay.token) return;
    rowMountVideo(src, rel, playedNative);
}

// 往右侧播放区挂一个 <video>。playedNative=true 时挂两道兜底：
// 解不出来（onerror）或 5 秒还拿不到元数据（既不报错也不加载）都自动退回转码，
// 否则用户只会看到一个永远黑屏、没有任何提示的播放器。详情页早就有这两道，行模式漏了。
function rowMountVideo(src, rel, playedNative) {
    const st = rowStageEl();
    if (!st) return;
    const tk = rowPlay.token;
    const poster = '/thumb?path=' + encodeURIComponent(rel);
    st.innerHTML = `<video src="${src}" poster="${poster}" controls autoplay playsinline preload="metadata"></video>`;
    const v = st.querySelector('video');
    if (!v) return;
    // autoplay 在部分策略下会被忽略（没有用户手势记录时），显式再 play 一次；
    // 失败就静默留着控制条，别弹错误打断浏览
    v.play().catch(() => {});
    if (!playedNative) return;
    let retried = false;
    const retry = () => {
        if (retried) return;
        retried = true;
        rowRetryTranscode(rel, tk);
    };
    v.addEventListener('error', retry, { once: true });
    setTimeout(() => {
        if (tk !== rowPlay.token) return;
        if (st.querySelector('video') === v && v.readyState === 0 && !v.error) retry();
    }, 5000);
}

// 原生解码失败后的兜底：转成 H.264 再播（和详情页 retryWithTranscode 同一口径）
async function rowRetryTranscode(rel, tk) {
    if (tk !== rowPlay.token) return;
    // 立刻换一个 token：onerror 和 5 秒超时可能同时触发，只让第一个真正跑
    rowPlay.token = (rowPlay.token || 0) + 1;
    const tk2 = rowPlay.token;
    const st = rowStageEl();
    if (!st) return;
    st.innerHTML = '<div class="rp-empty">这个视频浏览器解不出来，正在转成 H.264…<br><span id="rpPct">…</span></div>';
    const r = await transcodeVideo(rel, (pct) => {
        const el = document.getElementById('rpPct');
        if (el) el.textContent = pct >= 0 ? pct + '%' : '处理中…';
    }, () => tk2 !== rowPlay.token);
    if (tk2 !== rowPlay.token) return;
    if (!r.ok) {
        st.innerHTML = `<div class="rp-empty">转码失败：${escapeHtml(r.error || '未知原因')}<br>
            <span style="font-size:12px">可以直接打开作者页用原文件播放</span></div>`;
        return;
    }
    rowMountVideo(r.url, rel, false);
}

function rowRenderImage(tk) {
    const st = rowStageEl();
    if (!st) return;
    const list = rowPlay.images || [];
    const i = Math.max(0, Math.min(rowPlay.cur || 0, list.length - 1));
    const rel = list[i];
    st.innerHTML = `
        <img class="rp-img" src="${mediaUrl(rel)}" alt="">
        ${list.length > 1 ? `<div class="rp-img-nav">
            <button class="btn btn-nav" onclick="rowStepImage(-1)" ${i <= 0 ? 'disabled' : ''}>‹</button>
            <span class="rp-pos">${i + 1}/${list.length}</span>
            <button class="btn btn-nav" onclick="rowStepImage(1)" ${i >= list.length - 1 ? 'disabled' : ''}>›</button>
        </div>` : ''}`;
}

function rowStepImage(dir) {
    const list = rowPlay.images || [];
    if (list.length < 2) return;
    const next = Math.max(0, Math.min((rowPlay.cur || 0) + dir, list.length - 1));
    if (next === rowPlay.cur) return;
    rowPlay.cur = next;
    rowRenderImage(rowPlay.token);
}

function rowRenderFoot(a, w) {
    const f = rowFootEl();
    if (!f) return;
    const n = w.files || 1;
    f.innerHTML = `
        <div class="rp-title" title="${escapeHtml(w.desc || '')}">${escapeHtml(w.desc || '（无描述）')}</div>
        <div class="rp-meta">
            <span>${escapeHtml(w.date || '')}</span>
            ${n > 1 ? `<span>· ${n} 个文件</span>` : ''}
            <span>· ${escapeHtml(a.nickname || '')}</span>
        </div>
        <button class="btn btn-mini rp-open" onclick="rowOpenAuthor()">打开作者页 →</button>`;
}

function rowOpenAuthor() {
    if (rowPlay.uid) openAuthor(rowPlay.uid);
}

// 一行固定放几张：按可用宽度算，**绝不左右滑动**（用户 2026-11-03 标注：
// 「这里不要可以左右滑动的，固定一定数量的作品，不够就是空白」）。
// 作品少于列数时就空着——grid 的固定列数天生留白，不用额外补占位元素。
const ROW_CARD_MIN = 112;   // 卡片最窄宽度（再窄封面看不清）
const ROW_GAP = 8;
function rowStripCols(width) {
    if (!width || width < 120) return 8;
    return Math.max(3, Math.min(12, Math.floor((width + ROW_GAP) / (ROW_CARD_MIN + ROW_GAP))));
}
function rowFitStrip(grid) {
    const strips = grid.querySelectorAll('.ar-strip');
    if (!strips.length) return;
    // 每行等宽，量第一行就够（量不到说明视图藏着，用 grid 宽度兜底）
    const W = strips[0].clientWidth || grid.clientWidth || 0;
    const cols = rowStripCols(W);
    grid.style.setProperty('--ar-cols', cols);
    strips.forEach((strip) => {
        strip.querySelectorAll('.ar-card').forEach((c) => {
            const i = Number(c.dataset.i) || 0;
            c.classList.toggle('hidden', i >= cols);
        });
    });
}

// 窗口变宽变窄（含收起侧边栏）要重算列数，否则会留一大片空白或者挤成一团
let rowFitTimer = 0;
window.addEventListener('resize', () => {
    if (state.authorLayout !== 'rows') return;
    clearTimeout(rowFitTimer);
    rowFitTimer = setTimeout(() => {
        const g = document.getElementById('authorGrid');
        if (g) rowFitStrip(g);
    }, 180);
});

// ===== 显示名：别名优先，原名跟在后面 =====
// user_data.accounts[uid].alias 是用户自己起的名字；没起就还显示昵称。
function nameParts(o) {
    const alias = String((o && o.alias) || '').trim();
    const nick = String((o && o.nickname) || '').trim();
    return { alias, nick: nick || (alias ? '' : '未命名账号') };
}

// 纯文本名（confirm / textContent / title 用）
function plainName(o) {
    const p = nameParts(o);
    return p.alias || p.nick || '未命名账号';
}

// 带样式的名字：有别名时「别名 + 灰色原名」
function nameHtml(o, withOrigin) {
    const p = nameParts(o);
    if (!p.alias) return escapeHtml(p.nick);
    const origin = (p.nick && withOrigin !== false)
        ? `<span class="name-origin" title="原昵称">${escapeHtml(p.nick)}</span>`
        : '';
    return `<span class="name-alias">${escapeHtml(p.alias)}</span>${origin}`;
}

// 账号状态配色：正常绿 / 私密蓝 / 封号红 / 停更黄 / 卖号紫 / 注销深灰 / 不好看了青。
// 没标记（空字符串）不显示徽章——「正常」才绿，不设置就是干净的一张卡。
const STATUS_STYLE = {
    '正常': 'normal',
    '私密账号': 'private',
    '注销': 'gone',
    '禁言/封号': 'banned',
    '停更/偶更': 'stale',
    '卖号': 'selling',
    '不好看了': 'bored',
    '待修改': 'todo',
};

function statusKey(status) {
    const s = String(status || '').trim();
    if (!s) return '';
    if (STATUS_STYLE[s]) return STATUS_STYLE[s];
    // 老数据里的废弃选项（封号 / 停更 / 改名 / 弃用…）按关键字兜底，别都掉成灰色
    if (/封号|封禁|禁言/.test(s)) return 'banned';
    if (/停更/.test(s)) return 'stale';
    if (/注销/.test(s)) return 'gone';
    if (/卖号/.test(s)) return 'selling';
    if (/正常/.test(s)) return 'normal';
    if (/私密/.test(s)) return 'private';
    if (/不好看/.test(s)) return 'bored';
    if (/待修改|待改|要改/.test(s)) return 'todo';
    return 'other';
}

function statusTagHtml(status) {
    const key = statusKey(status);
    if (!key) return '';                       // 没标记就不标
    return `<span class="mini-tag st-${key}">${escapeHtml(String(status).trim())}</span>`;
}

// 作者等级徽章（夯=红/顶级=紫/一般=蓝/拉=灰），空则不显示
function levelTagHtml(level) {
    const v = String(level || '').trim();
    if (!v) return '';
    const key = levelKey(v);
    const cls = key === 'hang' ? 'lv-hang' : (key === 'top' ? 'lv-top' : (key === 'lv1' ? 'lv-lv1' : (key === 'lv2' ? 'lv-lv2' : 'lv-other')));
    return `<span class="mini-tag ${cls}">${escapeHtml(v)}</span>`;
}

function authorCardHtml(a) {
    // 标签全量显示（后端 2026-11-02 起不再只给 3 个）
    const tags = sortTags(a.tags || []).map(t => `<span class="mini-tag">${escapeHtml(t)}</span>`).join('');
    const statusBadge = statusTagHtml(a.status);
    // 等级 / 更新频率：以前只有详情页和收藏列表有，主页卡片看不到，
    // 扫一遍列表判断不出这个号多久更一次，2026-11-02 加到卡片上
    const levelBadge = levelTagHtml(a.level);
    const freqBadge = freqTagHtml(a.update_freq, a.status);
    const star = a.starred ? '⭐' : '';
    // 「多久没下载过」：30 天以上标提醒色，扫一眼就知道哪些该重新抓了
    const dlTs = Number(a.last_download) || 0;
    const dlDays = dlTs ? Math.floor((Date.now() / 1000 - dlTs) / 86400) : -1;
    const dlHtml = dlTs
        ? `<span class="dl-ago${dlDays > 30 ? ' old' : ''}" title="最后一次下载：${tsText(dlTs, true)}（文件写入时间）"> · ⬇ ${daysAgoText(dlTs)}</span>`
        : '';
    const cover = coverUrlFrom(a.cover);
    const coverHtml = cover
        ? `<img class="author-cover cm-${state.coverMode}" src="${cover}" loading="lazy" alt="" onerror="coverBroken(this)">`
        : `<div class="author-cover placeholder cm-${state.coverMode}" style="display:flex;align-items:center;justify-content:center;font-size:32px">📱</div>`;
    // 追更标记：左上角一枚小角标。判据是后端 /api/authors 逐条带的 watch 字段
    // （直接来自 data/watch.json 名单，按 uid 认人），前端不用再单独请求一次名单。
    const watchBadge = a.watch
        ? `<span class="watch-badge" title="在追更名单里：这位作者发新作品会自动下载">追更</span>`
        : '';
    return `
    <div class="author-card${a.watch ? ' is-watch' : ''}" data-uid="${escapeHtml(a.uid)}" onclick="openAuthor('${onClickArg(a.uid)}')">
        ${watchBadge}
        ${coverHtml}
        <div class="author-info">
            <div class="author-name">${star}${nameHtml(a)}</div>
            <div class="author-meta"${a.latest ? ` title="已下载作品：${a.work_count || 0} 个，最新一条发布于 ${escapeHtml(String(a.latest).slice(0, 10))}"` : ''}>${a.work_count} 作品${a.latest ? ` · 最新 ${escapeHtml(String(a.latest).slice(0, 10))}` : ''}${dlHtml}</div>
            <div class="author-tags">${statusBadge}${levelBadge}${freqBadge}${tags}</div>
        </div>
    </div>`;
}

// ===== 渲染统计 =====
function renderStats() {
    const m = state.meta || {};
    const el = document.getElementById('statInfo');
    if (!el) return;
    // 顶栏统计改成分段胶囊（纯展示，只读 /api/meta，不写任何数据）
    const tagCount = Object.keys(m.tags || {}).length;
    const yearCount = Object.keys(m.years || {}).length;
    el.innerHTML = [
        ['作者', m.total_authors || 0],
        ['星标', m.starred || 0],
        ['标签', tagCount],
        ['年份', yearCount],
    ].map(([k, v]) => `<span class="stat-cell"><i>${k}</i><b>${v}</b></span>`).join('');
}

// 在标题旁显示当前视图的账号数量（实时：loadAuthors 拿到 total 后更新）
function updateViewCount(isWorks) {
    const el = document.getElementById('viewCount');
    if (!el) return;
    const n = state.total;
    el.textContent = isWorks ? `· ${n} 个作品` : `· ${n} 个作者`;
}

// 设置标题文字（保留后面的数量 span，避免 textContent 把 viewCount 清掉）
function setViewTitle(text) {
    const h = document.getElementById('viewTitle');
    if (!h) return;
    const span = document.getElementById('viewCount');
    if (span) {
        h.textContent = '';
        h.appendChild(document.createTextNode(text + ' '));
        h.appendChild(span);
    } else {
        h.textContent = text;
    }
}

// 内联事件属性里的字符串参数（onclick="foo('…')"）。
// 只做 escapeHtml 是不够的——它把 ' 变成 &#39;，HTML 解析时又还原成 '，
// 等于没转义，还会把 onclick 里的 JS 字符串截断。
function onClickArg(s) {
    return escapeHtml(String(s == null ? '' : s)
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'")
        .replace(/[\r\n]+/g, ' '));
}

// ===== 侧边栏 =====
// 2026-10-28 重排：侧边栏分三栏 —— 功能(#featureList：获取资料/缓存) /
// 分类(#categoryList：全部作者、星标、大小号、7 个账号状态) / 等级(#levelList：夯/顶级/一般/拉)。
// 高亮是**全局唯一**的（三栏里同时只能有一个亮），所以清理/判断一律走下面两个 helper，
// 不要再单独 querySelector 某一个 ul —— 重排前就是只清 #featureList，
// 点完等级再点分类时等级那栏还亮着。
function sideItems() {
    return document.querySelectorAll(
        '#featureList .side-item, #categoryList .side-item, #levelList .side-item, #freqList .side-item');
}

function clearSideActive() {
    sideItems().forEach((i) => i.classList.remove('active'));
}

function renderCategoryList() {
    const m = state.meta || {};
    document.querySelectorAll('#categoryList .side-item').forEach(item => {
        const cat = item.dataset.category;
        if (cat === 'all' || cat === 'starred') {
            const count = cat === 'all' ? (m.total_authors || 0) : (m.starred || 0);
            let c = item.querySelector('.count');
            if (!c) { c = document.createElement('span'); c.className = 'count'; item.appendChild(c); }
            c.textContent = count;
        }
    });
}

function renderTagCloud() {
    const tags = Object.keys((state.meta && state.meta.tags) || {}).sort();
    const container = document.getElementById('tagCloud');
    if (tags.length === 0) {
        container.innerHTML = '<span class="tag-hint">暂无标签，点作者详情的「＋标签」添加</span>';
        return;
    }
    container.innerHTML = tags.map(t =>
        `<span class="tag-chip" data-tag="${escapeHtml(t)}">${escapeHtml(t)} (${state.meta.tags[t]})</span>`
    ).join('');
}

function renderYearList() {
    const years = Object.keys((state.meta && state.meta.years) || {}).sort().reverse();
    const container = document.getElementById('yearList');
    container.innerHTML = years.map(y =>
        `<li class="side-item" data-year="${y}">${y} 年 <span class="count">${state.meta.years[y]}</span></li>`
    ).join('');
}

// ===== 视图转场 =====
// 进入详情向右进入、返回列表向左进入；普通整页切换只淡入。
// 不再让「恢复列表滚动位置」触发平滑滚动，否则返回会误显示成自上而下的滑动。
const VIEW_ENTER_CLASSES = ['view-enter-forward', 'view-enter-back', 'view-enter-neutral'];
function animateViewEnter(viewId, direction = 'neutral') {
    const el = document.getElementById(viewId);
    if (!el || el.classList.contains('hidden')) return;
    el.classList.remove(...VIEW_ENTER_CLASSES);
    // 强制重新计算，连续打开/返回同一个视图时也能重新播放
    void el.offsetWidth;
    const cls = direction === 'forward' ? 'view-enter-forward'
        : direction === 'back' ? 'view-enter-back' : 'view-enter-neutral';
    el.classList.add(cls);
    const clear = () => el.classList.remove(cls);
    el.addEventListener('animationend', clear, { once: true });
    setTimeout(clear, 280);
}

// ===== 筛选 =====
// 清掉标签 / 年份筛选（含侧边栏高亮）。
// 切分类、点状态列、点等级时都要调：否则标题显示「全部作者」，params 里 tag/year
// 还在，列表仍然是子集，侧边栏高亮也和内容对不上。
function clearTagYearFilter() {
    state.currentTag = null;
    state.currentYear = null;
    document.querySelectorAll('#tagCloud .tag-chip').forEach(c => c.classList.remove('active'));
    document.querySelectorAll('#yearList .side-item').forEach(i => i.classList.remove('active'));
}

async function applyFilters() {
    // 改筛选条件 = 回到作者列表（且回到顶部，不恢复旧位置）
    state.detailStack = [];     // 清空详情页跳转历史，强制回列表而不是回上一个作者
    const cameFromDetail = isDetailOpen();
    if (cameFromDetail) {
        state.suppressViewTransition = true;
        closeDetail();
    }
    showAuthorsView();
    if (cameFromDetail) animateViewEnter('authorView', 'back');
    state.returnView = 'authors';
    state.listScrollTop = 0;
    updateStatusNoteBar();      // 状态说明栏跟着当前状态显示 / 隐藏
    updateExportBtn();
    await loadAuthors(true);
    const c = document.querySelector('.content');
    if (c) c.scrollTop = 0;
}

function showAuthorsView() {
    rememberViewScroll();   // 记住离开的那个视图的滚动（切回来还能恢复）
    state.view = 'authors';
    hideAllViews();
    document.getElementById('authorView').classList.remove('hidden');
    // 只清「功能」栏（批量获取资料的高亮），**不能** clearSideActive()：
    // 本函数由 applyFilters() 调用，而 applyFilters 是点分类/等级/状态之后触发的，
    // 那时候高亮刚被点亮的那个设好，这里再全清就等于「点了没反应」。
    document.querySelectorAll('#featureList .side-item').forEach(i => i.classList.remove('active'));
}

// ===== 作者详情页（替换内容区：上方作者信息，下方左预览列表 / 右播放区） =====
const detailState = {
    uid: null,
    author: null,
    filter: 'video',   // all | video | image | image_single | image_multi
    workDir: 'desc',   // 作品排序方向：desc=最新在前（默认），asc=最早在前
    list: [],          // 当前筛选下，works 里的索引数组
    index: -1,         // 当前选中的 work 索引
    imgIndex: 0,       // 当前图文作品里看到第几张
    vidIndex: 0,       // 当前纯视频作品里看到第几段（一个作品下成 _1.mp4 _2.mp4 时能翻段）
    secEdit: false,    // SEC_UID 是否处于展开编辑状态
    histOpen: false,   // 历史记录面板是否展开
    aliasEdit: false,  // 别名是否处于编辑状态
    tagOpen: false,    // 标签下拉是否展开
    tagQuery: '',      // 标签下拉里的搜索词（重绘后保留）
    selectMode: false, // 作品多选删除模式
    selected: new Set(), // 选中的作品下标
    token: 0,          // 打开请求序号：连点两个作者时只认最后一次
};

async function openAuthor(uid, workIndex, transition = 'forward') {
    const token = ++detailState.token;
    // 已经在详情页里再点进另一个作者（比如点小号）→ 把当前作者压栈，
    // 返回时先回到上一个作者，而不是直接跳回列表主页
    if (isDetailOpen() && detailState.uid && detailState.uid !== uid) {
        // 去重：同一个作者在栈里只保留一次，避免 A→B→A→B 来回点时无限叠加、
        // 返回时反复回到已经进过的页面
        const i = state.detailStack.indexOf(detailState.uid);
        if (i >= 0) state.detailStack.splice(i, 1);
        state.detailStack.push(detailState.uid);
    } else if (!isDetailOpen()) {
        // 从列表点进来时记住当前滚动位置，返回时回到这里
        const listScroller = document.querySelector('.content');
        state.listScrollTop = listScroller ? listScroller.scrollTop : 0;
        // 从「大小号」文件夹点进来，返回时回大小号，而不是全部作者
        const inAlts = document.getElementById('altView') && !document.getElementById('altView').classList.contains('hidden');
        const inBatch = document.getElementById('batchView') && !document.getElementById('batchView').classList.contains('hidden');
        // 从收藏结果点进来时 returnView 已被 openFavAuthor 预设为 favorites（收藏视图此时已隐藏，用 state.view 兜底）
        const inFav = state.view === 'favorites' || (document.getElementById('favoritesView') && !document.getElementById('favoritesView').classList.contains('hidden'));
        // 从「导出作者信息」的预览列表点进来 → 返回时回到导出页（筛选和预览结果都保留）
        const inExport = state.view === 'export' || (document.getElementById('exportView') && !document.getElementById('exportView').classList.contains('hidden'));
        // 从「新下载」点作者名进来 → 返回时回新下载页（不重新请求，保留那批结果和滚动位置）
        const inNew = state.view === 'new' || (document.getElementById('newView') && !document.getElementById('newView').classList.contains('hidden'));
        // 从「追更」名单点进来 → 返回时回追更页（筛选档位和滚动位置都还在）
        const inWatch = state.view === 'watch' || (document.getElementById('watchView') && !document.getElementById('watchView').classList.contains('hidden'));
        // 「追更下载」也借用 newView，但它必须先于 inNew 判 —— 否则返回会掉到「新下载」
        state.returnView = state.view === 'watchDl' ? 'watchDl'
            : (inExport ? 'export' : (inWatch ? 'watch' : (inNew ? 'new' : (inBatch ? 'batch' : (inAlts ? 'alts' : (inFav ? 'favorites' : 'authors'))))));
    }
    document.getElementById('authorView').classList.add('hidden');
    document.getElementById('altView').classList.add('hidden');
    const ev0 = document.getElementById('exportView');
    if (ev0) ev0.classList.add('hidden');
    const bv0 = document.getElementById('batchView');
    if (bv0) bv0.classList.add('hidden');
    const nv0 = document.getElementById('newView');
    if (nv0) nv0.classList.add('hidden');
    const wv0 = document.getElementById('watchView');
    if (wv0) wv0.classList.add('hidden');
    // 从这里点进作者详情后，新下载页的常驻播放器要停掉（不然它藏在后面继续放声音）
    if (typeof ndPlayReset === 'function') ndPlayReset();
    document.getElementById('detailView').classList.remove('hidden');
    animateViewEnter('detailView', transition === 'back' ? 'back' : 'forward');
    const ws = document.getElementById('wallScroll');
    if (ws) ws.scrollTop = 0;
    // 详情页的回顶按钮在返回栏里，浮动按钮只在列表态用，进来就收掉
    const floatTop = document.getElementById('backTopBtn');
    if (floatTop) floatTop.classList.add('hidden');
    document.getElementById('detailBarTitle').textContent = '';
    document.getElementById('detailTop').innerHTML = '<div class="loading">加载中...</div>';
    document.getElementById('workRail').innerHTML = '';
    document.getElementById('playerStage').innerHTML = '<div class="player-empty">加载中...</div>';

    try {
        const resp = await fetch('/api/author?uid=' + encodeURIComponent(uid));
        const a = await resp.json();
        // 连点两个作者时，慢的那个回来不能把快的覆盖掉——否则页面是 A、
        // 用户以为在看 B，之后所有写操作（状态/标签/备注）都落到 A 上
        if (token !== detailState.token) return;
        if (a.error) {
            document.getElementById('detailTop').innerHTML = '<div class="loading">作者不存在</div>';
            return;
        }
        // 合并用户标注
        // 昵称要特殊处理：注销/封号后抖音会把昵称改成「用户+数字」这种系统默认名，
        // 批量取资料抄进 user_data 之后就一路盖到详情页标题上，等于认不出是谁。
        // 服务端 /api/author 已经优先给了文件夹里的真名，这里不能被 user_data 的默认名盖回去。
        const ud = ((state.userData || {}).accounts || {})[uid];
        if (ud) {
            const _keepNick = a.nickname;
            Object.assign(a, ud);
            if (/^用户\d+$/.test(String(ud.nickname || '').trim()) && _keepNick) {
                a.nickname = _keepNick;
            }
        }
        detailState.uid = uid;
        detailState.author = a;
        detailState.index = -1;
        // 换作者：上一位的多选状态和日期区间不能带过来（日期范围差得远，容易误删）
        if (detailState.selectMode) exitWorkSelect();
        const _dpFrom = document.getElementById('dpFrom');
        const _dpTo = document.getElementById('dpTo');
        if (_dpFrom) _dpFrom.value = '';
        if (_dpTo) _dpTo.value = '';
        // 上一个作者展开的下拉/编辑态不要带过来
        detailState.histOpen = false;
        detailState.tagOpen = false;
        detailState.tagQuery = '';
        detailState.aliasEdit = false;
        detailState.secEdit = false;
        detailState.imgIndex = 0;
        state.currentAuthor = a;
        try { history.replaceState(null, '', '#author=' + uid); } catch (e) { /* file:// 下可能不支持 */ }
        renderDetailTop();
        ensureLatestOnline(uid);     // 后台联网查最新发布时间，回来就地刷新那一行
        if (typeof workIndex === 'number' && workIndex >= 0 && workIndex < (a.works || []).length) {
            // 从作品搜索结果点进来 → 显示全部作品并定位到命中的那个
            setRailFilter('all', false);
            playWork(workIndex);
            // 让左侧卡片墙滚到定位作品附近
            const target = document.querySelector(`#workRail .wall-card[data-idx="${workIndex}"]`);
            if (target && target.scrollIntoView) {
                target.scrollIntoView({ block: 'nearest' });
            }
        } else {
            setRailFilter('video', false);   // 只列出来，不自动播放
            showStagePlaceholder();
        }
    } catch (e) {
        if (token === detailState.token) {
            document.getElementById('detailTop').innerHTML = '<div class="loading">加载失败</div>';
        }
    }
}

// 删一个账号时，光发 {uid: null} 不够：别人 alt_accounts 里还留着他，
// 服务端 normalize_alt_groups 会把「被引用的空 uid」重新补成 {} 空壳，
// 结果组里多出一个「未命名账号」。所以这里把同组其他人的列表一起改掉。
function accountDeletePayload(uid) {
    const acc = (state.userData && state.userData.accounts) || {};
    const key = String(uid);
    const patch = { [key]: null };
    const self = acc[key] || {};
    const members = new Set((self.alt_accounts || []).map(String));
    // 反向补一遍：别人把我列进 alt 的也算同组
    Object.keys(acc).forEach((k) => {
        const alts = (acc[k] && acc[k].alt_accounts) || [];
        if (alts.some((x) => String(x) === key)) members.add(k);
    });
    members.delete(key);
    members.forEach((m) => {
        const a = acc[m] || {};
        const rest = (a.alt_accounts || []).map(String).filter((x) => x !== key && x !== m);
        const p = { alt_accounts: rest.length ? rest : null };  // null = 删掉这个字段
        if (a.main_uid && String(a.main_uid) === key) p.main_uid = null;
        patch[m] = p;
    });
    return patch;
}

// 删除作者：从 user_data 里删掉这条主页 + 自动加入黑名单。
// 本地已下载的文件**不动**（要删文件去「文件管理」，那是另一套回收站流程）。
async function deleteAuthor(uid) {
    if (!uid) return;
    const acc = (state.userData && state.userData.accounts && state.userData.accounts[uid]) || {};
    const cur = (state.currentAuthor && state.currentAuthor.uid === uid) ? state.currentAuthor : {};
    const name = acc.nickname || cur.nickname || uid;
    const sec = acc.sec_uid || cur.sec_uid || '';
    let works = 0;
    try { works = workStatsOf(cur).work_count || 0; } catch (e) { works = cur.work_count || 0; }

    const msg = '删除作者「' + name + '」？\n\n'
        + '· 从作者列表里移除（user_data 里这条记录会被删掉）\n'
        + '· 自动加入黑名单（以后抓取/下载都会跳过这位博主）\n'
        + (works ? ('· 本地已有的 ' + works + ' 个作品文件不会被删除\n') : '')
        + '\n确定删除吗？';
    if (!confirm(msg)) return;

    // ① 先加黑名单：这一步失败就整体中止，避免「主页删了、却没进黑名单」
    try {
        const rb = await fetch('/api/blacklist', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'add', sec_uid: sec, uid: uid, nickname: name, reason: '删除作者' })
        });
        const db = await rb.json().catch(() => ({}));
        if (!rb.ok || !db.ok) {
            showToast('加入黑名单失败：' + (db.error || ('HTTP ' + rb.status)) + '（未删除）');
            return;
        }
    } catch (e) {
        showToast('加入黑名单失败：' + (e && e.message || e) + '（未删除）');
        return;
    }

    // ② 再删主页条目（null = 整条删掉）
    try {
        const rs = await fetch('/api/save', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ accounts: accountDeletePayload(uid) })
        });
        const ds = await rs.json().catch(() => ({}));
        if (!rs.ok || !ds.ok) {
            refreshBlacklistCount();
            showToast('主页删除失败：' + (ds.error || ('HTTP ' + rs.status)) + '（已加入黑名单，可到黑名单页手动核对）');
            return;
        }
    } catch (e) {
        refreshBlacklistCount();
        showToast('主页删除失败：' + (e && e.message || e) + '（已加入黑名单）');
        return;
    }

    // ③ 本地状态清理 + 回列表刷新
    try {
        if (state.userData && state.userData.accounts) delete state.userData.accounts[uid];
    } catch (e) { /* ignore */ }
    // 地址栏里可能还挂着 #author=<uid>，不清掉的话刷新会去找一个已经不存在的作者
    try { history.replaceState(null, '', location.pathname + location.search); } catch (e) { /* ignore */ }
    try { backToList(); } catch (e) { /* ignore */ }
    try { await loadAuthors(true); } catch (e) { /* ignore */ }
    try { refreshBlacklistCount(); } catch (e) { /* ignore */ }
    // ④ 顺手退出追更名单：人都不在库里了，留着只会让守护进程每小时白查一次
    try {
        await fetch('/api/watch', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'remove', uids: [String(uid)] })
        });
    } catch (e) { /* 名单没清掉也不影响删除本身 */ }
    showToast('已删除作者「' + name + '」，并加入黑名单');
}

function closeDetail() {
    // 赏图浮层是盖在详情页上的，详情页关了它还留着会挡住列表
    if (!document.getElementById('galleryView').classList.contains('hidden')) closeGallery();
    if (detailState.selectMode) exitWorkSelect();
    // 详情页里曾跳转过别的作者（点小号/关联成员）→ 返回上一个作者，而不是直接回列表
    if (state.detailStack.length) {
        const prev = state.detailStack.pop();
        detailState.uid = null;    // 先清空，避免 openAuthor 又把当前作者压回栈
        openAuthor(prev, undefined, 'back');
        return;
    }
    backToList();
}

// 强制彻底关闭详情页（清空跳转历史，直接回列表）。用于从详情页切到整页视图
// （设置/收藏/export/黑名单/大小号/批量）时：不能用 closeDetail()，否则点过小号后
// 栈非空会「返回上一个作者」——详情页还开着，再显示目标视图就两个页面叠一起了。
function forceCloseDetail() {
    state.detailStack = [];
    if (isDetailOpen()) {
        state.suppressViewTransition = true;
        closeDetail();
    }
}

// 直接回到进入详情前的列表视图（跳过详情页内跳转历史，清空返回栈）
function backToList() {
    if (detailState.selectMode) exitWorkSelect();
    state.detailStack = [];
    const shouldAnimate = !state.suppressViewTransition;
    state.suppressViewTransition = false;
    document.getElementById('detailView').classList.add('hidden');
    let targetViewId = 'authorView';
    // 从「大小号」文件夹或批量页进来的，返回原来的整页视图
    if (state.returnView === 'batch') {
        targetViewId = 'batchView';
        document.getElementById('authorView').classList.add('hidden');
        document.getElementById('altView').classList.add('hidden');
        document.getElementById('batchView').classList.remove('hidden');
        renderBatchPanel();
        refreshBatchView();
    } else if (state.returnView === 'alts') {
        targetViewId = 'altView';
        document.getElementById('authorView').classList.add('hidden');
        document.getElementById('altView').classList.remove('hidden');
        loadAltGroups();
    } else if (state.returnView === 'favorites') {
        // 从「获取最近收藏」结果点进作者详情，返回时回到收藏结果页（保留已抓取结果和滚动进度）
        targetViewId = 'favoritesView';
        document.getElementById('authorView').classList.add('hidden');
        document.getElementById('favoritesView').classList.remove('hidden');
        clearSideActive();
        document.querySelectorAll('#featureList .side-item').forEach(i => i.classList.toggle('active', i.dataset.action === 'fetchFavorites'));
        renderFavOpts();
        renderFavoritesState();
        // 收藏视图的滚动位置在 openFavAuthor 里单独记了（openAuthor 读 .content 时收藏视图已隐藏）
        state.listScrollTop = state.viewScroll['favorites'] || 0;
    } else if (state.returnView === 'export') {
        // 从导出页预览列表点进详情：返回时原样回导出页。
        // 注意不能走 openExportView()——那会重置筛选、清空预览列表和已选 uid
        targetViewId = 'exportView';
        document.getElementById('authorView').classList.add('hidden');
        document.getElementById('exportView').classList.remove('hidden');
        state.view = 'export';
        state.returnView = 'export';
        clearSideActive();
        document.querySelectorAll('#featureList .side-item').forEach(i => i.classList.toggle('active', i.dataset.action === 'exportAuthors'));
        const eb2 = document.getElementById('exportBtn');
        if (eb2) eb2.classList.add('hidden');
        state.listScrollTop = state.viewScroll['export'] || 0;
    } else if (state.returnView === 'watchDl') {
        // 从「追更下载」点作者进详情：返回时仍停在追更下载（数据和滚动都在内存里，不重新请求）
        targetViewId = 'newView';
        hideAllViews();
        document.getElementById('newView').classList.remove('hidden');
        clearSideActive();
        document.querySelectorAll('#featureList .side-item').forEach(i => i.classList.toggle('active', i.dataset.action === 'watch'));
        const ebw = document.getElementById('exportBtn');
        if (ebw) ebw.classList.add('hidden');
        applyNdWatchMode(true);
        state.listScrollTop = state.viewScroll['watchDl'] || 0;
    } else if (state.returnView === 'new') {
        // 从「新下载」点作者名进详情：返回时还回新下载页（不重新请求，保留原来那批数据和滚动位置）
        targetViewId = 'newView';
        hideAllViews();
        document.getElementById('newView').classList.remove('hidden');
        clearSideActive();
        document.querySelectorAll('#featureList .side-item').forEach(i => i.classList.toggle('active', i.dataset.action === 'newDownloads'));
        const ebn = document.getElementById('exportBtn');
        if (ebn) ebn.classList.add('hidden');
        state.listScrollTop = state.viewScroll['new'] || 0;
    } else if (state.returnView === 'watch') {
        // 从「追更」名单点作者进详情：返回时回追更页，档位筛选和滚动位置都保留
        targetViewId = 'watchView';
        hideAllViews();
        document.getElementById('watchView').classList.remove('hidden');
        state.view = 'watch';
        state.returnView = 'watch';
        clearSideActive();
        document.querySelectorAll('#featureList .side-item').forEach(i => i.classList.toggle('active', i.dataset.action === 'watch'));
        const ebw = document.getElementById('exportBtn');
        if (ebw) ebw.classList.add('hidden');
        state.listScrollTop = state.viewScroll['watch'] || 0;
    } else {
        hideAllViews();
        document.getElementById('authorView').classList.remove('hidden');
    }
    if (shouldAnimate) animateViewEnter(targetViewId, 'back');
    // 清空播放器，彻底停止播放
    document.getElementById('playerStage').innerHTML = '';
    document.getElementById('workRail').innerHTML = '';
    const ws = document.getElementById('wallScroll');
    if (ws) ws.scrollTop = 0;
    // 回到点进作者时的位置（列表 DOM 还在，直接恢复即可；必须瞬时恢复，不做滚动动画）
    const c = document.querySelector('.content');
    if (c) {
        const top = state.listScrollTop || 0;
        c.scrollTop = top;
        // 布局稳定后再兜一次（部分浏览器切换 display 后首帧高度还没算好）
        const raf = window.requestAnimationFrame || ((fn) => setTimeout(fn, 16));
        raf(() => { c.scrollTop = top; });
    }
    detailState.uid = null;
    detailState.author = null;
    detailState.list = [];
    detailState.index = -1;
    state.currentAuthor = null;
    closeAltPicker();
    try { history.replaceState(null, '', location.pathname + location.search); } catch (e) { /* 忽略 */ }
}

function isDetailOpen() {
    const el = document.getElementById('detailView');
    return !!el && !el.classList.contains('hidden');
}

// 抖音上的当前作品数：本地 vs 抖音的差值一眼可见
// （aweme_count 只算对方公开可见的作品，所以「本地多」通常意味着对方删了/设了私密）
function remoteStatHtml(a, m) {
    const rc = typeof a.remote_count === 'number' ? a.remote_count : null;
    const local = (m && m.work_count) || 0;
    let diff = '';
    if (rc !== null) {
        const d = local - rc;
        if (d > 0) diff = `<i class="stat-diff minus" title="本地比抖音多，通常是对方删了或设为私密">本地多 ${d}</i>`;
        else if (d < 0) diff = `<i class="stat-diff plus" title="抖音上比本地多，说明有没下载的新作品">新增 ${-d}</i>`;
    }
    // 时间口径：**优先用联网查到的真实发布时间**（/api/latest_work，走 a_bogus 签名打 aweme/post）。
    // 查不到时才退化：先看下载器作品表（xlsx）和本地最新谁更新（旧快照可能是一年前的，
    // 直接显示会出现「本地 2026-10-28、抖音却 2025-11-03」这种倒挂），最后才用取数时间。
    const live = String(a.remote_live || '').trim();        // "YYYY-MM-DD HH:MM"
    const liveAt = String(a.remote_live_at || '').trim();
    const pubRaw = String(a.remote_latest || '').trim();
    const crawl = String(a.remote_crawl || '').trim();
    const localLatest = String((m && m.latest) || '').trim().slice(0, 10);
    const localOk = localLatest && localLatest !== '?';
    const newerFromTable = pubRaw && (!localOk || pubRaw > localLatest);
    const pub = live ? live.slice(0, 10)
        : (newerFromTable ? pubRaw : (localOk ? localLatest : pubRaw));
    const at = String(a.remote_at || '').trim();
    const atShort = at ? at.slice(0, 16) : '';
    let when = '';
    if (live) {
        // 本地最新比抖音主页还新 = 那条作品在抖音上已经看不到了（对方删了/设私密/仅好友可见），
        // 不是我们算错。有的作者就是这种情况（本地 09-16，主页只剩 09-11）。
        const goneNewer = localOk && localLatest > live.slice(0, 10);
        const tip = '联网查的抖音最新作品发布时间：' + live
            + (liveAt ? '（查于 ' + liveAt + '）' : '')
            + (goneNewer ? `。本地还有一条 ${localLatest} 的，抖音主页上已经看不到 —— 对方删了或设了私密` : '');
        when = `<i class="stat-at live${goneNewer ? ' gone' : ''}" title="${escapeHtml(tip)}">发布 ${escapeHtml(live.slice(0, 10))}</i>`;
    } else if (pub) {
        const tip = newerFromTable
            ? `还没联网查过。下载器作品表里最新一条发布于 ${pubRaw}${crawl ? '（采集于 ' + crawl + '）' : ''}，比本地新 —— 可能有还没下载的作品；点右边 ↻ 联网确认`
            : `还没联网查过。这里显示的是本地已下载作品的最新发布时间${pubRaw && crawl ? '（作品表采集于 ' + crawl + '，比本地旧，不作数）' : ''}；点右边 ↻ 联网查`;
        when = `<i class="stat-at" title="${escapeHtml(tip)}">发布 ${escapeHtml(pub)}</i>`;
    } else if (atShort) {
        when = `<i class="stat-at" title="还没联网查过，这里显示的是数据获取时间">获取 ${escapeHtml(atShort)}</i>`;
    }
    return `<span class="stat-remote" id="statRemote" title="抖音当前公开可见的作品数${at ? '（' + escapeHtml(at) + ' 取数）' : ''}">
        <span>抖音 <b>${rc === null ? '—' : rc}</b>${when}</span>${diff}
    </span>`;
}

async function refreshWorkCount(uid, silent, force) {
    const el = document.getElementById('statRemote');
    if (el) el.classList.add('loading');
    // 数据条最右端那颗 ↻ 也转起来（转的只是动画，不能连点）
    const rf = document.getElementById('statRefreshBtn');
    if (rf) { rf.classList.add('loading'); rf.disabled = true; }
    try {
        // 顺手把「最新作品发布时间」也联网查一遍（两个请求，都只打一次接口）。
        // force=true 只在你手点 ↻ 时传：绕过服务端 6 小时缓存，真的重查一次。
        fetch('/api/latest_work?uid=' + encodeURIComponent(uid) + (force ? '&force=1' : '')).then((x) => x.json()).then((lv) => {
            if (!lv || !lv.ok) return;
            if (state.currentAuthor && state.currentAuthor.uid === uid) {
                state.currentAuthor.remote_live = lv.latest;
                state.currentAuthor.remote_live_at = lv.at;
            }
            if (isDetailOpen() && detailState.author && detailState.author.uid === uid) {
                detailState.author.remote_live = lv.latest;
                detailState.author.remote_live_at = lv.at;
                renderDetailTop();
            }
        }).catch(() => {});
        const r = await fetch('/api/work_count?uid=' + encodeURIComponent(uid)).then((x) => x.json());
        if (!r.ok) { if (!silent) showToast('读不到作品数：' + (r.error || '未知原因')); return; }
        if (state.currentAuthor && state.currentAuthor.uid === uid) {
            state.currentAuthor.remote_count = r.count;
            state.currentAuthor.remote_at = r.at;
            if (r.remote_latest) state.currentAuthor.remote_latest = r.remote_latest;
            if (r.remote_crawl) state.currentAuthor.remote_crawl = r.remote_crawl;
        }
        if (isDetailOpen() && detailState.author && detailState.author.uid === uid) {
            detailState.author.remote_count = r.count;
            detailState.author.remote_at = r.at;
            renderDetailTop();
        }
        if (!silent) showToast(`抖音当前 ${r.count} 篇（${r.source === 'login' ? '登录态' : '公开接口'}）`);
    } catch (e) {
        if (!silent) showToast('读不到作品数：' + e.message);
    } finally {
        const el2 = document.getElementById('statRemote');
        if (el2) el2.classList.remove('loading');
        // 中途 renderDetailTop() 会把按钮重建一遍，所以按 id 重新取，不能用上面那个引用
        const rf2 = document.getElementById('statRefreshBtn');
        if (rf2) { rf2.classList.remove('loading'); rf2.disabled = false; }
    }
}

// 打开详情页时顺手联网查一次「最新作品发布时间」：缓存 6 小时内不重复打接口。
// 查询要 1~2 秒（3.12 子进程 + 签名），所以异步刷新，不挡页面渲染。
const _latestTried = {};
async function ensureLatestOnline(uid) {
    const a = detailState.author;
    if (!a || a.uid !== uid || !a.sec_uid) return;
    const at = String(a.remote_live_at || '');
    const ts = at ? Date.parse(at.replace(' ', 'T')) : 0;
    const fresh = a.remote_live && ts && (Date.now() - ts < 6 * 3600 * 1000);
    if (fresh || _latestTried[uid]) return;
    _latestTried[uid] = 1;
    try {
        const r = await fetch('/api/latest_work?uid=' + encodeURIComponent(uid)).then((x) => x.json());
        if (!r || !r.ok) return;
        if (detailState.author && detailState.author.uid === uid) {
            detailState.author.remote_live = r.latest;
            detailState.author.remote_live_at = r.at;
            if (state.currentAuthor && state.currentAuthor.uid === uid) {
                state.currentAuthor.remote_live = r.latest;
                state.currentAuthor.remote_live_at = r.at;
            }
            renderDetailTop();
        }
    } catch (e) { /* 查不到就保持原样，不打扰用户 */ }
}

// ===== 自动追更（作者详情页最下面那条横栏）=====
// 状态只存在追更名单 data/watch.json 里，**不进 user_data 标注**：
// 所以这里刻意不走 setAccountField / markDirty，否则按钮状态会被写进标注文件，
// 跟追更面板里的真名单对不上。
// 打开作者时 /api/author 会把 watching / watch_enabled / watch_interval /
// watch_next / watch_downloads 一起带回来，所以刷新页面、从列表/大小号/收藏
// 换条路点进来，状态都是对的，不用前端另外问一次。
const WATCH_FREQS = [[0, '自动'], [900, '15 分钟'], [1800, '30 分钟'], [3600, '1 小时']];

function watchNextText(a) {
    if (!a.watching) return '';
    if (a.watch_enabled === false) return '追更已暂停';
    const t = Number(a.watch_next || 0);
    if (!t) return '等下一轮检查';
    const ms = t * 1000 - Date.now();
    if (ms <= 0) return '马上检查';
    const min = Math.round(ms / 60000);
    if (min < 60) return '约 ' + min + ' 分钟后检查';
    const h = min / 60;
    return '约 ' + (h >= 10 ? Math.round(h) : h.toFixed(1)) + ' 小时后检查';
}

function watchBarHtml(a) {
    const inList = !!a.watching;
    const on = inList && a.watch_enabled !== false;
    const iv = Number(a.watch_interval || 0);
    // 没 SEC_UID 的作者：追更了也下载不了。按钮照旧可点（点了会明确说原因），
    // 但提示语先讲清楚，别让人以为是坏了。
    const noSec = !inList && !String(a.sec_uid || '').trim();
    const label = on ? '追更中' : (inList ? '已暂停' : '未追更');
    const tip = noSec
        ? '这位作者还没有 SEC_UID：先在 SEC_UID 那一栏填上，才能加入追更'
        : (on ? '正在追更：发新作品会自动下载。点一下取消'
            : (inList ? '追更已暂停（在追更面板里关掉的）。点一下恢复'
                : '加入追更：定时检查这位作者，发现新作品自动下载'));
    const chips = WATCH_FREQS.map(function (f) {
        const v = f[0], lb = f[1];
        return '<span class="wf-chip' + (iv === v ? ' active' : '') + (on ? '' : ' disabled') + '"'
            + ' data-iv="' + v + '"'
            + ' title="' + (on ? '检查间隔改成 ' + lb : '先打开追更，才能设间隔') + '"'
            + ' onclick="setWatchInterval(\'' + a.uid + '\', ' + v + ')">' + lb + '</span>';
    }).join('');
    const dl = Number(a.watch_downloads || 0);
    const meta = [watchNextText(a), dl ? '已自动下载 ' + dl + ' 个' : ''].filter(Boolean).join(' · ');
    return '<div class="watch-bar' + (on ? ' on' : '') + (inList && !on ? ' paused' : '') + '" id="watchBar">'
        + '<span class="wb-title">📡 自动追更</span>'
        + '<button class="watch-switch' + (on ? ' on' : '') + '" id="watchSwitch" type="button"'
        + ' onclick="toggleWatch(\'' + a.uid + '\')" title="' + tip + '"'
        + ' aria-pressed="' + (on ? 'true' : 'false') + '">'
        + '<span class="ws-dot"></span>' + label + '</button>'
        + '<span class="wb-freq"><i>间隔</i>' + chips + '</span>'
        + (meta ? '<span class="wb-meta">' + escapeHtml(meta) + '</span>' : '')
        + (inList ? '' : '<span class="wb-hint">追更后：发现新作品自动存进这个作者的文件夹，不需要 DouK，也不用你点</span>')
        + '</div>';
}

// 只换这一整条，不整块重绘 detailTop —— 重绘会把滚动位置、
// 正在播的视频、展开的历史浮层全冲掉。
function paintWatchBar() {
    const a = detailState.author;
    const old = document.getElementById('watchBar');
    if (!a || !old) return;
    const box = document.createElement('div');
    box.innerHTML = watchBarHtml(a);
    const fresh = box.firstElementChild;
    if (fresh) old.replaceWith(fresh);
}

// 失败时恢复可点；成功时按钮已被 paintWatchBar 换成新的，不用管
function _watchBtnUnbusy() {
    const b = document.getElementById('watchSwitch');
    if (b) { b.classList.remove('busy'); b.disabled = false; }
}

async function toggleWatch(uid) {
    const a = detailState.author;
    if (!a || String(a.uid) !== String(uid)) return;
    const btn = document.getElementById('watchSwitch');
    if (!btn || btn.classList.contains('busy')) return;   // 连点保护：别一秒点出三条名单
    const wasOn = !!(a.watching && a.watch_enabled !== false);
    btn.classList.add('busy');
    btn.disabled = true;
    try {
        const r = await fetch('/api/watch', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                action: 'mark', uid: String(uid), enabled: !wasOn,
                sec_uid: a.sec_uid || '', nickname: a.nickname || '',
                // 重新点亮时沿用原来选的间隔，别偷偷退回自动分级
                interval: wasOn ? null : Number(a.watch_interval || 0),
            }),
        });
        const d = await r.json().catch(function () { return {}; });
        if (!r.ok || !d.ok) {
            showToast('追更失败：' + (d.error || ('HTTP ' + r.status)));
            _watchBtnUnbusy();
            return;
        }
        a.watching = !!d.watching;
        a.watch_enabled = !!d.watching;
        if (d.watching) a.watch_next = 0;   // 下一轮排期由服务端现算，先别显示旧值
        paintWatchBar();
        showToast(d.watching
            ? (d.added ? ('已加入追更，名单共 ' + (d.count || 1) + ' 位作者') : '已恢复追更')
            : '已取消追更');
    } catch (e) {
        showToast('追更失败：' + (e && e.message ? e.message : e));
        _watchBtnUnbusy();
    }
}

async function setWatchInterval(uid, iv) {
    const a = detailState.author;
    if (!a || String(a.uid) !== String(uid)) return;
    if (!(a.watching && a.watch_enabled !== false)) {
        showToast('先打开追更，再选检查间隔');
        return;
    }
    const f = WATCH_FREQS.filter(function (x) { return x[0] === Number(iv); })[0];
    try {
        const r = await fetch('/api/watch', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                action: 'mark', uid: String(uid), enabled: true,
                sec_uid: a.sec_uid || '', nickname: a.nickname || '',
                interval: Number(iv) || 0,
            }),
        });
        const d = await r.json().catch(function () { return {}; });
        if (!r.ok || !d.ok) { showToast('设置间隔失败：' + (d.error || ('HTTP ' + r.status))); return; }
        a.watch_interval = Number(iv) || 0;
        paintWatchBar();
        showToast('检查间隔已改成「' + (f ? f[1] : '自动') + '」');
    } catch (e) {
        showToast('设置间隔失败：' + (e && e.message ? e.message : e));
    }
}

// ① 上方区块：作者信息 + 状态/大小号/标签/备注/历史 + 作品筛选
function renderDetailTop() {
    const a = detailState.author;
    if (!a) return;
    // 统计按本地实际作品现算：scanner 的 stats 口径和筛选不一致（带动的帖算进了视频），
    // 现算才能保证「视频 + 图文 = 作品数」，和左栏筛选点出来的条数对得上。
    const m = workStatsOf(a);
    // 返回栏标题也用别名（有别名就显示别名）
    document.getElementById('detailBarTitle').textContent = plainName(a);

    const cover = getCoverUrl(a);
    const coverHtml = cover
        ? `<img class="detail-cover" src="${cover}" onerror="coverBroken(this)">`
        : `<div class="detail-cover placeholder" style="display:flex;align-items:center;justify-content:center;font-size:40px">📱</div>`;

    // ---- 名字：自己起的别名优先，原昵称跟在后面；✎ 随时改 ----
    const aliasBox = detailState.aliasEdit ? `
        <input class="inline-input alias-input" id="aliasInput" placeholder="起个名字（留空 = 只用原昵称）"
               value="${escapeHtml(a.alias || '')}"
               onkeydown="if(event.key==='Enter')saveAlias(this.value);if(event.key==='Escape')cancelAliasEdit()">
        <button class="btn btn-mini" onclick="saveAlias(document.getElementById('aliasInput').value)">保存</button>
        <button class="btn btn-mini" onclick="cancelAliasEdit()">取消</button>
    ` : `
        ${nameHtml(a)}
        <a class="name-edit" title="给这个账号起个名字（列表和大小号里都显示这个名字）" onclick="startAliasEdit()">✎</a>
    `;

    const altArr = a.alt_accounts || [];
    const altItems = altLabels(a);
    const mainUid = a.main_uid;   // 这组的大号（设大号时整组 main_uid 一致）
    const alts = altItems.map(it =>
        `<span class="alt-pill" title="${escapeHtml(it.name || '未命名')}${it.origin ? '（昵称：' + escapeHtml(it.origin) + '）' : ''} · UID ${it.uid} · 点击打开"><a onclick="openAuthor('${it.uid}')">${escapeHtml(it.label)}${it.uid === mainUid ? '<b class="main-badge">大号</b>' : ''}</a>${it.origin ? `<i class="alt-origin">${escapeHtml(it.origin)}</i>` : ''}</span>`).join('');

    // 历史记录：不再占一个格子，改成「按钮 + 浮层」。按钮上直接写有几条，
    // 点开才渲染列表（都能编辑/删除），收起时不占用任何高度。
    const histArr = a.history || [];
    const histTotal = histArr.length;
    const histReversed = histArr.slice().reverse();
    const historyRows = histReversed.map((h, di) => {
        const realIdx = histTotal - 1 - di;   // 数组真实下标（展示是倒序）
        return `<div class="history-item">
            <span class="history-time">${h.time || ''}</span>
            <span class="history-field">${fieldLabel(h.field)}</span>
            <span class="history-change">${escapeHtml(h.from || '')}${h.from ? ' → ' : ''}${escapeHtml(h.to || '')}</span>
            <span class="hist-ops">
                <a title="编辑" onclick="editHistoryEntry(${realIdx})">✎</a>
                <a title="删除" onclick="delHistoryEntry(${realIdx})">×</a>
            </span>
        </div>`;
    }).join('');

    // 已加的标签：× 用 data-tag 传值（标签里可能有引号，内联字符串会炸）
    const tagList = sortTags(a.tags).map(t =>
        `<span class="tag-chip active" title="${escapeHtml(t)}">${escapeHtml(t)}<span class="tag-x" data-tag="${escapeHtml(t)}" title="移除这个标签" onclick="removeTag(this.dataset.tag)">×</span></span>`
    ).join('');

    // 状态选项：'' = 未设置（默认，正常账号不用管，出问题才改）
    const statusList = ['', '正常', '私密账号', '注销', '禁言/封号', '停更/偶更', '卖号', '不好看了', '待修改'];
    // 老选项名 → 新选项名（不映射的话下拉里会同时出现「停更」和「停更/偶更」）
    // 2026-10-31：'停更/偶尔更' 缩短成 '停更/偶更'，老data/老backup/老 CSV 读进来时在这里翻译
    const statusAlias = { '停更': '停更/偶更', '停更/偶尔更': '停更/偶更' };
    const curStatus = statusAlias[a.status] || a.status || '';
    // 历史值兜底：老数据里可能有已废弃的选项（封号/改名/弃用），补进列表别让选中态丢失
    if (curStatus && statusList.indexOf(curStatus) < 0) statusList.push(curStatus);
    const statusOptions = statusList.map(s =>
        `<option value="${s}" ${curStatus === s ? 'selected' : ''}>${s || '未设置'}</option>`
    ).join('');

    // 作者等级：'夯 / 顶级 / 一般 / 拉'，空 = 未设置。
    // 做成和更新频率一样的胶囊标签（点哪个亮哪个，再点取消）
    const curLevel = normLevel(a.level);
    const levelChips = LEVEL_LIST.map((v) =>
        `<span class="level-chip lv-${levelKey(v)}${curLevel === v ? ' active' : ''}" data-level="${v}" title="作者等级：${v}" onclick="setLevel('${a.uid}', '${v}')">${v}</span>`
    ).join('');

    // 更新频率：day / 3天 / 周 / 月，做成「多选样式的标签」，点哪个亮哪个（单选语义）
    // 账号状态在「禁用更新频率」列表里（注销/封号/卖号等）时不显示可点的 chips，只给一句提示。
    // 2026-11-08 起频率值不再被清空（以前改状态就静默删值），所以这里要把存着的值
    // 明确写出来 —— 否则用户会以为「设过的频率没了」。
    const curFreq = String(a.update_freq || '').trim();
    const _freqDisabled = (state.updateFreqDisabledStatus || []).indexOf(String(a.status || '').trim()) >= 0;
    const freqChips = _freqDisabled
        ? `<span class="tag-hint" title="这个状态不参与更新频率筛选，但之前设的值保留着，改回正常就会恢复">${escapeHtml(a.status || '未设置')}永不更新${curFreq ? `（原设置：${escapeHtml(curFreq)}）` : ''}</span>`
        : FREQ_LIST.map((v) =>
            `<span class="freq-chip${curFreq === v ? ' active' : ''}" data-freq="${v}" title="更新频率：${v}" onclick="setFreq('${a.uid}', '${v}')">${v}</span>`
        ).join('');

    // 数量直接写在每个分类后面（原来的「共 N 条」并到「全部作品」里，不再单独占一行标题）
    const typeCounts = workTypeCounts(a);
    let filters = [
        ['all', '全部作品'], ['video', '▶ 视频'], ['image', '🖼 图文'],
        ['image_single', '单图'], ['image_multi', '多图'],
        ['live_single', '单动图'], ['live_multi', '多动图'],
    ].map(([t, label]) =>
        `<button class="btn work-filter-btn ${detailState.filter === t ? 'active' : ''}" data-type="${t}" onclick="setRailFilter('${t}')">${label}<b class="wf-n">${typeCounts[t] || 0}</b></button>`
    ).join('');
    const workDirLabel = detailState.workDir === 'asc' ? '↑ 最早在前' : '↓ 最新在前';
    // 纯净赏图：所有图文作品的图铺成一面墙（不跟当前分类走，永远是「全部图」）
    const gCount = galleryCountOf(a);
    filters += `<button class="btn gallery-btn${gCount ? '' : ' disabled'}" onclick="${gCount ? 'openGallery()' : 'showToast(\'这位作者没有图文作品\')'}"
        title="纯净赏图：把这位作者所有图文作品的图片铺成一整面照片墙">🖼 赏图${gCount ? `<b class="wf-n">${gCount}</b>` : ''}</button>`;
    filters += `<button class="btn work-dir-btn" id="workDirBtn" onclick="toggleWorkDir()" title="作品排序：最新在前 / 最早在前">${workDirLabel}</button>`;

    document.getElementById('detailTop').innerHTML = `
    <div class="detail-header">
        ${coverHtml}
        <div class="detail-info">
            <div class="detail-name">${aliasBox}</div>
            <div class="detail-fields">
                <div class="field"><span class="field-label">抖音号</span><span class="val">${a.douyin_id ? escapeHtml(a.douyin_id) : '无'}</span></div>
                <div class="field"><span class="field-label">UID</span><span class="val">${a.uid}</span></div>
                <div class="field field-sec${detailState.secEdit ? ' editing' : ''}"><span class="field-label">SEC_UID</span>
                    ${detailState.secEdit ? `
                        <input class="inline-input sec-input" id="secUidInput" placeholder="粘贴 MS4wLjAB… 或 https://www.douyin.com/user/… 网址"
                               value="${escapeHtml(a.sec_uid || '')}"
                               onkeydown="if(event.key==='Enter')saveSecUid(this.value);if(event.key==='Escape')cancelSecEdit()">
                        <button class="btn btn-mini" onclick="saveSecUid(document.getElementById('secUidInput').value)">保存</button>
                        <button class="btn btn-mini" onclick="fetchProfile()">获取资料</button>
                        <button class="btn btn-mini" onclick="cancelSecEdit()">取消</button>
                    ` : (a.sec_uid ? `
                        <a class="sec-val" id="secLink" href="${secUrl(a.sec_uid)}" target="_blank" rel="noopener"
                           title="点击打开抖音主页\n${escapeHtml(secUrl(a.sec_uid))}">${escapeHtml(secShortText(a.sec_uid))}</a>
                        <button class="btn btn-mini" onclick="startSecEdit()">更改</button>
                        <button class="btn btn-mini" onclick="fetchProfile()" title="按 SEC_UID 从抖音拉昵称/抖音号/简介">获取资料</button>
                        <button class="btn btn-mini" onclick="pushToQueue()" title="把这位作者推进「待下载区」，导出作者信息时可直接勾选，不用回抖音再找一遍">推送到待下载</button>
                    ` : `
                        <button class="btn btn-mini btn-dashed" onclick="startSecEdit()">＋ 填写</button>
                    `)}
                </div>
            </div>
            <div class="detail-sign"><span class="sign-label">简介</span><span class="sign-val${a.signature ? '' : ' empty'}">${escapeHtml(a.signature || '未填写')}</span></div>
            <div class="detail-actions">
                <button class="btn star-btn${a.starred ? ' on' : ''}" onclick="toggleStar('${a.uid}')"
                        title="${a.starred ? '取消星标' : '加星标'}" aria-pressed="${a.starred ? 'true' : 'false'}"
                        aria-label="${a.starred ? '取消星标' : '加星标'}">
                    <svg class="star-ico" viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" focusable="false">
                        <path d="M12 3.6l2.63 5.33 5.88.86-4.25 4.14 1 5.86L12 17.02l-5.26 2.77 1-5.86L3.49 9.79l5.88-.86z"/>
                    </svg>
                </button>
                <button class="btn ico-btn" onclick="openInFolder('${a.folder_rel}')"
                        title="打开这个作者的文件夹" aria-label="打开文件夹">
                    <svg class="ico" viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" focusable="false">
                        <path d="M3.2 6.4A2.2 2.2 0 0 1 5.4 4.2h3.1c.6 0 1.2.24 1.6.68l1 1.02h7.5a2.2 2.2 0 0 1 2.2 2.2v8.5a2.2 2.2 0 0 1-2.2 2.2H5.4a2.2 2.2 0 0 1-2.2-2.2z"/>
                        <path d="M3.4 9.6h17.2"/>
                    </svg>
                </button>
                <button class="btn ico-btn del-author-btn" onclick="deleteAuthor('${a.uid}')"
                        title="删除这位作者：从作者列表移除，并自动加入黑名单（本地已下载的文件不会删）"
                        aria-label="删除作者">
                    <svg class="ico" viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" focusable="false">
                        <path d="M4.6 6.6h14.8"/>
                        <path d="M9.4 6.6V4.9c0-.6.5-1.1 1.1-1.1h3c.6 0 1.1.5 1.1 1.1v1.7"/>
                        <path d="M6.6 6.6l.8 12.1c0 .7.6 1.3 1.3 1.3h6.6c.7 0 1.3-.6 1.3-1.3l.8-12.1"/>
                        <path d="M10.2 10.2v6.2M13.8 10.2v6.2"/>
                    </svg>
                </button>
                <div class="hist-wrap">
                    <button class="btn ico-btn hist-btn${detailState.histOpen ? ' on' : ''}" onclick="toggleHistory()"
                            title="历史记录（${histTotal} 条）：改昵称 / 状态 / 等级 / 标签 / 备注都会记一条" aria-label="历史记录（${histTotal} 条）">
                        <svg class="ico" viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" focusable="false">
                            <circle cx="12" cy="12" r="8.4"/>
                            <path d="M12 7.6V12l3 1.9"/>
                        </svg>
                        <b class="hist-n">${histTotal}</b>
                    </button>
                </div>
                ${detailState.histOpen ? `<div class="hist-pop" id="histPop">
                    <div class="hist-pop-head">信息变更记录<span class="hist-pop-close" title="关闭" onclick="toggleHistory()">×</span></div>
                    <div class="history-list">${historyRows || '<div class="tag-hint">暂无记录</div>'}</div>
                    <button class="btn btn-mini btn-dashed hist-add" onclick="addHistoryEntry()">＋ 手动记录</button>
                </div>` : ''}
            </div>
            <div class="detail-alts-inline">
                <span class="alts-inline-label">大小号${altArr.length ? '（组内 ' + (altArr.length + 1) + '）' : ''}</span>
                <span class="alt-list alt-list-inline">${alts}${altArr.length === 0 ? '<span class="tag-hint">暂无关联</span>' : ''}</span>
                ${altArr.length ? `<a class="alt-main-toggle" title="${a.main_uid === a.uid ? '取消这个账号的大号标记' : '把当前账号设为这组的大号'}" onclick="setMainAccount('${a.uid}')">${a.main_uid === a.uid ? '取消大号' : '设为大号'}</a>` : ''}<a class="alt-link-btn" title="搜昵称/抖音号/UID 新增账号到这组，组内互相关联" onclick="linkAltAccount('${a.uid}')">＋ 新增</a>${altArr.length ? `<a class="alt-main-toggle alt-detach" title="解除当前账号和组内其他所有账号的关联（其余账号之间不受影响）" onclick="detachAuthorAlts('${a.uid}')">解除关联</a>` : ''}
            </div>
            <div class="detail-stats-wrap">
            <div class="detail-stats" title="作品数（v=视频 p=图文）· 抖音最新作品发布时间 · 最后下载时间">
            <span class="stat-works" title="本地已下载：作品 ${m.work_count || 0} 个（视频 ${m.video_count || 0} / 图文 ${m.image_count || 0}）">作品 <b>${m.work_count || 0}</b><i class="stat-vp">（v${m.video_count || 0} p${m.image_count || 0}）</i></span>
            ${remoteStatHtml(a, m)}
            ${a.last_download ? `<span title="最后一次下载：${tsText(a.last_download, true)}（文件写入时间）${a.download_count ? `；下载记录库里这个作者有 ${a.download_count} 个文件` : ''}">最后下载 <b>${daysAgoText(a.last_download)}</b><i class="stat-at">${tsText(a.last_download)}</i></span>` : ''}
            </div>
            <button class="stat-refresh" id="statRefreshBtn" onclick="refreshWorkCount('${a.uid}', false, true)"
                    title="重新读一次抖音的作品数 + 最新发布时间（各 1 个请求，绕过 6 小时缓存）" aria-label="刷新抖音数据">
                <svg class="ico" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" focusable="false">
                    <path d="M20.2 12a8.2 8.2 0 1 1-2.4-5.8"/>
                    <path d="M20.4 4.4v4.4h-4.4"/>
                </svg>
            </button>
            </div>
        </div>
    </div>
    <div class="detail-grid">
        <div class="detail-card c-status">
            <h3>账号状态</h3>
            <select id="statusSelect" class="st-${statusKey(curStatus) || 'unset'}" onchange="setStatus('${a.uid}', this.value)">${statusOptions}</select>
        </div>
        <div class="detail-card c-level">
            <h3>作者等级<a class="lv-help" title="${LEVEL_HINT}">?</a></h3>
            <div class="level-chips">${levelChips}</div>
        </div>
        <div class="detail-card c-freq">
            <h3>更新频率</h3>
            <div class="freq-chips">${freqChips}</div>
        </div>
        <div class="detail-card c-tags">
            <h3>标签<a class="tag-toggle" title="从已有标签里选，或新建一个" onclick="toggleTagPicker()">${detailState.tagOpen ? '完成' : '＋ 添加'}</a></h3>
            <div class="tag-editor">${tagList || '<span class="tag-hint">暂无标签</span>'}</div>
            ${detailState.tagOpen ? `
            <div class="tag-picker" id="tagPicker">
                <input class="tag-picker-input" id="tagPickerInput" autocomplete="off"
                       placeholder="选已有标签，或输入新名字回车创建"
                       value="${escapeHtml(detailState.tagQuery)}"
                       oninput="renderTagPicker(this.value)" onkeydown="onTagInputKey(event)">
                <div class="tag-picker-list" id="tagPickerList"></div>
            </div>` : ''}
        </div>
        <div class="detail-card c-note">
            <h3>备注</h3>
            <textarea id="noteTextarea" class="note-input" rows="1" placeholder="写点备注…" title="${escapeHtml(a.note || '写点备注…')}" onchange="setNote('${a.uid}', this.value)">${escapeHtml(a.note || '')}</textarea>
        </div>
    </div>
    ${watchBarHtml(a)}`;

    document.getElementById('detailFilter').innerHTML = filters;

    // 标签下拉重绘后要把输入框的词和焦点接回去（不然每加一个标签就得重新点开）
    if (detailState.tagOpen) {
        renderTagPicker(detailState.tagQuery);
        focusTagPicker();
    }
}

// ===== 别名 =====
function startAliasEdit() {
    detailState.aliasEdit = true;
    refreshTop();
    const raf = window.requestAnimationFrame || ((fn) => setTimeout(fn, 16));
    raf(() => {
        const el = document.getElementById('aliasInput');
        if (el) { el.focus(); el.select(); }
    });
}

function cancelAliasEdit() {
    detailState.aliasEdit = false;
    refreshTop();
}

function saveAlias(value) {
    const a = detailState.author;
    if (!a) return;
    const val = String(value || '').trim();
    if (val === (a.alias || '')) { detailState.aliasEdit = false; refreshTop(); return; }
    setAccountField(a.uid, 'alias', val);
    detailState.aliasEdit = false;
    detailState.secEdit = false;
    a.alias = val;
    refreshTop();
    // 列表卡片、大小号文件里也跟着变
    const it = state.filtered.find((x) => x.uid === a.uid);
    if (it) { it.alias = val; renderAuthorGrid(); }
    state.altGroups = [];
    showToast(val ? '已改叫「' + val + '」（原名仍保留在后面）' : '已恢复用原昵称');
}

// ===== 标签：下拉选已有 + 输入新建（不重复） =====
function toggleTagPicker() {
    detailState.tagOpen = !detailState.tagOpen;
    detailState.tagQuery = '';
    refreshTop();
}

function closeTagPicker() {
    if (!detailState.tagOpen) return false;
    detailState.tagOpen = false;
    detailState.tagQuery = '';
    refreshTop();
    return true;
}

function focusTagPicker() {
    const raf = window.requestAnimationFrame || ((fn) => setTimeout(fn, 16));
    raf(() => {
        const el = document.getElementById('tagPickerInput');
        if (!el) return;
        el.focus();
        try { el.setSelectionRange(el.value.length, el.value.length); } catch (e) { /* 忽略 */ }
    });
}

// 下拉列表：已有的标签（按使用次数排）+ 输入不存在的名字时的「新建」行
function renderTagPicker(q) {
    detailState.tagQuery = q || '';
    const box = document.getElementById('tagPickerList');
    if (!box) return;
    const a = detailState.author;
    const mine = (a && a.tags) || [];
    const all = Object.assign({}, (state.meta && state.meta.tags) || {});
    // 刚加的标签可能还没来得及进 meta（/api/save 与 /api/meta 有竞态），先并进来
    mine.forEach((t) => { if (!Object.prototype.hasOwnProperty.call(all, t)) all[t] = 0; });
    const kw = detailState.tagQuery.trim().toLowerCase();

    const tags = Object.keys(all)
        .filter((t) => !kw || t.toLowerCase().indexOf(kw) >= 0)
        .sort((x, y) => (all[y] || 0) - (all[x] || 0) || x.localeCompare(y));

    const rows = [];
    const raw = detailState.tagQuery.trim();
    // 输入了一个库里没有的名字 → 给一行「新建并添加」
    if (raw && !Object.prototype.hasOwnProperty.call(all, raw) && mine.indexOf(raw) < 0) {
        rows.push(`<div class="tag-picker-row new" data-tag="${escapeHtml(raw)}" onclick="tagPickerPick(this.dataset.tag)">
            <span class="tag-picker-name">新建「${escapeHtml(raw)}」</span>
            <span class="tag-picker-op">＋ 创建并添加</span>
        </div>`);
    }
    tags.forEach((t) => {
        const has = mine.indexOf(t) >= 0;
        rows.push(`<div class="tag-picker-row${has ? ' has' : ''}" data-tag="${escapeHtml(t)}" ${has ? '' : 'onclick="tagPickerPick(this.dataset.tag)"'}>
            <span class="tag-picker-name">${escapeHtml(t)}</span>
            <span class="tag-picker-op">${has ? '已添加' : (all[t] || 0) + ' 个账号'}</span>
        </div>`);
    });

    box.innerHTML = rows.length
        ? rows.join('')
        : `<div class="tag-picker-empty">还没有任何标签，输入名字回车就能创建第一个</div>`;
}

function onTagInputKey(e) {
    if (e.key === 'Escape') { e.stopPropagation(); closeTagPicker(); return; }
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const v = (e.target.value || '').trim();
    if (!v) return;
    if ((state.currentAuthor && (state.currentAuthor.tags || [])).indexOf(v) >= 0) {
        showToast('「' + v + '」已经在标签里了');
        return;
    }
    addTag(v);
}

function tagPickerPick(tag) {
    tag = String(tag || '').trim();
    if (!tag) return;
    const a = state.currentAuthor;
    if (a && (a.tags || []).indexOf(tag) >= 0) { showToast('「' + tag + '」已经在标签里了'); return; }
    addTag(tag);
}



// ② 左栏：作品方形卡片网格
// 按当前分类 + 正/倒序，算出 rail 要显示的 works 下标数组（纯计算，不碰 DOM）
function buildRailList(type) {
    const works = (detailState.author && detailState.author.works) || [];
    const list = [];
    works.forEach((w, i) => {
        const kind = workKindOf(w);
        if (type === 'all') list.push(i);
        // 「图文」= 所有图 + 所有动图（不含视频）
        else if (type === 'image') {
            if (kind !== 'video') list.push(i);
        } else if (kind === type) list.push(i);
    });
    // 按作品日期排序（desc = 最新在前，asc = 最早在前）
    list.sort((x, y) => {
        const dx = works[x].date || '', dy = works[y].date || '';
        if (detailState.workDir === 'asc') return (dx < dy ? -1 : dx > dy ? 1 : x - y);
        return (dx < dy ? 1 : dx > dy ? -1 : x - y);
    });
    return list;
}

function setRailFilter(type, autoplay) {
    detailState.filter = type;
    detailState.list = buildRailList(type);
    document.querySelectorAll('.work-filter-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.type === type));
    renderRail();
    // 多选 + 时间段条开着的时候切分类：区间内的作品数会变，提示跟着刷新
    const db = document.getElementById('dateBar');
    if (db && !db.classList.contains('hidden')) updateDateHint();

    if (autoplay) {
        if (detailState.list.length > 0) playWork(detailState.list[0]);
        else document.getElementById('playerStage').innerHTML = '<div class="player-empty">该分类下没有作品</div>';
    }
}

// ===== 纯净赏图：把这位作者所有图文作品的图片铺成一整面照片墙 =====
// 不进详情页也能看清「这人到底拍了多少图」：没有卡片边框、没有标题、没有播放器，
// 只有图 + 一条细顶栏。点任意一张进大图，左右键翻，Esc 退。
const GALLERY_CHUNK = 120;              // 一屏一屏往 DOM 里加，几千张也不卡
const galleryState = { items: [], shown: 0, lb: -1, keyBound: false, timer: 0 };

// 按钮上那个数字：只数张数、不建完整列表（refreshTop 被调用得很频繁）
// 结果缓存在 author 对象上，openAuthor 每次重新拉数据就自然失效
function galleryCountOf(a) {
    if (!a) return 0;
    if (a._gCount === undefined) {
        let n = 0;
        (a.works || []).forEach((w) => {
            n += mediaItemsOf(w).filter((it) => it.kind === 'image').length;
        });
        a._gCount = n;
    }
    return a._gCount;
}

// 摊平成「一张图一条」，带上是第几个作品的第几张（大图层要用）
function galleryItemsOf(a) {
    const out = [];
    const ws = ((a && a.works) || []).slice()
        .sort((x, y) => String(y.date || '').localeCompare(String(x.date || '')));   // 最新在前
    ws.forEach((w) => {
        const imgs = mediaItemsOf(w).filter((it) => it.kind === 'image');
        const n = imgs.length;
        imgs.forEach((it, k) => out.push({ rel: it.rel, work: w, k, n }));
    });
    return out;
}

function openGallery() {
    const a = detailState.author;
    if (!a) return;
    const items = galleryItemsOf(a);
    if (!items.length) { showToast('这位作者没有图文作品'); return; }

    galleryState.items = items;
    galleryState.shown = 0;
    galleryState.lb = -1;
    document.getElementById('galleryTitle').textContent = (a.alias || a.nickname || a.uid) + '';
    document.getElementById('galleryCount').textContent =
        `${items.length} 张 · ${dedupeWorks(items)} 个作品`;
    const body = document.getElementById('galleryBody');
    const cols = document.getElementById('galleryCols');
    cols.innerHTML = '';
    body.scrollTop = 0;
    document.getElementById('galleryLb').classList.add('hidden');
    document.getElementById('galleryView').classList.remove('hidden');
    document.body.classList.add('gallery-open');

    appendGalleryChunk();
    // 图片是懒加载 + 缩略图现生成，刚插进去时高度还是 0，这一刻量 scrollHeight 没意义。
    // 交给 galleryImgLoaded()：每张图加载完都顺手检查一次「墙够不够高」，不够就再铺一批。
    setTimeout(tryFillGallery, 400);

    // 滚动加载：用 addEventListener 而不是内联 onscroll（内联属性在某些环境下不触发）
    if (!body.dataset.scrollBound) {
        body.addEventListener('scroll', onGalleryScroll, { passive: true });
        body.dataset.scrollBound = '1';
    }
    if (!galleryState.keyBound) {
        document.addEventListener('keydown', onGalleryKey);
        galleryState.keyBound = true;
    }
    // 兜底：scroll 事件在个别环境（无头浏览器、某些触控板驱动）不一定派发，
    // 打开期间每 700ms 自己瞄一眼「是不是快到底了」。几百张图的场景下这点开销可以忽略。
    if (galleryState.timer) clearInterval(galleryState.timer);
    galleryState.timer = setInterval(() => {
        if (document.getElementById('galleryView').classList.contains('hidden')) return;
        onGalleryScroll();
    }, 700);
}

function dedupeWorks(items) {
    const seen = new Set();
    items.forEach((it) => seen.add(it.work));
    return seen.size;
}

function closeGallery() {
    if (galleryState.timer) { clearInterval(galleryState.timer); galleryState.timer = 0; }
    document.getElementById('galleryView').classList.add('hidden');
    document.getElementById('galleryLb').classList.add('hidden');
    document.body.classList.remove('gallery-open');
    galleryState.lb = -1;
}

function appendGalleryChunk() {
    const body = document.getElementById('galleryCols');
    if (!body) return;
    const items = galleryState.items;
    const from = galleryState.shown;
    const to = Math.min(items.length, from + GALLERY_CHUNK);
    if (from >= to) return;
    let html = '';
    for (let i = from; i < to; i++) {
        const it = items[i];
        const tag = it.n > 1 ? `<i class="gallery-n">${it.k + 1}/${it.n}</i>` : '';
        html += `<figure class="gallery-item" onclick="openGalleryLb(${i})" title="${escapeHtml(it.work.date || '')} ${escapeHtml(it.work.desc || '')}">`
            + `<img src="${mediaUrl(it.rel)}" alt="" decoding="async"`
            + ` onload="galleryImgFit(this)" onerror="this.parentNode.classList.add('bad')">`
            + tag
            + `</figure>`;
    }
    body.insertAdjacentHTML('beforeend', html);
    galleryState.shown = to;
}

// 图片加载完 → 先把 3:4 的占位比例换成照片自己的真实比例，墙再变高。
// 不换会怎样：aspect-ratio 的优先级高于图片固有比例，height:auto 从 3:4 算出来，
// 于是 9:16 的竖图被压扁、方图被拉长（用户反馈的「图片被压缩」就是这个）。
function galleryImgFit(img) {
    const w = img.naturalWidth, h = img.naturalHeight;
    if (w > 0 && h > 0) {
        const want = w + ' / ' + h;
        if (img.style.aspectRatio !== want) img.style.aspectRatio = want;
    }
    galleryImgLoaded();
}

// 图片加载完 → 墙变高了 → 再看要不要继续铺。
// 用 200ms 合并：一屏几十张同时 load，不合并会每张都量一次 scrollHeight（强制重排）。
let galleryFillTimer = 0;
function galleryImgLoaded() {
    if (galleryFillTimer) return;
    galleryFillTimer = setTimeout(() => {
        galleryFillTimer = 0;
        tryFillGallery();
    }, 200);
}

function tryFillGallery() {
    // 关掉以后不能继续铺：隐藏状态下 scrollHeight 恒为 0，会一直判定「墙不够高」
    // 然后无限往 DOM 里加图（关了半天再打开发现卡，就是这个）
    const view = document.getElementById('galleryView');
    if (!view || view.classList.contains('hidden')) return;
    const body = document.getElementById('galleryBody');
    if (!body) return;
    if (galleryState.shown >= galleryState.items.length) return;
    if (body.scrollHeight > body.clientHeight + 120) return;   // 已经能滚了就别急着铺
    appendGalleryChunk();
}

function onGalleryScroll() {
    const body = document.getElementById('galleryBody');
    if (!body || body.scrollHeight <= body.clientHeight) return;
    if (body.scrollTop + body.clientHeight >= body.scrollHeight - 1200) appendGalleryChunk();
}

function openGalleryLb(i) {
    const it = galleryState.items[i];
    if (!it) return;
    galleryState.lb = i;
    document.getElementById('galleryLbImg').src = mediaUrl(it.rel);
    const parts = [String(i + 1) + '/' + galleryState.items.length];
    if (it.work.date) parts.push(it.work.date);
    if (it.n > 1) parts.push('第 ' + (it.k + 1) + ' 张');
    document.getElementById('galleryLbCount').textContent = parts.join('　');
    document.getElementById('galleryLb').classList.remove('hidden');
}

function closeGalleryLb(ev, force) {
    // 点图片本身不关（只有点空白处才关）
    if (!force && ev && ev.target && ev.target.id !== 'galleryLb') return;
    document.getElementById('galleryLb').classList.add('hidden');
    galleryState.lb = -1;
}

function stepGalleryLb(dir, ev) {
    if (ev) ev.stopPropagation();
    const n = galleryState.items.length;
    if (!n) return;
    let i = galleryState.lb;
    if (i < 0) return;
    i = (i + dir + n) % n;                       // 首尾循环，翻到头继续按不会卡住
    openGalleryLb(i);
    // 翻到还没渲染的那一段时，先把墙往后铺，免得关掉大图后卡在空白处
    if (i >= galleryState.shown - 1) appendGalleryChunk();
}

function onGalleryKey(e) {
    const view = document.getElementById('galleryView');
    if (!view || view.classList.contains('hidden')) return;
    const lbOpen = !document.getElementById('galleryLb').classList.contains('hidden');
    if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        if (lbOpen) closeGalleryLb(null, true); else closeGallery();
        return;
    }
    if (!lbOpen) return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); stepGalleryLb(-1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); stepGalleryLb(1); }
}


// 大作者几千个作品，renderRail 一次要重建几千个节点（外加重新 observe 缩略图），
// 在删除这种「用户正盯着看」的操作里会明显卡一下。这里就地移除 + 把后面的下标前移。
function refreshRailAfterDelete(delIdxs) {
    detailState.list = buildRailList(detailState.filter || 'all');
    const rail = document.getElementById('workRail');
    if (!rail) return;
    const del = new Set(delIdxs);
    const sorted = delIdxs.slice().sort((a, b) => a - b);
    rail.querySelectorAll('.wall-card').forEach((el) => {
        const i = Number(el.dataset.idx);
        if (del.has(i)) { el.remove(); return; }
        let shift = 0;
        while (shift < sorted.length && sorted[shift] < i) shift += 1;
        if (shift) {
            const ni = i - shift;
            el.dataset.idx = ni;
            el.setAttribute('onclick', 'onCardClick(' + ni + ', this)');
        }
    });
    // 整类被删空时要显示「该分类下暂无作品」的占位，这时才整体重渲
    if (!detailState.list.length) renderRail();
}

// 切换作品正序/倒序（保持当前分类不变，只反转列表顺序）
function toggleWorkDir() {
    detailState.workDir = detailState.workDir === 'asc' ? 'desc' : 'asc';
    setRailFilter(detailState.filter, false);
    const btn = document.getElementById('workDirBtn');
    if (btn) btn.textContent = detailState.workDir === 'asc' ? '↑ 最早在前' : '↓ 最新在前';
}

function wallThumbHtml(w) {
    const p = pickPosterFile(w);
    if (!p) return '<div class="wall-thumb placeholder">🎬</div>';
    if (p.isImage) return `<img class="wall-thumb" data-src="${mediaUrl(p.rel)}" onload="checkThumbRatio(this)" onerror="thumbError(this)">`;
    return `<img class="wall-thumb" data-src="/thumb?path=${encodeURIComponent(p.rel)}" onload="checkThumbRatio(this)" onerror="thumbError(this)">`;
}

// 自动检测缩略图比例：横屏视频/图片完整展示（不放大裁剪、上下留适度黑边），竖屏/方形铺满卡片
function checkThumbRatio(img) {
    if (!img) return;
    const nw = img.naturalWidth;
    const nh = img.naturalHeight;
    if (nw && nh) {
        if (nw / nh > 1.08) {
            img.classList.add('is-landscape');
            const card = img.closest('.wall-card');
            if (card) card.classList.add('has-landscape');
        } else {
            img.classList.remove('is-landscape');
            const card = img.closest('.wall-card');
            if (card) card.classList.remove('has-landscape');
        }
    }
}

// 多图 / 多动图 / 多段视频的徽标（右上角，柔和精致）
// 纯视频作品（一张图都没有）只有 2 段以上才标，且用播放三角而不是动图圆点 ——
// 否则一个「2 个 mp4」的作品会被看成「多动图 2 段」，但分类里它其实算视频（2026-11-02 用户困惑点）。
function wallBadgeHtml(w) {
    const items = mediaItemsOf(w);
    const imgs = items.filter((it) => it.kind === 'image').length;
    const vids = items.length - imgs;
    if (!imgs) {
        return vids > 1
            ? `<div class="wall-badge vid-badge" title="多段视频 · 共 ${vids} 段（点开后在播放器里翻段）"><svg class="badge-ico" viewBox="0 0 16 16" width="10" height="10" fill="currentColor"><path d="M5.4 3.3h1.9v9.4H5.4zM8.7 3.3h1.9v9.4H8.7z"/></svg><span>${vids}</span></div>`
            : '';
    }
    if (vids >= 2) {
        return `<div class="wall-badge live-badge" title="多动图 · 共 ${vids} 段"><svg class="badge-ico" viewBox="0 0 16 16" width="10" height="10" fill="none"><circle cx="8" cy="8" r="5.5" stroke="currentColor" stroke-width="1.4"/><circle cx="8" cy="8" r="2.5" fill="currentColor"/></svg><span>${vids}</span></div>`;
    }
    if (imgs > 1) {
        return `<div class="wall-badge img-badge" title="多图 · 共 ${imgs} 张"><svg class="badge-ico" viewBox="0 0 16 16" width="10" height="10" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="4.5" y="4.5" width="8.5" height="8.5" rx="1.5"/><path d="M3 11.5V3.5C3 3 3.5 2.5 4 2.5H12"/></svg><span>${imgs}</span></div>`;
    }
    return '';
}

// ===== 作品多选删除（详情页返回栏那个 🗑） =====
// 2026-11-02：加「按时间段选」——作者作品上千条时，一个个点勾选不现实，
// 给个日期区间一次圈一批。作用范围是**当前分类筛选下的作品**（视频/图文…），
// 选完仍然走原来的「移入回收站」，回收站里能整体恢复。
function dateRangeOfList() {
    const works = (detailState.author && detailState.author.works) || [];
    let min = '', max = '';
    (detailState.list || []).forEach((i) => {
        const d = (works[i] || {}).date || '';
        if (!d) return;
        if (!min || d < min) min = d;
        if (!max || d > max) max = d;
    });
    return { min: min, max: max };
}

function dateInputs() {
    const f = document.getElementById('dpFrom');
    const t = document.getElementById('dpTo');
    return { from: (f && f.value) || '', to: (t && t.value) || '', fEl: f, tEl: t };
}

function toggleDateBar() {
    const bar = document.getElementById('dateBar');
    if (!bar) return;
    if (!bar.classList.contains('hidden')) { bar.classList.add('hidden'); return; }
    const { fEl, tEl } = dateInputs();
    const r = dateRangeOfList();
    if (fEl && !fEl.value) fEl.value = r.min;   // 首次展开：默认铺满该作者的日期范围
    if (tEl && !tEl.value) tEl.value = r.max;
    bar.classList.remove('hidden');
    updateDateHint();
}

// 当前筛选列表里，日期落在区间内的作品下标
function dateHits() {
    const works = (detailState.author && detailState.author.works) || [];
    const { from, to } = dateInputs();
    const out = [];
    (detailState.list || []).forEach((i) => {
        const d = (works[i] || {}).date || '';
        if (!d) return;                     // 没日期的作品不参与区间选择，免得不小心漏网
        if (from && d < from) return;
        if (to && d > to) return;
        out.push(i);
    });
    return out;
}

function updateDateHint() {
    const el = document.getElementById('dpHint');
    if (!el) return;
    const { from, to } = dateInputs();
    if (from && to && from > to) {
        el.textContent = '起始日期比结束日期还晚';
        el.classList.add('bad');
        return;
    }
    el.classList.remove('bad');
    const hits = dateHits();
    const total = (detailState.list || []).length;
    el.textContent = '这个区间有 ' + hits.length + ' 个作品'
        + (total && hits.length === total ? '（＝当前分类全部）' : '');
}

function applyDateSelect(replace) {
    const { from, to } = dateInputs();
    if (from && to && from > to) { showToast('起始日期不能晚于结束日期'); return; }
    if (!from && !to) { showToast('先填个起止日期'); return; }
    const hits = dateHits();
    if (!hits.length) {
        showToast('这个区间没有作品——检查一下上方的分类筛选（当前是「' + railFilterName() + '」）');
        return;
    }
    if (replace) detailState.selected = new Set();
    hits.forEach((i) => detailState.selected.add(i));
    // 只改卡片 class，不重渲染：重渲染会丢滚动位置、缩略图也要重新加载
    document.querySelectorAll('#workRail .wall-card').forEach((el) => {
        const i = parseInt(el.dataset.idx, 10);
        el.classList.toggle('selected', !!(detailState.selected && detailState.selected.has(i)));
    });
    updateSelCount();
    showToast((replace ? '已改选 ' : '已加选 ') + hits.length + ' 个作品，共选中 '
        + (detailState.selected ? detailState.selected.size : 0) + ' 个');
}

function railFilterName() {
    const m = { all: '全部', video: '视频', image: '图文', image_single: '单图', image_multi: '多图' };
    return m[detailState.filter] || detailState.filter;
}

function enterWorkSelect() {
    if (!detailState.author) return;
    detailState.selectMode = true;
    detailState.selected = new Set();
    const dv = document.getElementById('detailView');
    if (dv) dv.classList.add('selecting');
    const sa = document.getElementById('selActions');
    if (sa) sa.classList.remove('hidden');
    const btn = document.getElementById('workSelBtn');
    if (btn) btn.classList.add('hidden');
    document.querySelectorAll('#workRail .wall-card').forEach((el) => {
        el.classList.remove('selected');
        if (!el.querySelector('.wall-check')) {
            const c = document.createElement('span');
            c.className = 'wall-check';
            el.appendChild(c);
        }
    });
    updateSelCount();
}

function exitWorkSelect() {
    detailState.selectMode = false;
    detailState.selected = new Set();
    const dv = document.getElementById('detailView');
    if (dv) dv.classList.remove('selecting');
    const sa = document.getElementById('selActions');
    if (sa) sa.classList.add('hidden');
    const db = document.getElementById('dateBar');
    if (db) db.classList.add('hidden');       // 退出多选就收起时间段条，免得占着位置
    const btn = document.getElementById('workSelBtn');
    if (btn) btn.classList.remove('hidden');
    document.querySelectorAll('#workRail .wall-card').forEach((el) => {
        el.classList.remove('selected');
        const c = el.querySelector('.wall-check');
        if (c) c.remove();
    });
}

// 多选模式下点卡片是勾选，不是播放
function onCardClick(i, el) {
    if (!detailState.selectMode) { playWork(i); return; }
    const set = detailState.selected;
    if (set.has(i)) { set.delete(i); el.classList.remove('selected'); }
    else { set.add(i); el.classList.add('selected'); }
    updateSelCount();
}

function updateSelCount() {
    const n = detailState.selected ? detailState.selected.size : 0;
    const span = document.getElementById('selCount');
    if (span) span.textContent = '已选 ' + n + ' 个';
    const del = document.querySelector('#selActions .sel-del');
    if (del) del.disabled = n === 0;
}

// 删除作品 = **移入回收站**（文件搬到 browser\recycle-bin\，作品从列表摘掉），随时能恢复。
// 只有「文件管理 → 回收站 → 彻底删除」才是真删。
async function deleteSelectedWorks() {
    const a = detailState.author;
    if (!a) return;
    const idxs = Array.from(detailState.selected || []);
    if (!idxs.length) { showToast('先点作品选中要删的'); return; }
    const works = a.works || [];
    let files = 0;
    idxs.forEach((i) => { files += ((works[i] || {}).files || []).length; });
    const ok = window.confirm(
        '把 ' + idxs.length + ' 个作品（' + files + ' 个文件）移入回收站？\n\n'
        + '· 网页里不再显示，文件搬到 browser\\recycle-bin\\\n'
        + '· 后悔了可以在「文件管理 → 回收站」里点恢复\n\n确定继续吗？'
    );
    if (!ok) return;

    const btn = document.querySelector('#selActions .sel-del');
    if (btn) { btn.disabled = true; btn.textContent = '移入回收站中…'; }
    const stage = document.getElementById('playerStage');
    try {
        const r = await fetch('/api/works/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ uid: detailState.uid, idxs: idxs }),
        }).then((x) => x.json());
        if (!r.ok) { showToast(r.error || '删除失败'); return; }

        // 就地更新，不重新拉数据（保住滚动位置）
        idxs.slice().sort((x, y) => y - x).forEach((i) => { works.splice(i, 1); });
        detailState.index = -1;
        try {
            exitWorkSelect();
            renderDetailTop();
            refreshRailAfterDelete(idxs);
        } catch (e) {
            // 重渲染出错也不能把整页卡在「删除中」——刷新一次就好
            console.error('删除后刷新界面失败', e);
            showToast('已移入回收站，界面刷新失败，正在重载…');
            setTimeout(() => window.location.reload(), 600);
            return;
        }
        if (stage) {
            stage.innerHTML = '<div class="player-empty">已移入回收站 ' + r.works
                + ' 个作品（「文件管理 → 回收站」里可以恢复）</div>';
        }
        showToast('已移入回收站：' + r.works + ' 个作品、' + r.files + ' 个文件（' + fmtBytes(r.freed || 0) + '）');
        if (r.failed && r.failed.length) {
            setTimeout(() => showToast(r.failed.length + ' 个文件没能搬走：' + (r.failed[0].error || '')), 2400);
        }
        if (filesState.loaded) refreshFilesView();
    } catch (e) {
        showToast('删除失败：' + e.message);
    } finally {
        const b2 = document.querySelector('#selActions .sel-del');
        if (b2) { b2.disabled = false; b2.textContent = '移入回收站'; }
        updateSelCount();
    }
}

function renderRail() {
    const rail = document.getElementById('workRail');
    const works = (detailState.author && detailState.author.works) || [];
    rail.innerHTML = detailState.list.map(i => {
        const w = works[i];
        const tip = ((w.date || '') + ' ' + (w.desc || '')).trim();
        const sel = detailState.selectMode && detailState.selected && detailState.selected.has(i);
        return `
        <div class="wall-card${detailState.index === i && !detailState.selectMode ? ' active' : ''}${sel ? ' selected' : ''}" data-idx="${i}"
             title="${escapeHtml(tip)}" onclick="onCardClick(${i}, this)">
            ${wallThumbHtml(w)}
            ${wallBadgeHtml(w)}
            ${w.date ? `<div class="wall-date">${w.date}</div>` : ''}
            ${detailState.selectMode ? '<span class="wall-check"></span>' : ''}
        </div>`;
    }).join('') || '<div class="tag-hint" style="padding:16px;text-align:center;grid-column:1/-1">该分类下暂无作品</div>';
    observeThumbs(rail);
}

function markRailActive() {
    document.querySelectorAll('#workRail .wall-card').forEach(el => {
        el.classList.toggle('active', Number(el.dataset.idx) === detailState.index);
    });
}

// ③ 右栏：媒体预览区（视频和图文统一在这里展示大图/播放器）
function playWork(idx) {
    const a = detailState.author;
    if (!a) return;
    const w = (a.works || [])[idx];
    if (!w) return;
    detailState.index = idx;
    detailState.imgIndex = 0;
    detailState.vidIndex = 0;                          // 换作品永远从第 1 段开始
    detailState.token = (detailState.token || 0) + 1;   // 防止转码回来时覆盖了后点的作品
    bindPlayerEnded();                                  // 幂等：保证「放完怎么办」的委托已挂上
    markRailActive();
    // 帖子里有图 → 按序号逐张看（静图是图、动图直接当视频播）；纯视频作品才走编码检测/转码那条路
    const items = mediaItemsOf(w);
    const hasImage = items.some((it) => it.kind === 'image');
    if (items.length && !hasImage) renderVideoStage(w, detailState.token);
    else renderMediaStage(w);
}

function playerNavHtml() {
    // 上一页/下一页 + 位置，压缩成底部一行里的三小块（省掉一整行 = 视频多一点高度）
    const pos = detailState.list.indexOf(detailState.index);
    return `<button class="btn btn-nav" title="上一个" ${pos <= 0 ? 'disabled' : ''} onclick="stepWork(-1)">‹</button>
        <span class="player-pos">${pos >= 0 ? pos + 1 : 0}/${detailState.list.length}</span>
        <button class="btn btn-nav" title="下一个" ${pos < 0 || pos >= detailState.list.length - 1 ? 'disabled' : ''} onclick="stepWork(1)">›</button>`;
}

function stageFootHtml(w, rel, extra, showMode) {
    return `
        <div class="player-foot">
            ${playerNavHtml()}
            <div class="player-title" title="${escapeHtml(w.desc || '')}">${w.date || ''} · ${escapeHtml(w.desc || '')}</div>
            ${extra || ''}
            ${showMode ? playModeBtnHtml() : ''}
            <button class="btn" data-rel="${escapeHtml(rel)}" onclick="setHomeCover(this.dataset.rel)">🖼 设为封面</button>
        </div>
        <div class="player-hint" id="playerHint"></div>`;
}

// ---------- 视频放完之后的行为（2026-11-05 用户要求） ----------
// 四种模式，默认「放完就停」= 保持原来的行为不变：
//   stop    放完就停（默认）
//   next    继续播放下一个（列表顺序，走到最后一个就停）
//   repeat  重复播放（当前这段单曲循环）
//   shuffle 随机播放（随机跳到另一个有视频的作品）
// 选择存在 localStorage 的 playMode，刷新不丢。
const PLAY_MODES = [
    { k: 'stop',    icon: '⏹', short: '放完停',   label: '放完就停',
      hint: '播完停在最后一帧，什么都不做（默认）' },
    { k: 'next',    icon: '⏭', short: '下一个',   label: '继续播放下一个',
      hint: '播完自动看下一个作品；已经是最后一个就停住' },
    { k: 'repeat',  icon: '🔁', short: '重复',     label: '重复播放',
      hint: '当前这段播完从头再放（单曲循环）' },
    { k: 'shuffle', icon: '🔀', short: '随机',     label: '随机播放',
      hint: '播完随机跳到另一个有视频的作品' },
];

function playModeDef(k) {
    return PLAY_MODES.find((m) => m.k === k) || PLAY_MODES[0];
}

function playModeBtnHtml() {
    const m = playModeDef(state.playMode);
    const on = state.playMode === 'stop' ? '' : ' on';
    return `<button class="btn play-mode-btn${on}" id="playModeBtn" onclick="togglePlayModeMenu(event)"
        title="视频放完之后：${m.label}（${m.hint}）｜点这里切换">${m.icon} ${m.short}</button>`;
}

function refreshPlayModeBtn() {
    const b = document.getElementById('playModeBtn');
    if (!b) return;
    const m = playModeDef(state.playMode);
    b.innerHTML = `${m.icon} ${m.short}`;
    b.classList.toggle('on', state.playMode !== 'stop');
    b.title = `视频放完之后：${m.label}（${m.hint}）｜点这里切换`;
}

function closePlayModeMenu() {
    const el = document.getElementById('playModeMenu');
    if (el) el.remove();
    document.removeEventListener('keydown', onPlayModeEsc);
}

function onPlayModeEsc(e) {
    if (e.key === 'Escape') closePlayModeMenu();
}

// 菜单挂在 body 上、fixed 定位：播放器底部那一行是 flex-wrap，overflow 会把它裁掉
function togglePlayModeMenu(ev) {
    if (ev) ev.stopPropagation();
    if (document.getElementById('playModeMenu')) { closePlayModeMenu(); return; }
    const btn = document.getElementById('playModeBtn');
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const el = document.createElement('div');
    el.className = 'pm-menu';
    el.id = 'playModeMenu';
    el.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 232)) + 'px';
    el.innerHTML = PLAY_MODES.map((m) => `
        <div class="pm-item${m.k === state.playMode ? ' on' : ''}" data-k="${m.k}" onclick="setPlayMode('${m.k}', event)">
            <span class="pm-ico">${m.icon}</span>
            <span class="pm-txt"><b>${m.label}</b><i>${m.hint}</i></span>
        </div>`).join('');
    document.body.appendChild(el);
    // 按钮就贴在窗口底部，往下弹会被切掉 —— 先量高度，放不下就翻到按钮上方
    const mh = el.offsetHeight;
    let top = r.bottom + 4;
    if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 4);
    el.style.top = top + 'px';
    // 点别处关掉（延到下一轮再绑，否则这次点击自己就把它关了）
    setTimeout(() => {
        document.addEventListener('click', closePlayModeMenu, { once: true });
        document.addEventListener('keydown', onPlayModeEsc);
    }, 0);
}

function setPlayMode(k, ev) {
    if (ev) ev.stopPropagation();
    if (!PLAY_MODES.some((m) => m.k === k)) return;
    state.playMode = k;
    try { localStorage.setItem('playMode', k); } catch (e) { /* 隐私模式忽略 */ }
    closePlayModeMenu();
    refreshPlayModeBtn();
    showToast('视频放完：' + playModeDef(k).label);
}

// <video> 的 ended 不冒泡，所以在 playerStage 上挂捕获阶段的委托，绑一次管所有新渲染出来的播放器
function bindPlayerEnded() {
    const stage = document.getElementById('playerStage');
    if (!stage || stage.dataset.endedBound === '1') return;
    stage.dataset.endedBound = '1';
    stage.addEventListener('ended', onPlayerVideoEnded, true);
}

function workHasVideo(w) {
    return !!(w && videoFilesOf(w).length);
}

function onPlayerVideoEnded(ev) {
    const v = ev.target;
    if (!v || v.tagName !== 'VIDEO') return;
    if (v.loop) return;                    // 图文里那张动图自己循环，不参与这套规则
    const mode = state.playMode || 'stop';
    if (mode === 'stop') return;           // 默认：什么都不做
    const a = detailState.author;
    const w = a && (a.works || [])[detailState.index];
    if (!w) return;
    if (mode === 'repeat') {
        try { v.currentTime = 0; v.play().catch(() => {}); } catch (e) { /* 忽略 */ }
        return;
    }
    // 一个作品下成 _1.mp4 + _2.mp4 时，先把段看完，最后一段放完再决定要不要换作品
    const segs = videoFilesOf(w).length;
    if (segs > 1 && (detailState.vidIndex || 0) < segs - 1) { stepVideo(1); return; }
    if (mode === 'next') { stepWork(1); return; }
    if (mode === 'shuffle') playRandomWork();
}

function playRandomWork() {
    const a = detailState.author;
    const list = detailState.list || [];
    if (!a || list.length < 2) return;
    const works = a.works || [];
    // 优先只在「确实有视频」的作品里随机，否则连续撞上图文会像卡住了
    let pool = list.filter((i) => workHasVideo(works[i]));
    if (pool.length < 2) pool = list;
    const pool2 = pool.filter((i) => i !== detailState.index);
    const from = pool2.length ? pool2 : pool;
    const pick = from[Math.floor(Math.random() * from.length)];
    if (pick == null || pick === detailState.index) return;
    playWork(pick);
    const cur = document.querySelector('#workRail .wall-card.active');
    if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest' });
}

// 填满模式：视频/图片裁切铺满整块媒体区（会切掉超出部分），适应模式：等比缩放留黑边。
// 播放器底部的「填满」切换按钮已移除（2026-10-29），这里保留 applyFillMode 让已存
// localStorage 的 fillMode 偏好继续生效（尊重之前切过填满的用户）。
function applyFillMode() {
    const stage = document.getElementById('playerStage');
    if (stage) stage.classList.toggle('fill-mode', !!state.fillMode);
}

// 浏览器能不能解 HEVC（装了 HEVC 扩展/Safari 才行）
function canPlayHevc() {
    try {
        const v = document.createElement('video');
        return !!(v.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"') || v.canPlayType('video/mp4; codecs="hev1.1.6.L93.B0"'));
    } catch (e) { return false; }
}

async function probeVideo(rel) {
    try {
        const r = await fetch('/api/probe?path=' + encodeURIComponent(rel));
        const d = await r.json();
        if (d && d.ok) return d;
    } catch (e) { /* 探测失败就当能播 */ }
    return { ok: true, playable: true, codec: '' };
}

// 转码是后台任务：POST 拿 job id，再轮询进度。
// 原来是一次请求同步等转码跑完，长视频会让这个请求挂几分钟、界面只有一句「正在转码」。
async function transcodeVideo(rel, onProgress, isCancelled) {
    let job;
    try {
        const r = await fetch('/api/transcode', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: rel }),
        });
        const d = await r.json();
        if (!d || !d.ok) return { ok: false, error: (d && d.error) || '' };
        if (d.url) return { ok: true, url: d.url };   // 已经转过，秒开
        job = d.job;
    } catch (e) {
        return { ok: false };
    }
    const deadline = Date.now() + 15 * 60 * 1000;
    for (;;) {
        await new Promise((s) => setTimeout(s, 800));
        // 用户切到别的作品/退出详情后就没必要继续轮询了（原来会一直打到转完或 15 分钟）
        if (isCancelled && isCancelled()) return { ok: false, error: '已取消' };
        if (Date.now() > deadline) return { ok: false, error: '转码超时' };
        let s;
        try {
            s = await (await fetch('/api/transcode_status?job=' + encodeURIComponent(job))).json();
        } catch (e) { continue; }
        if (s && s.status === 'done' && s.url) return { ok: true, url: s.url };
        if (s && s.status === 'error') return { ok: false, error: s.error || '' };
        if (s && s.ok === false) return { ok: false, error: s.error || '' };
        if (onProgress) onProgress((s && typeof s.pct === 'number') ? s.pct : -1);
    }
}

// 刚进作者页时右栏只显示提示，不自动播
function showStagePlaceholder() {
    const stage = document.getElementById('playerStage');
    if (!stage) return;
    const a = detailState.author || {};
    stage.innerHTML = `<div class="player-empty">点击左侧作品开始播放或查看<br>
        <span style="font-size:12px">共 ${(a.stats || {}).work_count || 0} 个作品</span></div>`;
}

// 纯视频作品里的视频文件（按序号排）。多数作品只有 1 段；个别作品（抖音的动图/合集会下成
// _1.mp4 + _2.mp4）有 2 段以上 —— 那些段在播放器里要能翻，否则第二段永远看不到。
function videoFilesOf(w) {
    return mediaItemsOf(w).filter((it) => it.kind === 'video').map((it) => it.rel);
}

function vidNavHtml(w) {
    const rels = videoFilesOf(w);
    if (rels.length < 2) return '';
    const n = rels.length;
    const i = Math.max(0, Math.min(detailState.vidIndex || 0, n - 1));
    return `<span class="vid-nav" title="这个作品有 ${n} 段视频">`
        + `<button class="btn btn-nav" title="上一段" ${i <= 0 ? 'disabled' : ''} onclick="stepVideo(-1)">‹</button>`
        + `<span class="player-pos">${i + 1}/${n} 段</span>`
        + `<button class="btn btn-nav" title="下一段" ${i >= n - 1 ? 'disabled' : ''} onclick="stepVideo(1)">›</button>`
        + '</span>';
}

// 作品内翻段：只重画播放器，不动作品索引和列表位置
function stepVideo(dir) {
    const a = detailState.author;
    const w = a && (a.works || [])[detailState.index];
    if (!w) return;
    const n = videoFilesOf(w).length;
    if (n < 2) return;
    const next = Math.max(0, Math.min((detailState.vidIndex || 0) + dir, n - 1));
    if (next === detailState.vidIndex) return;
    detailState.vidIndex = next;
    detailState.token = (detailState.token || 0) + 1;   // 作废上一段的探测/转码回调
    renderVideoStage(w, detailState.token);
}

async function renderVideoStage(w, token) {
    const vids = videoFilesOf(w);
    if (!(detailState.vidIndex >= 0 && detailState.vidIndex < vids.length)) detailState.vidIndex = 0;
    const rel = vids[detailState.vidIndex] || '';
    const segNav = vidNavHtml(w);
    const stage = document.getElementById('playerStage');
    stage.innerHTML = `
        <div class="player-box"><div class="player-empty">正在检测视频编码…</div></div>
        ${stageFootHtml(w, rel, segNav, true)}`;

    const info = await probeVideo(rel);
    if (token !== detailState.token) return;   // 用户已点了别的作品

    // 编码能不能解，三条路（顺序不能换）：
    //   ① 以前转过码 → 直接播缓存。原来这条写在「needTranscode」里面，缓存过之后
    //      playable 变成 true，就再也进不去了，于是回头播原片 HEVC → 黑屏（2026-11-08 修）
    //   ② 浏览器自己解得动（H.264，或装了系统 HEVC 扩展后的 HEVC）→ 直接播原片，原画质、零等待
    //   ③ 解不动（没装扩展 / 其它冷门编码）→ 转码到 trans_cache/，转一次以后秒开
    const needTranscode = info.playable === false && !info.url && !canPlayHevc();
    let src = info.url || mediaUrl(rel);
    if (needTranscode) {
        if (info.url) {
            src = info.url;            // 以前转过的缓存，直接用
        } else {
            const hint = '<span style="font-size:12px">首次需要几秒到几分钟，转完会缓存，下次秒开</span>';
            const box = stage.querySelector('.player-box');
            if (box) box.innerHTML = `<div class="player-empty">这个视频是 ${escapeHtml((info.codec || 'HEVC').toUpperCase())} 编码，浏览器解不了画面，正在转成 H.264…<br>${hint}</div>`;
            const t = await transcodeVideo(rel, (pct) => {
                if (token !== detailState.token) return;
                const b = stage.querySelector('.player-box');
                if (b) b.innerHTML = `<div class="player-empty">正在转成 H.264 ${pct >= 0 ? pct + '%' : '…'}<br>${hint}</div>`;
            }, () => token !== detailState.token);
            if (token !== detailState.token) return;
            if (t && t.ok && t.url) src = t.url;
            else {
                const b2 = stage.querySelector('.player-box');
                if (b2) b2.innerHTML = `<div class="player-empty">转码失败${t && t.error ? '：' + escapeHtml(t.error) : ''}<br><span style="font-size:12px">可以点「📂 打开文件夹」到本地目录看原片</span></div>`;
                return;
            }
        }
    }

    // 播的是原片、且前端认为「浏览器能直解」时，才挂失败兜底：
    // 个别文件（10bit / 异常分辨率 / 损坏）仍可能解不出来，这时自动退回转码，
    // 否则用户只会看到一个永远黑屏、且没有任何提示的播放器。
    const playedNative = !needTranscode && info.playable === false;

    stage.innerHTML = `
        <div class="player-box">
            <video id="playerVideo" controls autoplay playsinline
                   poster="${posterUrl(w, rel)}"
                   src="${src}" onerror="onPlayerError()"></video>
        </div>
        ${stageFootHtml(w, rel, (info.codec ? `<span class="tag-hint">${escapeHtml(info.codec.toUpperCase())}</span>` : '') + segNav, true)}`;
    applyFillMode();

    if (playedNative) {
        const vid = document.getElementById('playerVideo');
        if (vid) {
            vid.addEventListener('error', () => retryWithTranscode(w, rel, token, info.codec), { once: true });
            // 还有一种更难查的失败：既不报错也不加载（readyState 一直是 0、没有 duration）。
            // 典型场景是浏览器在装「HEVC 视频扩展」之前就启动了，解码器没重新枚举，
            // canPlayType 说不行/行都不可靠。5 秒还拿不到元数据就按失败处理，直接转码。
            setTimeout(() => {
                if (token !== detailState.token) return;
                const still = document.getElementById('playerVideo');
                if (still === vid && vid.readyState === 0 && !vid.error) {
                    retryWithTranscode(w, rel, token, info.codec);
                }
            }, 5000);
        }
    }
}

// 原生解码失败后的兜底：转成 H.264 再播。只在前端判定「能直解」却实际失败时触发。
async function retryWithTranscode(w, rel, token, codec) {
    const stage = document.getElementById('playerStage');
    if (!stage || token !== detailState.token) return;
    const codecTag = codec
        ? `<span class="tag-hint">${escapeHtml(String(codec).toUpperCase())}</span>` : '';
    const segNav = vidNavHtml(w);       // 多段视频的段导航不能因为转码就消失
    stage.innerHTML = `<div class="player-box"><div class="player-empty">这个视频浏览器解不出来，正在转成 H.264…</div></div>`;
    const t = await transcodeVideo(rel, (pct) => {
        if (token !== detailState.token) return;
        const b = stage.querySelector('.player-empty');
        if (b) b.textContent = `这个视频浏览器解不出来，正在转成 H.264 ${pct >= 0 ? pct + '%' : '…'}`;
    }, () => token !== detailState.token);
    if (token !== detailState.token) return;
    if (t && t.ok && t.url) {
        stage.innerHTML = `
            <div class="player-box">
                <video id="playerVideo" controls autoplay playsinline
                       src="${t.url}" onerror="onPlayerError()"></video>
            </div>
            ${stageFootHtml(w, rel, codecTag + segNav, true)}`;
        applyFillMode();
    } else {
        stage.innerHTML = `
            <div class="player-box"><div class="player-empty">转码失败${t && t.error ? '：' + escapeHtml(t.error) : ''}<br>
                <span style="font-size:12px">可以点「📂 打开文件夹」到本地目录看原片</span></div></div>
            ${stageFootHtml(w, rel, segNav)}`;
    }
}

// 图文帖的媒体区：帖子里的文件**按序号逐张**展示
//   静图 → <img>；动图（抖音下载下来已经转成 mp4）→ <video> 直接播
// 所以一帖里「第一张是静图、第二张是动图」时，就是翻到第二页看视频，不需要任何切换按钮
function renderMediaStage(w) {
    const items = mediaItemsOf(w);
    if (!items.length) {
        // 没有可播放的媒体（只有 other 类型文件）：必须清空播放器并说明，
        // 否则右侧还停在上一个作品的画面上，看起来像「点错了」
        document.getElementById('playerStage').innerHTML =
            '<div class="player-empty">这个作品没有可播放的文件</div>';
        return;
    }
    const n = items.length;
    if (!(detailState.imgIndex >= 0 && detailState.imgIndex < n)) detailState.imgIndex = 0;
    const cur = items[detailState.imgIndex];
    const multi = n > 1;
    const motions = items.filter((it) => it.kind === 'video').length;

    const media = cur.kind === 'video'
        ? `<video id="playerVideo" src="${mediaUrl(cur.rel)}" controls autoplay loop playsinline
                  poster="${posterUrl(w, cur.rel)}" onerror="onPlayerError()"></video>`
        : `<img id="playerImg" src="${mediaUrl(cur.rel)}" alt="" onerror="onPlayerError()">`;
    const hint = motions
        ? `图文 ${n} 张 · 动图 ${motions}`
        : (multi ? `图文 ${n} 张` : '单图');

    document.getElementById('playerStage').innerHTML = `
        <div class="player-box">
            ${media}
            ${multi ? `<button class="media-step prev" onclick="stepImage(-1)">‹</button>
                       <button class="media-step next" onclick="stepImage(1)">›</button>
                       <span class="media-counter">${detailState.imgIndex + 1}/${n}</span>` : ''}
        </div>
        ${stageFootHtml(w, cur.rel, `<span class="tag-hint">${hint}</span>`)}`;
    applyFillMode();
}

// 把当前作品设为作者封面（视频用截帧）
function setHomeCover(rel) {
    const a = state.currentAuthor;
    if (!a || !rel) return;
    a.cover = { workFile: rel };
    setAccountField(a.uid, 'cover', a.cover);
    // 列表里那张卡片也立刻换成新封面（返回列表就能看到，不用刷新）
    const it = state.filtered.find((x) => x.uid === a.uid);
    if (it) { it.cover = a.cover; renderAuthorGrid(); }
    refreshTop();
    showToast('已设为封面');
}

function stepImage(dir) {
    const a = detailState.author;
    if (!a) return;
    const w = (a.works || [])[detailState.index];
    if (!w) return;
    const n = mediaItemsOf(w).length;
    if (n < 2) return;
    detailState.imgIndex = (detailState.imgIndex + dir + n) % n;
    renderMediaStage(w);
}

function stepWork(dir) {
    const pos = detailState.list.indexOf(detailState.index);
    if (pos < 0) return;
    const next = pos + dir;
    if (next < 0 || next >= detailState.list.length) return;
    playWork(detailState.list[next]);
    const cur = document.querySelector('#workRail .wall-card.active');
    if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest' });
}

function onPlayerError() {
    const hint = document.getElementById('playerHint');
    if (!hint) return;
    const w = (detailState.author && detailState.author.works[detailState.index]) || {};
    const cur = mediaItemsOf(w)[detailState.imgIndex] || {};
    hint.textContent = cur.kind === 'video'
        ? '⚠ 这段视频/动图没能播放（可能是编码问题），可以点「📂 打开文件夹」到本地目录看原片。'
        : '⚠ 这张图片加载失败，可以点「📂 打开文件夹」到本地目录看原图。';
}

function fieldLabel(field) {
    const map = { nickname: '昵称', alias: '别名', douyin_id: '抖音号', signature: '简介', status: '状态', note: '备注', sec_uid: 'SEC_UID' };
    return map[field] || field;
}


const VIDEO_EXT_RE = /\.(mp4|mov|avi|mkv|webm|flv)$/i;
const IMAGE_EXT_RE = /\.(jpeg|jpg|png|webp|gif)$/i;

// ===== 带「动图」的图文帖（抖音实况图） =====
// 这类帖子里每个「图」在磁盘上是两个文件：静帧 jpeg（2160×2880，约 1MB）+ 动效 mp4（720×960，约 3 秒，带原声）。
// 下载时动效已经转成 mp4 了，所以**不配对、不切静帧**：直接按序号当两个媒体项用
//   _1.jpeg → 第一张图（静图），_2.mp4 → 第二张（动图，当视频播）
// 序号是媒体流顺序，先后不定；张数也不一定相等。全库 2779 个作品是这种结构。
function isLivePost(w) {
    const files = (w && w.files) || [];
    if (files.length < 2) return false;
    let hasImg = false, hasVid = false;
    files.forEach((f) => {
        if (IMAGE_EXT_RE.test(f.rel)) hasImg = true;
        else if (VIDEO_EXT_RE.test(f.rel)) hasVid = true;
    });
    return hasImg && hasVid;
}

// 帖子里的媒体项，按文件名末尾的 `_序号` 排序：[{rel, kind:'image'|'video'}]
function mediaItemsOf(w) {
    const seqOf = (rel) => {
        const m = String(rel).match(/_(\d+)\.[a-z0-9]+$/i);
        return m ? Number(m[1]) : 1e9;
    };
    return ((w && w.files) || [])
        .slice()
        .sort((a, b) => seqOf(a.rel) - seqOf(b.rel))
        .map((f) => ({
            rel: f.rel,
            kind: VIDEO_EXT_RE.test(f.rel) ? 'video'
                : (IMAGE_EXT_RE.test(f.rel) ? 'image' : 'other'),
        }))
        .filter((it) => it.kind !== 'other');
}

// 作品分类：video / image_single / image_multi / live_single / live_multi
// 有图的帖子里，按**动图段数**分：1 段 → 单动图，2 段以上 → 多动图；没有动图再看图有几张
function workKindOf(w) {
    const items = mediaItemsOf(w);
    const imgs = items.filter((it) => it.kind === 'image').length;
    const motions = items.length - imgs;
    if (!imgs) return 'video';                       // 纯视频（没有图）→ 视频
    if (motions >= 2) return 'live_multi';           // 好几张都是动图 → 多动图
    if (motions === 1) return 'live_single';         // 只有一段动图（常见：一张静图 + 一段动图）
    return imgs > 1 ? 'image_multi' : 'image_single';
}

// 各分类作品数（左栏按钮上的数字，口径和 setRailFilter 完全一致）
function workTypeCounts(a) {
    const c = { all: 0, video: 0, image: 0, image_single: 0, image_multi: 0, live_single: 0, live_multi: 0 };
    ((a && a.works) || []).forEach((w) => {
        const k = workKindOf(w);
        c.all += 1;
        if (k === 'video') {
            c.video += 1;           // 视频只算一次（别再走下面的分类自增，否则会翻倍）
        } else {
            c.image += 1;           // 图文 = 所有图 + 动图
            c[k] += 1;
        }
    });
    return c;
}

// ===== 作者等级（更新频率）=====
// 等级：只表示「这号有多重要」，**和更新频率无关**（频率自己在详情页点）。
// 同一组大小号共用一个等级（setLevel 会同步整组）。
const LEVEL_LIST = ['夯', '顶级', '一般', '拉'];
const LEVEL_STYLE = { '夯': 'hang', '顶级': 'top', '一般': 'lv1', '拉': 'lv2' };
// 2026-10-31 改名：'1级（次）'→'一般'、'2级（拉）'→'拉'。
// 存量标注已经迁移过（user_data.json 里现在存的是新值），但备份恢复/老导出文件
// 里仍可能带旧值；读的时候先过一层别名，免得那批账号的等级看起来「丢了」。
const LEVEL_ALIAS = { '1级（次）': '一般', '1级(次)': '一般', '2级（拉）': '拉', '2级(拉)': '拉' };
const LEVEL_HINT = '等级只表示重要程度，和更新频率无关：夯（最高）→ 顶级 → 一般 → 拉（最低）。同一组大小号共用一个等级。';

// 更新频率：day / 3天 / 周 / 月（独立于「作者等级」，用户可以自己改）
const FREQ_LIST = ['day', '3天', '周', '月'];
// 等级 → 建议的更新频率（设等级时自动带一个默认值，用户可再改）

// 旧值 → 新值（读的时候统一过一层，写回去的自然就是新值）
function normLevel(v) {
    const raw = String(v || '').trim();
    return LEVEL_ALIAS[raw] || raw;
}

function levelKey(level) {
    const v = normLevel(level);
    return LEVEL_STYLE[v] || (v ? 'other' : '');
}

// 作者的作品统计（现算，保证和左栏筛选一致）
function workStatsOf(a) {
    const ws = (a && a.works) || [];
    let video = 0;
    ws.forEach((w) => { if (workKindOf(w) === 'video') video++; });
    const dates = ws.map((w) => w.date).filter(Boolean).sort();
    const s = (a && a.stats) || {};
    return {
        work_count: ws.length,
        video_count: video,
        image_count: ws.length - video,
        earliest: dates[0] || s.earliest || '?',
        latest: dates.length ? dates[dates.length - 1] : (s.latest || '?'),
    };
}

// 一个作品里可能同时有「封面图 _1.jpeg」和「视频 _2.mp4」，必须按类型挑，不能盲取 files[0]
function pickVideoFile(w) {
    const files = (w && w.files) || [];
    return files.find((f) => VIDEO_EXT_RE.test(f.rel)) || files[0] || {};
}
function imageFilesOf(w) {
    return ((w && w.files) || []).filter((f) => IMAGE_EXT_RE.test(f.rel));
}
// 缩略图/海报优先用作品自带的封面图，没有再用 ffmpeg 抽帧
function pickPosterFile(w) {
    const imgs = imageFilesOf(w);
    if (imgs.length) return { rel: imgs[0].rel, isImage: true };
    const v = pickVideoFile(w);
    return v.rel ? { rel: v.rel, isImage: false } : null;
}
function posterUrl(w, fallbackRel) {
    const p = pickPosterFile(w);
    if (p) return p.isImage ? mediaUrl(p.rel) : '/thumb?path=' + encodeURIComponent(p.rel);
    return fallbackRel ? '/thumb?path=' + encodeURIComponent(fallbackRel) : '';
}

// 封面对象 -> 可用的图片地址（视频走 ffmpeg 截帧，图片用原图）
function coverUrlFrom(cover) {
    if (!cover) return '';
    if (cover.custom) return cover.custom;
    const rel = cover.workFile;
    if (!rel) return '';
    if (/\.(mp4|mov|avi|mkv|webm|flv)$/i.test(rel)) // 封面走 /cover（独立目录、永不淘汰）。以前借 /thumb，缩略图超上限被当垃圾删掉后
    // 要重新截帧，截帧一失败/一慢，看起来就像「我设的封面没了」
    return '/cover?path=' + encodeURIComponent(rel);
    return mediaUrl(rel);
}

function getCoverUrl(a) {
    return coverUrlFrom(a && a.cover);
}

// 封面图加载失败（比如 ffmpeg 截帧失败）→ 回退占位，不要留空白
function coverBroken(el) {
    // 先自动重试一次（带破缓存参数），还失败才退回占位。
    // 以前一次失败就直接换成 📱，看起来就像「封面丢了」，其实只是这次请求没成功。
    const src = el.getAttribute('src') || el.dataset.src || '';
    if (src && !el.dataset.retried) {
        el.dataset.retried = '1';
        el.setAttribute('src', src + (src.indexOf('?') >= 0 ? '&' : '?') + 'r=1');
        return;
    }
    const cls = el.className || '';
    const kind = cls.indexOf('alt-member-cover') >= 0 ? 'alt-member-cover'
        : (cls.indexOf('author-cover') >= 0 ? 'author-cover' : 'detail-cover');
    const size = kind === 'alt-member-cover' ? 16 : (kind === 'author-cover' ? 32 : 40);
    const div = document.createElement('div');
    div.className = kind + ' placeholder';
    div.textContent = '📱';
    div.style.cssText = `display:flex;align-items:center;justify-content:center;font-size:${size}px`;
    el.replaceWith(div);
}

// ===== 安全媒体 URL =====
function mediaUrl(rel) {
    return String(rel).split('/').map(encodeURIComponent).join('/');
}

// ===== 作品缩略图（懒加载；视频用 ffmpeg 缩略图接口，带浏览器缓存） =====

function thumbError(el) {
    const d = document.createElement('div');
    d.className = ((el.className || 'work-thumb') + ' placeholder').trim();
    d.textContent = '🎬';
    el.replaceWith(d);
}

let thumbObserver = null;
function getThumbObserver() {
    if (!thumbObserver) {
        if (typeof IntersectionObserver === 'undefined') {
            // 没有 IntersectionObserver 的环境（很老的浏览器、部分内嵌内核）：
            // 退化成「直接加载」，别因为懒加载让整页图片都空着
            thumbObserver = {
                observe(el) {
                    if (el.dataset && el.dataset.src) {
                        el.src = el.dataset.src;
                        delete el.dataset.src;
                    }
                },
                unobserve() {},
            };
        } else {
            thumbObserver = new IntersectionObserver(entries => {
                for (const en of entries) {
                    if (en.isIntersecting) {
                        const el = en.target;
                        if (el.dataset.src) {
                            el.src = el.dataset.src;
                            if (el.tagName === 'VIDEO') el.preload = 'metadata';
                            delete el.dataset.src;
                            if (el.complete) checkThumbRatio(el);
                        }
                        thumbObserver.unobserve(el);
                    }
                }
            }, { rootMargin: '200px' });
        }
    }
    return thumbObserver;
}

function observeThumbs(container) {
    const ob = getThumbObserver();
    container.querySelectorAll('[data-src]').forEach(el => ob.observe(el));
}

// ===== 媒体操作 =====
// 视频/图文都在右栏媒体区展示；浏览器放不了的编码由 /api/transcode 转码
// ===== 用户操作 =====
// 只上送本会话改过的账号（dirtyAccounts），绝不发全量。
// 服务端是合并写：只合并 body 里出现的账号，没出现的保留磁盘现状。
// 这样旧标签页/旧快照永远无法把过期数据写回去。
// 保存串行队列：所有保存排成一队。并发发两个请求时，先发的可能后到，
// 服务端按字段合并就会用旧值盖掉新值；串行化后顺序稳定。
let _saveChain = Promise.resolve();

function saveUserData() {
    _saveChain = _saveChain.then(() => saveUserDataOnce(), () => saveUserDataOnce());
    return _saveChain;
}

async function saveUserDataOnce() {
    try {
        const acc = (state.userData && state.userData.accounts) || {};
        // 快照：成功后只清掉这次真正送上去的部分，期间新加的脏标记要保留
        const uids = Array.from(state.dirtyAccounts);
        const versions = {};
        uids.forEach((uid) => { versions[uid] = state.dirtyVersion[uid] || 0; });
        const metaDirty = state.dirtyMeta;
        // 状态说明：只发这次改过的状态名（增量），两个标签页各写各的状态不会互相覆盖
        const noteKeys = Array.from(state.dirtyStatusNotes || []);
        if (!uids.length && !metaDirty && !noteKeys.length) return true;   // 没改过，不发请求

        const body = { accounts: {} };
        // 快照这次的 dirtyFields 一起，成功后按同样内容清（期间新加的字段要保留）
        const fieldSnap = {};
        uids.forEach((uid) => {
            const src = acc[uid];
            if (!src) return;
            const fs = state.dirtyFields[uid];
            const keys = fs ? Object.keys(fs) : [];
            if (!keys.length) {
                // 兜底：没记到具体字段（老代码路径 / 手工改的整条对象）就整条发。
                // 正常路径不会走到这里 —— 所有改动都该带上字段名。
                body.accounts[uid] = src;
                return;
            }
            fieldSnap[uid] = keys.slice();
            const part = {};
            keys.forEach((k) => {
                // 内存里已经没有这个键 = 要删掉它（服务端约定 null = 删字段）。
                part[k] = (k in src) ? src[k] : null;
            });
            body.accounts[uid] = part;
        });
        if (metaDirty && state.userData && state.userData.alt_group_names) {
            body.alt_group_names = state.userData.alt_group_names;
        }
        if (noteKeys.length) {
            body.status_notes = {};
            noteKeys.forEach((s) => {
                const v = (state.statusNotes || {})[s];
                // 清空 → 显式 null（服务端按「null = 删除这个键」处理）
                body.status_notes[s] = (typeof v === 'string' && v.trim()) ? v : null;
            });
        }
        const resp = await fetch('/api/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const d = await resp.json().catch(() => ({}));
        if (!resp.ok || !d.ok) {
            showToast('保存失败：' + (d.error || ('HTTP ' + resp.status)) + '（未写入，请重试）');
            return false;
        }
        uids.forEach((uid) => {
            // 只有「期间没被再改过」的账号才能摘掉脏标记
            if ((state.dirtyVersion[uid] || 0) === versions[uid]) {
                state.dirtyAccounts.delete(uid);
                delete state.dirtyFields[uid];
            } else {
                // 保存期间又改了：只摘掉这次真正送上去的那几个字段，
                // 期间新加的字段留着下次发（否则那笔改动永远送不出去）
                const sent = fieldSnap[uid];
                const cur = state.dirtyFields[uid];
                if (sent && cur) {
                    sent.forEach((k) => { delete cur[k]; });
                    if (!Object.keys(cur).length) delete state.dirtyFields[uid];
                }
            }
        });
        if (metaDirty) state.dirtyMeta = false;
        noteKeys.forEach((s) => state.dirtyStatusNotes.delete(s));
        return true;
    } catch (e) {
        showToast('保存失败：' + (e && e.message || e) + '（未写入，请重试）');
        return false;
    }
}

function setAccountField(uid, field, value) {
    const a = state.currentAuthor;
    if (!a || a.uid !== uid) return;
    if (field === 'nickname' || field === 'douyin_id' || field === 'signature' || field === 'alias') {
        const old = a[field];
        if (old !== value) {
            a.history = a.history || [];
            a.history.push({ field, from: old || '', to: value, time: new Date().toLocaleString() });
            a[field] = value;
        }
    } else {
        a[field] = value;
    }
    state.userData.accounts = state.userData.accounts || {};
    state.userData.accounts[uid] = state.userData.accounts[uid] || {};
    markDirty(uid, field);
    const ua = state.userData.accounts[uid];
    SAVE_FIELDS.forEach(k => {
        ua[k] = a[k];
    });
    saveUserData();
}

// 直接给某个 uid 写一个字段（不管它是不是当前打开的账号）。
// 群组同步（等级/更新频率）要用它：组成员可能没被打开，不能走 setAccountField
// 那条「只在 currentAuthor 上改」的路径。当前账号会在下方单独同步内存对象。
function setAccountFieldRaw(uid, field, value) {
    state.userData.accounts = state.userData.accounts || {};
    const ua = state.userData.accounts[uid] || (state.userData.accounts[uid] = {});
    ua[field] = value;
    markDirty(uid, field);
    // 如果改的正好是当前打开的作者，同步它的内存对象，界面立刻反映
    if (state.currentAuthor && state.currentAuthor.uid === uid) {
        state.currentAuthor[field] = value;
    }
}

function toggleStar(uid) {
    const a = state.currentAuthor;
    a.starred = !a.starred;
    setAccountField(uid, 'starred', a.starred);
    refreshMeta();
    // 就地更新列表卡片，不重拉分页（保留滚动位置和已加载的页数）
    const item = state.filtered.find(x => x.uid === uid);
    if (item) { item.starred = a.starred; renderAuthorGrid(); }
    refreshTop();
    showToast(a.starred ? '已星标' : '已取消星标');
}

// 重新拉取聚合信息（星标数/标签/年份），刷新侧边栏
async function refreshMeta() {
    try {
        state.meta = await fetch('/api/meta').then(r => r.json());
        renderStats();
        renderCategoryList();
        renderTagCloud();
        renderYearList();
    } catch (e) { /* 忽略 */ }
}

function setStatus(uid, value) {
    setAccountField(uid, 'status', value);
    // 2026-11-08：**不再自动清掉 update_freq**。
    // 老逻辑是「状态进了『不能设更新频率』的列表就 null 掉频率」，而且没有任何提示 ——
    // 用户只是把某个号标成「不好看了 / 私密账号 / 注销」，之前设的频率就凭空消失了，
    // 过两天改回「正常」也回不来，看着就像「数据自己变了」。
    // 现在改成：值原样保留，只是这类账号不参与频率筛选（服务端 _t_freq 里已排除）。
    refreshTop();
    refreshStatusCounts();   // 状态列/等级/频率计数跟着变
    showToast('状态已更新');
}

function setLevel(uid, value) {
    const cur = normLevel((state.userData && state.userData.accounts[uid] && state.userData.accounts[uid].level) || '');
    const final = (value === cur) ? '' : value;   // 再点一次 = 取消
    // 群组账号自动同步相同等级：当前账号所在大小号组的所有成员一起改
    const targets = groupUidsWithSelf(uid);
    targets.forEach((u) => setAccountFieldRaw(u, 'level', final));
    // 等级**不再**联动更新频率（用户明确要求）：以前设「夯」会自动填「day」，
    // 结果改等级就把频率改了，很别扭。现在等级只跟大小号有关（整组同等级，见上面的 targets），
    // 频率想在详情页单独点。
    saveUserData();
    refreshTop();
    refreshStatusCounts();   // 等级计数和状态共用同一次统计
    showToast(final ? `等级已设为「${final}」${targets.length > 1 ? '（已同步整组 ' + targets.length + ' 人）' : ''}` : '已清除等级');
}

// 当前账号 + 它所在大小号组的所有成员 uid（含自己）。单人就是 [自己]。
function groupUidsWithSelf(uid) {
    const acc = (state.userData && state.userData.accounts) || {};
    const a = acc[uid] || {};
    const alts = Array.isArray(a.alt_accounts) ? a.alt_accounts : [];
    const set = new Set([uid, ...alts]);
    return Array.from(set);
}

// 更新频率：day / 3天 / 周 / 月。点选即生效（做成标签多选样式的单选）
// 点当前已选中的那个 = 取消（清空）
// 某些账号状态（注销/禁言/封号/卖号等，见设置）不能设更新频率——这些账号没有「更新」可言。
function canSetFreq(uid) {
    const a = (state.userData && state.userData.accounts[uid]) || {};
    const st = (a.status || '').trim();
    return (state.updateFreqDisabledStatus || []).indexOf(st) < 0;
}

function setFreq(uid, value) {
    if (!canSetFreq(uid)) {
        showToast('这个账号的状态不允许设置更新频率（可在设置里调整）');
        return;
    }
    const cur = ((state.userData && state.userData.accounts[uid] && state.userData.accounts[uid].update_freq) || '').trim();
    const final = (value === cur) ? '' : value;   // 再点一次 = 取消
    // 更新频率**只改当前账号**，不跟着大小号走（2026-11-02 用户要求取消这个关联）：
    // 同一组小号可能发得勤、大号几个月不动，频率本来就该各自记；
    // 联动之后改一个人的频率会把整组一起改掉，反而不好用。
    // 等级仍然整组同步（setLevel 里 targets），那是「重要程度」不是「更新节奏」。
    setAccountFieldRaw(uid, 'update_freq', final);
    saveUserData();
    refreshTop();
    showToast(final ? `更新频率已设为「${final}」` : '已清除更新频率');
}

// 等级列（侧边栏「等级」栏：夯 / 顶级 / 一般 / 拉）
function openLevelView(level) {
    state.currentLevel = level;
    state.currentStatus = null;
    state.currentFreq = null;
    state.currentCategory = 'all';
    clearTagYearFilter();
    state.returnView = 'authors';
    clearSideActive();
    document.querySelectorAll('#levelList .side-item').forEach(i => {
        i.classList.toggle('active', i.dataset.level === level);
    });
    setViewTitle(level);
    applyFilters();
}

// 点「更新频率」栏：只筛出该更新频率的账号（day / 3天 / 周 / 月）
function openFreqView(freq) {
    state.currentFreq = freq;
    state.currentLevel = null;
    state.currentStatus = null;
    state.currentCategory = 'all';
    clearTagYearFilter();
    state.returnView = 'authors';
    clearSideActive();
    document.querySelectorAll('#freqList .side-item').forEach(i => {
        i.classList.toggle('active', i.dataset.freq === freq);
    });
    setViewTitle(freq);
    applyFilters();
}

// 标签的新增/移除：uid 一律取当前作者（详情页里只可能改当前这个账号）
// 去重：已经有的标签不再加，避免「美妆」和「美妆」两条一样的
function addTag(tag) {
    tag = String(tag || '').trim();
    if (!tag) return;
    const a = state.currentAuthor;
    if (!a) return;
    a.tags = a.tags || [];
    if (a.tags.indexOf(tag) >= 0) { showToast('「' + tag + '」已经在标签里了'); return; }
    a.tags.push(tag);
    setAccountField(a.uid, 'tags', a.tags);
    refreshMeta();
    refreshTop();
    showToast('已添加标签「' + tag + '」');
}

function removeTag(tag) {
    const a = state.currentAuthor;
    if (!a) return;
    tag = String(tag || '').trim();
    if (!tag) return;
    const before = (a.tags || []).length;
    a.tags = (a.tags || []).filter(t => t !== tag);
    if (a.tags.length === before) return;
    setAccountField(a.uid, 'tags', a.tags);
    refreshMeta();
    refreshTop();
    showToast('已移除标签「' + tag + '」');
}

function setNote(uid, value) {
    setAccountField(uid, 'note', value);
    showToast('备注已保存');
}



// SEC_UID 太长：平时只显示头尾，点「更改」才展开成长输入框
// 把用户粘进来的东西统一抽成纯 sec_uid。
// 以后手动填的时候大概率是直接复制整条主页网址
//（https://www.douyin.com/user/MS4wLjABAAAA...），所以这里必须能吃网址：
//   https://www.douyin.com/user/MS4w...            → MS4w...
//   https://www.douyin.com/user/MS4w...?vid=123    → MS4w...
//   https://www.iesdouyin.com/share/user/MS4w...   → MS4w...
//   MS4wLjABAAAA...                                → 原样
// 认不出来（比如 v.douyin.com 短链）返回空串，由调用方提示。
function parseSecUid(input) {
    let v = String(input || '').trim();
    if (!v) return '';
    // 复制来的网址常带换行/零宽字符，先清掉
    v = v.replace(/[\s\u200b\u200c\u200d\u200e\u200f\ufeff]+/g, '');
    const m = v.match(/\/user\/([A-Za-z0-9_-]+)/);
    if (m) return m[1];
    if (/^[A-Za-z0-9_-]{10,}$/.test(v)) return v;
    return '';
}

// SEC_UID → 抖音主页地址。**永远用纯 id 重新拼**，所以
// ① 老数据里即使存成了整条网址，点开也能到真正的主页；
// ② 界面直接把这一段当链接显示，不需要单独的「主页」按钮。
function secUrl(secUid) {
    const id = parseSecUid(secUid) || String(secUid || '');
    return 'https://www.douyin.com/user/' + encodeURIComponent(id);
}

// 界面上只显示「一段」地址（完整地址放 title，点开仍是真正的主页）：
//   https://www.douyin.com/user/MS4wLjAB…
// 这样既能看出是 https 主页链接，又不会把整行撑爆。
function secShortText(secUid) {
    // 2026-10-31 (c76)：不再截成「前 12 字 + …」。
    // 原来固定截断，宽屏时这颗链接只有 99px，右边空一大片，用户说「太短了，网页长它就长」。
    // 现在返回完整 id，显示多少由 CSS 分配（.sec-val 是 flex:1 1 auto + ellipsis），
    // 窄了自动省略号、宽了就整条铺开，且不会掉行（.field-sec 是 nowrap）。
    return parseSecUid(secUid) || String(secUid || '');
}

function startSecEdit() {
    detailState.secEdit = true;
    refreshTop();
    const raf = window.requestAnimationFrame || ((fn) => setTimeout(fn, 16));
    raf(() => {
        const el = document.getElementById('secUidInput');
        if (el) { el.focus(); el.select(); }
    });
}

function cancelSecEdit() {
    detailState.secEdit = false;
    refreshTop();
}

// ===== 历史记录：展开 / 编辑 / 删除 / 手动新增 =====
function toggleHistory() {
    detailState.histOpen = !detailState.histOpen;
    refreshTop();
}

function saveHistory(arr) {
    const a = state.currentAuthor;
    if (!a) return;
    a.history = arr;
    setAccountField(a.uid, 'history', arr);
    refreshTop();
}

function editHistoryEntry(idx) {
    const a = state.currentAuthor;
    if (!a || !a.history || !a.history[idx]) return;
    const cur = a.history[idx];
    const val = prompt('修改这条记录的内容：', cur.to || '');
    if (val === null) return;
    const arr = a.history.slice();
    arr[idx] = Object.assign({}, cur, { to: val });
    saveHistory(arr);
    showToast('记录已修改');
}

function delHistoryEntry(idx) {
    const a = state.currentAuthor;
    if (!a || !a.history || !a.history[idx]) return;
    if (!confirm('删除这条历史记录？')) return;
    const arr = a.history.slice();
    arr.splice(idx, 1);
    saveHistory(arr);
    showToast('已删除');
}

function addHistoryEntry() {
    const a = state.currentAuthor;
    if (!a) return;
    const val = prompt('新增一条记录（会记在当前时间）：');
    if (!val) return;
    const arr = (a.history || []).slice();
    arr.push({ field: 'note', from: '', to: val, time: new Date().toLocaleString() });
    saveHistory(arr);
    detailState.histOpen = true;
    refreshTop();
    showToast('已添加');
}

// 保存手填的 SEC_UID
function saveSecUid(value) {
    const a = state.currentAuthor;
    if (!a) return;
    const raw = String(value || '').trim();
    if (!raw) { showToast('SEC_UID 不能为空'); return; }
    // 允许直接粘贴 https://www.douyin.com/user/… 整条网址，这里抽成纯 id 再存
    const val = parseSecUid(raw);
    if (!val) {
        showToast('没认出 SEC_UID：请粘贴 https://www.douyin.com/user/… 这种主页地址，或 MS4wLjAB… 开头的纯 ID');
        return;
    }
    if (!/^MS4wLjAB/.test(val)) {
        if (!confirm('这段看起来不像 SEC_UID（一般以 MS4wLjAB 开头），仍然保存吗？')) return;
    }
    setAccountField(a.uid, 'sec_uid', val);
    detailState.secEdit = false;
    refreshTop();
    showToast(val === raw ? 'SEC_UID 已保存' : '已从网址里取出 SEC_UID 并保存');
}

// 按 SEC_UID 从抖音拉昵称 / 抖音号 / 简介
// 获取资料：空字段直接补；已有值的字段算「冲突」，交给用户选（只换昵称/抖音号/全部替换）
let pendingProfile = null;

async function fetchProfile() {
    const a = state.currentAuthor;
    if (!a) return;
    const input = document.getElementById('secUidInput');
    const sec = ((input && input.value) || a.sec_uid || '').trim();
    if (!sec) {
        // 旧写法 `if (!sec && !a.uid)` 永远为假（uid 必然存在），提示是死分支。
        // 没有 SEC_UID 时后端只能拿 uid 试，成功率低但存在，所以提示而不阻断。
        showToast('没有 SEC_UID，用 UID 试着获取（成功率低，建议先填 SEC_UID）');
    }
    if (sec && sec !== (a.sec_uid || '')) setAccountField(a.uid, 'sec_uid', sec);

    showToast('正在获取抖音资料…');
    try {
        const resp = await fetch('/api/fetch_profile', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sec_uid: sec, uid: a.uid }),
        });
        const d = await resp.json();
        if (!d.ok) { showToast('获取失败：' + (d.error || '未知错误')); return; }
        if (d.sec_uid && !a.sec_uid) setAccountField(a.uid, 'sec_uid', d.sec_uid);
        handleFetchedProfile(d, a);
        // 顺便把抖音作品数一起更新（静默，不弹「抖音当前 N 篇」的重复提示）
        refreshWorkCount(a.uid, true);
    } catch (e) {
        showToast('获取失败：' + e.message);
    }
}

// 详情页「推送到待下载」：把当前作者推进待下载区（本地查询服务 8790），
// 之后在「导出作者信息 → ① 选作者 → 待下载区」里就能直接勾到他，不用再回抖音找。
// 队列里判重用 uid / sec_uid，重复推只会提示「已经在里面了」，不会建两条。
async function pushToQueue() {
    const a = state.currentAuthor;
    if (!a) return;
    if (!a.uid && !a.sec_uid) { showToast('没有 UID / SEC_UID，推不了'); return; }
    showToast('正在推送…');
    try {
        const r = await fetch(QUEUE_BASE + '/queue/add', {
            method: 'POST', cache: 'no-store',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                uid: a.uid || '',
                sec_uid: a.sec_uid || '',
                nickname: a.nickname || '',
                douyin_id: a.douyin_id || '',
                level: a.level || '',
                status: a.status || '',
                tags: a.tags || [],
                signature: a.signature || '',
            }),
        });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const d = await r.json();
        if (!d.ok) { showToast('推送失败：' + (d.error || '未知错误')); return; }
        // 队列面板开着的话同步一份，切过去就是最新的
        try { if (document.getElementById('queueBody')) await refreshQueue(); } catch (e) { }
        showToast(d.duplicated ? 'TA 已经在待下载区里了' : '已推送到待下载区（导出作者信息 → ① 选作者 → 待下载区）');
    } catch (e) {
        // 8790 没起是最常见的原因：这是另一个服务，不是 8723 本体的接口
        showToast('推送失败：' + ((e && e.message) || e) + '（本地查询服务 8790 没启动？）');
    }
}

// 把拉回来的资料分成两类：原来空着的（直接补）、原来有值但不一样的（要用户拍板）
function handleFetchedProfile(d, a) {
    const fields = [
        ['nickname', '昵称', d.nickname],
        ['douyin_id', '抖音号', d.douyin_id],
        ['signature', '简介', d.signature],
    ];
    const fill = [], conflicts = [];
    fields.forEach(([key, label, val]) => {
        if (typeof val !== 'string' || !val.trim()) return;
        const cur = String(a[key] || '').trim();
        if (!cur) fill.push({ key, label, val });
        else if (val.trim() !== cur) conflicts.push({ key, label, cur, val });
    });

    // 有备注（alias）时，列表/详情显示的都是备注，昵称换成什么都影响不到识别 →
    // 直接采用，不再弹窗问（2026-10-27 用户要求：有备注就别改备注，其他信息直接替换）
    if (String(a.alias || '').trim()) {
        for (let i = conflicts.length - 1; i >= 0; i--) {
            if (conflicts[i].key === 'nickname') fill.push(conflicts.splice(i, 1)[0]);
        }
    }

    fill.forEach((f) => setAccountField(a.uid, f.key, f.val));

    if (conflicts.length) {
        // 补上的字段必须立刻重绘，否则用户看到「抖音号：无」还以为没取到
        // （上一版这里直接 return，抖音号其实已写入，只是页面没刷）
        if (fill.length) refreshTop();
        openProfileChoice(conflicts, fill, d);
        return;
    }
    if (fill.length) { finishProfileApply(`已补全 ${fill.length} 项资料`); return; }
    finishProfileApply(d.partial
        ? '资料没有变化（这次只拿到公开资料，简介需要登录态，稍后再试或重启服务）'
        : '资料没有变化');
}

function finishProfileApply(msg) {
    const a = state.currentAuthor;
    detailState.secEdit = false;
    refreshTop();
    if (a) {
        const it = state.filtered.find((x) => x.uid === a.uid);
        if (it) {
            it.nickname = a.nickname;
            it.douyin_id = a.douyin_id;
            renderAuthorGrid();
        }
    }
    showToast(msg);
}

// 冲突选择：只换昵称 / 只换抖音号 / 只换简介（有冲突才出现）/ 全部替换 / 取消
function openProfileChoice(conflicts, fill, d) {
    fill = fill || [];
    pendingProfile = { conflicts, filledCount: fill.length };
    const box = document.getElementById('profileChoice');
    const body = document.getElementById('profileChoiceBody');
    const sub = document.getElementById('profileChoiceSub');
    if (!box || !body) return;
    if (sub) {
        sub.textContent = fill.length
            ? `已自动补全 ${fill.length} 项空字段，下面这些原来有值`
            : (d && d.partial ? '（这次只拿到公开资料，简介可能不完整）' : '');
    }
    const rows = conflicts.map((c) => `
        <div class="pc-row">
            <span class="pc-label">${escapeHtml(c.label)}</span>
            <span class="pc-cur" title="${escapeHtml(c.cur)}">${escapeHtml(clip(c.cur, 26))}</span>
            <span class="pc-arrow">→</span>
            <span class="pc-new" title="${escapeHtml(c.val)}">${escapeHtml(clip(c.val, 26))}</span>
        </div>`).join('');
    // 本地为空的字段是「直接补、不用问」的，但也得让用户看见补了什么，
    // 否则会出现「明明抖音有抖音号，弹窗里却没有它」的疑惑。
    const doneRow = fill.length
        ? `<div class="pc-filled"><span class="pc-label">已补全</span>${
            fill.map((f) => `<span class="pc-done">${escapeHtml(f.label)} ${escapeHtml(clip(f.val, 22))}</span>`).join('')
        }</div>`
        : '';
    const ops = conflicts.map((c) =>
        `<button class="btn btn-mini" onclick="applyProfilePatch('${c.key}')">只换${escapeHtml(c.label)}</button>`).join('');
    body.innerHTML = `${doneRow}<div class="pc-rows">${rows}</div>
        <div class="pc-ops">${ops}
            <button class="btn btn-mini btn-primary" onclick="applyProfilePatch('all')">全部替换</button>
            <button class="btn btn-mini" onclick="closeProfileChoice()">取消</button>
        </div>`;
    box.classList.remove('hidden');
}

function closeProfileChoice() {
    const box = document.getElementById('profileChoice');
    if (box) box.classList.add('hidden');
    pendingProfile = null;
}

function applyProfilePatch(which) {
    const a = state.currentAuthor;
    if (!a || !pendingProfile) return;
    let n = 0;
    pendingProfile.conflicts.forEach((c) => {
        if (which === 'all' || which === c.key) {
            setAccountField(a.uid, c.key, c.val);
            n++;
        }
    });
    const filled = pendingProfile.filledCount || 0;
    closeProfileChoice();
    finishProfileApply(`已更新 ${n} 项资料${filled ? `（另补全 ${filled} 项空字段）` : ''}`);
}

function clip(s, n) {
    s = String(s || '');
    return s.length > n ? s.slice(0, n) + '…' : s;
}

// ===== 大小号关联：组模型（多向全连，不是二元边） =====
// 数据结构：user_data.accounts[uid].alt_accounts = [同组其他所有人 uid]（不含自己）
// 不变量：x ∈ alt[y] ⇔ y ∈ alt[x]，且同组任意两人看到的其他成员集合完全相同
// 数量关系：组大小 = alt_accounts.length + 1
// 于是「A 填了 B、C、D」→ 四个人各自的 alt 都是另外三个人，天然全连。

// 组内成员的显示名：别名优先（有别名就不显示原昵称，pill 太窄），回退昵称。
// 同组里重名时补作品数区分，例如「kitt霓（221）」。
function altLabels(a) {
    const arr = (a && a.alt_accounts) || [];
    const info = (a && a.alt_info) || {};
    const acc = (state.userData && state.userData.accounts) || {};
    const items = arr.map((uid) => {
        const it = info[uid] || {};
        const an = acc[uid] || {};
        const alias = String(it.alias || an.alias || '').trim();
        const fallback = String(it.nickname || an.nickname || '').trim();
        return {
            uid,
            alias,
            name: alias || fallback,
            origin: alias ? fallback : '',
            works: it.work_count || 0,
        };
    });
    const seen = {};
    items.forEach((n) => { const k = n.name || '未命名'; seen[k] = (seen[k] || 0) + 1; });
    items.forEach((n) => {
        n.label = n.name || '未命名账号';
        if (seen[n.name || '未命名'] > 1) n.label += n.works ? '（' + n.works + '）' : '（重名）';
    });
    return items;
}

// 单个 uid 的显示名（confirm 文案里用），拿不到就回退成「这个账号」
function altDisplayName(a, uid) {
    const info = (a && a.alt_info) || {};
    const acc = (state.userData && state.userData.accounts) || {};
    const it = info[uid] || {};
    const an = acc[uid] || {};
    return String(it.alias || an.alias || it.nickname || an.nickname || '').trim() || '这个账号';
}

// 取 uid 所属的整组（含自己）
function altGroupOf(uid) {
    const acc = (state.userData && state.userData.accounts) || {};
    let arr = acc[uid] && Array.isArray(acc[uid].alt_accounts) ? acc[uid].alt_accounts : null;
    if (!arr) {
        const a = state.currentAuthor;
        if (a && a.uid === uid) arr = a.alt_accounts || [];
    }
    const set = new Set((arr || []).filter((x) => x && x !== uid));
    set.add(uid);
    return set;
}

// 把 uid 设为一组里的「大号」：整组所有成员（含自己）的 main_uid 都指向它。
// 一组只能有一个大号，其余是「小号」。大号只在大小号视图里加深显示，无其它作用。
// 若当前账号已经是这组的大号（main_uid === uid），再点就是「取消大号」。
async function setMainAccount(uid) {
    if (!uid) return;
    const group = altGroupOf(uid);
    if (group.size < 2) { showToast('当前账号还没有关联其他账号，先关联再设大号'); return; }
    const acc = state.userData.accounts || (state.userData.accounts = {});
    const name = altDisplayName(state.currentAuthor, uid);
    const cur = acc[uid] && acc[uid].main_uid;
    const isMain = cur === uid;   // 已经是这组的大号 → 本次是取消
    group.forEach((u) => {
        const entry = acc[u] || (acc[u] = {});
        entry.main_uid = isMain ? '' : uid;
        markDirty(u, 'main_uid');
        const a = state.currentAuthor;
        if (a && a.uid === u) a.main_uid = isMain ? '' : uid;
    });
    await saveUserData();
    refreshTop();
    // 若正停在大小号视图则重拉，让「大号」加深立刻生效
    if (state.view === 'alts') loadAltGroups();
    showToast(isMain ? '已取消「' + name + '」的大号' : '已将「' + name + '」设为这组的大号');
}

// 取消某账号的大号标记（清掉整组的 main_uid）。已被 setMainAccount 的 toggle 覆盖，
// 这里保留一个独立入口，供其它需要「强制取消」的场景使用。

// 把一组 uid 写成「团」：组内每个人 alt = 组内其他人。
// 单人组不留空数组（删掉字段，保持 user_data 干净）。
function writeAltGroup(members) {
    const list = Array.from(new Set(Array.from(members).filter(Boolean))).sort();
    state.userData.accounts = state.userData.accounts || {};
    const acc = state.userData.accounts;
    list.forEach((u) => {
        const others = list.filter((x) => x !== u);
        const entry = acc[u] || (acc[u] = {});
        markDirty(u, 'alt_accounts');
        // 单人组显式写空数组：服务端合并写约定「null/空 = 清除」。
        // 不能用 delete：合并写遇到「键不存在」会保留磁盘旧值，解除关联就失效了。
        if (others.length) entry.alt_accounts = others;
        else entry.alt_accounts = [];
        const a = state.currentAuthor;      // 当前打开的就是这个账号时同步内存对象，界面立刻反映
        if (a && a.uid === u) {
            if (others.length) a.alt_accounts = others.slice();
            else delete a.alt_accounts;     // 内存对象直接删，渲染按「无关联」走
        }
    });
    // 等级跟着组走：同一组大小号必须同一个等级（用户明确要求「等级只和大小号关联」）。
    // 新拉进来的号也要继承，否则刚关联完等级是空的，看着像没关联上。
    if (list.length > 1) {
        const curUid = (state.currentAuthor && state.currentAuthor.uid) || '';
        let lv = normLevel(list.indexOf(curUid) >= 0 ? ((acc[curUid] || {}).level || '') : '');
        if (!lv) {
            for (const u of list) {
                const v = normLevel((acc[u] || {}).level);
                if (v) { lv = v; break; }
            }
        }
        if (lv) {
            list.forEach((u) => {
                const e = acc[u] || (acc[u] = {});
                if ((e.level || '') !== lv) { e.level = lv; markDirty(u, 'level'); }
            });
            if (state.currentAuthor && list.indexOf(state.currentAuthor.uid) >= 0) {
                state.currentAuthor.level = lv;
            }
        }
    }
    return list;
}

// 历史遗留数据自愈：老逻辑只写两端，会出现「A 关联了 B、C，但 B 只知道 A」。
// 用并查集把 alt_accounts 里的边求连通分量，再把每个分量重写成团。
// 返回被修正的条目数（幂等，重复跑不会再有变化）。
function normalizeAltGroups() {
    const acc = (state.userData && state.userData.accounts) || {};
    const parent = {};
    const find = (x) => {
        while (parent[x] !== x) { parent[x] = parent[parent[x]]; x = parent[x]; }
        return x;
    };
    const union = (a, b) => {
        const ra = find(a), rb = find(b);
        if (ra !== rb) parent[rb] = ra;
    };
    const touch = (u) => { if (parent[u] === undefined) parent[u] = u; };

    Object.keys(acc).forEach((u) => {
        touch(u);
        const arr = acc[u] && Array.isArray(acc[u].alt_accounts) ? acc[u].alt_accounts : [];
        arr.forEach((v) => { if (v) { touch(v); union(u, v); } });
    });

    const groups = {};
    Object.keys(parent).forEach((u) => {
        const r = find(u);
        (groups[r] = groups[r] || []).push(u);
    });

    let changed = 0;
    Object.keys(groups).forEach((r) => {
        const g = groups[r];
        if (g.length < 2) {
            // 单人：清掉脏数据里可能残留的空数组（内存对象直接删；
            // 上送层由 writeAltGroup 的空数组约定负责，落盘前服务端 normalize 也会清）
            g.forEach((u) => {
                if (acc[u] && Array.isArray(acc[u].alt_accounts) && acc[u].alt_accounts.length === 0) {
                    delete acc[u].alt_accounts; changed++; markDirty(u, 'alt_accounts');
                }
            });
            return;
        }
        g.forEach((u) => {
            const others = g.filter((x) => x !== u).sort();
            const entry = acc[u] || (acc[u] = {});
            const old = Array.isArray(entry.alt_accounts) ? entry.alt_accounts.slice().sort() : null;
            if (!old || old.join('|') !== others.join('|')) {
                entry.alt_accounts = others; changed++; markDirty(u, 'alt_accounts');
            }
        });
    });
    return changed;
}

// ===== 关联大小号：账号搜索选择器（不是只认 UID） =====
// 搜的是「人」，所以固定 scope=account：昵称 / 别名 / 备注 / 抖音号 / UID / sec_uid / 作者简介。
// 不能用默认的 all —— 那个还会搜每个作品的简介（desc），搜一个词能蹦出几十个
// 只是「作品里提到过这个词」的陌生人，看起来像误加了一堆账号。
const altPickerState = { uid: null, timer: null, q: '', items: [], total: 0 };

function linkAltAccount(uid) {
    openAltPicker(uid);
}

function openAltPicker(uid) {
    const a = state.currentAuthor;
    if (!a || a.uid !== uid) return;
    altPickerState.uid = uid;
    altPickerState.q = '';
    altPickerState.items = [];
    altPickerState.total = 0;
    document.getElementById('altPicker').classList.remove('hidden');
    const input = document.getElementById('altPickerInput');
    if (input) input.value = '';
    renderAltPickerHead();
    // 空查询先给一批「作品数最多」的账号，没关键词时也能直接挑
    doAltPickerSearch('');
    const raf = window.requestAnimationFrame || ((fn) => setTimeout(fn, 16));
    raf(() => { if (input) input.focus(); });
}

function closeAltPicker() {
    const box = document.getElementById('altPicker');
    if (box) box.classList.add('hidden');
    if (altPickerState.timer) { clearTimeout(altPickerState.timer); altPickerState.timer = null; }
    altPickerState.uid = null;
}

function altPickerSearch(value, now) {
    const q = (value || '').trim();
    if (altPickerState.timer) clearTimeout(altPickerState.timer);
    if (now) { doAltPickerSearch(q); return; }
    altPickerState.timer = setTimeout(() => doAltPickerSearch(q), 250);
}

async function doAltPickerSearch(q) {
    const box = document.getElementById('altPickerResult');
    if (!box || !altPickerState.uid) return;
    altPickerState.q = q;
    if (!altPickerState.items.length) box.innerHTML = '<div class="alt-picker-empty">搜索中…</div>';
    try {
        const params = new URLSearchParams({ offset: 0, limit: 30, sort: q ? 'nickname' : 'work_count', scope: 'account' });
        if (q) params.set('q', q);
        const d = await fetch('/api/authors?' + params.toString()).then((r) => r.json());
        if (altPickerState.q !== q) return;          // 有更新的请求，丢弃旧结果
        altPickerState.items = d.items || [];
        altPickerState.total = d.total || 0;
        renderAltPickerResult();
    } catch (e) {
        box.innerHTML = '<div class="alt-picker-empty">搜索失败：' + escapeHtml(e.message) + '</div>';
    }
}

function renderAltPickerHead() {
    const uid = altPickerState.uid;
    if (!uid) return;
    const a = state.currentAuthor;
    const g = altGroupOf(uid);
    const el = document.getElementById('altPickerGroup');
    if (el) {
        const nick = (a && a.uid === uid) ? plainName(a) : uid;
        el.textContent = `给「${nick}」关联 · ` + (g.size > 1 ? `本组已有 ${g.size} 个账号` : '还没有关联');
    }
}

function renderAltPickerResult() {
    const box = document.getElementById('altPickerResult');
    if (!box) return;
    const uid = altPickerState.uid;
    const group = altGroupOf(uid);
    const items = altPickerState.items || [];
    const rows = [];

    // 输入像 UID（纯数字）且结果里没有它时，给一行「直接关联这个 UID」
    const raw = (altPickerState.q || '').trim();
    if (/^\d{6,}$/.test(raw) && !items.some((it) => it.uid === raw)) {
        rows.push(`<div class="alt-picker-row raw-uid">
            <div class="alt-picker-avatar placeholder">🔗</div>
            <div class="alt-picker-info">
                <div class="alt-picker-name">直接关联 UID ${escapeHtml(raw)}</div>
                <div class="alt-picker-meta">库里没有这个 UID 的资料，仍可先建立关联</div>
            </div>
            ${group.has(raw) ? '<span class="alt-picker-tag">已在组内</span>'
                : `<button class="btn btn-mini" onclick="altPickerPick('${onClickArg(raw)}')">关联</button>`}
        </div>`);
    }

    items.forEach((it) => {
        const inGroup = group.has(it.uid);
        const isSelf = it.uid === uid;
        const cover = coverUrlFrom(it.cover);
        const avatar = cover
            ? `<img class="alt-picker-avatar" src="${cover}" loading="lazy" alt="" onerror="coverBroken(this)">`
            : `<div class="alt-picker-avatar placeholder">📱</div>`;
        let action;
        if (isSelf) action = '<span class="alt-picker-tag">当前账号</span>';
        else if (inGroup) action = `<button class="btn btn-mini" title="从本组移出" onclick="altPickerUnpick('${it.uid}')">已在组内</button>`;
        else action = `<button class="btn btn-mini" onclick="altPickerPick('${it.uid}')">关联</button>`;
        const meta = [
            it.douyin_id ? '抖音号 ' + escapeHtml(it.douyin_id) : '',
            it.work_count ? it.work_count + ' 作品' : '',
        ].filter(Boolean).join(' · ') || '无更多信息';
        rows.push(`<div class="alt-picker-row${inGroup ? ' in-group' : ''}" title="UID ${it.uid}">
            ${avatar}
            <div class="alt-picker-info">
                <div class="alt-picker-name">${it.starred ? '⭐' : ''}${nameHtml(it)}</div>
                <div class="alt-picker-meta">${meta}</div>
            </div>
            ${action}
        </div>`);
    });

    box.innerHTML = rows.length ? rows.join('') : '<div class="alt-picker-empty">没搜到账号，换个昵称/抖音号试试，或直接粘贴 UID</div>';
    const foot = document.getElementById('altPickerFoot');
    if (foot) {
        foot.textContent = altPickerState.q
            ? `匹配 ${altPickerState.total} 个账号 · 同组账号会全部互相关联`
            : `按作品数显示前 ${items.length} 个 · 同组账号会全部互相关联`;
    }
}

async function altPickerPick(targetUid) {
    const uid = altPickerState.uid;
    if (!uid) return;
    if (!targetUid || targetUid === uid) { showToast('不能关联自己'); return; }

    const merged = altGroupOf(uid);
    altGroupOf(targetUid).forEach((x) => merged.add(x));
    const list = writeAltGroup(merged);
    await saveUserData();

    refreshTop();
    altChanged();
    renderAltPickerHead();
    renderAltPickerResult();          // 按钮就地变成「已在组内」
    showToast(`已并入同组：本组共 ${list.length} 个账号，组内全部互相关联`);
}

function altPickerUnpick(targetUid) {
    const uid = altPickerState.uid;
    if (!uid) return;
    unlinkAltAccount(uid, targetUid);
    renderAltPickerHead();
    renderAltPickerResult();
}

// 解除 uid 和它组内其他所有账号的关联（其余成员之间互相关联保持不变）
// altPicker 的「解除本号全部关联」和详情页的「解除关联」都走这里
async function detachAuthorAlts(uid) {
    if (!uid) return;
    const group = altGroupOf(uid);
    const others = Array.from(group).filter((u) => u !== uid);
    if (!others.length) { showToast('这个账号本来就没有和其他账号关联'); return; }
    const names = others.map((u) => altDisplayName(state.currentAuthor, u)).join('、');
    if (!confirm(`确定解除这个账号和其他账号的关联吗？\n\n会断开的账号：${names}\n\n（只断开它和这些人的关联；这些人之间仍然互相关联，不会删账号）`)) return;
    const rest = new Set(others);      // 其余成员重写成不含 uid 的团
    writeAltGroup(rest);
    writeAltGroup(new Set([uid]));     // uid 自己 alt 清空
    const ok = await saveUserData();
    refreshTop();                      // 详情页信息区就地重渲染（大小号/统计立即反映）
    altChanged();
    loadAltGroups();
    if (ok) showToast('已解除当前账号的全部关联');
}

// altPicker 里的入口：解除「当前账号」的全部关联
async function detachCurrentAlt() {
    const uid = altPickerState.uid;
    if (!uid) return;
    await detachAuthorAlts(uid);
    renderAltPickerHead();
    renderAltPickerResult();
}

// 新建作者：粘主页链接 → 后端查权威 uid → 建档 → 和当前账号关联
async function createAuthorPrompt() {
    const uid = altPickerState.uid;
    const link = prompt('粘贴新作者的主页链接（SEC-UID）：\n\n例：https://www.douyin.com/user/MS4wLjAB…\n\n会自动去掉后面的 ?from_tab_name=main&vid=… 之类参数，并自动查 UID、拉资料、和当前账号关联。');
    if (link === null) return;
    const val = String(link).trim();
    if (!val) { showToast('没输入链接'); return; }
    showToast('正在查询新作者资料…');
    try {
        const resp = await fetch('/api/create_author', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sec_uid: val, link_uid: uid }),
        });
        const d = await resp.json();
        if (!d.ok) { showToast('新建失败：' + (d.error || '未知错误')); return; }
        // 重新加载 user_data.json，把后端刚写入的新账号 + 新的大小号关联同步进前端内存。
        // 之前这里只调 refreshMeta()（侧边栏统计）和 loadAltGroups()（文件夹视图读磁盘），
        // 但 state.userData.accounts 还是旧的：详情页「大小号」区读 currentAuthor.alt_accounts、
        // altGroupOf 读 state.userData.accounts，都拿不到新关联 —— 表现就是「新建了作者却没自动关联」。
        try {
            const ud = await fetch('user_data.json').then((r) => (r.ok ? r.json() : null));
            if (ud && ud.accounts) {
                state.userData = ud;
                state.statusNotes = (ud.status_notes && typeof ud.status_notes === 'object') ? ud.status_notes : {};
                // 当前打开的账号也要同步最新 alt_accounts（否则详情页大小号区不刷新）
                if (state.currentAuthor && state.currentAuthor.uid) {
                    const fresh = ud.accounts[state.currentAuthor.uid];
                    if (fresh) {
                        state.currentAuthor.alt_accounts = fresh.alt_accounts || [];
                        state.currentAuthor.alt_info = fresh.alt_info;
                    }
                }
            }
        } catch (e) { /* 重载失败不致命，刷新页面也能看到 */ }
        await refreshMeta();
        altChanged();
        loadAltGroups();
        if (altPickerState.uid) { renderAltPickerHead(); renderAltPickerResult(); }
        // 详情页若正开着，就地把大小号区重绘，让新关联立刻可见
        if (state.currentAuthor && state.currentAuthor.uid === uid) refreshTop();
        const linked = (d.linked && d.linked.length)
            ? `，并和当前账号关联（组内共 ${d.linked.length + 1} 个账号）`
            : '';
        // 全新账号会自动抓昵称/抖音号/简介，把抓到的写到提示里，抓不全也如实说
        const got = [];
        if (d.nickname) got.push('昵称 ' + d.nickname);
        if (d.douyin_id) got.push('抖音号 ' + d.douyin_id);
        if (d.signature) got.push('简介 ' + (d.signature.length > 16 ? d.signature.slice(0, 16) + '…' : d.signature));
        const gotText = (!d.existed && got.length) ? `，已自动获取 ${got.join(' / ')}` : '';
        showToast(`已新建作者「${d.nickname || d.sec_uid.slice(0, 10)}…」${d.existed ? '（该账号已存在，资料已更新）' : ''}${linked}${gotText}`);
        if (d.profile_note) showToast(d.profile_note);
    } catch (e) {
        showToast('新建失败：' + e.message);
    }
}

// 解除单个关联：把 target 从组里摘出去。
// 组内是全连，所以摘掉一个人 = 他和组内其余所有人一起断开（组 >2 时先确认）。
// 解除单个关联：把 target 从组里摘出去。
// 组内是全连，摘掉一个人等于把他和组内其余所有人一起断开 —— 所以**任何一次移除都要先确认**，
// 不管组里剩几个人（之前只有组 >2 人才确认，两个人时点 × 直接就断了，容易误点）。
async function unlinkAltAccount(uid, targetUid) {
    const group = altGroupOf(uid);
    if (!group.has(targetUid)) { showToast('本来就没有关联'); return; }
    const name = altDisplayName(state.currentAuthor, targetUid);
    const total = group.size;                 // 组人数（含当前账号）
    const left = total - 2;                   // 移除后，除当前账号以外还剩几个人
    const msg = left > 0
        ? `确定把「${name}」从这个组里移出吗？\n\n` +
          `这一组共 ${total} 个账号，组内是「全关联」：\n` +
          `移出后它会和另外 ${total - 1} 个账号一起断开，\n` +
          `剩下的 ${left} 个账号仍然互相关联。`
        : `确定解除和「${name}」的关联吗？\n\n解除后你们互相都看不到对方（只是断开关联，不会删账号）。`;
    if (!confirm(msg)) return;

    group.delete(targetUid);
    writeAltGroup(group);                    // 剩余成员重写成团
    writeAltGroup(new Set([targetUid]));     // 被移除者单独成组（alt 清空）
    const ok = await saveUserData();
    refreshTop();
    altChanged();
    // 保存成功后立即重拉分组（服务端并查集重新聚合），保证侧边栏计数/大小号视图即刻反映
    if (ok) loadAltGroups();
    showToast(group.size > 1 ? `已移除，本组剩 ${group.size} 个账号` : '已解除关联');
}

// 解散整组：所有成员的大小号关系一起清掉（组 >2 时确认）

// ===== 左侧「大小号」入口：一「组」一个文件夹 =====
// 组来自 user_data 的 alt_accounts（并查集求连通分量）；文件夹名默认用组里作品最多的账号，
// 也可以自己改（存 user_data.alt_group_names[成员key]）。
function altGroupsFromUserData() {
    const acc = (state.userData && state.userData.accounts) || {};
    const parent = {};
    const find = (x) => {
        if (parent[x] === undefined) parent[x] = x;
        while (parent[x] !== x) { parent[x] = parent[parent[x]]; x = parent[x]; }
        return x;
    };
    const union = (a, b) => { const ra = find(a), rb = find(b); if (ra !== rb) parent[rb] = ra; };
    Object.keys(acc).forEach((u) => {
        find(u);
        const arr = Array.isArray(acc[u].alt_accounts) ? acc[u].alt_accounts : [];
        arr.forEach((v) => { if (v && v !== u) union(u, v); });
    });
    const map = {};
    Object.keys(parent).forEach((u) => {
        const r = find(u);
        (map[r] = map[r] || []).push(u);
    });
    return Object.keys(map).map((r) => map[r].sort()).filter((g) => g.length > 1);
}

function refreshAltEntryCount() {
    const el = document.getElementById('altEntryCount');
    if (!el) return;
    const n = altGroupsFromUserData().length;
    el.textContent = n ? n : '0';
}

// 大小号改过之后：清掉缓存的分组（下次进视图重新拉），侧边栏数字同步
function altChanged() {
    state.altGroups = [];
    refreshAltEntryCount();
    // 当前就停在大小号视图时立刻重拉，别等用户手动刷新
    if (state.view === 'alts') loadAltGroups();
}

async function openAltView() {
    forceCloseDetail();
    rememberViewScroll();
    state.view = 'alts';
    state.returnView = 'alts';
    hideAllViews();
    document.getElementById('altView').classList.remove('hidden');
    animateViewEnter('altView', 'neutral');
    clearSideActive();
    // 大小号现在挂在「分类」栏里（2026-10-28 重排）
    document.querySelectorAll('#categoryList .side-item').forEach(i => i.classList.toggle('active', i.dataset.view === 'alts'));
    // 大小号文件夹视图没有「导出」概念，隐藏导出按钮
    const eb = document.getElementById('exportBtn');
    if (eb) eb.classList.add('hidden');
    restoreViewScroll('alts');
    await loadAltGroups();
}

// ===== 账号状态列：每个状态一个侧边栏入口，点击后只筛出该状态的账号 =====
const STATUS_ORDER = ['私密账号', '注销', '禁言/封号', '停更/偶更', '卖号', '不好看了', '待修改'];

// 点状态列：只筛选该状态的账号（直接复用作者列表视图，不发新请求重建界面）
function openStatusView(status) {
    flushStatusNote();            // 上一条说明还没保存就切走 → 先落盘
    state.currentStatus = status;
    state.currentLevel = null;
    state.currentFreq = null;
    state.currentCategory = 'all';
    clearTagYearFilter();
    state.returnView = 'authors';
    // 高亮：清空三栏，点亮当前状态列（状态项现在挂在「分类」栏里）
    clearSideActive();
    document.querySelectorAll('#categoryList .side-item').forEach(i => {
        i.classList.toggle('active', i.dataset.status === status);
    });
    setViewTitle(status);
    updateStatusNoteBar();
    applyFilters();
}

// ===== 状态说明栏：每个账号状态写一段「我怎么理解它」 =====
// 只在「功能」里点某个状态进来时出现；存 user_data.status_notes，按 key 增量保存，
// 两个标签页各写各的状态不会互相覆盖。
const STATUS_NOTE_PLACEHOLDER = {
    '私密账号': '主页看不到作品（要关注才可见）？还是整个主页打不开？写清楚你怎么判断的…',
    '注销': '主页提示账号已注销 / 头像变灰 / 搜索不到？把判断依据记下来…',
    '禁言/封号': '还能看主页但发不出新作品？主页提示违规？',
    '停更/偶更': '多久没更新算停更？比如：三个月没更但没注销的都归这里…',
    '卖号': '简介留了联系方式、内容风格突变、开始带货？你怎么认出来的…',
    '不好看了': '内容质量下降、换了风格、开始 AI 配音？写你的标准…',
    '待修改': '资料要改但还没想好怎么改的，先丢这里…',
};
let _statusNoteTimer = null;

function statusNoteText(status) {
    const v = (state.statusNotes || {})[status];
    return typeof v === 'string' ? v : '';
}

// 切换筛选条件后调用：有 currentStatus 就露出输入框（单行、跟标题齐平），否则藏起来
function updateStatusNoteBar() {
    const input = document.getElementById('statusNoteInput');
    if (!input) return;
    const st = state.currentStatus;
    if (!st) { input.classList.add('hidden'); return; }
    input.classList.remove('hidden');
    input.placeholder = STATUS_NOTE_PLACEHOLDER[st] || '写点你对这个状态的理解…';
    // 正在打字时不要覆盖输入框（切筛选会重绘列表，不能把光标位置打掉）
    if (document.activeElement !== input) input.value = statusNoteText(st);
}

function onStatusNoteInput(v) {
    const st = state.currentStatus;
    if (!st) return;
    if (!state.statusNotes) state.statusNotes = {};
    if (v && v.trim()) state.statusNotes[st] = v;
    else delete state.statusNotes[st];
    state.dirtyStatusNotes.add(st);
    // 防抖：停下来 700ms 才真正写盘（每敲一个字就 POST 一次没必要）
    if (_statusNoteTimer) clearTimeout(_statusNoteTimer);
    _statusNoteTimer = setTimeout(() => { _statusNoteTimer = null; flushStatusNote(); }, 700);
}

// 立即写盘（失焦、切状态、切视图时调）
function flushStatusNote() {
    if (_statusNoteTimer) { clearTimeout(_statusNoteTimer); _statusNoteTimer = null; }
    if (!state.dirtyStatusNotes || !state.dirtyStatusNotes.size) return Promise.resolve(true);
    return saveUserData();
}
// 刷新各状态列的计数（从 /api/meta 或全量列表算；这里用一次全量轻量列表统计）
async function refreshStatusCounts() {
    try {
        const d = await fetch('/api/authors?limit=100000&offset=0').then((r) => r.json());
        const counts = {};
        (d.items || []).forEach((a) => {
            const s = a.status || '';
            if (STATUS_ORDER.indexOf(s) >= 0) counts[s] = (counts[s] || 0) + 1;
        });
        STATUS_ORDER.forEach((s) => {
            const el = document.getElementById('statusCount_' + s);
            if (el) el.textContent = counts[s] || '0';
        });
        // 等级计数（和状态共用这一次请求，别额外再拉一遍列表）
        const lvCounts = {};
        (d.items || []).forEach((a) => {
            const v = normLevel(a.level);
            if (LEVEL_LIST.indexOf(v) >= 0) lvCounts[v] = (lvCounts[v] || 0) + 1;
        });
        LEVEL_LIST.forEach((v) => {
            const el = document.getElementById('levelCount_' + v);
            if (el) el.textContent = lvCounts[v] || '0';
        });
        // 更新频率计数（day/3天/周/月）
        // 状态落在「不能设更新频率」列表里的账号不计数 —— 服务端 /api/authors?freq=
        // 也是这么排除的（_t_freq 里 return False）。不跟着排除的话，侧边栏显示
        // 「day 475」点进去只有 470 个，对不上账。
        // 2026-11-08 之前这些账号的频率会被服务端直接清空，所以没这个问题；
        // 现在不再销毁用户数据，就得在这里对齐口径。
        const _locked = new Set((state.updateFreqDisabledStatus || []).map((x) => String(x || '').trim()));
        const freqCounts = {};
        (d.items || []).forEach((a) => {
            if (_locked.has(String(a.status || '').trim())) return;
            const v = a.update_freq || '';
            if (FREQ_LIST.indexOf(v) >= 0) freqCounts[v] = (freqCounts[v] || 0) + 1;
        });
        FREQ_LIST.forEach((v) => {
            const el = document.getElementById('freqCount_' + v);
            if (el) el.textContent = freqCounts[v] || '0';
        });
    } catch (e) { /* 忽略，计数不关键 */ }
}

// ===== 右上角筛选面板：SEC_UID / 星标 / 最后更新（可自定义时间）/ 等级 / 封面 / 状态（均单选）=====
// 触发式：点选即应用，面板常驻（点击外部才收起）；底部有「重置」一键恢复默认
function toggleFilterPanel() {
    const p = document.getElementById('filterPanel');
    if (!p) return;
    p.classList.toggle('hidden');
    if (!p.classList.contains('hidden')) syncFilterPanelFromState();
}

// 「?」说明气泡：鼠标移上去就显示，点一下可锁定
// 关键：浮层要 append 到 <body>。页面视图有 transform 动画，祖先带 transform 时
// position:fixed 会以那个祖先为参照系、还会被它的层叠上下文裁掉——挂到 body 才稳。
function hintPopOf(btn) {
    const wrap = btn.closest('.hint-wrap');
    if (!wrap) return null;
    if (!wrap._hintPop) wrap._hintPop = wrap.querySelector('.hint-pop');
    return wrap._hintPop || null;
}

function placeHintPop(btn) {
    const pop = hintPopOf(btn);
    if (!pop) return;
    const r = btn.getBoundingClientRect();
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;
    const sx = window.scrollX || 0;
    const sy = window.scrollY || window.pageYOffset || 0;
    const w = Math.min(360, vw - 24);
    pop.style.width = w + 'px';
    let left = r.left + sx - 8;
    left = Math.max(sx + 12, Math.min(left, sx + vw - w - 12));
    pop.style.left = left + 'px';
    pop.style.top = (r.bottom + sy + 8) + 'px';
    const h = pop.offsetHeight;
    if (r.bottom + 8 + h > vh - 12) pop.style.top = Math.max(sy + 12, r.top + sy - h - 8) + 'px';
}

function showHintPop(btn) {
    const wrap = btn.closest('.hint-wrap');
    const pop = hintPopOf(btn);
    if (!wrap || !pop) return;
    closeAllHintPops();
    if (pop.parentNode !== document.body) document.body.appendChild(pop);
    pop.classList.add('show');
    wrap.classList.add('open');
    placeHintPop(btn);
}

function hideHintPop(btn) {
    const wrap = btn.closest('.hint-wrap');
    const pop = hintPopOf(btn);
    if (!wrap || !pop || wrap.dataset.pinned === '1') return;
    pop.classList.remove('show');
    wrap.classList.remove('open');
}

function toggleHintPop(ev, btn) {
    if (ev) ev.stopPropagation();
    const wrap = btn.closest('.hint-wrap');
    if (!wrap) return;
    const on = wrap.classList.contains('open') && wrap.dataset.pinned === '1';
    closeAllHintPops();
    if (!on) {
        wrap.dataset.pinned = '1';
        showHintPop(btn);
    }
}

function closeAllHintPops() {
    document.querySelectorAll('.hint-wrap').forEach((w) => {
        w.classList.remove('open');
        w.dataset.pinned = '';
        const p = w._hintPop || w.querySelector('.hint-pop');
        if (p) { p.classList.remove('show'); w._hintPop = p; }
    });
}

document.addEventListener('click', closeAllHintPops);
window.addEventListener('scroll', closeAllHintPops, true);
window.addEventListener('resize', closeAllHintPops);

// 把当前 state.filters 同步回面板控件（打开时）
function syncFilterPanelFromState() {
    const f = state.filters || {};
    const set = (name, val) => {
        document.querySelectorAll(`input[name="${name}"]`).forEach((i) => { i.checked = (i.value === val); });
    };
    // 多选字段：把选中的值勾上（其余取消）
    const setMulti = (name, arr) => {
        const vals = Array.isArray(arr) ? arr : [];
        document.querySelectorAll(`input[name="${name}"]`).forEach((i) => { i.checked = vals.indexOf(i.value) >= 0; });
    };
    set('fSec', f.sec_uid || '');
    set('fStar', f.starred || '');
    set('fWatch', f.watch || '');
    set('fLatest', f.latest || '');
    setMulti('fLevel', f.level);
    setMulti('fCover', f.cover);
    setMulti('fStatus', f.status);
    setMulti('fFreq', f.freq);
    // 自定义日期输入框
    document.getElementById('latestFrom').value = f.latestFrom || '';
    document.getElementById('latestTo').value = f.latestTo || '';
    // 选「自定义」才展开日期输入区
    document.getElementById('filterRange').classList.toggle('hidden', f.latest !== 'custom');
    renderAuthorMatch();
}

// 从面板当前勾选读出一份 filters（不关闭面板）
function readFilterPanel() {
    const f = {
        sec_uid: '', starred: '', watch: '', latest: '', latestFrom: '', latestTo: '',
        level: [], cover: [], status: [], freq: [],
        // 圆钮状态不是从勾选框读的，沿用当前这份（点圆钮时 already 改过了）
        matchMap: Object.assign({ level: 'or', cover: 'or', status: 'or', freq: 'or', latest: 'or' },
            (state.filters || {}).matchMap || {}),
    };
    const val = (name) => {
        let r = '';
        document.querySelectorAll(`input[name="${name}"]`).forEach((i) => { if (i.checked) r = i.value; });
        return r;
    };
    // 多选字段：收集所有勾中的值
    const valMulti = (name) => {
        const arr = [];
        document.querySelectorAll(`input[name="${name}"]:checked`).forEach((i) => { arr.push(i.value); });
        return arr;
    };
    f.sec_uid = val('fSec');
    f.starred = val('fStar');
    f.watch = val('fWatch');
    f.latest = val('fLatest');
    f.level = valMulti('fLevel');
    f.cover = valMulti('fCover');
    f.status = valMulti('fStatus');
    f.freq = valMulti('fFreq');
    f.latestFrom = document.getElementById('latestFrom').value || '';
    f.latestTo = document.getElementById('latestTo').value || '';
    return f;
}

// 选「自定义」时展开/收起日期输入区
function onLatestModeChange() {
    const custom = (readFilterPanel().latest === 'custom');
    document.getElementById('filterRange').classList.toggle('hidden', !custom);
}

// 清空自定义日期（不动「自定义」那个单选）
function clearLatestRange() {
    document.getElementById('latestFrom').value = '';
    document.getElementById('latestTo').value = '';
    state.filters.latestFrom = '';
    state.filters.latestTo = '';
    applyFilters();
}

// ===== 「全部作者」页筛选面板：每行一个 ＋/－ 圆钮（2026-11-05，和导出页同一套口径） =====
// ＋ = 包含（勾中的是我要的）/ － = 排除（勾中的去掉）。不同 ＋ 行取并集，－ 行全部剔除。
function renderAuthorMatch() {
    const mm = (state.filters && state.filters.matchMap) || {};
    document.querySelectorAll('#filterPanel .export-match').forEach((box) => {
        const key = box.dataset.key;
        const mode = mm[key] === 'not' ? 'not' : 'or';
        const isNot = mode === 'not';
        box.dataset.mode = mode;
        box.innerHTML = `<button type="button" class="em-btn ${mode}" data-mode="${mode}"`
            + ` title="${isNot ? '－ 排除：勾中的这些要去掉（点一下改回 ＋ 包含）'
                              : '＋ 包含：勾中的这些是我要的（点一下改成 － 排除）'}"`
            + ` aria-label="${isNot ? '排除' : '包含'}" onclick="toggleAuthorMatch('${key}')">`
            + (isNot ? '－' : '＋') + '</button>';
    });
}

function toggleAuthorMatch(key) {
    const cur = readFilterPanel();
    if (!cur.matchMap) cur.matchMap = {};
    cur.matchMap[key] = (cur.matchMap[key] === 'not') ? 'or' : 'not';
    state.filters = cur;
    renderAuthorMatch();
    updateFilterDot();
    refreshAuthorSentence();
    applyFilters();
}

// 行尾「全选」：全勾上再点一次全不选（先全选再取消几个，比一个个点快）
const F_ROW_KEY = { fLevel: 'level', fStatus: 'status', fFreq: 'freq', fCover: 'cover' };

function authorRowAll(name) {
    if (!F_ROW_KEY[name]) return;
    const inputs = [...document.querySelectorAll(`input[name="${name}"]`)];
    const allOn = inputs.length > 0 && inputs.every((i) => i.checked);
    inputs.forEach((i) => { i.checked = !allOn; });
    state.filters = readFilterPanel();
    updateFilterDot();
    refreshAuthorSentence();
    applyFilters();
}

const AUTHOR_LATEST_TEXT = {
    '3d': '3天内', '7d': '1周内', '14d': '2周内',
    '30d': '1个月内', '60d': '2个月内', '365d': '1年内',
};

// 一句话说清当前筛的是什么（显示在筛选面板**外面**）
function authorSentence() {
    const f = state.filters || {};
    const mm = f.matchMap || {};
    const inc = [], exc = [];
    const add = (key, text) => { if (text) (mm[key] === 'not' ? exc : inc).push(text); };
    const lv = (f.level || []).map((v) => (v === '__unset' ? '未设置' : v));
    add('level', lv.length ? '等级：' + lv.join('、') : '');
    const st = (f.status || []).map((v) => (v === '__unset' ? '未设置' : v));
    add('status', st.length ? '状态：' + st.join('、') : '');
    const fr = (f.freq || []).map((v) => (v === '__unset' ? '未设置' : v));
    add('freq', fr.length ? '频率：' + fr.join('、') : '');
    const cv = (f.cover || []).map((v) => (v === 'has' ? '有封面' : '无封面'));
    add('cover', cv.length ? cv.join('、') : '');
    if (f.latest === 'custom') {
        if (f.latestFrom || f.latestTo) {
            add('latest', '最后下载：' + (f.latestFrom || '不限') + ' 至 ' + (f.latestTo || '不限'));
        }
    } else if (f.latest) {
        add('latest', '最后下载：' + (AUTHOR_LATEST_TEXT[f.latest] || f.latest));
    }
    // SEC_UID / 星标 是「有 / 没」二选一，不分 ＋－，直接并进包含段
    if (f.sec_uid === 'has') inc.push('有 SEC_UID');
    if (f.sec_uid === 'none') inc.push('没有 SEC_UID');
    if (f.starred === 'yes') inc.push('已星标');
    if (f.starred === 'no') inc.push('未星标');
    // 追更：布尔三选，和星标一样直接并进包含段
    if (f.watch === 'yes') inc.push('追更中');
    if (f.watch === 'no') inc.push('不在追更');

    let s = inc.length ? inc.join(' 或 ') : '全部作者';
    if (exc.length) s += '，去掉 ' + exc.join('、');
    return s;
}

function refreshAuthorSentence() {
    const el = document.getElementById('authorFilterSentence');
    if (!el) return;
    const s = authorSentence();
    el.innerHTML = `<b>筛选结果：</b>${escapeHtml(s)}`;
    el.classList.toggle('hidden', s === '全部作者');
}

// 触发式应用：点选后立刻生效
function applyFilterPanel() {
    state.filters = readFilterPanel();
    updateFilterDot();
    updateExportBtn();
    refreshAuthorSentence();
    applyFilters();
}

// 重置：恢复全部筛选为默认值（不限），并立即应用
function resetFilterPanel() {
    state.filters = {
        sec_uid: '', starred: '', watch: '', latest: '', latestFrom: '', latestTo: '',
        level: [], cover: [], status: [], freq: [],
        matchMap: { level: 'or', cover: 'or', status: 'or', freq: 'or', latest: 'or' },
    };
    syncFilterPanelFromState();
    updateFilterDot();
    updateExportBtn();
    refreshAuthorSentence();
    applyFilters();
}

// 筛选面板有生效条件时，筛选按钮上亮一个小圆点
function updateFilterDot() {
    const f = state.filters || {};
    const active = !!(f.sec_uid || f.starred || f.watch || f.latest
        || (f.level && f.level.length) || (f.cover && f.cover.length)
        || (f.status && f.status.length) || (f.freq && f.freq.length));
    const dot = document.getElementById('filterDot');
    if (dot) dot.classList.toggle('hidden', !active);
}

// 面板里任意 radio/checkbox 变化 → 立即应用筛选（点选即筛，无需确认按钮）
function bindFilterPanelEvents() {
    const panel = document.getElementById('filterPanel');
    if (!panel) return;
    panel.querySelectorAll('input[type="radio"], input[type="checkbox"]').forEach((inp) => {
        inp.addEventListener('change', () => {
            // 最后更新的模式切换要先展开/收起日期区
            if (inp.name === 'fLatest') onLatestModeChange();
            applyFilterPanel();
        });
    });
    // 自定义日期：改了就生效
    ['latestFrom', 'latestTo'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('change', applyFilterPanel);
    });
}

// 点击面板外部关闭
document.addEventListener('click', (e) => {
    const panel = document.getElementById('filterPanel');
    if (!panel || panel.classList.contains('hidden')) return;
    if (e.target.closest && (e.target.closest('.filter-wrap'))) return;
    panel.classList.add('hidden');
});

// ===== 导出 SECID：把当前界面的作者（含所在大小号组）的 SEC_UID 导出成 txt =====
// 导出类型 = 当前视图：
//   - 星标视图      -> type「星标账号」，排除注销/禁言封号/卖号/不好看了（只有星标/标签要排除废号）
//   - 标签视图      -> type「标签-<标签名>」，同样排除废号
//   - 状态列视图    -> type「<状态名>」，不过滤（本来就是单独一个状态）
//   - 全部作者视图  -> 导出按钮隐藏（没有明确的「类型」）
const EXPORT_EXCLUDE_STATUS = ['注销', '禁言/封号', '卖号', '不好看了'];

function exportTypeInfo() {
    if (state.currentStatus) return { type: state.currentStatus, exclude: false };
    if (state.currentTag) return { type: '标签-' + state.currentTag, exclude: true };
    if (state.currentCategory === 'starred') return { type: '星标账号', exclude: true };
    return null;
}

// 导出按钮的显隐与文案，跟着当前视图走（切分类/状态/标签后刷新）
function updateExportBtn() {
    const btn = document.getElementById('exportBtn');
    if (!btn) return;
    const info = exportTypeInfo();
    if (!info) { btn.classList.add('hidden'); return; }
    btn.classList.remove('hidden');
    btn.textContent = '📤 导出「' + info.type + '」SECID';
    btn.title = info.exclude
        ? '导出当前界面（含右上角筛选条件）的账号及其所在大小号组的 SEC_UID；自动排除注销/禁言封号/卖号/不好看了'
        : '导出该类型全部账号（含所在大小号组）的 SEC_UID';
}

async function exportSecUids() {
    const info = exportTypeInfo();
    if (!info) { showToast('当前界面没有可导出的账号类型'); return; }
    // 从当前视图的账号里取 uid（state.filtered 是已加载的列表；为稳妥重新拉全量）
    let list = [];
    try {
        const params = new URLSearchParams({ offset: '0', limit: '100000' });
        if (state.currentStatus) {
            // 状态列视图：全量导出该状态，不叠加筛选面板
            params.set('status', state.currentStatus);
        } else {
            if (state.currentTag) params.set('tag', state.currentTag);
            if (state.currentCategory === 'starred') params.set('cat', 'starred');
            // 星标 / 标签界面：叠加右上角筛选面板的条件（其余界面全量导出）
            if (info.exclude) {
                const f = state.filters || {};
                if (f.sec_uid) params.set('sec_uid', f.sec_uid);
                if (f.starred) params.set('starred', f.starred);
                if (f.level && f.level.length) params.set('level', f.level.join(','));
                if (f.cover) params.set('cover', f.cover);
                if (f.status && f.status.length) params.set('status', f.status.join(','));
                if (f.freq && f.freq.length) params.set('freq', f.freq.join(','));
                if (f.latest === 'custom') {
                    if (f.latestFrom) params.set('latest_after', f.latestFrom);
                    if (f.latestTo) params.set('latest_before', f.latestTo);
                } else if (f.latest) {
                    const days = parseInt(f.latest, 10);
                    if (!isNaN(days)) {
                        const dt = new Date(Date.now() - days * 86400000);
                        const iso = dt.getFullYear() + '-' + String(dt.getMonth() + 1).padStart(2, '0') + '-' + String(dt.getDate()).padStart(2, '0');
                        params.set('latest_after', iso);
                    }
                }
            }
        }
        const d = await fetch('/api/authors?' + params.toString()).then((r) => r.json());
        list = d.items || [];
    } catch (e) { showToast('读取账号列表失败：' + e.message); return; }
    let uids = list.map((a) => a.uid);
    if (info.exclude) {
        const excluded = list.filter((a) => EXPORT_EXCLUDE_STATUS.indexOf(a.status) >= 0).length;
        uids = list.filter((a) => EXPORT_EXCLUDE_STATUS.indexOf(a.status) < 0).map((a) => a.uid);
        if (excluded > 0) showToast('已自动排除 ' + excluded + ' 个「注销/封号/禁言/卖号/不好看了」的账号');
    }
    if (!uids.length) { showToast('没有可导出的账号'); return; }
    showToast('正在导出 ' + uids.length + ' 个账号…');
    try {
        const resp = await fetch('/api/export_secuids', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ type: info.type, uids }),
        });
        const d = await resp.json();
        if (!d.ok) { showToast('导出失败：' + (d.error || '未知错误')); return; }
        showToast(`已导出 ${d.count} 条 SEC_UID 到 export/${d.file}${d.missing > 0 ? `（另有 ${d.missing} 个账号没填 SEC_UID 已跳过）` : ''}`);
    } catch (e) {
        showToast('导出失败：' + e.message);
    }
}


// 输入：cur = 账号当前标注（含 nickname / alias），prof = 抖音资料里的昵称。
// 输出：要写回的字段 patch（可能含 nickname / alias），以及是否发生了变化。
// 规则：
//   1. 有 alias（备注）→ 备注不动，仅用资料昵称更新 nickname（原名位置）；
//   2. 无 alias 且昵称没变 → 什么都不改；
//   3. 无 alias 且昵称变了 → 旧昵称设为 alias（备注），资料昵称替换 nickname。

// ===== 缓存管理（缩略图 / 转码产物） =====
// 这两个目录都是纯缓存，删了会按需重建；但涨到 1G+ 之后需要能看见、能清。
function fmtBytes(n) {
    n = Number(n) || 0;
    if (n >= 1024 * 1024 * 1024) return (n / 1073741824).toFixed(2) + ' GB';
    if (n >= 1024 * 1024) return (n / 1048576).toFixed(1) + ' MB';
    if (n >= 1024) return Math.round(n / 1024) + ' KB';
    return n + ' B';
}

async function fetchCacheInfo() {
    try {
        const r = await fetch('/api/cache_info');
        return await r.json();
    } catch (e) {
        return null;
    }
}

// 侧边栏上直接显示缓存总占用（不用点开就知道多大）
async function refreshCacheSize() {
    const el = document.getElementById('cacheEntrySize');
    if (!el) return;
    const d = await fetchCacheInfo();
    if (d && d.ok) el.textContent = fmtBytes(d.total_bytes);
}

function cacheRowHtml(key, title, meta, info) {
    const bytes = info.bytes || 0;
    const maxMb = info.max_mb || 0;
    const pct = maxMb ? Math.min(100, Math.round(bytes / (maxMb * 1048576) * 100)) : 0;
    return `
        <div class="cache-row">
            <div class="cache-row-main">
                <span class="cache-row-title">${title}</span>
                <span class="cache-row-meta">${info.files || 0} 个文件 · ${fmtBytes(bytes)} / 上限 ${maxMb} MB</span>
            </div>
            <div class="cache-row-ops">
                <button class="btn btn-mini" onclick="cleanCache('${key}', false)" title="按上限淘汰最久没用的文件">清理超额</button>
                <button class="btn btn-mini" onclick="cleanCache('${key}', true)" title="整个清空，会按需重建">全部清空</button>
            </div>
            <div class="cache-bar${pct >= 90 ? ' warn' : ''}"><i style="width:${pct}%"></i></div>
        </div>`;
}

// 把缓存占用渲染到指定容器（缓存现在嵌在「设置」页里，不再是独立页）
async function renderCacheInto(targetId) {
    const body = document.getElementById(targetId);
    if (!body) return;
    body.innerHTML = '<div class="cache-total">读取中…</div>';
    const d = await fetchCacheInfo();
    if (!d || !d.ok) { body.innerHTML = '<div class="cache-total">读取失败，稍后再试</div>'; return; }
    const cover = d.cover || { files: 0, bytes: 0 };
    body.innerHTML = cacheRowHtml('thumb', '🖼 缩略图缓存', '', d.thumb)
        + cacheRowHtml('trans', '🎬 转码缓存（HEVC→H.264）', '', d.trans)
        + `<div class="cache-row cache-row-locked">
                <div class="cache-row-main">
                    <span class="cache-row-title">⭐ 封面图</span>
                    <span class="cache-row-meta">${cover.files || 0} 张 · ${fmtBytes(cover.bytes || 0)} · 你设的封面，永远不会被清</span>
                </div>
                <div class="cache-row-ops"><span class="cache-lock">🔒 受保护</span></div>
            </div>`
        + `<div class="cache-total">可清理合计 <b>${fmtBytes(d.total_bytes)}</b>　·　超上限会自动淘汰最久没看的文件<br>封面单独存放（<code>cover_cache\\</code>），不在清理范围内</div>`
        + `<div class="cache-health" id="cacheHealth">正在体检标注数据…</div>`;
    loadDataHealth();
}

// 标注数据体检：账号数 / 封面数 / 最近保存 / 自动备份份数
async function loadDataHealth() {
    const el = document.getElementById('cacheHealth');
    if (!el) return;
    try {
        const d = await fetch('/api/data_health').then((r) => r.json());
        if (!d || !d.ok) { el.textContent = '体检失败'; return; }
        el.innerHTML = `标注数据：<b>${d.accounts}</b> 个账号 · <b>${d.covers}</b> 个封面`
            + `（图 ${d.cover_files} 张）· 最近保存 ${escapeHtml(d.saved_at || '—')}`
            + `<br>自动备份 <b>${d.backups}</b> 份${d.last_backup ? '，最近 ' + escapeHtml(d.last_backup) : ''}`
            + `　·　存在 <code>backup\\user_data.auto-*.json</code>`;
    } catch (e) {
        el.textContent = '体检失败（服务没起来？）';
    }
}

async function cleanCache(target, wipe) {
    const name = target === 'thumb' ? '缩略图缓存' : (target === 'trans' ? '转码缓存' : '缓存');
    if (wipe && !confirm(`确定清空${name}吗？\n\n删掉的文件会在下次用到时自动重建（缩略图一瞬、视频转码几秒）。`)) return;
    const body = document.getElementById('settingsCacheBody');
    if (body) body.innerHTML = '<div class="cache-total">正在清理…</div>';
    try {
        const r = await fetch('/api/cache_clean', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target, wipe: !!wipe }),
        });
        const d = await r.json();
        if (!d || !d.ok) { showToast('清理失败'); }
        else if (d.failed) { showToast(`已释放 ${fmtBytes(d.freed)}，${d.failed} 个文件删不掉（可能正被占用），稍后再试`); }
        else { showToast(`已释放 ${fmtBytes(d.freed)}${d.removed ? '（' + d.removed + ' 个文件）' : ''}`); }
    } catch (e) {
        showToast('清理失败');
    }
    await renderCacheInto('settingsCacheBody');
    refreshCacheSize();
}

// ===== 批量获取资料：对所有已填 SEC_UID 的账号拉抖音公开资料 =====
// ===== 批量获取资料 =====
// 规则：空字段直接补；**昵称 / 抖音号「原来有值且不一样」的先不动**，攒成待确认清单，
// 列在「🧰 批量获取资料」面板里，一条条决定用不用抖音的新值（记在 localStorage，刷新不丢）。
const BATCH_KEY = 'dy_batch_conflicts';

function loadBatchConflicts() {
    try {
        const v = JSON.parse(localStorage.getItem(BATCH_KEY) || '[]');
        return Array.isArray(v) ? v : [];
    } catch (e) { return []; }
}

function saveBatchConflicts(list) {
    state.batchConflicts = list;
    try { localStorage.setItem(BATCH_KEY, JSON.stringify(list)); } catch (e) { /* 隐私模式忽略 */ }
    refreshBatchBadge();
}

// ===== 已处理：刚点过「采用 / 忽略」的账号挪到下面，不跟待确认混在一起 =====
const BATCH_DONE_KEY = 'dy_batch_done';
const BATCH_DONE_MAX = 200;      // 只留最近 200 条，免得 localStorage 越来越肥

function loadBatchDone() {
    try {
        const v = JSON.parse(localStorage.getItem(BATCH_DONE_KEY) || '[]');
        return Array.isArray(v) ? v : [];
    } catch (e) { return []; }
}

function saveBatchDone(list) {
    state.batchDone = (list || []).slice(0, BATCH_DONE_MAX);
    try { localStorage.setItem(BATCH_DONE_KEY, JSON.stringify(state.batchDone)); } catch (e) { /* 隐私模式忽略 */ }
    renderBatchDone();
}

const BATCH_ACTION_LABEL = {
    nickname: '用了新昵称',
    douyin_id: '用了新抖音号',
    all: '全部采用',
    skip: '已忽略',
};

function renderBatchDone() {
    const body = document.getElementById('batchDoneBody');
    if (!body) return;
    const list = state.batchDone || [];
    const cnt = document.getElementById('batchDoneCount');
    if (cnt) cnt.textContent = `${list.length} 个`;
    if (!list.length) {
        body.innerHTML = '<div class="bc-empty">还没有处理过的账号。在「待确认」里点「全部采用」或「忽略」，处理完的会挪到这里。</div>';
        return;
    }

    body.innerHTML = list.map((c) => {
        const act = BATCH_ACTION_LABEL[c.action] || '已处理';
        const val = c.action === 'nickname' || c.action === 'all' ? (c.newNickname || '')
            : c.action === 'douyin_id' ? (c.newDouyinId || '') : '';
        return `
        <div class="bc-row bc-done-row">
            <div class="bc-head">
                <span class="bc-name bc-link" onclick="openAuthor('${c.uid}')" title="打开这位作者的主页">${escapeHtml(c.nickname || c.uid)}<b class="bc-open">↗</b></span>
                <span class="bc-douyin">${escapeHtml(c.douyin_id || '无抖音号')}</span>
                <span class="bc-act ${escapeHtml(c.action || 'skip')}">${act}${val ? '：' + escapeHtml(clip(val, 18)) : ''}</span>
            </div>
            <div class="bc-ops">
                <button class="btn btn-mini" onclick="reopenBatchConflict('${c.uid}')" title="放回「待确认」，重新决定">↩ 放回待确认</button>
                <span class="bc-when">${escapeHtml(c.doneAt || '')}</span>
            </div>
        </div>`;
    }).join('');
}

function clearBatchDone() {
    if (!(state.batchDone || []).length) return;
    if (!confirm('清空「已处理」记录？\n\n只是清这份清单，已经写进去的昵称/抖音号/简介不受影响。')) return;
    saveBatchDone([]);
    renderBatchTabs();
    if (state.batchFilter === 'done') setBatchFilter('conflict');   // 清空了就别停在空标签上
    showToast('已处理记录已清空');
}

// 放回待确认：资料已经写了也不再倒回去（想改就直接再看一遍这条），只是把卡片挪回来
function reopenBatchConflict(uid) {
    const done = (state.batchDone || []).slice();
    const i = done.findIndex((c) => c.uid === uid);
    if (i < 0) return;
    const item = done.splice(i, 1)[0];
    const pending = (state.batchConflicts || []).slice();
    if (!pending.some((c) => c.uid === uid)) {
        const { action, doneAt, ...rest } = item;
        pending.unshift(rest);
    }
    saveBatchDone(done);
    saveBatchConflicts(pending);
    renderBatchPanel();
    renderBatchTabs();
    setBatchFilter('conflict');
    showToast('已放回待确认');
}

function refreshBatchBadge() {
    const el = document.getElementById('batchBadge');
    if (!el) return;
    const n = (state.batchConflicts || []).length;
    el.textContent = n ? String(n) : '0';
    el.classList.toggle('zero', !n);
}

// 批量获取资料 = 独立页面（和「大小号」同级），不再是浮层。
// 侧边栏点进来 → 内容区整页切换，布局/滚动/页头都跟作者列表一致。
async function openBatchView() {
    forceCloseDetail();
    rememberViewScroll();
    state.view = 'batch';
    state.returnView = 'batch';
    hideAllViews();
    document.getElementById('batchView').classList.remove('hidden');
    animateViewEnter('batchView', 'neutral');
    clearSideActive();
    document.querySelectorAll('#featureList .side-item').forEach((i) => {
        i.classList.toggle('active', i.dataset.action === 'batchProfile' || i.dataset.view === 'batch');
    });
    const eb = document.getElementById('exportBtn');
    if (eb) eb.classList.add('hidden');
    restoreViewScroll('batch');
    restoreBatchOptions();       // 恢复上次的勾选/顺序
    refreshBatchPreview();       // 算「本次将获取 N 个」
    renderBatchPanel();
    await refreshBatchView();
}

// 只刷新这个页面的数据（进度 / 统计 / 失败明细），不动其他视图
async function refreshBatchView() {
    try {
        const [s, r] = await Promise.all([
            fetch('/api/batch_status').then((x) => x.json()).catch(() => null),
            fetch('/api/batch_result').then((x) => x.json()).catch(() => null),
        ]);
        if (s) applyBatchState(s);
        // 结果留在服务端内存里（consume 只清「未读」标记），
        // 所以进页面随时能把上次的失败明细捞出来看
        if (r && r.ok && (r.total || (r.failed || []).length)) {
            renderBatchSummary(r);
            renderBatchFailed(r.failed || []);
            renderBatchFilled(r.filledList || []);
            applyBatchFilter();      // 三块都渲染完再按当前筛选收敛显隐
        }
        if (s && s.running) { startBatchPolling(); return; }
        if (s && s.unread) await consumeBatchResult();
    } catch (e) { /* 服务没起来就保持原样 */ }
}

function renderBatchPanel() {
    const body = document.getElementById('batchConflictBody');
    const sub = document.getElementById('batchConflictCount');
    renderBatchDone();
    if (!body) return;
    const list = state.batchConflicts || [];
    if (sub) sub.textContent = list.length ? `${list.length} 个` : '0 个';
    if (!list.length) {
        body.innerHTML = '<div class="bc-empty">批量获取时，昵称或抖音号和本地不一致的账号会列在这里，等你决定用哪个。都一致的话这里一直是空的。</div>';
        return;
    }
    body.innerHTML = list.map((c) => `
        <div class="bc-row">
            <div class="bc-head">
                <span class="bc-name bc-link" onclick="openAuthor('${c.uid}')" title="打开这位作者的主页">${escapeHtml(c.nickname || c.uid)}<b class="bc-open">↗</b></span>
                <span class="bc-douyin">${escapeHtml(c.douyin_id || '无抖音号')}</span>
            </div>
            ${c.suspect ? '<div class="bc-suspect">昵称对不上，可能 SEC_UID 指到了别的账号 → 这条整条没写，连简介都没补</div>' : ''}
            <div class="bc-diffs">
                ${c.nickDiff ? `<div class="bc-diff"><span class="bc-k">昵称</span><span class="bc-old">${escapeHtml(c.nickname || '（空）')}</span><span class="bc-arrow">→</span><span class="bc-new">${escapeHtml(c.newNickname)}</span></div>` : ''}
                ${c.idDiff || (c.newDouyinId && c.newDouyinId !== (c.douyin_id || '')) ? `<div class="bc-diff"><span class="bc-k">抖音号</span><span class="bc-old">${escapeHtml(c.douyin_id || '（空）')}</span><span class="bc-arrow">→</span><span class="bc-new">${escapeHtml(c.newDouyinId)}</span></div>` : ''}
                ${c.newSignature && (!c.signature || c.newSignature !== c.signature) ? `<div class="bc-diff"><span class="bc-k">简介</span><span class="bc-new">${escapeHtml(clip(c.newSignature, 40))}</span></div>` : ''}
            </div>
            <div class="bc-ops">
                <button class="btn btn-mini bc-home" onclick="openAuthor('${c.uid}')" title="打开作者主页（看作品 / 查 SEC_UID）">↗ 进主页</button>
                ${c.nickDiff ? `<button class="btn btn-mini" onclick="resolveBatchConflict('${c.uid}','nickname')">用新昵称</button>` : ''}
                ${c.idDiff || (c.newDouyinId && c.newDouyinId !== (c.douyin_id || '')) ? `<button class="btn btn-mini" onclick="resolveBatchConflict('${c.uid}','douyin_id')">用新抖音号</button>` : ''}
                <button class="btn btn-mini btn-primary" onclick="resolveBatchConflict('${c.uid}','all')">全部采用</button>
                <button class="btn btn-mini" onclick="resolveBatchConflict('${c.uid}','skip')">忽略</button>
            </div>
        </div>`).join('');
}

// 把 patch 写进某个账号的标注（不动当前详情页的 state.currentAuthor）
function applyBatchPatch(uid, patch) {
    const next = { ...((state.userData.accounts || {})[uid] || {}) };
    let changed = false;
    ['nickname', 'douyin_id', 'signature'].forEach((k) => {
        const val = patch[k];
        if (typeof val !== 'string' || !val) return;
        if (val === (next[k] || '')) return;
        next.history = (next.history || []).slice();
        if (k !== 'signature') {
            next.history.push({ field: k, from: next[k] || '', to: val, time: new Date().toLocaleString() });
        }
        next[k] = val;
        changed = true;
    });
    if (!changed) return false;
    state.userData.accounts[uid] = next;
    markDirtyFields(uid, ['nickname', 'douyin_id', 'signature', 'history']);
    return true;
}

function syncListName(uid, patch) {
    const it = state.filtered.find((x) => x.uid === uid);
    if (it) {
        if (patch.nickname) it.nickname = patch.nickname;
        if (patch.douyin_id) it.douyin_id = patch.douyin_id;
    }
}

function resolveBatchConflict(uid, which) {
    const list = (state.batchConflicts || []).slice();
    const idx = list.findIndex((c) => c.uid === uid);
    if (idx < 0) return;
    const c = list[idx];
    if (which !== 'skip') {
        const patch = {};
        if ((which === 'all' || which === 'nickname') && c.nickDiff) patch.nickname = c.newNickname;
        if ((which === 'all' || which === 'douyin_id')
            && (c.idDiff || (c.newDouyinId && c.newDouyinId !== (c.douyin_id || '')))) {
            patch.douyin_id = c.newDouyinId;
        }
        // 「全部采用」= 认定这就是同一个账号：本地没简介就用抖音那份
        // （存疑账号在批量阶段是故意一个字都没写的，等这一步）
        const curWa = (state.userData.accounts || {})[uid] || {};
        if (which === 'all' && c.newSignature && !String(curWa.signature || '').trim()) {
            patch.signature = c.newSignature;
        }
        if (applyBatchPatch(uid, patch)) {
            syncListName(uid, patch);
            if (state.currentAuthor && state.currentAuthor.uid === uid) {
                Object.keys(patch).forEach((k) => { state.currentAuthor[k] = patch[k]; });
                if (isDetailOpen()) refreshTop();
            }
            saveUserData();
            renderAuthorGrid();
        }
    }
    list.splice(idx, 1);
    saveBatchConflicts(list);
    // 挪到「已处理」而不是凭空消失：刚点完能回头核对，点错了还能放回来
    const done = (state.batchDone || []).slice();
    done.unshift({ ...c, action: which, doneAt: new Date().toLocaleString() });
    saveBatchDone(done);
    renderBatchPanel();
    renderBatchTabs();
    showToast(which === 'skip' ? '已忽略，挪到「已处理」' : '已采用，挪到「已处理」');
}

// ===== 批量获取资料：任务跑在服务端，前端只负责「点一下 → 回来看结果」=====
// 以前是前端 for 循环逐个请求，等于「面板不能关、页面不能关」，用户要盯着十几分钟。
// 现在 POST 一下就交给后台线程，面板立刻关掉，侧边栏显示进度，跑完自动弹结果。
let batchTimer = null;
let batchTicking = false;

// 把服务端状态同步到界面（侧边栏进度 + 页面页头/按钮）
const BATCH_TIP_IDLE = '空字段（简介、本地没填的抖音号）直接补上；昵称或抖音号跟本地不一致的先列在下面等你决定。'
    + '点「开始获取」后任务在后台跑，你可以切到别的页面继续浏览，跑完会提示。';

function applyBatchState(s) {
    const running = !!(s && s.running);
    const prog = document.getElementById('batchProgress');
    if (prog) {
        prog.classList.toggle('hidden', !running);
        prog.textContent = running ? `${s.done}/${s.total || '?'}` : '';
    }
    const runBtn = document.getElementById('batchRunBtn');
    const stopBtn = document.getElementById('batchStopBtn');
    if (runBtn) {
        runBtn.disabled = running;
        runBtn.textContent = running ? '获取中…' : '开始获取';
    }
    if (stopBtn) stopBtn.classList.toggle('hidden', !running);
    // 跑起来以后设置就锁住（改了也不会影响正在跑的这一轮，容易让人误以为生效了）
    const optBox = document.getElementById('batchOpts');
    if (optBox) optBox.classList.toggle('disabled', !!running);

    const sub = document.getElementById('batchViewSub');
    if (sub) {
        if (running) {
            sub.textContent = `${s.done}/${s.total || '?'} · 补全 ${s.filled || 0} · 待确认 ${s.conflicts || 0}`;
        } else {
            const n = (state.batchConflicts || []).length;
            sub.textContent = n ? `${n} 个待你确认` : '';
        }
    }
    const tip = document.getElementById('batchRunTip');
    if (tip) {
        if (running) {
            tip.textContent = `后台获取中 ${s.done}/${s.total || '?'} · 补全 ${s.filled || 0}`
                + ` · 待确认 ${s.conflicts || 0} · 失败 ${s.failed || 0}。可以切到别的页面继续浏览，跑完会提示。`;
            tip.dataset.mode = 'run';
        } else if (tip.dataset.mode === 'run') {
            tip.textContent = BATCH_TIP_IDLE;
            tip.dataset.mode = 'idle';
        }
    }
    // 进页面就能看到「上次运行」的统计，不用等新结果
    // （只在还没有完整结果时才用 status 的简化数字，否则会把明细覆盖掉）
    if (s && s.finished && !state.batchResult) {
        renderBatchSummary({
            total: s.total, filled: s.filled, conflicts: s.conflicts,
            failed: s.failed, finished: s.finished,
        });
    }
}

function startBatchPolling() {
    if (batchTimer) return;
    const tick = async () => {
        if (batchTicking) return;
        batchTicking = true;
        let s = null;
        try { s = await fetch('/api/batch_status').then((r) => r.json()); } catch (e) { /* 服务重启中，下轮再说 */ }
        batchTicking = false;
        if (s) applyBatchState(s);
        if (s && s.running) return;
        if (batchTimer) { clearInterval(batchTimer); batchTimer = null; }
        if (s && s.unread) await consumeBatchResult();
    };
    tick();
    batchTimer = setInterval(tick, 1500);
}

// 重新拉一份 user_data.json（服务端后台已经把空字段补进去了）
async function reloadUserData() {
    try {
        const r = await fetch('user_data.json?t=' + Date.now());
        if (!r.ok) return false;
        const d = await r.json();
        if (!d || !d.accounts) return false;
        state.userData = d;
        if (state.currentAuthor) {
            const ua = d.accounts[state.currentAuthor.uid];
            if (ua) {
                ['nickname', 'douyin_id', 'signature', 'alias', 'sec_uid', 'status', 'cover'].forEach((k) => {
                    if (ua[k] !== undefined) state.currentAuthor[k] = ua[k];
                });
                if (isDetailOpen()) refreshTop();
            }
        }
        refreshMeta();
        renderAuthorGrid();
        return true;
    } catch (e) { return false; }
}

// 顶部这一排是「标签页」，不是统计数字：点哪个，下面就只显示哪一块。
// （以前是四块堆在一起、点完还要滚下去找，点「失败」得一路滑到底）
function renderBatchSummary(d) {
    if (d) state.batchResult = d;          // 记住这份结果，切标签时用它渲染明细
    renderBatchTabs();
    applyBatchFilter();
}

function renderBatchTabs() {
    const box = document.getElementById('batchSummary');
    if (!box) return;
    const r = state.batchResult || {};
    const pending = (state.batchConflicts || []).length;      // 本地实际还剩多少要处理
    const done = (state.batchDone || []).length;
    const filled = typeof r.filled === 'number' ? r.filled : 0;
    const failed = Array.isArray(r.failed) ? r.failed.length : (r.failed || 0);
    const total = r.total || 0;
    if (!total && !filled && !failed && !pending && !done) { box.classList.add('hidden'); return; }
    box.classList.remove('hidden');
    box.innerHTML = `
        ${total || r.finished ? `<span class="bs-title">上批 ${total || '?'} 个${r.finished ? ' · ' + escapeHtml(r.finished) : ''}</span>` : ''}
        <span class="bs-item warn" data-bf="conflict" onclick="setBatchFilter('conflict')" title="昵称/抖音号跟本地不一致，等你决定">待确认 ${pending}</span>
        <span class="bs-item ok" data-bf="filled" onclick="setBatchFilter('filled')" title="这次自动补了哪些字段">补全 ${filled}</span>
        <span class="bs-item bad" data-bf="failed" onclick="setBatchFilter('failed')" title="没取到资料的账号">失败 ${failed}</span>
        <span class="bs-item done" data-bf="done" onclick="setBatchFilter('done')" title="刚处理过的账号">已处理 ${done}</span>`;
}

// 切标签：只显示对应那一块，并且直接回到顶部（不用再往下滚）
function setBatchFilter(f) {
    state.batchFilter = f || 'conflict';
    applyBatchFilter();
    if (f === 'failed') {
        const det = document.querySelector('#batchFailedBox details');
        if (det) det.open = true;
    }
    const c = document.querySelector('.content');
    if (c) {
        if (typeof c.scrollTo === 'function') { try { c.scrollTo({ top: 0, behavior: 'smooth' }); } catch (e) { c.scrollTop = 0; } }
        else c.scrollTop = 0;
    }
}

const BATCH_PANES = {
    conflict: 'paneConflict',
    filled: 'paneFilled',
    failed: 'paneFailed',
    done: 'paneDone',
};

function applyBatchFilter() {
    const f = state.batchFilter || 'conflict';
    Object.keys(BATCH_PANES).forEach((k) => {
        const el = document.getElementById(BATCH_PANES[k]);
        if (el) el.classList.toggle('hidden', k !== f);
    });
    document.querySelectorAll('#batchSummary .bs-item').forEach((el) => {
        el.classList.toggle('active', el.dataset.bf === f);
    });
}

const BATCH_FIELD_LABEL = { nickname: '昵称', douyin_id: '抖音号', signature: '简介' };

function renderBatchFilled(list) {
    const box = document.getElementById('batchFilledBox');
    if (!box) return;
    const cnt = document.getElementById('batchFilledCount');
    if (cnt) cnt.textContent = `${list.length} 个`;
    if (!list.length) {
        const filled = (state.batchResult || {}).filled || 0;
        box.innerHTML = `<div class="bc-empty">${filled
            ? '这份结果没有补全明细（旧版本跑的，当时没记录）。重新跑一次就能看到「补了多少个、各改了哪些字段」。'
            : '这次没有自动补全任何字段。'}</div>`;
        return;
    }
    box.innerHTML = list.map((f) => `
        <div class="bf-row">
            <span class="bf-name bc-link" onclick="openAuthor('${f.uid}')" title="打开作者主页">${escapeHtml(f.nickname || f.uid)}<b class="bc-open">↗</b></span>
            <span class="bf-err">${(f.changed || []).map((k) => BATCH_FIELD_LABEL[k] || k).join(' · ')}</span>
            <span class="bf-dy">${escapeHtml(f.douyin_id || '')}</span>
        </div>`).join('');
}

function renderBatchFailed(list) {
    const box = document.getElementById('batchFailedBox');
    if (!box) return;
    const cnt = document.getElementById('batchFailedCount');
    if (cnt) cnt.textContent = `${list.length} 个`;
    // 显隐交给外层「失败」面板（标签页），这里只管内容
    if (!list.length) {
        box.innerHTML = '<div class="bc-empty">这批没有失败的账号。</div>';
        return;
    }
    box.innerHTML = `<details><summary>${list.length} 个没取到资料（点开看原因）</summary>`
        + list.map((f) => `<div class="bf-row">`
            + `<span class="bf-name bc-link" onclick="openAuthor('${f.uid}')" title="打开这位作者的主页">`
            + `${escapeHtml(f.nickname || f.uid)}<b class="bc-open">↗</b></span>`
            + `<span class="bf-err">${escapeHtml(f.error || '')}</span></div>`).join('')
        + '</details>';
}

// 取走后台跑完的结果：冲突入待确认清单、标注重新加载、面板打开等处理
async function consumeBatchResult() {
    let d = null;
    try { d = await fetch('/api/batch_result?consume=1').then((r) => r.json()); } catch (e) { return; }
    if (!d || !d.ok) return;

    const byUid = new Map((state.batchConflicts || []).map((c) => [c.uid, c]));
    const doneMap = new Map((state.batchDone || []).map((c) => [c.uid, c]));
    (d.conflicts || []).forEach((c) => {
        if (!c || !c.uid) return;
        const was = doneMap.get(c.uid);
        // 上次已经「忽略」或「全部采用」，而这次返回的还是同一组值 → 别再冒出来烦人
        // （只点了「用新昵称 / 用新抖音号」的不能跳过：另一个字段可能还没处理）
        const settled = was && (was.action === 'skip' || was.action === 'all');
        if (settled
            && (was.newNickname || '') === (c.newNickname || '')
            && (was.newDouyinId || '') === (c.newDouyinId || '')) return;
        byUid.set(c.uid, c);
    });
    saveBatchConflicts([...byUid.values()]);

    // 先把自己没保存的改动落盘，再拉磁盘版本，避免覆盖用户刚改的字段
    await saveUserData();
    await reloadUserData();

    renderBatchSummary(d);
    renderBatchFailed(d.failed || []);
    renderBatchFilled(d.filledList || []);
    applyBatchFilter();
    applyBatchState({ running: false });
    renderBatchPanel();
    // 有结果就直接把这页打开给你处理；正在看视频/详情时不打断，只提示
    if (state.view === 'batch') { renderBatchPanel(); }
    else if (!isDetailOpen()) { await openBatchView(); }
    else { showToast(`批量获取完成，${(d.conflicts || []).length} 个待确认（侧边栏「批量获取资料」）`); return; }

    const n = (d.conflicts || []).length;
    showToast(`批量获取完成：补全 ${d.filled || 0} 个`
        + (n ? `，${n} 个要你确认` : '')
        + ((d.failed || []).length ? `，失败 ${(d.failed || []).length} 个` : ''));
}

// ===== 批量刷新「抖音当前作品数」 =====
// 风险控制：走公开接口（不登录）+ 每个 1.6 秒左右随机抖动 + 连续 20 个失败就停 + 单轮上限 400。
// 用户点一次只跑一小批，不要一口气刷 1400 个。
let countTimer = null;

async function startCountRefresh() {
    const btn = document.getElementById('countBtn');
    if (btn && btn.disabled) return;
    const raw = prompt('这一轮刷多少个账号的作品数？\n\n· 建议 200~300（约 1.6 秒一个，300 个约 8 分钟）\n· 单轮最多 400，可以分几次刷完\n· 随时可以点「停止数作品」', '300');
    if (raw === null) return;
    const limit = Math.max(1, Math.min(400, parseInt(raw, 10) || 300));
    if (btn) { btn.disabled = true; btn.textContent = '启动中…'; }
    try {
        const r = await fetch('/api/count_refresh', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'start', limit }),
        }).then((x) => x.json());
        if (!r.ok) {
            showToast(r.already ? '上一轮还在跑' : ('启动失败：' + (r.error || '未知')));
            if (btn) { btn.disabled = false; btn.textContent = '📈 数作品'; }
            return;
        }
        showToast(`开始数作品：${limit} 个，约 ${Math.round(limit * 1.6 / 60)} 分钟`);
        startCountPolling();
    } catch (e) {
        showToast('启动失败：' + e.message);
        if (btn) { btn.disabled = false; btn.textContent = '📈 数作品'; }
    }
}

function startCountPolling() {
    if (countTimer) return;
    const tick = async () => {
        let s = null;
        try { s = await fetch('/api/count_status').then((r) => r.json()); } catch (e) { return; }
        const btn = document.getElementById('countBtn');
        const stopBtn = document.getElementById('countStopBtn');
        if (s.running) {
            if (btn) { btn.disabled = true; btn.textContent = `📈 ${s.done}/${s.total}`; }
            if (stopBtn) stopBtn.classList.remove('hidden');
        } else {
            if (btn) { btn.disabled = false; btn.textContent = '📈 数作品'; }
            if (stopBtn) stopBtn.classList.add('hidden');
            stopCountPolling();
            if (s.done) {
                showToast(`作品数刷新完成：成功 ${s.succeeded} · 失败 ${s.failed}`
                    + (s.error ? `（${s.error}）` : ''));
                // 详情页开着的话顺手刷新一下，让新数字立刻生效
                if (isDetailOpen() && detailState.author) {
                    await openAuthor(detailState.author.uid);
                }
            }
        }
    };
    countTimer = setInterval(tick, 2000);
    tick();
}

function stopCountPolling() {
    if (countTimer) { clearInterval(countTimer); countTimer = null; }
}

async function stopCountRefresh() {
    try {
        await fetch('/api/count_refresh', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'stop' }),
        });
        showToast('已请求停止，正在收尾');
    } catch (e) { showToast('停止失败：' + e.message); }
}

// ===== 批量联网查「最新作品发布时间」 =====
// 和「数作品」是两条完全不同的链路：数作品打公开接口（不登录、不签名，压力小，1.6 秒一个）；
// 这个必须走登录态 + a_bogus 签名 + curl_cffi 指纹打 aweme/post，单个请求重得多，
// 所以节奏放到 2.6 秒一个、单轮最多 120、连续 8 个失败就熔断（数作品是连续 20 个）。
// 7 天内查过的自动跳过——查同一个号第 2 次和第 500 次对风控没区别，纯浪费额度。
let latestTimer = null;

async function startLatestRefresh() {
    const btn = document.getElementById('latestBtn');
    if (btn && btn.disabled) return;
    const raw = prompt(
        '这一轮联网查多少个作者的「最新作品发布时间」？\n\n'
        + '· 建议 50~100（约 3.5 秒一个，每 25 个还会停 30 秒，100 个约 7 分钟）\n'
        + '· 单轮最多 100，可以分几次刷完全部\n'
        + '· 7 天内查过的会自动跳过，只查没查过的和过期了的\n'
        + '· 想全部重查就填 0（强制重查，慎用）',
        '60');
    if (raw === null) return;
    const n = parseInt(raw, 10);
    if (isNaN(n) || n < 0) { showToast('请填 0~100 的整数'); return; }
    const force = n === 0;
    const limit = force ? 100 : Math.max(1, Math.min(100, n));
    if (btn) { btn.disabled = true; btn.textContent = '启动中…'; }
    try {
        const r = await fetch('/api/latest_refresh', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'start', limit, force }),
        }).then((x) => x.json());
        if (!r.ok) {
            showToast(r.already ? '上一轮还在跑' : ('启动失败：' + (r.error || '未知')));
            if (btn) { btn.disabled = false; btn.textContent = '🕗 查发布时间'; }
            return;
        }
        const mins = Math.ceil((limit * 3.5 + Math.floor(limit / 25) * 30) / 60);
        showToast(`开始查发布时间：最多 ${limit} 个${force ? '（强制重查）' : ''}，约 ${mins} 分钟`);
        startLatestPolling();
    } catch (e) {
        showToast('启动失败：' + e.message);
        if (btn) { btn.disabled = false; btn.textContent = '🕗 查发布时间'; }
    }
}

function startLatestPolling() {
    if (latestTimer) return;
    const tick = async () => {
        let s = null;
        try { s = await fetch('/api/latest_status').then((r) => r.json()); } catch (e) { return; }
        const btn = document.getElementById('latestBtn');
        const stopBtn = document.getElementById('latestStopBtn');
        if (s.running) {
            if (btn) {
                btn.disabled = true;
                // ⏸ = 正在「每 25 个停 30 秒」的空档里，不做个记号会像卡死了
                btn.textContent = `🕗 ${s.done}/${s.total}${s.pausing ? ' ⏸' : ''}`;
            }
            if (stopBtn) stopBtn.classList.remove('hidden');
        } else {
            if (btn) { btn.disabled = false; btn.textContent = '🕗 查发布时间'; }
            if (stopBtn) stopBtn.classList.add('hidden');
            stopLatestPolling();
            if (s.done || s.error) {
                const why = s.last_error ? `；最近一个失败：${s.last_error}` : '';
                showToast(`发布时间查询结束：成功 ${s.succeeded} · 失败 ${s.failed}`
                    + (s.empty ? ` · 无公开作品 ${s.empty}` : '')
                    + (s.dead ? ` · SEC_UID 失效 ${s.dead}（要重新补）` : '')
                    + (s.skipped ? ` · 跳过 ${s.skipped}（${s.ttl_days || 7} 天内查过）` : '')
                    + (s.error ? `（${s.error}）` : (s.failed ? why : '')));
            }
            // 列表里的「发布 XXXX-XX-XX」是开页时一起带下来的，刷完得重新拉一遍才更新
            if (state.view === 'authors') loadAuthors(true);
            if (isDetailOpen() && detailState.author) await openAuthor(detailState.author.uid);
        }
    };
    latestTimer = setInterval(tick, 2000);
    tick();
}

function stopLatestPolling() {
    if (latestTimer) { clearInterval(latestTimer); latestTimer = null; }
}

async function stopLatestRefresh() {
    try {
        await fetch('/api/latest_refresh', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'stop' }),
        });
        showToast('已请求停止，正在收尾');
    } catch (e) { showToast('停止失败：' + e.message); }
}

async function batchFetchProfiles() {
    await openBatchView();       // 整页切换，不是弹层
}


// ===== 批量获取资料的设置（2026-10-28）=====
// 三组：**获取哪些字段** / **跳过哪些状态** / **按什么顺序**。
// DOM 里的勾选框就是唯一真相（readBatchOptions 直接读它），localStorage 只负责记住上次选择。
// 顺序默认「添加时间 新→旧」：刚下载进来的博主最缺资料，先查它们才有意义。
const BATCH_OPT_KEY = 'batchOptions';

function readBatchOptions() {
    const box = document.getElementById('batchOpts');
    if (!box) {
        return { fields: ['nickname', 'douyin_id', 'signature'],
                 skip_status: ['禁言/封号', '注销'], order: 'added' };
    }
    const fields = Array.from(box.querySelectorAll('input[data-field]'))
        .filter((i) => i.checked).map((i) => i.dataset.field);
    const skip = Array.from(box.querySelectorAll('input[data-skip]'))
        .filter((i) => i.checked).map((i) => i.dataset.skip);
    const sel = document.getElementById('batchOrder');
    return { fields, skip_status: skip, order: sel ? sel.value : 'added' };
}

function restoreBatchOptions() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(BATCH_OPT_KEY) || 'null'); } catch (e) { saved = null; }
    const box = document.getElementById('batchOpts');
    if (!saved || !box) return;
    if (Array.isArray(saved.fields)) {
        box.querySelectorAll('input[data-field]').forEach((i) => {
            i.checked = saved.fields.indexOf(i.dataset.field) >= 0;
        });
    }
    if (Array.isArray(saved.skip_status)) {
        box.querySelectorAll('input[data-skip]').forEach((i) => {
            i.checked = saved.skip_status.indexOf(i.dataset.skip) >= 0;
        });
    }
    const sel = document.getElementById('batchOrder');
    if (sel && saved.order) sel.value = saved.order;
}

function onBatchOptionChange() {
    const o = readBatchOptions();
    // 一个字段都没勾 = 跑一遍什么都写不进去。后端会回落到「全选」，所以这里必须提醒，
    // 否则用户以为「只看了不改」，实际却把三个字段都补上了。
    if (!o.fields.length) showToast('至少选一个要获取的内容');
    try { localStorage.setItem(BATCH_OPT_KEY, JSON.stringify(o)); } catch (e) {}
    refreshBatchPreview();
}

// 「本次将获取 N 个」——按当前设置预演一遍（不真跑），让大家知道跳过多少
let _batchPreviewTimer = null;
function refreshBatchPreview() {
    if (_batchPreviewTimer) clearTimeout(_batchPreviewTimer);
    _batchPreviewTimer = setTimeout(async () => {
        _batchPreviewTimer = null;
        const el = document.getElementById('batchOptCount');
        if (el) el.textContent = '正在算…';
        try {
            const r = await fetch('/api/batch_preview', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(readBatchOptions()),
            });
            const d = await r.json();
            if (!d.ok) throw new Error(d.error || ('HTTP ' + r.status));
            if (el) {
                el.innerHTML = '共 ' + d.all + ' 个 · 本次将获取 <b>' + d.total + '</b> 个'
                    + (d.skipped ? '（跳过 ' + d.skipped + ' 个）' : '');
            }
        } catch (e) {
            if (el) el.textContent = '算不出来：' + e.message;
        }
    }, 120);
}


async function runBatchProfiles() {
    const btn = document.getElementById('batchRunBtn');
    if (btn && btn.disabled) return;
    if (btn) { btn.disabled = true; btn.textContent = '启动中…'; }
    const tip = document.getElementById('batchRunTip');
    try {
        const r = await fetch('/api/batch_profile', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            // 带上设置：获取哪些字段 / 跳过哪些状态 / 按什么顺序
            body: JSON.stringify(Object.assign({ action: 'start' }, readBatchOptions())),
        });
        const d = await r.json();
        if (!d.ok) {
            if (d.already) {
                showToast('上一批还在跑，先等它结束');
                startBatchPolling();
            } else {
                showToast('启动失败：' + (d.error || '未知错误'));
                if (btn) { btn.disabled = false; btn.textContent = '开始获取'; }
                if (tip) tip.textContent = '启动失败：' + (d.error || '未知错误');
            }
            return;
        }
        showToast('已在后台获取，你可以切到别的页面继续浏览，跑完会提示');
        startBatchPolling();
    } catch (e) {
        showToast('启动失败：' + e.message);
        if (btn) { btn.disabled = false; btn.textContent = '开始获取'; }
    }
}

async function stopBatchProfiles() {
    try {
        const r = await fetch('/api/batch_profile', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'stop' }),
        });
        const d = await r.json();
        showToast(d.ok ? '已请求停止，当前这个查完就停' : (d.error || '停止失败'));
    } catch (e) {
        showToast('停止失败：' + e.message);
    }
}

async function loadAltGroups() {
    const box = document.getElementById('altFolderGrid');
    if (box && !state.altGroups.length) box.innerHTML = '<div class="loading">加载中...</div>';
    try {
        const d = await fetch('/api/alt_groups').then((r) => r.json());
        state.altGroups = d.groups || [];
        state.altGroupNames = d.names || ((state.userData && state.userData.alt_group_names) || {});
        const sub = document.getElementById('altViewSub');
        if (sub) sub.textContent = `${d.total_groups || 0} 组 · ${d.total_accounts || 0} 个账号`;
        const el = document.getElementById('altEntryCount');
        if (el) el.textContent = d.total_groups || 0;
        renderAltGroups();
    } catch (e) {
        if (box) box.innerHTML = '<div class="loading">读取分组失败：' + escapeHtml(e.message) + '</div>';
    }
}

function renderAltGroups() {
    const box = document.getElementById('altFolderGrid');
    if (!box) return;
    const groups = state.altGroups || [];
    if (!groups.length) {
        box.innerHTML = `<div class="alt-empty">
            <div class="alt-empty-icon">📁</div>
            <div>还没有大小号分组</div>
            <div class="tag-hint">在作者详情页点「🔗 关联大小号」，把同一个人的几个号并成一组，就会出现在这里</div>
        </div>`;
        return;
    }
    box.innerHTML = groups.map((g, i) => altFolderHtml(g, i)).join('');
    // 头像/封面懒加载（复用缩略图观察器）
    observeThumbs(box);
}

function altFolderHtml(g, i) {
    const key = g.key;
    const open = state.altOpen[key] !== false;          // 默认展开
    const name = (state.altGroupNames[key] || g.name || '未命名组').trim();
    const members = (g.members || []).map((m) => {
        const cover = coverUrlFrom(m.cover);
        const avatar = cover
            ? `<img class="alt-member-cover" src="${cover}" loading="lazy" alt="" onerror="coverBroken(this)">`
            : `<div class="alt-member-cover placeholder">📱</div>`;
        const star = m.starred ? '⭐' : '';
        const statusTag = m.status ? statusTagHtml(m.status).replace('mini-tag', 'mini-tag alt-status') : '';
        // 大号只加一个小标记，不做整行加深；设大号入口在作者主页（详情页）
        const isMain = m.main_uid && m.main_uid === m.uid;
        const mainBadge = isMain ? '<span class="main-badge">大号</span>' : '';
        return `<div class="alt-member" data-uid="${escapeHtml(m.uid)}" onclick="openAuthor('${onClickArg(m.uid)}')"
                     title="UID ${m.uid}${m.scanned ? '' : ' · 磁盘上没有这个账号'}">
            ${avatar}
            <div class="alt-member-main">
                <div class="alt-member-name">${star}${nameHtml(m)}${mainBadge}</div>
                <div class="alt-member-meta">${m.work_count ? m.work_count + ' 作品' : '无本地作品'}${statusTag}</div>
            </div>
            <span class="alt-member-arrow">›</span>
        </div>`;
    }).join('');

    return `<div class="alt-folder${open ? ' open' : ''}" data-idx="${i}">
        <div class="alt-folder-head" onclick="toggleAltFolder(${i})">
            <span class="folder-icon">${open ? '📂' : '📁'}</span>
            <span class="folder-name" title="${escapeHtml(key)}">${escapeHtml(name)}</span>
            <span class="folder-count">${g.size} 个账号</span>
            <span class="folder-ops">
                <a title="给这个文件夹改名" onclick="event.stopPropagation();renameAltFolder(${i})">✎</a>
                <a class="folder-clear" title="解除这一组的全部关联" onclick="event.stopPropagation();clearAltFolder(${i})">解散</a>
            </span>
            <span class="folder-caret">${open ? '▴' : '▾'}</span>
        </div>
        ${open ? `<div class="alt-folder-body">${members}</div>` : ''}
    </div>`;
}

function toggleAltFolder(i) {
    const g = (state.altGroups || [])[i];
    if (!g) return;
    const cur = state.altOpen[g.key] !== false;
    state.altOpen[g.key] = !cur;
    renderAltGroups();
}

async function renameAltFolder(i) {
    const g = (state.altGroups || [])[i];
    if (!g) return;
    const cur = state.altGroupNames[g.key] || g.name || '';
    const val = prompt('给这个文件夹起个名字：', cur);
    if (val === null) return;
    const name = String(val).trim();
    const prev = state.altGroupNames[g.key];
    state.userData.alt_group_names = state.userData.alt_group_names || {};
    // 清空名字必须送 null 而不是 delete 键：服务端是「合并写」，
    // 键不存在 = 不改动，只有显式 null 才会把磁盘上那条删掉
    // （旧写法 delete 之后保存，刷新一下旧名字又回来了）
    state.userData.alt_group_names[g.key] = name ? name : null;
    state.dirtyMeta = true;
    state.altGroupNames = state.altGroupNames || {};
    if (name) state.altGroupNames[g.key] = name;
    else delete state.altGroupNames[g.key];
    const okSave = await saveUserData();
    if (!okSave) {
        // 保存失败：把内存改回去，别让界面显示「改了」而磁盘还是旧名
        if (prev) state.altGroupNames[g.key] = prev;
        else delete state.altGroupNames[g.key];
        renderAltGroups();
        return;
    }
    renderAltGroups();
    showToast(name ? '文件夹改名为「' + name + '」' : '已恢复默认名字');
}

// 从文件夹里直接解散整组：把每人写成单人组
async function clearAltFolder(i) {
    const g = (state.altGroups || [])[i];
    if (!g) return;
    const ok = confirm(`确定解除「${state.altGroupNames[g.key] || g.name}」这 ${g.size} 个账号之间的全部关联吗？\n\n组内是全关联，解散后它们互相都看不到对方（账号本身不会被删除）。`);
    if (!ok) return;
    (g.members || []).forEach((m) => writeAltGroup(new Set([m.uid])));
    // 必须等保存回来再重拉，否则会读到解散前的分组（界面上看着「点了没反应」）
    const okSave = await saveUserData();
    if (!okSave) return;
    showToast('已解散这一组');
    loadAltGroups();
}

// 只重绘上方作者信息区，播放器和左栏预览列表保持不动（避免视频被打断）
function refreshTop() {
    if (detailState.author) renderDetailTop();
}

async function openInFolder(rel) {
    try {
        const r = await fetch('/api/open', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: rel }),
        });
        const d = await r.json().catch(() => ({}));
        if (r.ok && d.ok) showToast('已在文件夹中定位');
        else showToast('打开失败：' + (d.error || ('HTTP ' + r.status)));
    } catch (e) {
        showToast('打开失败：' + (e && e.message || e));
    }
}

// ===== 事件绑定 =====
function bindEvents() {
    const searchInput = document.getElementById('searchInput');
    const searchClear = document.getElementById('searchClear');
    let searchTimer;
    const syncSearchClear = () => {
        if (searchClear) searchClear.classList.toggle('hidden', !searchInput.value);
    };
    searchInput.addEventListener('input', () => {
        syncSearchClear();
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
            state.searchQuery = searchInput.value.trim();
            applyFilters();
        }, 300);
    });
    if (searchClear) {
        searchClear.addEventListener('click', () => {
            searchInput.value = '';
            state.searchQuery = '';
            syncSearchClear();
            applyFilters();
            searchInput.focus();
        });
    }
    document.getElementById('searchBtn').addEventListener('click', () => {
        state.searchQuery = searchInput.value.trim();
        applyFilters();
    });

    // 快捷键：按 / 直接跳到搜索框（焦点不在输入框时）
    document.addEventListener('keydown', (e) => {
        if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
        const tag = (document.activeElement && document.activeElement.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        e.preventDefault();
        searchInput.focus();
        searchInput.select();
    });

    // 搜索范围下拉：切换昵称/备注/简介/作品/全部
    const searchScope = document.getElementById('searchScope');
    if (searchScope) {
        searchScope.addEventListener('change', () => {
            state.searchScope = searchScope.value;
            applyFilters();
        });
    }

    document.querySelectorAll('#categoryList .side-item').forEach(item => {
        item.addEventListener('click', () => {
            // 「分类」栏里现在混着三种语义不同的入口（2026-10-28 重排）：
            //   data-view="alts"  → 大小号文件夹视图
            //   data-status="xx"  → 账号状态筛选
            //   data-category="xx"→ 真正的分类（全部作者 / 星标）
            // 必须先分流。否则 dataset.category 是 undefined、下面又会把
            // currentStatus/currentLevel 清空——点下去等于没点。等级那一栏虽然已经
            // 独立出去了，这里也留着分流，防止以后又挪回来。
            if (item.dataset.view === 'alts') { openAltView(); return; }
            if (item.dataset.view === 'blacklist') { openBlacklistView(); return; }
            if (item.dataset.status) { openStatusView(item.dataset.status); return; }
            if (item.dataset.level) { openLevelView(item.dataset.level); return; }
            clearSideActive();
            item.classList.add('active');
            state.currentCategory = item.dataset.category || 'all';
            // 点分类（全部作者/星标）= 退出状态列 / 等级筛选 / 更新频率 / 标签 / 年份
            state.currentStatus = null;
            state.currentLevel = null;
            state.currentFreq = null;
            clearTagYearFilter();
            // 标题用固定文案，不要读 textContent（会把计数数字一起带进标题）
            setViewTitle(state.currentCategory === 'starred' ? '星标' : '全部作者');
            applyFilters();
        });
    });

    // 点标签卡和下拉面板以外的地方 → 收起标签下拉（改完标签重绘后节点已脱离文档，closest 仍能命中旧卡片）
    document.addEventListener('click', (e) => {
        if (!detailState.tagOpen) return;
        if (e.target.closest && (e.target.closest('.c-tags') || e.target.closest('.tag-picker'))) return;
        closeTagPicker();
    });

    // 点历史记录浮层以外的地方 → 收起（按钮自己会再 toggle，所以命中 .hist-wrap 就跳过）
    document.addEventListener('click', (e) => {
        if (!detailState.histOpen) return;
        if (e.target.closest && (e.target.closest('.hist-wrap') || e.target.closest('.hist-pop'))) return;
        detailState.histOpen = false;
        refreshTop();
    });

    // 「功能」栏入口：批量获取资料 / 获取最近收藏 / 导出作者信息 / 缓存
    // （大小号、账号状态、等级已移到「分类」「等级」两栏，由上面的 #categoryList / #levelList 监听器处理）
    const featureList = document.getElementById('featureList');
    if (featureList) {
        featureList.addEventListener('click', (e) => {
            const item = e.target.closest('.side-item');
            if (!item) return;
            if (item.dataset.action === 'batchProfile') batchFetchProfiles();
            else if (item.dataset.action === 'fetchFavorites') openFavoritesView();
            else if (item.dataset.action === 'exportAuthors') openExportView();
            else if (item.dataset.action === 'newDownloads') openNewDownloadsView();
            else if (item.dataset.action === 'watch') openWatchView();
            else if (item.dataset.action === 'files') openFilesView();
            else if (item.dataset.action === 'settings') openSettingsView();
        });
    }

    // 「等级」栏入口：夯 / 顶级 / 一般 / 拉
    const levelList = document.getElementById('levelList');
    if (levelList) {
        levelList.addEventListener('click', (e) => {
            const item = e.target.closest('.side-item');
            if (!item || !item.dataset.level) return;
            openLevelView(item.dataset.level);
        });
    }

    // 「更新频率」栏入口：day / 3天 / 周 / 月
    const freqList = document.getElementById('freqList');
    if (freqList) {
        freqList.addEventListener('click', (e) => {
            const item = e.target.closest('.side-item');
            if (!item || !item.dataset.freq) return;
            openFreqView(item.dataset.freq);
        });
    }

    // 侧边栏折叠 / 展开：顶栏左上角那颗常驻的 ☰ 按钮切换（2026-11-08 改版）。
    // 原来有「侧栏右上角 « 收起 + 折叠后浮出的 » 展开」两个按钮，位置会跑、也不好找，
    // 现在只剩一颗汉堡，图标和位置都不变 —— 展开时是它、收起时还是它。
    const sidebar = document.getElementById('sidebar');
    const toggleBtn = document.getElementById('sidebarToggle');
    const mobileSidebar = window.matchMedia ? window.matchMedia('(max-width: 680px)') : null;
    const setSidebarCollapsed = (collapsed) => {
        if (!sidebar) return;
        sidebar.classList.toggle('collapsed', collapsed);
        if (toggleBtn) {
            toggleBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
            toggleBtn.title = collapsed ? '展开左侧功能栏' : '收起左侧功能栏';
        }
        // 给 body 也挂一份：右侧播放区靠这个类名跟着加宽（新下载页用）。
        // 不能用媒体查询——收侧栏时窗口宽度不变，媒体查询根本不会触发。
        document.body.classList.toggle('side-collapsed', collapsed);
    };
    const syncSidebarForViewport = () => {
        if (mobileSidebar) setSidebarCollapsed(mobileSidebar.matches);
    };
    syncSidebarForViewport();
    if (mobileSidebar) {
        if (mobileSidebar.addEventListener) mobileSidebar.addEventListener('change', syncSidebarForViewport);
        else if (mobileSidebar.addListener) mobileSidebar.addListener(syncSidebarForViewport);
        window.addEventListener('resize', syncSidebarForViewport);
    }
    if (sidebar && toggleBtn) {
        toggleBtn.addEventListener('click', () => {
            setSidebarCollapsed(!sidebar.classList.contains('collapsed'));
        });
    }
    if (sidebar) {
        sidebar.addEventListener('click', (e) => {
            if (mobileSidebar && mobileSidebar.matches && e.target.closest('.side-item, .tag-chip')) {
                setSidebarCollapsed(true);
            }
        });
    }

    document.getElementById('tagCloud').addEventListener('click', e => {
        const chip = e.target.closest('.tag-chip');
        if (!chip) return;
        const tag = chip.dataset.tag;
        // 点标签 = 换一种筛选口径，退出状态列 / 等级（否则标题写「标签：x」，
        // 列表其实是「状态 ∩ 标签」，对不上）
        state.currentStatus = null;
        state.currentLevel = null;
        clearSideActive();
        if (state.currentTag === tag) {
            state.currentTag = null;
            chip.classList.remove('active');
            setViewTitle('全部作者');
        } else {
            document.querySelectorAll('.tag-chip').forEach(c => c.classList.remove('active'));
            chip.classList.add('active');
            state.currentTag = tag;
            setViewTitle('标签：' + tag);
        }
        applyFilters();
    });

    document.getElementById('yearList').addEventListener('click', e => {
        const item = e.target.closest('.side-item');
        if (!item) return;
        const year = item.dataset.year;
        // 同标签：点年份也是换口径，退出状态列 / 等级
        state.currentStatus = null;
        state.currentLevel = null;
        clearSideActive();
        if (state.currentYear === year) {
            state.currentYear = null;
            item.classList.remove('active');
            setViewTitle('全部作者');
        } else {
            document.querySelectorAll('#yearList .side-item').forEach(i => i.classList.remove('active'));
            item.classList.add('active');
            state.currentYear = year;
            setViewTitle(year + ' 年');
        }
        applyFilters();
    });

    document.getElementById('sortSelect').addEventListener('change', e => {
        state.sortBy = e.target.value;
        state.sortDir = '';    // 换排序字段就回到字段默认方向
        updateSortDirBtn();
        applyFilters();
    });

    // 正反序按钮：默认方向 ↔ 反方向 来回切
    window.toggleSortDir = function () {
        const def = SORT_DEFAULT_DIR[state.sortBy] || 'asc';
        state.sortDir = (state.sortDir || def) === 'asc' ? 'desc' : 'asc';
        updateSortDirBtn();
        applyFilters();
    };

    document.getElementById('rescanBtn').addEventListener('click', async () => {
        const btn = document.getElementById('rescanBtn');
        if (btn.disabled) return;
        const label = btn.textContent;
        btn.disabled = true;
        try {
            const r = await fetch('/api/rescan', { method: 'POST' });
            const d = await r.json();
            if (!d || !d.ok) {
                showToast((d && d.error) || '扫描启动失败');
                btn.disabled = false;
                return;
            }
        } catch (e) {
            showToast('扫描启动失败');
            btn.disabled = false;
            return;
        }
        // 全量扫描要几分钟：按服务端真实进度显示，扫完才自动刷新（原来固定等 1.5 秒就开始刷，等于假进度）
        const timer = setInterval(async () => {
            let s;
            try {
                s = await (await fetch('/api/rescan_status')).json();
            } catch (e) { return; }
            if (s && s.running) {
                if (s.total) {
                    btn.textContent = `🔄 ${s.done}/${s.total}`;
                    showToast(`扫描中 ${s.done}/${s.total}${s.phase ? '（' + s.phase + '）' : ''}`);
                } else {
                    btn.textContent = '🔄 扫描中…';
                    showToast(s.phase ? `扫描中：${s.phase}` : '扫描中…');
                }
                return;
            }
            clearInterval(timer);
            btn.textContent = label;
            btn.disabled = false;
            if (s && s.error) { showToast('扫描失败：' + s.error); return; }
            showToast('扫描完成，正在刷新…');
            setTimeout(() => location.reload(), 800);
        }, 1000);
    });

    // 后台增量扫描：服务端每 5 分钟自己看一次有没有新下载，扫完这里提示一声。
    // 不自动刷新页面（免得打断正在看的作品），只让人知道「有新东西了」。
    let autoScanSeen = null;
    setInterval(async () => {
        let s;
        try { s = await (await fetch('/api/rescan_status')).json(); } catch (e) { return; }
        if (!s || !s.finished) return;
        const key = (s.auto ? 'auto:' : 'manual:') + s.finished;
        if (autoScanSeen === null) { autoScanSeen = key; return; }   // 首次只记录，不弹
        if (key === autoScanSeen) return;
        autoScanSeen = key;
        if (s.auto) showToast(`后台已更新（${s.changes || 0} 个作者有新内容），点 ⟳ 刷新看新作品`);
    }, 60000);

    document.addEventListener('keydown', e => {
        if (e.key !== 'Escape') return;
        // 详情页资料选择 > 标签下拉 > 关联选择器 > 详情页，逐层关（Esc 只关最上面那层）
        // 注：批量获取资料、缓存、黑名单、收藏、导出、设置都已改成独立页面，不吃 Esc
        const pcBox = document.getElementById('profileChoice');
        if (pcBox && !pcBox.classList.contains('hidden')) { closeProfileChoice(); return; }
        if (closeTagPicker()) return;
        if (detailState.histOpen) { detailState.histOpen = false; refreshTop(); return; }
        const picker = document.getElementById('altPicker');
        if (picker && !picker.classList.contains('hidden')) { closeAltPicker(); return; }
        if (detailState.aliasEdit) { cancelAliasEdit(); return; }
        if (isDetailOpen()) closeDetail();
    });

    // 滚动容器有两个：列表态滚 .content；详情态滚 #wallScroll（左侧整块容器）
    const contentEl = document.querySelector('.content');
    const wallEl = document.getElementById('wallScroll');
    const backTop = document.getElementById('backTopBtn');
    let scrollTick = false;

    const onScroll = (el) => {
        if (scrollTick) return;
        scrollTick = true;
        requestAnimationFrame(() => {
            scrollTick = false;
            // 详情态的回顶按钮在返回栏里（常驻），浮动按钮只留给作者列表
            if (backTop) backTop.classList.toggle('hidden', isDetailOpen() || (el.scrollTop || 0) < 320);
            // 作者列表的无限滚动只在列表态生效
            if (isDetailOpen()) return;
            if (state.view !== 'authors') return;   // 大小号视图里别偷偷继续加载作者列表
            const loadMore = document.getElementById('loadMore');
            if (loadMore.classList.contains('hidden')) return;
            const rect = loadMore.getBoundingClientRect();
            if (rect.top < window.innerHeight + 200) loadAuthors(false);
        });
    };

    contentEl.addEventListener('scroll', () => onScroll(contentEl));
    if (wallEl) wallEl.addEventListener('scroll', () => onScroll(wallEl));
}

// 返回顶部：详情态滚左侧容器，列表态滚 .content
function scrollToTop() {
    const el = isDetailOpen()
        ? document.getElementById('wallScroll')
        : document.querySelector('.content');
    if (!el) return;
    if (el.scrollTo) el.scrollTo({ top: 0, behavior: 'smooth' });
    else el.scrollTop = 0;
    setTimeout(() => {
        const b = document.getElementById('backTopBtn');
        if (b && !isDetailOpen() && (el.scrollTop || 0) < 320) b.classList.add('hidden');
    }, 400);
}

// ===== 黑名单视图（侧边栏「分类」里的「🚫 黑名单」） =====
let blAllTags = [];       // 黑名单里出现过的所有标签（供选择器复用）
let blEditSec = null;     // 当前正在编辑标签的那一行的 sec_uid
let blEditQuery = '';     // 标签选择器的搜索词

function blReasonsHtml(r) {
    const tags = r.reasons || [];
    if (!tags.length) return '<span class="tag-hint">无</span>';
    return tags.map(t => `<span class="tag-chip" title="${escapeHtml(t)}">${escapeHtml(t)}<span class="tag-x" data-tag="${escapeHtml(t)}" onclick="blRemoveReason('${onClickArg(r.sec_uid)}', this.dataset.tag)">×</span></span>`).join('');
}

// 隐藏所有「整页视图」（作者列表 / 大小号 / 黑名单 / 缓存 / 收藏 / 导出 / 设置 / 批量）。
// 详情页 detailView 是另一套，不在这里管（由 openAuthor/closeDetail 单独控制）。
function hideAllViews() {
    ['authorView', 'altView', 'blacklistView', 'favoritesView', 'exportView', 'settingsView', 'newView', 'filesView', 'batchView', 'watchView']
        .forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.classList.add('hidden');
        });
    // 切视图一律把正在响的音视频停掉。播放器都是**常驻在 DOM 里**的（行模式右侧那块、
    // 详情页右栏、新下载右栏），只加 display:none 不会停声音 —— 用户 2026-11-08 报的
    // 「点了播放再切到别的界面它也不停」就是这个。这里统一扫一遍，不依赖各页面自己记着停。
    document.querySelectorAll('video, audio').forEach((el) => {
        try { el.pause(); } catch (e) { /* 忽略 */ }
    });
    // 「新下载」的播放区还要把 <video> 清掉（它是常驻的，留着会占内存）
    if (typeof ndPlayReset === 'function') ndPlayReset();
    // 行模式右侧播放区同理：把 <video> 拆了、卡片高亮清掉，下次进来是干净的待播状态
    if (typeof rowStopPlay === 'function') rowStopPlay();
}

// 记住当前视图的滚动位置（切走前调），下次切回再恢复，不丢进度
function rememberViewScroll() {
    const c = document.querySelector('.content');
    if (c && state.view) state.viewScroll[state.view] = c.scrollTop || 0;
}

// 恢复目标视图之前记住的滚动位置（首次进入 = 0）
function restoreViewScroll(key) {
    const c = document.querySelector('.content');
    if (!c) return;
    const saved = state.viewScroll[key] || 0;
    // 布局稳定后再恢复（切 display 后首帧高度可能还没算好）
    const raf = window.requestAnimationFrame || ((fn) => setTimeout(fn, 16));
    raf(() => { c.scrollTop = saved; });
}

async function openBlacklistView() {
    forceCloseDetail();
    rememberViewScroll();
    state.view = 'blacklist';
    state.returnView = 'blacklist';
    hideAllViews();
    document.getElementById('blacklistView').classList.remove('hidden');
    animateViewEnter('blacklistView', 'neutral');
    clearSideActive();
    document.querySelectorAll('#categoryList .side-item').forEach(i => i.classList.toggle('active', i.dataset.view === 'blacklist'));
    const eb = document.getElementById('exportBtn');
    if (eb) eb.classList.add('hidden');
    restoreViewScroll('blacklist');
    await loadBlacklist();
}

async function refreshBlacklistCount() {
    try {
        const d = await fetch('/api/blacklist').then((r) => r.json());
        const cnt = document.getElementById('blacklistCount');
        if (cnt) cnt.textContent = d.total || 0;
    } catch (e) { /* 忽略 */ }
}

// 手动添加黑名单：粘贴主页链接（SEC-UID），后端自动查 uid / 昵称再入库
async function addBlacklistAccount() {
    const link = prompt('粘贴要加入黑名单的博主主页链接（或纯 SEC_UID）：\n\n例：https://www.douyin.com/user/MS4wLjAB…\n\n会自动查 UID、昵称并加入黑名单。');
    if (link === null) return;
    const val = String(link).trim();
    if (!val) { showToast('没输入链接'); return; }
    showToast('正在查询并加入黑名单…');
    try {
        const r = await fetch('/api/blacklist', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'add', sec_uid: val, lookup: true }),
        });
        const d = await r.json();
        if (!d.ok) { showToast('添加失败：' + (d.error || '未知错误')); return; }
        showToast(d.added ? `已加入黑名单「${d.nickname || d.sec_uid || d.uid}」` : '这个博主已经在黑名单里了');
        await loadBlacklist();
        await refreshBlacklistCount();
    } catch (e) {
        showToast('添加失败：' + e.message);
    }
}

async function loadBlacklist() {
    const box = document.getElementById('blacklistBody');
    if (!box) return;
    box.innerHTML = '<div class="loading">加载中...</div>';
    try {
        const d = await fetch('/api/blacklist').then((r) => r.json());
        const items = (d && d.items) || [];
        blAllTags = (d && d.all_tags) || [];
        const sub = document.getElementById('blacklistViewSub');
        if (sub) sub.textContent = `共 ${d.total || 0} 个`;
        const cnt = document.getElementById('blacklistCount');
        if (cnt) cnt.textContent = d.total || 0;
        if (!items.length) {
            box.innerHTML = '<div class="alt-empty"><div class="alt-empty-icon">🚫</div><div>黑名单还是空的</div><div class="tag-hint">在「获取最近收藏」里可以把不想要的博主加进来</div></div>';
            return;
        }
        box.innerHTML = `<table class="bl-table">
            <thead><tr>
                <th>昵称</th><th>UID</th><th>原因（标签）</th><th>加入时间</th><th>主页链接</th><th>操作</th>
            </tr></thead>
            <tbody>${items.map((r) => `
                <tr>
                    <td class="bl-nick">${escapeHtml(r.nickname || '（未命名）')}</td>
                    <td class="bl-uid">${escapeHtml(r.uid || '')}</td>
                    <td class="bl-reason">${blReasonsHtml(r)}</td>
                    <td class="bl-time">${escapeHtml(r.added_at || '')}</td>
                    <td class="bl-url">${r.url ? `<a href="${escapeHtml(r.url)}" target="_blank" rel="noopener" title="${escapeHtml(r.sec_uid || '主页')}&#10;点击打开抖音主页">${escapeHtml(r.sec_uid || '主页')}</a>` : '<span class="tag-hint">无</span>'}</td>
                    <td class="bl-ops">
                        <button class="btn btn-mini" onclick="editBlacklistReason('${onClickArg(r.sec_uid)}')" title="改标签">✎ 标签</button>
                        <button class="btn btn-mini" onclick="removeBlacklist('${onClickArg(r.sec_uid)}')" title="移出黑名单">移除</button>
                    </td>
                </tr>`).join('')}
            </tbody></table>`;
    } catch (e) {
        box.innerHTML = '<div class="loading">读取黑名单失败：' + escapeHtml(e.message) + '</div>';
    }
}

async function removeBlacklist(secUid) {
    if (!secUid) return;
    if (!confirm('确定把这个博主移出黑名单吗？')) return;
    try {
        const r = await fetch('/api/blacklist', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'remove', sec_uid: secUid }),
        });
        const d = await r.json();
        if (!d.ok) { showToast('移除失败：' + (d.error || '未知错误')); return; }
        showToast(d.removed ? '已移出黑名单' : '没有找到这条记录');
        await loadBlacklist();
    } catch (e) {
        showToast('移除失败：' + e.message);
    }
}

// 打开「编辑黑名单标签」弹窗：勾选/新建标签，确定后保存
async function editBlacklistReason(secUid) {
    if (!secUid) return;
    // 取这一条的当前标签
    let curTags = [];
    try {
        const list = await fetch('/api/blacklist').then((r) => r.json());
        const rec = (list.items || []).find((x) => x.sec_uid === secUid);
        curTags = (rec && rec.reasons) || [];
    } catch (e) { /* 忽略 */ }
    blEditSec = secUid;
    blEditQuery = '';
    const panel = document.getElementById('blTagPanel');
    if (panel) panel.classList.remove('hidden');
    renderBlTagPicker(curTags);
}

function closeBlTagPanel() {
    blEditSec = null;
    blEditQuery = '';
    const panel = document.getElementById('blTagPanel');
    if (panel) panel.classList.add('hidden');
}

// 黑名单标签选择器：列出所有已出现的标签（可勾选）+ 输入框新建
function renderBlTagPicker(curTags) {
    const listEl = document.getElementById('blTagPickerList');
    if (!listEl) return;
    const all = blAllTags || [];
    const kw = blEditQuery.trim().toLowerCase();
    const selected = new Set(curTags || []);
    const tags = all.filter(t => !kw || t.toLowerCase().indexOf(kw) >= 0);
    const rows = [];
    // 已选标签放前面（用「已选」排序）
    const sorted = tags.sort((a, b) => (selected.has(b) ? 1 : 0) - (selected.has(a) ? 1 : 0));
    sorted.forEach((t) => {
        const on = selected.has(t);
        rows.push(`<div class="tag-picker-row${on ? ' has' : ''}" data-tag="${escapeHtml(t)}" onclick="blToggleTag(this.dataset.tag)">
            <span class="tag-picker-name">${escapeHtml(t)}</span>
            <span class="tag-picker-op">${on ? '✓ 已选' : '点击添加'}</span>
        </div>`);
    });
    // 输入了新名字 → 给「新建」行
    const raw = blEditQuery.trim();
    if (raw && all.indexOf(raw) < 0) {
        rows.unshift(`<div class="tag-picker-row new" data-tag="${escapeHtml(raw)}" onclick="blToggleTag(this.dataset.tag)">
            <span class="tag-picker-name">新建「${escapeHtml(raw)}」</span>
            <span class="tag-picker-op">＋ 创建并添加</span>
        </div>`);
    }
    listEl.innerHTML = rows.length ? rows.join('') : `<div class="tag-picker-empty">还没有标签，输入名字回车创建第一个</div>`;
    // 更新「已选标签」预览
    updateBlTagPreview(selected);
}

function blTagInput(v) {
    blEditQuery = v || '';
    const cur = blCurrentTags();
    renderBlTagPicker(cur);
}

// 拿到当前已选的标签（存在一个隐藏的临时数组里）
function blCurrentTags() {
    // 从预览区读
    const el = document.getElementById('blTagPreview');
    if (!el) return [];
    return Array.from(el.querySelectorAll('.tag-chip')).map(c => c.dataset.tag);
}

function blToggleTag(tag) {
    tag = String(tag || '').trim();
    if (!tag) return;
    let cur = blCurrentTags();
    if (cur.indexOf(tag) >= 0) {
        cur = cur.filter(t => t !== tag);
    } else {
        cur.push(tag);
    }
    updateBlTagPreview(new Set(cur));
    renderBlTagPicker(cur);
}

function updateBlTagPreview(selected) {
    const el = document.getElementById('blTagPreview');
    if (!el) return;
    const tags = Array.from(selected);
    el.innerHTML = tags.length
        ? tags.map(t => `<span class="tag-chip active" data-tag="${escapeHtml(t)}" title="${escapeHtml(t)}">${escapeHtml(t)}<span class="tag-x" data-tag="${escapeHtml(t)}" onclick="blTagPreviewRemove(this.dataset.tag)">×</span></span>`).join('')
        : '<span class="tag-hint">还没选标签</span>';
}

function blTagPreviewRemove(tag) {
    blToggleTag(tag);
}

async function saveBlTags() {
    const sec = blEditSec;
    if (!sec) return;
    const tags = blCurrentTags();
    try {
        // 拿到这条记录的 uid/nickname，删了重加（改标签）
        const list = await fetch('/api/blacklist').then((r) => r.json());
        const rec = (list.items || []).find((x) => x.sec_uid === sec);
        await fetch('/api/blacklist', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'remove', sec_uid: sec }),
        });
        await fetch('/api/blacklist', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                action: 'add', sec_uid: sec,
                uid: (rec && rec.uid) || '',
                nickname: (rec && rec.nickname) || '',
                reasons: tags,
            }),
        });
        showToast('已更新标签');
    } catch (e) {
        showToast('编辑失败：' + e.message);
    }
    closeBlTagPanel();
    await loadBlacklist();
}

// 直接从表格里点某个标签的 × 移除该标签
async function blRemoveReason(secUid, tag) {
    if (!secUid || !tag) return;
    try {
        const list = await fetch('/api/blacklist').then((r) => r.json());
        const rec = (list.items || []).find((x) => x.sec_uid === secUid);
        const rest = ((rec && rec.reasons) || []).filter(t => t !== tag);
        await fetch('/api/blacklist', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'remove', sec_uid: secUid }),
        });
        await fetch('/api/blacklist', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                action: 'add', sec_uid: secUid,
                uid: (rec && rec.uid) || '',
                nickname: (rec && rec.nickname) || '',
                reasons: rest,
            }),
        });
        showToast('已移除标签「' + tag + '」');
    } catch (e) {
        showToast('移除失败：' + e.message);
    }
    await loadBlacklist();
}

// ===== 获取最近收藏 =====
let favPollTimer = null;
let favCache = null;   // 最近一次抓取结果缓存，切走页面再回来也能保留
const favState = { source: 'collect', count: 20 };   // 来源 / 条数（横栏分段按钮切换）
let favActiveCat = 'new';   // 结果里当前选中的分类：new / downloaded / blacklist

// 分类顺序：未下载 → 下载过 → 黑名单（横排分段标签，点击切换）
const FAV_CATS = [
    ['new', '🆕 未下载过', 'st-todo'],
    ['downloaded', '✅ 下载过', 'st-normal'],
    ['blacklist', '🚫 黑名单', 'st-banned'],
];

const FAV_SOURCES = [['collect', '收藏'], ['like', '喜欢']];
const FAV_COUNTS = [10, 20, 50, 100, 200];

function renderFavOpts() {
    const sEl = document.getElementById('favSourceSeg');
    if (sEl) {
        sEl.innerHTML = FAV_SOURCES.map(([v, label]) =>
            `<button class="btn seg-btn ${favState.source === v ? 'active' : ''}" data-v="${v}" onclick="setFavSource('${v}')">${label}</button>`
        ).join('');
    }
    const cEl = document.getElementById('favCountSeg');
    if (cEl) {
        cEl.innerHTML = FAV_COUNTS.map((v) =>
            `<button class="btn seg-btn ${favState.count === v ? 'active' : ''}" data-v="${v}" onclick="setFavCount(${v})">${v} 条</button>`
        ).join('');
    }
}

function setFavSource(v) {
    favState.source = v;
    renderFavOpts();
}

function setFavCount(v) {
    favState.count = v;
    renderFavOpts();
}

function openFavoritesView() {
    forceCloseDetail();
    rememberViewScroll();
    state.view = 'favorites';
    state.returnView = 'favorites';
    hideAllViews();
    document.getElementById('favoritesView').classList.remove('hidden');
    animateViewEnter('favoritesView', 'neutral');
    clearSideActive();
    document.querySelectorAll('#featureList .side-item').forEach(i => i.classList.toggle('active', i.dataset.action === 'fetchFavorites'));
    const eb = document.getElementById('exportBtn');
    if (eb) eb.classList.add('hidden');
    restoreViewScroll('favorites');
    renderFavOpts();
    // 不再清空结果：切换页面保留已获取的作者，只有点「清空」才重置
    renderFavoritesState();
}

// 有已抓取结果时，按缓存结果重绘；否则显示占位提示
function renderFavoritesState() {
    const box = document.getElementById('favResult');
    if (!box) return;
    if (favCache && favCache.ok) {
        renderFavoritesResult(favCache);
    } else {
        box.innerHTML = '<div class="tag-hint">点「开始获取」后，这里会列出抓到的作者。</div>';
    }
}

function clearFavoritesResult() {
    favCache = null;
    if (favPollTimer) { clearInterval(favPollTimer); favPollTimer = null; }
    const box = document.getElementById('favResult');
    if (box) box.innerHTML = '<div class="tag-hint">已清空。点「开始获取」后，这里会列出抓到的作者。</div>';
    showToast('已清空获取结果');
}

async function runFetchFavorites() {
    const source = favState.source;
    const count = favState.count;
    favActiveCat = 'new';   // 每次抓取完默认先看「未下载过」
    const btn = document.getElementById('favRunBtn');
    const box = document.getElementById('favResult');
    btn.disabled = true; btn.textContent = '获取中…';
    box.innerHTML = '<div class="loading">正在抓取最近收藏（可能需要几秒到一两分钟）…</div>';
    try {
        const r = await fetch('/api/fetch_favorites', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ count, source }),
        });
        const d = await r.json();
        if (!d.ok) {
            box.innerHTML = '<div class="loading">' + escapeHtml(d.error || '启动失败') + '</div>';
            btn.disabled = false; btn.textContent = '开始获取';
            return;
        }
        // 轮询进度
        if (favPollTimer) clearInterval(favPollTimer);
        favPollTimer = setInterval(async () => {
            const s = await fetch('/api/favorites_status').then((x) => x.json());
            if (s.running) {
                box.innerHTML = `<div class="loading">正在抓取…（已拿到 ${s.done || 0} 条）</div>`;
                return;
            }
            clearInterval(favPollTimer); favPollTimer = null;
            btn.disabled = false; btn.textContent = '开始获取';
            if (s.error) {
                box.innerHTML = '<div class="loading">' + escapeHtml(s.error) + '</div>';
                return;
            }
            renderFavoritesResult(s.result);
        }, 1000);
    } catch (e) {
        box.innerHTML = '<div class="loading">抓取失败：' + escapeHtml(e.message) + '</div>';
        btn.disabled = false; btn.textContent = '开始获取';
    }
}

function renderFavoritesResult(result) {
    const box = document.getElementById('favResult');
    if (!result || !result.ok) { box.innerHTML = '<div class="loading">没有结果</div>'; favCache = null; return; }
    favCache = result;   // 缓存结果，切走页面再回来也能保留
    const authors = result.authors || [];
    // 横排分段标签：未下载过 / 已下载 / 黑名单（点击切换）
    const cats = FAV_CATS.map(([key, label, cls]) => {
        const n = authors.filter((a) => a.category === key).length;
        return `<button class="btn fav-cat-btn ${cls} ${favActiveCat === key ? 'active' : ''}" data-cat="${key}" onclick="setFavCat('${key}')">${label} <b>${n}</b></button>`;
    }).join('');
    const cur = authors.filter((a) => a.category === favActiveCat);
    const listHtml = cur.length
        ? `<div class="fav-list">${cur.map((a) => {
            const localUid = a.local_uid || '';   // 本地账号 uid（已下载的作者才有）
            const statusBadge = statusTagHtml(a.status);   // 账号状态徽章（红/绿/灰等）
            const levelBadge = levelTagHtml(a.level);       // 作者等级徽章
            const freqBadge = a.update_freq ? `<span class="mini-tag freq-tag">${escapeHtml(a.update_freq)}</span>` : '';  // 更新频率
            const tagChips = sortTags(a.tags).map(t => `<span class="mini-tag">${escapeHtml(t)}</span>`).join('');
            // 已下载的作者：名字可点，跳进本地作者详情页；未下载/黑名单没有本地账号，名字不可点
            const nameHtml = localUid
                ? `<a class="fav-name-link" title="打开本地作者详情页" onclick="openFavAuthor('${escapeHtml(localUid)}')">${escapeHtml(a.nickname || a.uid || '未命名')}</a>`
                : `<span>${escapeHtml(a.nickname || a.uid || '未命名')}</span>`;
            return `
            <div class="fav-row" data-sec="${escapeHtml(a.sec_uid || '')}" data-uid="${escapeHtml(a.uid || '')}" data-nick="${escapeHtml(a.nickname || '')}">
                <div class="fav-row-info">
                    <div class="fav-row-name">${nameHtml}${statusBadge}${levelBadge}${freqBadge}${tagChips}</div>
                    <div class="fav-row-meta">${a.douyin_id ? '抖音号 ' + escapeHtml(a.douyin_id) + ' · ' : ''}收藏 ${a.count || 0} 条 · ${favLatestHtml(a)}${favCollectedMark(a)}</div>
                </div>
                <div class="fav-row-ops">
                    ${a.url ? `<a class="btn btn-mini fav-ext-link" href="${escapeHtml(a.url)}" target="_blank" rel="noopener" title="在抖音打开这个作者的主页">抖音主页 ↗</a>` : ''}
                    ${a.category !== 'blacklist' ? `<button class="btn btn-mini" onclick="addFavoriteToBlacklist(this)" title="加入黑名单">加入黑名单</button>` : ''}
                </div>
            </div>`;
        }).join('')}</div>`
        : `<div class="tag-hint">这个分类下没有作者</div>`;
    box.innerHTML = `<div class="fav-summary">共抓取 ${result.total || 0} 条收藏 · 涉及 ${authors.length} 个作者</div>
        <div class="fav-cats">${cats}</div>
        ${listHtml}`;
}

// 「最新发布」+「本地已存」两个时间 + 相差天数
function favLatestHtml(a) {
    const pub = a.last_work || '';         // 抖音最新作品发布时间（YYYY-MM-DD HH:MM）
    const local = a.local_latest || '';    // 本地保存的最新作品日期（YYYY-MM-DD）
    // 口径：查过主页拿到真实最新发布（latest_post_time 存在）→「最新发布」；否则是收藏作品时间 →「已收藏最新发布」
    const label = a.latest_post_time ? '最新发布' : '已收藏最新发布';
    let html = '';
    if (pub) html += `<span title="${a.latest_post_time ? '抖音上该作者真实最新作品的发布时间' : '已收藏那条作品的发布时间（未查主页）'}">${label} ${escapeHtml(pub)}</span>`;
    if (local) {
        // 相差天数：发布日 vs 本地最新日
        const gap = favDaysBetween(pub.slice(0, 10), local);
        let gapTxt = '';
        if (gap !== null) {
            if (gap > 0) gapTxt = `<span class="fav-gap late" title="本地比抖音最新作品落后 ${gap} 天">缺 ${gap} 天</span>`;
            else if (gap === 0) gapTxt = `<span class="fav-gap ok" title="本地已存到最新">已同步</span>`;
            else gapTxt = `<span class="fav-gap ahead" title="本地作品比抖音最新还新 ${-gap} 天（抖音可能删了或设私密）">本地超前 ${-gap} 天</span>`;
        }
        html += ` · <span title="我电脑里保存的该作者最新作品的日期">本地已存 ${escapeHtml(local)}</span>${gapTxt ? ' ' + gapTxt : ''}`;
    } else {
        html += ' · <span class="fav-gap none" title="本地还没有保存这个作者的作品">本地未存</span>';
    }
    return html || '<span class="tag-hint">无</span>';
}

// 两个 YYYY-MM-DD 相差天数（pub - local）；任一无效返回 null
function favDaysBetween(pub, local) {
    if (!pub || !local) return null;
    const p = new Date(pub + 'T00:00:00');
    const l = new Date(local + 'T00:00:00');
    if (isNaN(p.getTime()) || isNaN(l.getTime())) return null;
    return Math.round((p - l) / 86400000);
}

// 「收藏的这条作品是否已下载」标记
function favCollectedMark(a) {
    // 只对「已下载过」分类显示这条标记（未下载/黑名单本来就没下载）
    if (a.category !== 'downloaded') return '';
    if (a.collect_downloaded) {
        return ' · <span class="fav-gap ok" title="最新收藏的这条作品本地已经下载了">✓ 该收藏作品已下载</span>';
    }
    return ' · <span class="fav-gap late" title="最新收藏的这条作品本地还没下载">该收藏作品未下载</span>';
}

// 切换结果分类标签（未下载/已下载/黑名单）
function setFavCat(cat) {
    favActiveCat = cat;
    if (favCache && favCache.ok) renderFavoritesResult(favCache);
}

// 从「获取最近收藏」结果点作者名 → 进入本地作者详情页（返回时回到收藏结果页，进度不丢）
function openFavAuthor(localUid) {
    if (!localUid) { showToast('这个作者还没有本地账号'); return; }
    // 记住返回目标 + 滚动位置，再隐藏收藏视图，让 openAuthor 干净地切到详情页
    state.returnView = 'favorites';
    rememberViewScroll();
    state.view = 'favorites';
    const favView = document.getElementById('favoritesView');
    if (favView) favView.classList.add('hidden');
    openAuthor(localUid, undefined, 'forward');
}

async function addFavoriteToBlacklist(btn) {
    const row = btn.closest('.fav-row');
    if (!row) return;
    const sec = row.dataset.sec || '';
    const uid = row.dataset.uid || '';
    const nick = row.dataset.nick || '';
    if (!sec && !uid) { showToast('这个作者没有可用的 sec_uid / uid'); return; }
    try {
        const r = await fetch('/api/blacklist', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'add', sec_uid: sec, uid: uid, nickname: nick, reason: '不需要下载' }),
        });
        const d = await r.json();
        if (!d.ok) { showToast('加入黑名单失败：' + (d.error || '未知错误')); return; }
        showToast(d.added ? '已加入黑名单' : '这个博主已经在黑名单里了');
        btn.remove();
        const tag = document.createElement('span');
        tag.className = 'mini-tag st-banned';
        tag.textContent = '已加黑名单';
        row.querySelector('.fav-row-ops').appendChild(tag);
    } catch (e) {
        showToast('加入黑名单失败：' + e.message);
    }
}

// ===== 导出作者信息（左：选作者 / 右：DouK 参数 + 导出格式） =====
const exportState = {
    level: [], tag: [], status: [], freq: [], watch: [], cover: [],
    latest: '', latestFrom: '', latestTo: '',
    // 每行左侧的圆钮（2026-11-05 用户口径）：
    //   or  = ＋ 包含：这一行勾中的值属于「我要的」
    //   not = － 排除：这一行勾中的值属于「我要去掉的」
    // 全是 ＋ 就是纯并集；全都不勾 = 全部作品。
    matchMap: { level: 'or', tag: 'or', status: 'or', freq: 'or', watch: 'or', cover: 'or', latest: 'or' },
    uids: null, previewed: false,
    stale: false,            // 预览之后又改了筛选 → 旧名单作废，但提示要留着
    // 2026-11-01：导出对象有两个来源 ——
    //   'filter' = ① 里点「预览选中的作者」筛出来的（默认）
    //   'queue'  = 待下载区点「预览」挂过来的（见 stageQueueForExport）
    // 待下载区里没有 uid 的作者靠 secUids 带出去，服务端两边都会收。
    source: 'filter', secUids: [],
};
let exportPreviewHtml = '';   // 上次预览的 DOM 快照，切走再回来直接塞回去（不用重算）
let exportPreviewScroll = 0; // 预览列表内部的滚动位置，切回来也还原
let exportPreviewItems = [];  // 预览的全量数据（点「显示更多」时不用再请求）
let exportPreviewLimit = 50;  // 当前显示了多少个，滚到底点按钮 +50
let exportDropped = [];       // 这次被 ✕ 移出的作者（只不导出，不动筛选条件，可恢复）
let exportPreviewOrder = [];  // 预览接口返回时的原始顺序，「恢复」时按它排回去
const EXPORT_STATUS_LIST = ['正常', '私密账号', '注销', '禁言/封号', '停更/偶更', '卖号', '不好看了', '待修改'];
// 已下载作品的最后时间：就是该作者本地已有作品里最新那条的发布时间
const EXPORT_LATEST_LIST = [
    ['', '不限'], ['3d', '3天内'], ['7d', '1周内'], ['14d', '2周内'],
    ['30d', '1个月内'], ['60d', '2个月内'], ['365d', '1年内'], ['custom', '自选'],
];

// ===== 待下载区：浏览器插件在抖音页面点「加入待下载」推过来的作者 =====
// 数据存在本地查询服务（8790）里，和本页面同机不同端口，靠 CORS 读取。
const QUEUE_BASE = 'http://127.0.0.1:8790';
const queueState = { items: [], canUndo: false, preview: false, err: '', clearedAt: '' };

async function queueFetch(path, method) {
    const r = await fetch(QUEUE_BASE + path, { method: method || 'GET', cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
}

async function refreshQueue() {
    const box = document.getElementById('queueBody');
    if (!box) return;
    document.getElementById('queueSub').textContent = '读取中…';
    box.innerHTML = '<div class="queue-empty">正在读取…</div>';
    try {
        const d = await queueFetch('/queue');
        queueState.items = d.items || [];
        queueState.canUndo = !!d.can_undo;
        queueState.clearedAt = d.cleared_at || '';
        queueState.err = '';
        queueState.undoCount = d.undo_count || 0;
    } catch (e) {
        queueState.err = String((e && e.message) || e);
        queueState.items = [];
    }
    renderQueue();
}

function renderQueue() {
    const box = document.getElementById('queueBody');
    if (!box) return;
    const sub = document.getElementById('queueSub');
    const undoBtn = document.getElementById('queueUndoBtn');
    const previewBtn = document.getElementById('queuePreviewBtn');

    if (queueState.err) {
        sub.textContent = '未连接';
        if (undoBtn) undoBtn.classList.add('hidden');
        box.innerHTML = '<div class="queue-empty warn">读不到待下载区：本地查询服务（8790）没启动。<br>'
            + '双击项目根目录的 <b>start-all.bat</b> 后再点这里 '
            + '<button class="btn btn-mini" onclick="refreshQueue()">重试</button></div>';
        return;
    }

    const n = queueState.items.length;
    sub.textContent = n ? (n + ' 位待下载' + (queueState.clearedAt ? ' · 上次清空 ' + queueState.clearedAt : ''))
                        : (queueState.canUndo ? '已清空 · 可撤销' : '空');
    if (undoBtn) undoBtn.classList.toggle('hidden', !queueState.canUndo);
    if (previewBtn) previewBtn.textContent = queueState.preview ? '收起预览' : '预览';

    if (!n) {
        box.innerHTML = '<div class="queue-empty">还没有推送的作者。<br>'
            + '在抖音页面看视频时，点插件卡片里的「＋ 待下载」就会出现在这里。</div>';
        return;
    }

    box.innerHTML = queueState.items.map((it) => {
        const uid = String(it.uid || '');
        const tags = (it.tags || []).map((t) => `<span class="queue-tag">${escapeHtml(t)}</span>`).join('');
        const st = it.created ? '<span class="queue-state ok">已新建主页</span>'
            : it.in_lib ? '<span class="queue-state">库里已有</span>'
            : '<span class="queue-state warn">' + escapeHtml(it.note || '未建档') + '</span>';
        const extra = queueState.preview ? `
            <div class="queue-item-sub">
                抖音号 ${escapeHtml(it.douyin_id || '—')} ·
                粉丝 ${it.follower ? (it.follower >= 10000 ? (it.follower / 10000).toFixed(1) + ' 万' : it.follower) : '—'} ·
                ${escapeHtml(it.sec_uid ? it.sec_uid.slice(0, 18) + '…' : '无 sec_uid')}
                ${it.note ? ' · ' + escapeHtml(it.note) : ''}
            </div>` : '';
        const sec = String(it.sec_uid || '');
        return `<div class="queue-item" ${uid ? `onclick="queueOpenAuthor('${escapeHtml(uid)}')"` : ''}
                     title="${uid ? '点开这位作者的详情页' : ''}">
            <div class="queue-item-main">
                <span class="queue-nick">${escapeHtml(it.nickname || it.uid || '未命名')}</span>
                ${tags}${st}
                <span class="queue-time">${escapeHtml((it.added_at || '').slice(5, 16))}</span>
                <button class="queue-del" title="从待下载区移除"
                        onclick="queueRemove(event, '${escapeHtml(uid)}', '${escapeHtml(sec)}')">×</button>
            </div>${extra}
        </div>`;
    }).join('');
}

// 点 × 时先问一句：只从待下载区移除，还是连作者主页一起删
let queueDelPending = null;

function queueRemove(ev, uid, sec) {
    if (ev) { ev.stopPropagation(); ev.preventDefault(); }
    const it = queueState.items.find((x) =>
        (uid && String(x.uid || '') === uid) || (sec && String(x.sec_uid || '') === sec));
    const works = it ? Number(it.works || 0) : 0;
    queueDelPending = {
        uid: uid || (it && it.uid) || '',
        sec: sec || (it && it.sec_uid) || '',
        name: it ? (it.nickname || it.uid || '这一位') : '这一位',
        works: works,
        created: !!(it && it.created),
        delHome: works === 0          // 没有作品的，默认勾上「连主页一起删」
    };
    showQueueDelDialog();
}

function queueDelCancel() {
    queueDelPending = null;
    const m = document.getElementById('qdlgMask');
    if (m) m.remove();
}

function showQueueDelDialog() {
    const p = queueDelPending;
    if (!p) return;
    const old = document.getElementById('qdlgMask');
    if (old) old.remove();
    const canDelHome = p.works === 0 && !!p.uid;
    const sub = p.works > 0
        ? `这位作者本地还有 <b>${p.works}</b> 个作品，主页不会被删除。`
        : (p.created
            ? '这位作者本地还没有作品，主页是推送时自动建的。'
            : '这位作者本地还没有作品。可以连主页一起删掉。');
    const opt = canDelHome ? `
        <label class="qdlg-opt">
            <input type="checkbox" id="qdlgDelHome" ${p.delHome ? 'checked' : ''}>
            <span>同时删除这位作者的<b>主页</b>（user_data 里的条目）</span>
        </label>` : '';
    const mask = document.createElement('div');
    mask.id = 'qdlgMask';
    mask.className = 'qdlg-mask';
    mask.innerHTML = `
        <div class="qdlg" role="dialog">
            <div class="qdlg-title">从待下载区移除「${escapeHtml(p.name)}」？</div>
            <div class="qdlg-body">${sub}</div>
            ${opt}
            <div class="qdlg-acts">
                <button class="btn" onclick="queueDelCancel()">取消</button>
                <button class="btn btn-primary" onclick="queueDelConfirm()">确定移除</button>
            </div>
        </div>`;
    mask.addEventListener('click', (e) => { if (e.target === mask) queueDelCancel(); });
    document.body.appendChild(mask);
}

async function queueDelConfirm() {
    const p = queueDelPending;
    if (!p) return;
    const delHome = !!(document.getElementById('qdlgDelHome') || {}).checked;
    const btn = document.querySelector('#qdlgMask .btn-primary');
    if (btn) { btn.disabled = true; btn.textContent = '处理中…'; }

    let homeMsg = '';
    if (delHome && p.uid) {
        try {
            const r = await fetch('/api/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ accounts: accountDeletePayload(p.uid) })
            });
            const d = await r.json().catch(() => ({}));
            if (r.ok && d.ok) {
                homeMsg = '，主页已删除';
                // 本地状态也要跟着删，否则下次算「同组其他人」时会用到过期数据
                try { if (state.userData && state.userData.accounts) delete state.userData.accounts[p.uid]; } catch (e) { /* ignore */ }
                if (typeof loadAuthors === 'function') { try { loadAuthors(true); } catch (e) { /* ignore */ } }
            } else {
                homeMsg = '，但主页删除失败：' + (d.error || ('HTTP ' + r.status));
            }
        } catch (e) {
            homeMsg = '，但主页删除失败：' + (e && e.message || e);
        }
    }

    try {
        const r = await fetch(QUEUE_BASE + '/queue/remove', {
            method: 'POST', cache: 'no-store',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ uid: p.uid || '', sec_uid: p.sec || '' })
        });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const d = await r.json();
        queueState.items = d.items || [];
        queueState.canUndo = !!d.can_undo;
        queueState.clearedAt = d.cleared_at || '';
        queueDelCancel();
        renderQueue();
        showToast(d.removed
            ? ('已移除：' + p.name + homeMsg + '（待下载区还剩 ' + queueState.items.length + ' 位）')
            : '这条已经不在待下载区了');
    } catch (e) {
        queueDelCancel();
        showToast('移除失败：' + (e && e.message || e));
    }
}

function queueTogglePreview() {
    queueState.preview = !queueState.preview;
    renderQueue();
    // 2026-11-01（用户要求）：待下载区「没点过预览就不算」——
    // 点开预览才把这一区挂成 ③ 的导出对象；不点，③ 只用 ① 筛出来的人。
    if (queueState.preview) stageQueueForExport();
}

// 把待下载区整块挂成「③ 导出」的对象。
// 这一区不少人还没有 uid（只有 8790 队列里的 sec_uid），所以 uids / secUids 两样都收着，
// 一起交给 /api/export_authors —— 服务端会按 sec_uid 反查，查不到也照样能写链接。
function stageQueueForExport() {
    const items = queueState.items || [];
    const uids = [];
    const secs = [];
    items.forEach((it) => {
        const u = String(it.uid || '').trim();
        const s = String(it.sec_uid || '').trim();
        if (u && uids.indexOf(u) < 0) uids.push(u);
        if (s && secs.indexOf(s) < 0) secs.push(s);
    });
    if (!uids.length && !secs.length) {
        showToast('待下载区里没有可导出的作者');
        return;
    }
    exportState.source = 'queue';
    exportState.uids = uids;
    exportState.secUids = secs;
    exportState.previewed = true;
    exportState.stale = false;
    exportPreviewHtml = '';
    exportPreviewItems = [];
    const n = Math.max(uids.length, secs.length);
    const sub = document.getElementById('exportCountSub');
    if (sub) sub.textContent = `已选 ${n} 个（待下载区）`;
    const box = document.getElementById('exportPreview');
    if (box) {
        box.classList.remove('hidden');
        box.innerHTML = `<div class="export-preview-note">本次导出对象：<b>待下载区 ${n} 位</b>`
            + `（就是上面那个列表，可以继续用行尾 × 移出）。<br>`
            + `想改回按条件筛人，去 ① 点「预览选中的作者」。</div>`;
    }
    showToast(`已把待下载区 ${n} 位设为导出对象`);
}

async function queueClear() {
    if (!queueState.items.length) { showToast('待下载区已经是空的'); return; }
    try {
        const d = await queueFetch('/queue/clear', 'POST');
        queueState.items = d.items || [];
        queueState.canUndo = !!d.can_undo;
        queueState.clearedAt = d.cleared_at || '';
        renderQueue();
        showToast('已清空 ' + (d.cleared || 0) + ' 位，可点「取消清空」恢复');
    } catch (e) {
        showToast('清空失败：' + e.message);
    }
}

async function queueUndo() {
    try {
        const d = await queueFetch('/queue/undo', 'POST');
        queueState.items = d.items || [];
        queueState.canUndo = !!d.can_undo;
        queueState.clearedAt = d.cleared_at || '';
        renderQueue();
        showToast('已恢复 ' + queueState.items.length + ' 位');
    } catch (e) {
        showToast('恢复失败：' + e.message);
    }
}

async function queueRefreshClick() {
    const btn = document.getElementById('queueRefreshBtn');
    if (btn) { btn.disabled = true; btn.textContent = '刷新中…'; }
    await refreshQueue();
    if (btn) { btn.disabled = false; btn.textContent = '刷新'; }
    if (queueState.err) showToast('读取失败：' + queueState.err);
    else showToast('已刷新：' + queueState.items.length + ' 位待下载');
}

function queueOpenAuthor(uid) {
    if (typeof openAuthor === 'function' && uid) openAuthor(uid);
    else showToast('请先刷新作者列表');
}

async function openExportView() {
    // 注意：这里**不重置** exportState —— 切到别的界面再回来，筛选和预览结果要原样保留，
    // 只有点「清空」按钮（clearExportAll）才真的清掉。
    forceCloseDetail();
    rememberViewScroll();
    state.view = 'export';
    state.returnView = 'export';
    hideAllViews();
    document.getElementById('exportView').classList.remove('hidden');
    animateViewEnter('exportView', 'neutral');
    clearSideActive();
    document.querySelectorAll('#featureList .side-item').forEach(i => i.classList.toggle('active', i.dataset.action === 'exportAuthors'));
    const eb = document.getElementById('exportBtn');
    if (eb) eb.classList.add('hidden');
    restoreViewScroll('export');
    refreshQueue();
    // 标签列表来自 meta
    const tags = Object.keys((state.meta && state.meta.tags) || {}).sort();
    // 「未设置 / 没有标签」这类兜底项统一排在该行最前（用户 2026-11-05 要求）
    const rows = [
        ['exportLevelRadios', 'expLevel', 'level',
            [['__unset', '未设置'], ['夯', '夯'], ['顶级', '顶级'], ['一般', '一般'], ['拉', '拉']], exportState.level],
        ['exportTagRadios', 'expTag', 'tag',
            [['__notag', '没有标签']].concat(tags.map((t) => [t, t])), exportState.tag],
        ['exportStatusRadios', 'expStatus', 'status',
            [['__unset', '未设置']].concat(EXPORT_STATUS_LIST.map((s) => [s, s])), exportState.status],
        ['exportFreqRadios', 'expFreq', 'freq',
            [['__unset', '未设置'], ['day', 'day'], ['3天', '3天'], ['周', '周'], ['月', '月']], exportState.freq],
        // 追更：布尔，勾「追更中」= 只要名单里的；勾「不追更」= 只要名单外的；两个都勾 = 全部
        ['exportWatchRadios', 'expWatch', 'watch',
            [['yes', '追更中'], ['no', '不追更']], exportState.watch],
        ['exportCoverRadios', 'expCover', 'cover',
            [['has', '有封面'], ['none', '无封面']], exportState.cover],
    ];
    rows.forEach(([boxId, name, _key, items, sel]) => {
        const box = document.getElementById(boxId);
        if (!box) return;
        box.innerHTML = items.map(([v, t]) => expCheckHtml(name, v, t, sel.includes(v))).join('');
    });
    // 已下载作品最后时间：单选取一个区间（单选语义，跟「更新频率」的多选不一样）
    document.getElementById('exportLatestRadios').innerHTML =
        EXPORT_LATEST_LIST.map(([v, label]) =>
            `<label><input type="radio" name="expLatest" value="${v}" ${v === (exportState.latest || '') ? 'checked' : ''} onchange="onExportLatestChange(this.value)"> ${label}</label>`).join('');
    // 交集 / 并集：每个属性行右侧的 ＋ / － 按 exportState.matchMap 回填
    renderExportMatch();
    document.getElementById('exportLatestRange').classList.add('hidden');
    // 任何筛选控件变动 → 让上一次预览的名单作废，防止「改了条件却导出旧名单」
    const evBox = document.getElementById('exportView');
    if (evBox && !evBox.dataset.dirtyBound) {
        evBox.dataset.dirtyBound = '1';
        evBox.addEventListener('change', (e) => {
            const t = e.target;
            if (!t) return;
            const n = t.name || '';
            if (/^exp(Level|Tag|Status|Freq|Watch|Cover|Latest)$/.test(n) || t.id === 'exportLatestFrom' || t.id === 'exportLatestTo') {
                markExportDirty();
                refreshExportSentence();
            }
        });
    }
    onExportFormatChange();
    restoreExportUI();
    refreshExportSentence();
    loadDoukConfig();
}

// 记住预览列表滚到哪儿了（列表是内部滚动，重建 DOM 会回到顶部）
function bindPreviewListScroll(box) {
    const list = box && box.querySelector('.export-preview-list');
    if (!list) return;
    list.addEventListener('scroll', () => { exportPreviewScroll = list.scrollTop; });
    if (exportPreviewScroll) list.scrollTop = exportPreviewScroll;
}

// 把 exportState 里保存的进度还原到界面上（预览列表 / 已选数量 / 自选日期框）
function restoreExportUI() {
    const box = document.getElementById('exportPreview');
    const sub = document.getElementById('exportCountSub');
    const n = (exportState.uids ? exportState.uids.length : 0)
        || (exportState.secUids ? exportState.secUids.length : 0);
    if (sub) {
        sub.textContent = (exportState.previewed && n)
            ? `已选 ${n} 个${exportState.source === 'queue' ? '（待下载区）' : ''}` : '';
    }
    const range = document.getElementById('exportLatestRange');
    if (range) range.classList.toggle('hidden', exportState.latest !== 'custom');
    if (!box) return;
    if (exportState.previewed && exportState.source === 'queue') {
        // 名单来自待下载区：那份列表就在上方面板里，这里只还原一句说明
        box.classList.remove('hidden');
        box.innerHTML = `<div class="export-preview-note">本次导出对象：<b>待下载区 ${n} 位</b>`
            + `（就是上面那个列表，可以继续用行尾 × 移出）。<br>`
            + `想改回按条件筛人，去 ① 点「预览选中的作者」。</div>`;
    } else if (exportState.previewed && exportPreviewHtml) {
        box.innerHTML = exportPreviewHtml;
        box.classList.remove('hidden');
        bindPreviewListScroll(box);
        const list = box.querySelector('.export-preview-list');
        if (list && exportPreviewScroll) list.scrollTop = exportPreviewScroll;
    } else if (exportState.stale) {
        box.innerHTML = '<div class="export-preview-stale">筛选条件改过了，请重新点「预览选中的作者」</div>';
        box.classList.remove('hidden');
    } else {
        box.innerHTML = '';
        box.classList.add('hidden');
    }
}

// 「清空」：把选作者的条件、预览列表、已选名单一次性清掉（只有点它才清）
function clearExportAll() {
    exportState.level = []; exportState.tag = []; exportState.status = []; exportState.freq = [];
    exportState.watch = []; exportState.cover = [];
    exportState.latest = ''; exportState.latestFrom = ''; exportState.latestTo = '';
    // 每行的 ＋/－ 回到默认：全 ＋（包含）
    exportState.matchMap = { level: 'or', tag: 'or', status: 'or', freq: 'or', watch: 'or', cover: 'or', latest: 'or' };
    exportState.uids = null; exportState.previewed = false; exportState.stale = false;
    exportState.source = 'filter'; exportState.secUids = [];
    exportPreviewHtml = '';
    exportPreviewItems = [];
    exportPreviewLimit = 50;
    exportPreviewScroll = 0;
    // 控件回到初始态
    document.querySelectorAll('#exportView input[type="checkbox"][name^="exp"]').forEach((i) => { i.checked = false; });
    renderExportMatch();
    const noneLatest = document.querySelector('input[name="expLatest"][value=""]');
    if (noneLatest) noneLatest.checked = true;
    const f = document.getElementById('exportLatestFrom');
    const t = document.getElementById('exportLatestTo');
    if (f) f.value = '';
    if (t) t.value = '';
    const range = document.getElementById('exportLatestRange');
    if (range) range.classList.add('hidden');
    const box = document.getElementById('exportPreview');
    if (box) { box.innerHTML = ''; box.classList.add('hidden'); }
    const sub = document.getElementById('exportCountSub');
    if (sub) sub.textContent = '';
    refreshExportSentence();
    showToast('已清空筛选条件和预览');
}

// 「① 选作者」右上角的「重置」：和底部「清空」同一套动作（筛选回默认 + 上次预览作废），
// 只是放在最上面更顺手（用户嫌清空在底部、要滚下去）。
function resetExportFilters() {
    clearExportAll();
}

// 每行左边一个圆钮（2026-11-05 用户口径）：
//   ＋ = 包含：勾中的值属于「我要的」（默认）
//   － = 排除：勾中的值属于「我要去掉的」
// 点一下切换。行内勾多个值始终是「命中任一」；不同行之间，＋ 行取并集，－ 行全部剔除。
function renderExportMatch() {
    if (!exportState.matchMap) exportState.matchMap = {};
    document.querySelectorAll('#exportView .export-match').forEach((box) => {
        const key = box.dataset.key;
        const mode = exportState.matchMap[key] === 'not' ? 'not' : 'or';
        const isNot = mode === 'not';
        const tip = isNot
            ? '－ 排除：勾中的这些要去掉（点一下改回 ＋ 包含）'
            : '＋ 包含：勾中的这些是我要的（点一下改成 － 排除）';
        box.innerHTML = `<button type="button" class="em-btn ${mode}" data-mode="${mode}" title="${tip}"`
            + ` aria-label="${isNot ? '排除' : '包含'}" onclick="toggleExportMatch('${key}')">`
            + (isNot ? '－' : '＋') + '</button>';
    });
}

function toggleExportMatch(key) {
    if (!exportState.matchMap) exportState.matchMap = {};
    exportState.matchMap[key] = (exportState.matchMap[key] === 'not') ? 'or' : 'not';
    renderExportMatch();
    markExportDirty();
    refreshExportSentence();
}

// ===== 「① 选作者」的一行：勾选框 + 全选 + 一句人话描述（用户 2026-11-05 要求） =====
const EXP_CHECK_STATE = {
    expLevel: 'level', expTag: 'tag', expStatus: 'status', expFreq: 'freq',
    expWatch: 'watch', expCover: 'cover',
};

function expCheckHtml(name, value, label, checked) {
    return `<label><input type="checkbox" name="${name}" value="${escapeHtml(value)}"`
        + `${checked ? ' checked' : ''} onchange="onExportCheckChange('${name}')"> ${escapeHtml(label)}</label>`;
}

function onExportCheckChange(name) {
    const key = EXP_CHECK_STATE[name];
    if (!key) return;
    exportState[key] = collectExport(name);
    markExportDirty();
    refreshExportSentence();
}

// 「作者等级」旁边那颗「全选」：全勾上再点一次就是全不选（方便「只排除某几个」的场景）
function exportRowAll(name) {
    const key = EXP_CHECK_STATE[name];
    if (!key) return;
    const inputs = document.querySelectorAll(`input[name="${name}"]`);
    const allOn = [...inputs].length > 0 && [...inputs].every((i) => i.checked);
    inputs.forEach((i) => { i.checked = !allOn; });
    exportState[key] = collectExport(name);
    markExportDirty();
    refreshExportSentence();
}

const EXP_ROW_LABEL = {
    level: '等级', tag: '标签', status: '账号状态', freq: '更新频率', watch: '追更',
    cover: '封面', latest: '最后下载时间',
};

function expValText(key, v) {
    if (v === '__unset') return '未设置';
    if (v === '__notag') return '没有标签';
    if (v === 'yes' && key === 'watch') return '追更中';
    if (v === 'no' && key === 'watch') return '不追更';
    if (key === 'cover') return v === 'has' ? '有封面' : '无封面';
    return v;
}

// 一行的描述片段：+ 行 → 「等级：夯、顶级」；- 行 → 「纹身标签」
function expRowText(key) {
    if (key === 'latest') {
        if (!exportState.latest) return '';
        if (exportState.latest === 'custom') {
            const f = (document.getElementById('exportLatestFrom') || {}).value || '';
            const t = (document.getElementById('exportLatestTo') || {}).value || '';
            if (!f && !t) return '';
            return '最后下载：' + (f || '不限') + ' 至 ' + (t || '不限');
        }
        const hit = EXPORT_LATEST_LIST.find(([v]) => v === exportState.latest);
        return '最后下载：' + (hit ? hit[1] : exportState.latest);
    }
    const arr = exportState[key] || [];
    if (!arr.length) return '';
    const vals = arr.map((v) => expValText(key, v)).join('、');
    if (key === 'level') return '等级：' + vals;
    if (key === 'status') return '状态：' + vals;
    if (key === 'tag') return vals + '标签';
    if (key === 'freq') return '频率：' + vals;
    if (key === 'watch') return '追更：' + vals;
    return vals;
}

// 拼成一句给用户看的话：「全部作品，去掉 纹身标签」/「等级是 夯，去掉 注销」
function exportSentence() {
    const inc = [], exc = [];
    ['level', 'tag', 'status', 'freq', 'watch', 'cover', 'latest'].forEach((key) => {
        const t = expRowText(key);
        if (!t) return;
        const isNot = (exportState.matchMap || {})[key] === 'not';
        (isNot ? exc : inc).push(t);
    });
    let s = inc.length ? inc.join(' 或 ') : '全部作品';
    if (exc.length) s += '，去掉 ' + exc.join('、');
    return s;
}

function refreshExportSentence() {
    const el = document.getElementById('exportSentence');
    if (!el) return;
    const s = exportSentence();
    el.innerHTML = `<b>筛选结果：</b>${escapeHtml(s)}`;
    el.classList.toggle('empty', s === '全部作品');
}

// 收集某一组勾选框当前选中的值（导出界面的多选用）
function collectExport(name) {
    const arr = [];
    document.querySelectorAll(`input[name="${name}"]:checked`).forEach((i) => arr.push(i.value));
    return arr;
}

// N 天前的 YYYY-MM-DD
function exportIsoDaysAgo(days) {
    const d = new Date(Date.now() - days * 86400000);
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}

function onExportLatestChange(val) {
    exportState.latest = val;
    document.getElementById('exportLatestRange').classList.toggle('hidden', val !== 'custom');
    markExportDirty();
    refreshExportSentence();
}

// 「最新日期往前提 N 天」：写进 JSON 的 earliest = 每位作者自己的已下载最新作品日期 − N 天。
// 留空 = 不设置（全部抓取）。
function clearEarliestDays() {
    const box = document.getElementById('exportEarliestDays');
    if (box) { box.value = ''; box.focus(); }
}

function earliestDaysValue() {
    const raw = ((document.getElementById('exportEarliestDays') || {}).value || '').trim();
    if (!raw) return null;
    const n = parseInt(raw, 10);
    return isNaN(n) || n < 1 ? null : n;
}

// 把左侧筛选条件拼成 /api/authors 的查询串
function exportFilterParams() {
    const params = new URLSearchParams({ offset: '0', limit: '100000' });
    if (exportState.level.length) params.set('level', exportState.level.join(','));
    if (exportState.tag.length) params.set('tag', exportState.tag.join(','));
    if (exportState.status.length) params.set('status', exportState.status.join(','));
    if (exportState.freq.length) params.set('freq', exportState.freq.join(','));
    // 追更：后端只认单值 yes/no；两个都勾（= 全部）或都没勾（= 不限）时不传
    if (exportState.watch.length === 1) params.set('watch', exportState.watch[0]);
    if (exportState.cover && exportState.cover.length) params.set('cover', exportState.cover.join(','));
    // 每行自己的开关：match_x=not 表示这一行是「排除」；不传 = 包含（默认）
    Object.keys(exportState.matchMap || {}).forEach((k) => {
        if (exportState.matchMap[k] === 'not') params.set('match_' + k, 'not');
    });
    if (exportState.latest === 'custom') {
        const f = document.getElementById('exportLatestFrom').value;
        const t = document.getElementById('exportLatestTo').value;
        if (f) params.set('latest_after', f);
        if (t) params.set('latest_before', t);
    } else if (exportState.latest) {
        const days = parseInt(exportState.latest, 10);
        if (!isNaN(days)) params.set('latest_after', exportIsoDaysAgo(days));
    }
    return params;
}

// 筛选条件一改，之前那次预览的名单就作废了。
// 不这样做的话：先「预览」拿到 1000 个 uid，再改筛选（比如勾 day），
// 直接点「导出」还是会导出上一批 —— 看着像"筛 day 却导出了注销的号"。
function markExportDirty() {
    // 导出对象是待下载区挂过来的，改 ① 的筛选本来就不该把它作废
    if (exportState.source === 'queue') return;
    if (!exportState.previewed && !exportState.uids) return;   // 本来就没预览过，不用打扰
    exportState.previewed = false;
    exportState.uids = null;
    exportState.stale = true;        // 切走再回来仍要把「请重新预览」这条提示还原出来
    exportPreviewHtml = '';
    const sub = document.getElementById('exportCountSub');
    if (sub) sub.textContent = '';
    const box = document.getElementById('exportPreview');
    if (box && !box.classList.contains('hidden')) {
        box.innerHTML = '<div class="export-preview-stale">筛选条件改过了，请重新点「预览选中的作者」</div>';
    }
}

async function previewExport() {
    const params = exportFilterParams();
    try {
        const d = await fetch('/api/authors?' + params.toString()).then((r) => r.json());
        const items = d.items || [];
        exportState.uids = items.map((a) => a.uid);
        exportState.source = 'filter';   // ① 的预览 = 把来源切回筛选结果
        exportState.secUids = [];
        exportState.previewed = true;
        exportState.stale = false;
        exportPreviewItems = items;      // 全量存着，后面「显示更多」直接切片
        exportPreviewOrder = items.map((a) => a.uid);   // 记住原顺序，「恢复」时排回去
        exportDropped = [];
        exportPreviewLimit = 50;         // 默认只显示前 50 个
        exportPreviewScroll = 0;
        renderPreviewList();
    } catch (e) {
        showToast('预览失败：' + e.message);
    }
}

// 单行 HTML（四列：标签 / 等级 / 状态 / 最新，用 subgrid 保证上下对齐）
function previewRowHtml(a) {
    const lv = normLevel(a.level) || '未设级';
    const tg = sortTags(a.tags).slice(0, 3).map((t) => escapeHtml(t)).join('/') || '无标签';
    const st = a.status || '正常';
    const lt = a.latest ? String(a.latest).slice(0, 10) : '';
    return `
    <div class="export-preview-row" data-uid="${escapeHtml(a.uid)}" onclick="openAuthor('${escapeHtml(a.uid)}')"
         title="${escapeHtml(lv + ' · ' + tg + ' · ' + st + (lt ? ' · ' + lt : ''))}｜点开查看主页（返回后筛选和预览都还在）">
        <span class="ep-name"><span class="ep-text">${a.starred ? '⭐' : ''}${escapeHtml(a.alias || a.nickname || a.uid)}</span><span class="ep-open">查看 ›</span></span>
        <span class="ep-meta">
            <span class="ep-f ep-f-tag" title="标签：${escapeHtml(tg)}">${tg}</span>
            <span class="ep-f ep-f-level lv-${levelKey(lv) || 'other'}" title="等级：${escapeHtml(lv)}">${escapeHtml(lv)}</span>
            <span class="ep-f ep-f-status st-${statusKey(st) || 'other'}" title="状态：${escapeHtml(st)}">${escapeHtml(st)}</span>
            <span class="ep-f ep-f-latest">${escapeHtml(lt)}</span>
        </span>
        <button type="button" class="ep-x" title="这次先不导出这个作者（点一下移出，随时能恢复）"
                onclick="event.stopPropagation(); dropPreviewAuthor('${escapeHtml(a.uid)}')">✕</button>
    </div>`;
}

// 按当前的「显示数量」渲染预览列表（末尾带「显示更多」）
function renderPreviewList() {
    const box = document.getElementById('exportPreview');
    if (!box) return;
    const items = exportPreviewItems;
    const limit = Math.min(Math.max(exportPreviewLimit, 50), items.length);
    const rest = items.length - limit;
    const sub = document.getElementById('exportCountSub');
    if (sub) sub.textContent = `已选 ${items.length} 个`;
    box.classList.remove('hidden');
    const dropped = exportDropped.length;
    box.innerHTML = `
        <div class="export-preview-list">
            <div class="export-preview-headrow">
                <span class="ep-h ep-h-name" title="共 ${items.length} 个作者，列表里显示了前 ${limit} 个${dropped ? `；本次不导出 ${dropped} 个` : ''}">共 ${items.length} 个作者 · 显示 ${limit}${dropped ? ` · 不导出 ${dropped}` : ''}</span>
                <span class="ep-h ep-h-tag">标签</span>
                <span class="ep-h ep-h-level">等级</span>
                <span class="ep-h ep-h-status">状态</span>
                <span class="ep-h ep-h-latest">本地保存最新</span>
            </div>
            ${items.slice(0, limit).map(previewRowHtml).join('')}
            ${rest > 0 || dropped ? `<div class="export-preview-more">
                ${rest > 0 ? `<button type="button" class="btn btn-mini" onclick="loadMorePreview()">显示更多（还有 ${rest} 个）</button>` : ''}
                ${dropped ? `<button type="button" class="btn btn-mini" onclick="restorePreviewDropped()">恢复移出的 ${dropped} 个</button>` : ''}
            </div>` : ''}
        </div>`;
    exportPreviewHtml = box.innerHTML;
    bindPreviewListScroll(box);
}

// 某一行的 ✕：这次不导出这个作者。只改前端的名单，不动筛选条件，
// 所以「恢复」能原样放回来；正式导出读的是 exportState.uids，移出的自然就不带了
function dropPreviewAuthor(uid) {
    uid = String(uid || '');
    const hit = exportPreviewItems.find((a) => String(a.uid) === uid);
    if (!hit) return;
    exportPreviewItems = exportPreviewItems.filter((a) => String(a.uid) !== uid);
    exportState.uids = (exportState.uids || []).filter((u) => String(u) !== uid);
    exportDropped.push(hit);
    renderPreviewList();
}

// 把移出的作者按原顺序放回列表（位置可能靠后，用 uid 重新排序）
function restorePreviewDropped() {
    if (!exportDropped.length) return;
    const back = exportDropped;
    exportDropped = [];
    const have = new Set(exportPreviewItems.map((a) => String(a.uid)));
    back.forEach((a) => { if (!have.has(String(a.uid))) exportPreviewItems.push(a); });
    // 按原来的顺序（预览接口返回的先后）排回去：用 uid 在上次全量名单里的位置
    const order = exportPreviewOrder || [];
    if (order.length) {
        const pos = {};
        order.forEach((u, i) => { pos[String(u)] = i; });
        exportPreviewItems.sort((x, y) => (pos[String(x.uid)] ?? 1e9) - (pos[String(y.uid)] ?? 1e9));
    }
    exportState.uids = exportPreviewItems.map((a) => a.uid);
    renderPreviewList();
}

// 底部「显示更多」：一次多放 50 个，滚到新内容开头，别让人以为没反应
function loadMorePreview() {
    if (exportPreviewLimit >= exportPreviewItems.length) return;
    const list = document.querySelector('.export-preview-list');
    const oldH = list ? list.scrollHeight : 0;
    exportPreviewLimit += 50;
    exportPreviewScroll = Math.max(0, oldH - (list ? list.clientHeight : 0));
    renderPreviewList();
    const l2 = document.querySelector('.export-preview-list');
    if (l2) l2.scrollTop = exportPreviewScroll;
}

function onExportFormatChange() {
    const fmt = (document.querySelector('input[name="expFormat"]:checked') || {}).value || 'csv';
    document.getElementById('exportJsonModeRow').classList.toggle('hidden', fmt !== 'json');
    document.getElementById('exportEarliestRow').classList.toggle('hidden', fmt !== 'json');
    document.getElementById('exportEarliestTip').classList.toggle('hidden', fmt !== 'json');
}

// 「往前提 N 天」导出结果的一句小结（2026-11-01 用户定的规则）：
//   有本地作品 → earliest = 本地最新作品日期 − N 天；没本地作品 → 不设 earliest（DouK 全量抓）。
function earliestSummary(d) {
    const parts = [];
    if (d.with_earliest) parts.push(`${d.with_earliest} 个带时间起点`);
    if (d.no_works) parts.push(`${d.no_works} 个本地没作品、不设起点`);
    return parts.length ? `（${parts.join('，')}）` : '';
}

async function confirmExport() {
    if (!exportState.previewed) { showToast('先点「预览选中的作者」，或待下载区的「预览」'); return; }
    const nUid = (exportState.uids || []).length;
    const nSec = (exportState.secUids || []).length;
    if (!nUid && !nSec) { showToast('没有可导出的作者'); return; }
    const fmt = (document.querySelector('input[name="expFormat"]:checked') || {}).value || 'csv';
    const jsonMode = (document.querySelector('input[name="expJsonMode"]:checked') || {}).value || 'settings';
    const days = earliestDaysValue();
    showToast('正在导出 ' + Math.max(nUid, nSec) + ' 个作者…');
    try {
        const r = await fetch('/api/export_authors', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                uids: exportState.uids || [], sec_uids: exportState.secUids || [],
                format: fmt, json_mode: jsonMode, earliest_days: days,
            }),
        });
        const d = await r.json();
        if (!d.ok) { showToast('导出失败：' + (d.error || '未知错误')); return; }
        let msg;
        if (fmt === 'txt') msg = `已导出 ${d.count} 条链接 → export/${d.file}`;
        else if (fmt === 'json') msg = `已导出 ${d.count} 个账号${earliestSummary(d)} → export/${d.file}`;
        else msg = `已导出 ${d.count} 个作者 → export/${d.file}`;
        showToast(msg);
    } catch (e) {
        showToast('导出失败：' + e.message);
    }
}

// 一键写进 DouK（2026-10-31）：不落「export/」目录，直接把当前**预览出来的**账号队列
// 整份替换进 DouK 的 Volume\settings.json —— 点完就能开 DouK 下载。
// 2026-11-01 起这是唯一的「写 DouK」入口：对象要么是 ① 筛出来的，要么是待下载区
// 点过「预览」挂过来的（exportState.source 记着是哪个），两个来源都走这里。
// 服务端会先把旧配置备份到 backup\，cookie / 下载目录 / 命名格式等一律不动；
// DouK 在跑的话它直接拒绝（它退出时会用内存配置覆盖 settings.json）。
async function confirmExportToDouk() {
    if (!exportState.previewed) { showToast('先点「预览选中的作者」，或待下载区的「预览」'); return; }
    const nUid = (exportState.uids || []).length;
    const nSec = (exportState.secUids || []).length;
    if (!nUid && !nSec) { showToast('没有可导出的作者'); return; }
    const n = Math.max(nUid, nSec);
    const from = exportState.source === 'queue' ? '待下载区' : '① 预览的筛选结果';
    const days = earliestDaysValue();
    const tip = `把${from}的 ${n} 个作者直接写进 DouK 的 settings.json？\n\n` +
        `· 原来那份账号队列会被整份替换（旧文件先备份到 backup\\ 里）\n` +
        `· cookie、下载目录、命名格式等其它设置一个字不动\n` +
        (days ? `· 有本地作品的作者：本地最新作品日期往前 ${days} 天当起点\n` +
                `· 本地一件作品都没有的：不设最早时间，DouK 全量抓\n`
              : `· 没填「往前提」天数：不设最早时间，DouK 全量抓\n`) +
        `\nDouK 正在运行的话请先关掉，否则它退出时会把配置覆盖回去。`;
    if (!window.confirm(tip)) return;
    showToast('正在写入 DouK 配置…');
    try {
        const r = await fetch('/api/export_authors', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                uids: exportState.uids || [], sec_uids: exportState.secUids || [],
                format: 'json', json_mode: 'settings',
                earliest_days: days, target: 'douk',
            }),
        });
        const d = await r.json();
        if (!d.ok) { showToast('写入失败：' + (d.error || '未知错误')); return; }
        showToast(`已写入 DouK：${d.count} 个账号${earliestSummary(d)}` +
            `${d.skipped ? `，跳过 ${d.skipped} 个没有 SEC_UID 的` : ''}；旧配置已备份`);
        // 右上角「N 个账号队列」跟着变，别让人以为没写进去
        if (typeof loadDoukConfig === 'function') loadDoukConfig();
    } catch (e) {
        showToast('写入失败：' + e.message);
    }
}

// ===== DouK 参数面板 =====
let doukCfgCache = null;
async function loadDoukConfig() {
    const box = document.getElementById('doukFields');
    if (!box) return;
    box.innerHTML = '读取中…';
    try {
        const d = await fetch('/api/douk/config').then((r) => r.json());
        if (!d.ok) {
            // "unknown api" = 老版本服务端（还没重启），给一句能直接照做的话，别只报错
            const raw = String(d.error || '未知错误');
            const tip = /unknown api/i.test(raw)
                ? '服务端还是旧版本，没有这个接口：关掉那个黑窗口，重新双击「start-browser.bat」，再刷新本页（Ctrl+F5）'
                : raw;
            box.innerHTML = `<div class="export-tip">读取失败：${escapeHtml(tip)}</div>`;
            document.getElementById('doukCfgSub').textContent = '';
            return;
        }
        doukCfgCache = d;
        renderDoukFields(d);
    } catch (e) {
        box.innerHTML = `<div class="export-tip">读取失败：${escapeHtml(e.message)}</div>`;
    }
}

let doukLoaded = {};   // 打开面板时读到的值，作为每行「重置」的目标

function renderDoukFields(d) {
    const s = d.settings || {};
    const ro = d.readonly || {};
    const p = d.pyc || {};
    const sub = document.getElementById('doukCfgSub');
    sub.innerHTML = `${d.accounts || 0} 个账号队列` + (d.running
        ? ' · <span class="douk-badge warn">DouK 正在运行</span>'
        : '');
    // 记住原始值：重置按钮写的就是这个数，点了就回到当前 DouK 配置里的值
    doukLoaded = {
        douk_delay: p.delay, douk_every: p.every, douk_rest: p.rest,
        douk_max_pages: s.max_pages, douk_max_size: s.max_size,
        douk_timeout: s.timeout, douk_max_retry: s.max_retry,
        douk_download: s.download !== false,
    };

    const show = (v) => (v === null || v === undefined || v === '' ? '—' : v);
    const row = (id, label, val, unit, hint, step) => `
        <div class="douk-row">
            <label for="${id}">${label}</label>
            <input type="number" id="${id}" value="${show(val)}" ${step ? `step="${step}"` : ''}>
            ${unit ? `<span class="douk-unit">${unit}</span>` : ''}
            ${hint ? `<span class="douk-hint">${hint}</span>` : ''}
            <button type="button" class="douk-reset" onclick="resetDoukField('${id}')" title="恢复成现在 DouK 里的值">重置 ${show(val)}</button>
        </div>`;
    const roRow = (label, val) => `
        <div class="douk-ro"><span>${label}</span><code>${escapeHtml(val === null || val === undefined ? '—' : String(val))}</code></div>`;

    document.getElementById('doukFields').innerHTML = `
        <div class="douk-subhead">等待节奏（写进 DouK 的 pyc，重启生效）</div>
        ${row('douk_delay', '请求间隔（平均）', p.delay, '秒', '', '0.5')}
        ${row('douk_every', '每采集多少个号暂停', p.every, '个', '')}
        ${row('douk_rest', '每次暂停', p.rest, '秒', '')}
        <div class="douk-sep"></div>
        <div class="douk-subhead">采集范围</div>
        ${row('douk_max_pages', '每个号最多翻几页', s.max_pages, '页', '0 = 全部')}
        ${row('douk_max_size', '单文件体积上限', s.max_size, 'MB', '0 = 不限')}
        ${row('douk_timeout', '请求超时', s.timeout, '秒', '')}
        ${row('douk_max_retry', '失败重试次数', s.max_retry, '次', '')}
        <div class="douk-sep"></div>
        <div class="douk-subhead">下载</div>
        <div class="douk-row douk-bool">
            <label for="douk_download">直接下载文件</label>
            <input type="checkbox" id="douk_download" ${s.download !== false ? 'checked' : ''}>
            <span class="douk-hint">关掉就只抓清单、不落盘（一般不用动，保持勾选）</span>
            <button type="button" class="douk-reset" onclick="resetDoukField('douk_download')"
                    title="恢复成现在 DouK 里的值">重置 ${s.download !== false ? '开' : '关'}</button>
        </div>
        <div class="douk-sep"></div>
        <div class="douk-subhead">目录与命名（只读）</div>
        ${roRow('下载根目录', ro.root)}
        ${roRow('文件命名格式', ro.name_format)}
        <div class="douk-tip">
            每个作者一个文件夹（<code>UID{uid}_{昵称}_发布作品</code>），作品直接放在里面，没有子目录。<br>
            根目录和命名格式与浏览器的扫描索引绑死，改了索引就对不上，所以只能看不能改 —— 真要换，改 <code>src/paths.py</code> 的 <code>MEDIA_BASE</code> 再重扫。
        </div>
    `;
    // 改没改过 → 决定「保存」是灰的还是红的（事件委托，容器只绑一次）
    const boxEl = document.getElementById('doukFields');
    if (boxEl && !boxEl.dataset.dirtyBound) {
        boxEl.dataset.dirtyBound = '1';
        boxEl.addEventListener('input', updateDoukDirty);
        boxEl.addEventListener('change', updateDoukDirty);
    }
    updateDoukDirty();
}

// 重置某个输入框：回到打开面板时读到的 DouK 配置值
function resetDoukField(id) {
    const el = document.getElementById(id);
    if (!el) return;
    const v = doukLoaded[id];
    if (el.type === 'checkbox') el.checked = !!v;
    else el.value = (v === null || v === undefined) ? '' : v;
    updateDoukDirty();
    showToast('已重置为 ' + (v === null || v === undefined ? '空' : v));
}

// DouK 面板里参与「改没改」判定的字段
const DOUK_DIRTY_KEYS = ['douk_delay', 'douk_every', 'douk_rest', 'douk_max_pages',
    'douk_max_size', 'douk_timeout', 'douk_max_retry', 'douk_download'];

function doukValueOf(id) {
    const el = document.getElementById(id);
    if (!el) return undefined;
    if (el.type === 'checkbox') return !!el.checked;
    const raw = String(el.value == null ? '' : el.value).trim();
    if (raw === '') return '';
    const n = Number(raw);
    return isNaN(n) ? raw : n;
}

// 没改 → 保存按钮灰且不可点、不显示取消；改了 → 保存变红、旁边出现取消
function updateDoukDirty() {
    const save = document.getElementById('doukSaveBtn');
    const cancel = document.getElementById('doukCancelBtn');
    if (!save) return;
    const dirty = DOUK_DIRTY_KEYS.some((k) => {
        const cur = doukValueOf(k);
        if (cur === undefined) return false;
        const base = doukLoaded[k];
        const b = (base === null || base === undefined) ? '' : base;
        return String(cur) !== String(b);
    });
    save.disabled = !dirty;
    save.classList.toggle('is-dirty', dirty);
    if (cancel) cancel.classList.toggle('hidden', !dirty);
}

// 取消：全部恢复成现在 DouK 里的值，不写盘
function cancelDoukConfig() {
    DOUK_DIRTY_KEYS.forEach((k) => {
        const el = document.getElementById(k);
        if (!el) return;
        const v = doukLoaded[k];
        if (el.type === 'checkbox') el.checked = !!v;
        else el.value = (v === null || v === undefined) ? '' : v;
    });
    updateDoukDirty();
    showToast('已取消修改，参数恢复成 DouK 当前的值');
}

async function saveDoukConfig() {
    const g = (id) => document.getElementById(id);
    const payload = {
        settings: {
            max_pages: g('douk_max_pages').value,
            max_size: g('douk_max_size').value,
            timeout: g('douk_timeout').value,
            max_retry: g('douk_max_retry').value,
            download: g('douk_download').checked,
        },
        pyc: {
            delay: parseFloat(g('douk_delay').value),
            every: parseInt(g('douk_every').value, 10),
            rest: parseInt(g('douk_rest').value, 10),
        },
    };
    if (!isFinite(payload.pyc.delay) || payload.pyc.delay <= 0) { showToast('请求间隔要填大于 0 的秒数'); return; }
    if (!isFinite(payload.pyc.every) || payload.pyc.every < 1) { showToast('批量暂停的「多少个号」至少 1（不想暂停填 1000000000）'); return; }
    if (!isFinite(payload.pyc.rest) || payload.pyc.rest < 0) { showToast('暂停秒数不能是负数'); return; }
    showToast('正在保存到 DouK…');
    try {
        const r = await fetch('/api/douk/config', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const d = await r.json();
        if (!d.ok) { showToast('保存失败：' + (d.error || '未知错误')); return; }
        const changed = Object.keys(d.changed || {});
        const pyc = d.pyc || {};
        showToast(`已保存${changed.length ? '（' + changed.join('/') + '）' : '（无变化）'} · 请求间隔 ${pyc.delay} 秒 · 每 ${pyc.every} 个号停 ${pyc.rest} 秒`);
        if (d.warn) showToast(d.warn);
        loadDoukConfig();
    } catch (e) {
        showToast('保存失败：' + e.message);
    }
}

// ===== 设置 =====
let settingsBaseline = null;   // 打开设置页时加载的原始值，用于「改了才亮保存」和「取消恢复」

async function openSettingsView() {
    forceCloseDetail();
    rememberViewScroll();
    state.view = 'settings';
    state.returnView = 'settings';
    hideAllViews();
    document.getElementById('settingsView').classList.remove('hidden');
    animateViewEnter('settingsView', 'neutral');
    clearSideActive();
    document.querySelectorAll('#featureList .side-item').forEach(i => i.classList.toggle('active', i.dataset.action === 'settings'));
    const eb = document.getElementById('exportBtn');
    if (eb) eb.classList.add('hidden');
    restoreViewScroll('settings');
    const body = document.getElementById('settingsBody');
    body.innerHTML = '读取中…';
    try {
        const d = await fetch('/api/settings').then((r) => r.json());
        const s = d.settings || {};
        const defs = d.defaults || {};
        const statuses = d.statuses || EXPORT_STATUS_LIST;
        const disabled = s.update_freq_disabled_status || [];
        const paths = s.paths || {};
        const favMode = s.fav_latest_mode || 'none';
        settingsBaseline = {
            disabled: disabled.slice(),
            data_dir: paths.data_dir || '',
            ffmpeg: paths.ffmpeg || '',
            downloader_dir: paths.downloader_dir || '',
            fav_latest_mode: favMode,
        };
        const pathRow = (key, label, val, placeholder) => `
            <div class="settings-path-row" data-key="${key}">
                <label>${label}</label>
                <div class="settings-path-edit">
                    <input id="path_${key}" value="${escapeHtml(val || '')}" placeholder="${escapeHtml(placeholder || '')}" readonly oninput="updateSettingsDirty()">
                    <button class="btn btn-mini" type="button" data-act="edit" onclick="togglePathEdit('${key}')">修改</button>
                    <button class="btn btn-mini btn-ghost hidden" type="button" data-act="cancel" onclick="cancelPathEdit('${key}')">取消</button>
                </div>
            </div>`;
        body.innerHTML = `
            <div class="settings-section">
                <div class="settings-title">更新频率 · 账号状态</div>
                <div class="settings-desc">勾选的账号状态<b>不能设置更新频率</b>（这些账号已经注销/封号/卖号等，没有「更新」可言）。</div>
                <div class="settings-status-list">${statuses.map((st) => `
                    <label class="opt-chip"><input type="checkbox" data-status="${escapeHtml(st)}" ${disabled.indexOf(st) >= 0 ? 'checked' : ''} onchange="updateSettingsDirty()"><span>${escapeHtml(st)}</span></label>`).join('')}
                </div>
            </div>
            <div class="settings-section">
                <div class="settings-title">获取收藏 · 最新作品时间</div>
                <div class="settings-path-row">
                    <label>最新发布</label>
                    <select id="favLatestMode" class="fav-select" onchange="updateSettingsDirty()">
                        <option value="none" ${favMode === 'none' ? 'selected' : ''}>已收藏最新发布</option>
                        <option value="downloaded" ${favMode === 'downloaded' ? 'selected' : ''}>最新发布 · 只查已下载作者主页</option>
                        <option value="all" ${favMode === 'all' ? 'selected' : ''}>最新发布 · 所有作者都查主页</option>
                    </select>
                </div>
            </div>
            <div class="settings-section">
                <div class="settings-title">文件地址</div>
                <div class="settings-desc">下面是当前生效的地址，点右边的「修改」可更改（改完点底部「保存」生效；不想改就点「取消」还原）。</div>
                ${pathRow('data_dir', '数据库地址（data.json 目录）', paths.data_dir, defs.data_dir)}
                ${pathRow('ffmpeg', 'FFmpeg 地址', paths.ffmpeg, defs.ffmpeg)}
                ${pathRow('downloader_dir', '需要的组件地址（DouK-Downloader）', paths.downloader_dir, defs.downloader_dir)}
            </div>
            <div class="settings-section">
                <div class="settings-title">缓存管理</div>
                <div class="settings-desc">缩略图/转码都是纯缓存，删了会按需重建；封面单独存放、永不清理。</div>
                <div id="settingsCacheBody"><div class="loading">读取中…</div></div>
            </div>
            <div class="settings-foot">
                <button class="btn btn-ghost hidden" id="settingsCancelBtn" onclick="cancelSettings()">取消</button>
                <button class="btn btn-primary" id="settingsSaveBtn" onclick="saveSettings()" disabled>保存</button>
            </div>`;
        // 缓存信息渲染到设置页里的子容器
        await renderCacheInto('settingsCacheBody');
        updateSettingsDirty();
    } catch (e) {
        body.innerHTML = '<div class="loading">读取设置失败：' + escapeHtml(e.message) + '</div>';
    }
}

// ============ 新下载（2026-10-31 c83）============
// 为什么单独一页：DouK 一跑就是几百上千个文件，散在各位作者的作品墙里根本看不出
// 「这一次到底新增了什么」。数据不是靠扫目录猜的——服务端读 data/download_log.db
// 的 file 表，mtime 就是文件落盘时间（≈ 下载完成时间），所以「今天」就是真·今天下载的。
// watchOnly：这个视图被「追更 → 累计追更下载」借用来显示追更下到的作品。
// 数据源不同（/api/watch/downloads 而不是 /api/new_downloads），但渲染、播放、
// 灯箱全部复用，所以观感和「新下载」逐像素一样。
const newDlState = { range: 'today', data: null, loading: false, loadingMore: false, watchOnly: false };

// 「按作者筛选」——和「📤 导出作者信息」那一栏完全同一套口径：
// 作者等级 / 标签 / 账号状态 / 更新频率 / 最后下载时间，每行自带一个 ＋/－ 组合开关。
// 筛选放在服务端做（/api/new_downloads 复用 /api/authors 的判定函数 _build_attr_tests），
// 所以翻页取到的也都是「筛完之后」的结果，不会翻着翻着冒出没勾的作者。
const ndFilter = {
    level: [], tag: [], status: [], freq: [], watch: [], latest: '',
    matchMap: { level: 'or', tag: 'or', status: 'or', freq: 'or', watch: 'or', latest: 'or' },
};

function ndFilterActive() {
    const f = ndFilter;
    return !!(f.level.length || f.tag.length || f.status.length || f.freq.length
        || f.watch.length || f.latest);
}

function updateNdFilterDot() {
    const dot = document.getElementById('ndFilterDot');
    if (dot) dot.classList.toggle('hidden', !ndFilterActive());
}

function toggleNdFilter() {
    const p = document.getElementById('ndFilterPanel');
    if (!p) return;
    if (p.classList.contains('hidden')) {
        renderNdFilter();
        p.classList.remove('hidden');
    } else {
        p.classList.add('hidden');
    }
}

// 面板内容：选项 + 已勾状态 + 每行的 ＋/－
function renderNdFilter() {
    const tags = Object.keys((state.meta && state.meta.tags) || {}).sort();
    const box = (id, html) => { const e = document.getElementById(id); if (e) e.innerHTML = html; };
    const ck = (name, v, arr, label) => '<label><input type="checkbox" name="' + name + '" value="' +
        escapeHtml(v) + '" ' + (arr.indexOf(v) >= 0 ? 'checked' : '') +
        ' onchange="ndFilterChange()"> ' + escapeHtml(label) + '</label>';

    // 「未设置 / 没有标签」这类兜底项统一排在该行最前（2026-11-05 用户要求）
    box('ndLevelOpts', ['__unset', '夯', '顶级', '一般', '拉']
        .map((v) => ck('ndLevel', v, ndFilter.level, v === '__unset' ? '未设置' : v)).join(''));
    box('ndTagOpts', [['__notag', '没有标签']].concat(tags.map((t) => [t, t]))
        .map(([v, t]) => ck('ndTag', v, ndFilter.tag, t)).join(''));
    box('ndStatusOpts', [['__unset', '未设置']].concat(EXPORT_STATUS_LIST.map((s) => [s, s]))
        .map(([v, t]) => ck('ndStatus', v, ndFilter.status, t)).join(''));
    box('ndFreqOpts', ['__unset', 'day', '3天', '周', '月']
        .map((v) => ck('ndFreq', v, ndFilter.freq, v === '__unset' ? '未设置' : v)).join(''));
    box('ndWatchOpts', [['yes', '追更中'], ['no', '不追更']]
        .map(([v, t]) => ck('ndWatch', v, ndFilter.watch, t)).join(''));
    box('ndLatestOpts', EXPORT_LATEST_LIST.filter(([v]) => v !== 'custom')
        .map(([v, label]) => '<label><input type="radio" name="ndLatest" value="' + v + '" ' +
            (v === (ndFilter.latest || '') ? 'checked' : '') + ' onchange="ndFilterChange()"> ' +
            label + '</label>').join(''));

    document.querySelectorAll('#ndFilterPanel .ndf-match').forEach((el) => {
        const key = el.dataset.key;
        const mode = ndFilter.matchMap[key] === 'not' ? 'not' : 'or';
        const tip = mode === 'not'
            ? '－ 排除：勾中的这些要去掉（点一下改回 ＋ 包含）'
            : '＋ 包含：勾中的这些是我要的（点一下改成 － 排除）';
        el.innerHTML = '<button type="button" class="em-btn ' + mode + '" title="' + tip + '"' +
            ' onclick="ndToggleMatch(\'' + key + '\')">' + (mode === 'not' ? '－' : '＋') + '</button>';
    });
    const sub = document.getElementById('ndfSub');
    if (sub) sub.textContent = ndFilterActive() ? '筛完再列作品' : '不筛 = 全部作者';
    updateNdFilterDot();
}

// 勾选变化：把控件读回 ndFilter，立刻重取第一页（面板不关，方便连着勾）
function ndFilterChange() {
    const pick = (name) => Array.from(
        document.querySelectorAll('#ndFilterPanel input[name="' + name + '"]:checked')).map((i) => i.value);
    ndFilter.level = pick('ndLevel');
    ndFilter.tag = pick('ndTag');
    ndFilter.status = pick('ndStatus');
    ndFilter.freq = pick('ndFreq');
    ndFilter.watch = pick('ndWatch');
    const ll = document.querySelector('#ndFilterPanel input[name="ndLatest"]:checked');
    ndFilter.latest = ll ? ll.value : '';
    updateNdFilterDot();
    refreshNewDownloads(true);
}

function ndToggleMatch(key) {
    ndFilter.matchMap[key] = ndFilter.matchMap[key] === 'not' ? 'or' : 'not';
    renderNdFilter();
    if (ndFilterActive()) refreshNewDownloads(true);
}

function ndResetFilter() {
    ndFilter.level = []; ndFilter.tag = []; ndFilter.status = []; ndFilter.freq = [];
    ndFilter.watch = [];
    ndFilter.latest = '';
    ndFilter.matchMap = { level: 'or', tag: 'or', status: 'or', freq: 'or', watch: 'or', latest: 'or' };
    renderNdFilter();
    refreshNewDownloads(true);
}

// 拼成 /api/new_downloads 的参数（参数名和 /api/authors 完全一致，服务端一套逻辑）
function ndFilterParams() {
    const p = new URLSearchParams();
    if (ndFilter.level.length) p.set('level', ndFilter.level.join(','));
    if (ndFilter.tag.length) p.set('tag', ndFilter.tag.join(','));
    if (ndFilter.status.length) p.set('status', ndFilter.status.join(','));
    if (ndFilter.freq.length) p.set('freq', ndFilter.freq.join(','));
    // 追更：只勾「追更中」或只勾「不追更」才筛；两个都勾（= 全部）不传
    if (ndFilter.watch.length === 1) p.set('watch', ndFilter.watch[0]);
    Object.keys(ndFilter.matchMap || {}).forEach((k) => {
        if (ndFilter.matchMap[k] === 'not') p.set('match_' + k, 'not');
    });
    // 「最后下载」只留预设档：这一页本来就按下载时间切范围，再给自选日期纯属添乱
    if (ndFilter.latest && ndFilter.latest !== 'custom') {
        const days = parseInt(ndFilter.latest, 10);
        if (!isNaN(days)) p.set('latest_after', exportIsoDaysAgo(days));
    }
    return p;
}

// 一页取多少个文件（≈ 几百个作品）。2026-11-02 用户要求取消「只显示最新 600 个作品」
// 的硬上限：这里只当翻页的节奏，滚到底自动接着取，直到这个范围全部出来。
const ND_PAGE_FILES = 1200;

// 范围 → 起始时间戳（秒）。「今天」= 本地 0 点，不是「24 小时前」，跟人嘴里的今天对齐
function ndSince(range) {
    const now = new Date();
    if (range === 'today') {
        return Math.floor(new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000);
    }
    const days = range === '3d' ? 3 : range === '7d' ? 7 : range === '30d' ? 30 : 0;
    return days ? Math.floor(now.getTime() / 1000) - days * 86400 : 0;
}

// 下载时间（文件落盘）→ 「10-31 17:37」，只用在鼠标提示里
function ndStamp(ts) {
    const d = new Date((Number(ts) || 0) * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

async function openNewDownloadsView() {
    forceCloseDetail();
    rememberViewScroll();
    state.view = 'new';
    state.returnView = 'new';
    hideAllViews();
    const view = document.getElementById('newView');
    if (view) view.classList.remove('hidden');
    animateViewEnter('newView', 'neutral');
    clearSideActive();
    document.querySelectorAll('#featureList .side-item').forEach((i) => i.classList.toggle('active', i.dataset.action === 'newDownloads'));
    const eb = document.getElementById('exportBtn');
    if (eb) eb.classList.add('hidden');
    restoreViewScroll('new');
    applyNdWatchMode(false);
    renderNdFilter();          // 筛选面板的选项（标签来自 meta）+ 上次的勾选状态
    await refreshNewDownloads(true);
}

// ---------- 「追更下载」：借用 newView，只换数据源 ----------
// 用户要的是「点累计追更下载 → 看到追更下到的作品，界面和新下载一样」。
// 与其复制一套列表，不如把 newView 切成「追更模式」：隐藏时间范围条，
// 顶部换成一条「← 全部下载」的返回条，数据走 /api/watch/downloads。
function applyNdWatchMode(on) {
    newDlState.watchOnly = !!on;
    const title = document.getElementById('newViewTitle');
    const bar = document.getElementById('ndRangeBar');
    const wbar = document.getElementById('ndWatchBar');
    if (bar) bar.classList.toggle('hidden', !!on);
    if (wbar) wbar.classList.toggle('hidden', !on);
    // 标题单独一个 span：#newViewSub 要留给 renderNewDownloads() 写统计，不能一起清掉
    if (title) title.textContent = on ? '追更下载' : '新下载';
}

async function refreshWatchDownloads() {
    const body = document.getElementById('newBody');
    if (!body) return;
    body.innerHTML = '<div class="loading">读取中…</div>';
    try {
        const d = await fetch('/api/watch/downloads', { cache: 'no-store' }).then((r) => r.json());
        if (!d || d.ok === false) throw new Error((d && d.error) || '读取失败');
        // 结构照 /api/new_downloads 造：has_more=false 后台就不会去翻页
        newDlState.data = {
            groups: d.groups || [],
            total_files: d.total_files || 0,
            shown_items: d.total || 0,
            cursor: null,
            has_more: false,
        };
        renderNewDownloads();
        // 统计文案由 renderNewDownloads() 写进标题旁的 #newViewSub（「277 个作品 ·
        // 316 个文件 · 102 位作者」），这里不要再写一遍，否则一屏里出现两次同样的数字。
    } catch (e) {
        body.innerHTML = '<div class="loading">读取失败：' + escapeHtml(e.message) + '</div>';
    }
}

async function openWatchDownloadsView() {
    forceCloseDetail();
    rememberViewScroll();
    state.view = 'watchDl';
    state.returnView = 'watchDl';
    hideAllViews();
    const view = document.getElementById('newView');
    if (view) view.classList.remove('hidden');
    animateViewEnter('newView', 'neutral');
    clearSideActive();
    // 侧栏保持「追更」高亮：用户是从追更页点进来的，不是从「新下载」
    document.querySelectorAll('#featureList .side-item').forEach((i) => i.classList.toggle('active', i.dataset.action === 'watch'));
    const eb = document.getElementById('exportBtn');
    if (eb) eb.classList.add('hidden');
    restoreViewScroll('watchDl');
    applyNdWatchMode(true);
    await refreshWatchDownloads();
}

function refreshWatchDownloadsClick() {
    if (newDlState.watchOnly) refreshWatchDownloads();
    else refreshNewDownloads(true);
}

function ndPickRange(range) {
    newDlState.range = range;
    document.querySelectorAll('#ndRangeBar [data-range]').forEach((b) => b.classList.toggle('on', b.dataset.range === range));
    refreshNewDownloads(true);
}

async function refreshNewDownloads(showLoading) {
    const body = document.getElementById('newBody');
    if (!body || newDlState.loading) return;
    newDlState.loading = true;
    newDlState.loadingMore = false;
    newDlState.data = null;
    if (showLoading) body.innerHTML = '<div class="loading">读取中…</div>';
    try {
        const d = await ndFetchPage('');
        if (d.error) throw new Error(d.error);
        d.groups = d.groups || [];
        newDlState.data = d;
        renderNewDownloads();
    } catch (e) {
        body.innerHTML = '<div class="loading">读取失败：' + escapeHtml(e.message) + '</div>';
    } finally {
        newDlState.loading = false;
    }
}

// 取一页：cursor 为空 = 第一页，否则接着上一页往下（服务端按 mtime 倒序 keyset 分页）
function ndFetchPage(cursor) {
    const p = new URLSearchParams({
        since: String(ndSince(newDlState.range)),
        limit: String(ND_PAGE_FILES),
    });
    if (cursor) p.set('cursor', cursor);
    ndFilterParams().forEach((v, k) => p.set(k, v));   // 作者属性筛选（和导出页同一套参数名）
    return fetch('/api/new_downloads?' + p.toString()).then((r) => r.json());
}

// ---------- 继续加载（取消 600 上限的核心）----------
// 滚到底就自动取下一页，接到已有数据里；不打断正在播的视频，也不跳滚动位置。
async function ndLoadMore() {
    const st = newDlState;
    if (!st.data || st.loading || st.loadingMore) return;
    if (!st.data.has_more || !st.data.cursor) return;
    st.loadingMore = true;
    ndFooterSay('正在加载更多…');
    try {
        const d = await ndFetchPage(st.data.cursor);
        if (d.error) throw new Error(d.error);
        ndMergePage(d);
        st.loadingMore = false;
        renderNewDownloads({ keepScroll: true });
    } catch (e) {
        st.loadingMore = false;
        ndFooterSay('加载更多失败：' + (e.message || '网络错误') + '（往下滚可以再试）');
    }
}

// 把新一页并进已有数据：同一个作者的并成一个块，同一个作品的并成一条。
// 为什么要在前端合并：一页是按「文件」切的，同一个作品的几张图、
// 甚至同一个作者的作品，完全可能被切在两页里。
function ndMergePage(d) {
    const base = newDlState.data;
    if (!base) return;
    const byUid = new Map();
    base.groups.forEach((g) => byUid.set(g.uid, g));
    (d.groups || []).forEach((g) => {
        let t = byUid.get(g.uid);
        if (!t) {
            t = { uid: g.uid, nickname: g.nickname, level: g.level || '', freq: g.freq || '',
                  status: g.status || '', tags: g.tags || [], items: [], count: 0 };
            base.groups.push(t);
            byUid.set(g.uid, t);
        } else {
            // 标注以先到的为准，缺的用新页补上（同一作者两页里理论上一样）
            ['nickname', 'level', 'freq', 'status'].forEach((k) => {
                if (!t[k] && g[k]) t[k] = g[k];
            });
            if ((!t.tags || !t.tags.length) && g.tags && g.tags.length) t.tags = g.tags;
        }
        (g.items || []).forEach((it) => ndMergeItem(t, it));
        t.count = t.items.length;
    });
    base.total_files = d.total_files || base.total_files;
    base.cursor = d.cursor || null;
    base.has_more = !!d.has_more;
}

// 文件名里的数字按数值排（_2 排在 _10 前面）
function ndNatKey(s) {
    return String(s || '').replace(/(\d+)/g, (m) => m.padStart(12, '0'));
}

function ndMergeItem(g, it) {
    const key = (it.date || '') + '\u0000' + (it.desc || '');
    if (!g._map) {
        g._map = new Map();
        g.items.forEach((x) => g._map.set((x.date || '') + '\u0000' + (x.desc || ''), x));
    }
    if (!it.rels || !it.rels.length) it.rels = [it.rel];
    const hit = g._map.get(key);
    if (!hit) {
        g._map.set(key, it);
        g.items.push(it);
        return;
    }
    hit.files = (hit.files || 1) + (it.files || 1);
    hit.size = (hit.size || 0) + (it.size || 0);
    hit.mtime = Math.max(hit.mtime || 0, it.mtime || 0);
    if (it.is_video && !hit.is_video) {
        // 上一页只看到封面图，这一页才看到视频 → 换成视频当门面（跟服务端同一套规则）
        hit.is_video = true;
        hit.rel = it.rel;
        hit.rels = it.rels.slice();
    } else if (!it.is_video && !hit.is_video) {
        hit.rels = hit.rels.concat(it.rels);
    }
    if (hit.rels.length > 1) {
        hit.rels = Array.from(new Set(hit.rels)).sort((a, b) => {
            const ka = ndNatKey(a), kb = ndNatKey(b);
            return ka < kb ? -1 : ka > kb ? 1 : 0;
        });
    }
}

// 底栏文案：还在加载 / 还有更多（可点）/ 已经全部显示
function ndFooterHtml() {
    const d = newDlState.data || {};
    const shown = (newDlState.rendered || []).reduce((n, g) => n + g.items.length, 0);
    const files = d.total_files || 0;
    if (newDlState.loadingMore) return '<div class="nd-more">正在加载更多…</div>';
    if (d.has_more) {
        return `<div class="nd-more nd-more-btn" onclick="ndLoadMore()" title="往下滚会自动加载；也可以点这里">`
            + `已显示 ${shown} 个作品（这个范围共 ${files} 个文件）· 继续往下滚，剩下的会自动加载</div>`;
    }
    return `<div class="nd-more">已全部显示：${shown} 个作品 / ${files} 个文件</div>`;
}

// 只改底栏文字，不重建整个列表（加载中 / 出错时用）
function ndFooterSay(txt) {
    const body = document.getElementById('newBody');
    if (!body) return;
    let el = body.querySelector('.nd-more');
    if (!el) {
        el = document.createElement('div');
        el.className = 'nd-more';
        body.appendChild(el);
    }
    el.classList.remove('nd-more-btn');
    el.textContent = txt;
}

// 滚到底自动续页：绑一次就够，之后每次渲染只补一次「要不要立刻续」的判定
let ndScrollBound = false;
function ndArmMore() {
    const body = document.getElementById('newBody');
    if (!body) return;
    if (!ndScrollBound) {
        body.addEventListener('scroll', ndMaybeLoadMore, { passive: true });
        ndScrollBound = true;
    }
    // 首屏内容不满一屏（滚不动）时也要能接着取，等布局落定再判一次
    setTimeout(ndMaybeLoadMore, 60);
}

function ndMaybeLoadMore() {
    const body = document.getElementById('newBody');
    const d = newDlState.data;
    if (!body || !d || !d.has_more || !d.cursor) return;
    if (newDlState.loading || newDlState.loadingMore) return;
    if (state.view !== 'new') return;
    // 视图藏着的时候（进作者详情页看作品）不能续页：这时高度是 0，
    // 会被判成「已经滚到底」，结果在后台一路把所有页都拉下来
    if (!body.clientWidth || !body.clientHeight) return;
    if (body.scrollHeight - body.scrollTop - body.clientHeight > 500) return;
    ndLoadMore();
}

// ---------- 气泡列数：按当前可用宽度动态算 ----------
// 用户 2026-10-31 20:48：截图里红线标的是气泡右边的空白，要求「点击最左边的
// 收栏按钮还会动态调整页面展示效果」。
// 所以列数**不能写死**：收起侧边栏后可用宽度变大，气泡要多排几张把右边填满。
const ND_CARD_MIN = 138;   // 卡片最窄宽度（再窄封面就看不清了）
const ND_PAD = 24;         // 气泡左右内边距合计（12 + 12）
const ND_GAP = 10;         // 卡距

// 当前一行最多能放几张、每张多宽
let ndGoodW = 0;      // 上一次「视图可见」时量到的列表可用宽（隐藏时的兜底值）
function ndLayout() {
    const list = document.getElementById('newBody');
    let W = list ? list.clientWidth - 1 : 0;    // -1：防浮点误差把最后一列挤下去
    if (W > ND_PAD) {
        ndGoodW = W;
    } else {
        // 视图被藏起来（进作者详情 / 切到别的页）时 clientWidth = 0。
        // 这时绝不能拿 0 去算：maxCols 会退化成 1，每个气泡都被撑成整行宽、
        // 一张卡占满整个列表 —— 返回「新下载」的第一眼就是一张巨图闪一下
        // （用户 2026-11-02 反馈的「卡一帧图片」就是这个）。
        // 先沿用上次可见时量到的宽度；从没量到过才按窗口宽估一个。
        W = ndGoodW || Math.max(480, (window.innerWidth - 320) - ND_PAD);
    }
    const maxCols = Math.max(1, Math.floor((W - ND_PAD + ND_GAP) / (ND_CARD_MIN + ND_GAP)));
    // 卡片均分整行：放满 maxCols 张时正好铺满，不留缝
    const cardW = (W - ND_PAD - (maxCols - 1) * ND_GAP) / maxCols;
    return { maxCols, cardW };
}

// 一个气泡该排几列、多宽：作品多 → 占满整行；作品少 → 按实际张数收窄，
// 让几个窄气泡并排挨着（这样既有大气泡的填充感，小气泡也不会各自占一行留白）。
function ndGroupBox(n, lay) {
    const cols = Math.max(1, Math.min(n, lay.maxCols));
    const full = n >= lay.maxCols;                // 这位作者能把整行填满
    const w = full ? 0 : cols * lay.cardW + (cols - 1) * ND_GAP + ND_PAD;
    return { cols, full, w };
}

// 作者表头：一行一个名字，放在这位作者的作品上方；右边跟着
// 等级 / 更新频率 / 状态 / 标签 —— 用户 2026-10-31 标注要求「同一个作者要连起来
// 同时跟着分类、标签、等级哪些」，所以这些徽章从播放区复制一份到表头。
// 整行可点 → 进作者详情页（跟原来播放区的作者行一个行为）。
//
// 表头永远只有一行（用户 2026-10-31 20:31 标注）：
//   · 名字太长 → 省略号（CSS ellipsis）
//   · 标签放不下 → 优先留「等级/频率/状态/分类标签」，其余收成「＋N」
//     （截断逻辑在 ndTrimChips，渲染完量一次宽度再决定藏几个）
function ndAuthorHead(g, gi) {
    const chips = [];
    const lv = levelTagHtml(g.level);
    if (lv) chips.push(lv);
    if (g.freq) chips.push(`<span class="mini-tag freq-tag">${escapeHtml(g.freq)}</span>`);
    const st = statusTagHtml(g.status);
    if (st) chips.push(st);
    // 分类标签放最后：真放不下时先砍它们，等级/频率/状态优先显示
    sortTags(g.tags || []).forEach((t) => chips.push(`<span class="mini-tag">${escapeHtml(t)}</span>`));
    return `<div class="nd-head" onclick="ndOpenAuthor(${gi})" title="点击进入这位作者的详情页">
        <span class="nd-dot"></span>
        <span class="nd-name">${escapeHtml(g.nickname || g.uid)}</span>
        <span class="nd-chips">${chips.join('')}</span>
        <span class="nd-cnt">${g.items.length} 个</span>
    </div>`;
}

// 表头徽章截断：一行放不下就藏掉尾巴上的几个，末尾补「＋N」。
// 为什么不用纯 CSS：CSS 只能整体裁掉一半（看着像坏了），
// 量一下每个徽章的实际宽度、按顺序装，才知道「刚好放下几个」。
function ndTrimChips(root) {
    (root || document).querySelectorAll('#newBody .nd-head').forEach((head) => {
        const box = head.querySelector('.nd-chips');
        if (!box) return;
        const all = [].slice.call(box.querySelectorAll('.mini-tag:not(.nd-more-chip)'));
        const old = head.querySelector('.nd-more-chip');
        if (old) old.remove();
        if (!all.length) return;
        // 先全部显示、量一遍各自宽度（隐藏后再量会是 0）
        all.forEach((el) => { el.style.display = ''; });
        const widths = all.map((el) => el.getBoundingClientRect().width);
        const GAP = 4;
        // 「＋N」也得占位置，先估一个宽度（三个字符左右）
        const moreW = 30;

        // 按顺序装，装不下就停；返回实际能放几个
        const fitCount = (limit) => {
            let used = 0;
            let keep = 0;
            for (let i = 0; i < widths.length; i++) {
                const add = (keep ? GAP : 0) + widths[i];
                if (used + add > limit) break;
                used += add;
                keep++;
            }
            return keep;
        };

        const limit = box.clientWidth;
        let keep = fitCount(limit);
        // 全放下了 → 不用 ＋N
        if (keep >= all.length) {
            all.forEach((el) => { el.style.display = ''; });
            return;
        }
        // 要加 ＋N 就得给它腾位置，重算一次（这是最容易漏的一步）
        keep = fitCount(limit - (moreW + GAP));
        if (keep < 1) keep = 1;          // 一个都放不下时至少留第一个，别整行空着

        all.forEach((el, i) => { if (i >= keep) el.style.display = 'none'; });
        const hidden = all.length - keep;
        const more = document.createElement('span');
        more.className = 'mini-tag nd-more-chip';
        more.textContent = '＋' + hidden;
        more.title = all.slice(keep).map((el) => el.textContent).join(' / ');
        box.appendChild(more);
    });
}

// 点作者名进详情页：按 uid 走，跟播放区那个 › 同一个入口
function ndOpenAuthor(gi) {
    const g = (newDlState.rendered || [])[gi];
    if (g && g.uid) openAuthor(g.uid);
}

// 一张卡：缩略图 + 发布时间（作者名不再每张都写 —— 见上面的 ndAuthorHead）
// 用户 2026-10-31 19:59 标注：「一行出现太多名字不好看，同一个作者一行就一个名字放在视频上方」
// 所以名字上移到作者表头，卡下只留日期和角标。
// 卡片宽度不再写死 146px，改由 CSS 网格按容器宽度平分（minmax + 1fr），
// 这样一行能正好铺满、和右边播放区之间不留缝。
function ndCard(it, nick, gi, ii) {
    const src = it.is_video ? ('/thumb?path=' + encodeURIComponent(it.rel)) : mediaUrl(it.rel);
    const n = (it.rels && it.rels.length) || it.files || 1;
    const tip = (nick ? nick + '\n' : '') + (it.desc || '') + '\n' + (it.date || '')
        + ' · 下载于 ' + ndStamp(it.mtime)
        + (n > 1 ? '\n这个作品共 ' + n + ' 个文件（点开右边能一张张看）' : '');
    return `<div class="nd-card" data-gi="${gi}" data-ii="${ii}" onclick="ndOpenCard(this)" title="${escapeHtml(tip)}">
        <div class="nd-thumb">
            <img data-src="${src}" alt="" onerror="thumbError(this)">
            ${it.is_video
                ? (n > 1 ? `<span class="nd-multi">${n}</span>` : '')
                : `<span class="nd-multi">${n}</span>`}
        </div>
        <div class="nd-time"><i class="nd-time-k">发布</i>${escapeHtml(it.date || '日期未知')}</div>
    </div>`;
}

// ---------- 右侧常驻播放区（点卡片就在这儿播） ----------
// 为什么不做浮层：翻着看的时候浮层把列表盖住了，想挑下一个还得先关掉；
// 为什么不用 window.open：跳走了就丢列表位置，也没法连着翻。
// 做成右侧常驻就跟作者详情页一个手感，但窄一档（396px），把宽度留给左边的缩略图网格。
const ndPlay = { slides: [], index: 0, nick: '', uid: '', token: 0, active: false,
                 level: '', freq: '', tags: [], status: '',
                 // 当前作品的全部图片（图文作品才有），给右栏底部的缩略图条用
                 rels: [], cur: 0, zoom: false };

const NDP_EMPTY = '点左边的作品开始看<br><span>也可以用 ‹ › 或键盘 ← → 连着翻</span>';

function ndpSet(id, txt) {
    const e = document.getElementById(id);
    if (e) e.textContent = txt;
}

// 作者徽章：等级 / 更新频率 / 标签 —— 复用列表卡片那套 mini-tag，颜色跟侧边栏/详情页一致
function ndpMetaHtml() {
    const parts = [];
    if (ndPlay.level) parts.push(levelTagHtml(ndPlay.level));
    if (ndPlay.freq) parts.push(`<span class="mini-tag freq-tag">${escapeHtml(ndPlay.freq)}</span>`);
    sortTags(ndPlay.tags || []).forEach((t) => parts.push(`<span class="mini-tag">${escapeHtml(t)}</span>`));
    return parts.length
        ? parts.join('')
        : '<span class="ndp-none">未标注</span>';
}

// 右栏宽度、以及画面区域高度都固定死了（宽度在 CSS 的 .nd-body，高度靠 .ndp-panel 定高）：
// 上一版按视频比例实时调宽度，换个视频右栏就忽宽忽窄、左边网格跟着重排，看着晃。
// 宁可横屏视频两侧留点黑边，也别让整个版面跳。

// 清空播放区（切视图、换时间范围时调）。必须把 stage 的 DOM 清掉，只藏起来的话
// <video> 还在后台出声
function ndPlayReset() {
    ndPlay.slides = [];
    ndPlay.index = 0;
    ndPlay.nick = '';
    ndPlay.uid = '';
    ndPlay.level = '';
    ndPlay.freq = '';
    ndPlay.tags = [];
    ndPlay.status = '';
    ndPlay.active = false;
    ndPlay.rels = [];
    ndPlay.cur = 0;
    ndPlay.zoom = false;
    ndPlay.token++;                       // 让还在跑的转码轮询自己放弃
    const stage = document.getElementById('ndStage');
    if (stage) {
        stage.classList.remove('zoomed');
        stage.innerHTML = '<div class="ndp-empty">' + NDP_EMPTY + '</div>';
    }
    ndpStripReset();
    const meta = document.getElementById('ndpMeta');
    if (meta) meta.innerHTML = '';
    ndpSet('ndpPos', '—');
    ndpSet('ndpDate', '');
    ndpSet('ndpDesc', '还没选作品');
    ndpSet('ndpName', '—');
    const rawBtn = document.getElementById('ndpRaw');
    if (rawBtn) rawBtn.disabled = true;
    document.querySelectorAll('#newBody .nd-card.active').forEach((c) => c.classList.remove('active'));
}

function ndOpenCard(el) {
    const gi = Number(el.dataset.gi);
    const ii = Number(el.dataset.ii);
    const group = (newDlState.rendered || [])[gi];
    if (!group) return;
    // 把「作品 → 文件」摊平成一串：一个图文作品就是连续几张，翻页不会漏。
    // 每一张都带上「它属于哪个作品、这个作品共几张、自己是第几张」——
    // 右栏底部的缩略图条（需求③）就靠这组信息知道该把哪几张并排画出来。
    const slides = [];
    let start = 0;
    group.items.forEach((it, k) => {
        const rels = (it.rels && it.rels.length) ? it.rels : [it.rel];
        if (k < ii) start += rels.length;
        rels.forEach((rel, n) => slides.push({
            rel, rels, n, date: it.date, desc: it.desc, isVideo: it.is_video,
        }));
    });
    if (!slides.length) return;
    ndPlay.slides = slides;
    ndPlay.index = Math.min(start, slides.length - 1);
    ndPlay.nick = group.nickname;
    ndPlay.uid = group.uid;
    // 作者身上的标注，播放区下方那几个小徽章用
    ndPlay.level = group.level || '';
    ndPlay.freq = group.freq || '';
    ndPlay.tags = group.tags || [];
    ndPlay.status = group.status || '';
    ndPlay.active = true;
    document.querySelectorAll('#newBody .nd-card').forEach((c) => c.classList.remove('active'));
    el.classList.add('active');
    ndPlayRender();
}

function ndPlayStep(d) {
    const i = ndPlay.index + d;
    if (i < 0 || i >= ndPlay.slides.length) return;
    ndPlay.index = i;
    ndPlayRender();
}

// ---------- 需求③：图文作品的多张图并排显示 + 点一下原位放大 ----------
// 用户 2026-10-31 标注：「如果有多张图文，就在右边同时显示，点击放大，再次点击缩小。
// 注意不是全屏放大」。
// 所以：① 右栏底部加一条缩略图带，把这个作品的每张图并排列出来，当前那张高亮；
//      ② 点大图 → 在 .ndp-stage 里把 object-fit 从 contain 换成 zoom（就地放大、出滚动条），
//         再点一下复原 —— 不开遮罩、不进全屏，播放区那个框还是原来那么大。

// 缩略图带：只有「当前作品是多图」时才显示，单图/视频直接清空
function ndpStripRender() {
    const wrap = document.getElementById('ndpStrip');
    if (!wrap) return;
    const rels = ndPlay.rels || [];
    const s = ndPlay.slides[ndPlay.index];
    // 视频不带缩略图带；只有一张图的也没必要画
    if (!s || s.isVideo || rels.length < 2) {
        wrap.innerHTML = '';
        wrap.classList.add('hidden');
        return;
    }
    wrap.classList.remove('hidden');
    wrap.innerHTML = rels.map((rel, k) => `<img class="ndp-thumb${k === ndPlay.cur ? ' on' : ''}"
        data-k="${k}" src="${mediaUrl(rel)}" alt="" onclick="ndpPickSlide(${k})"
        title="第 ${k + 1} 张 / 共 ${rels.length} 张">`).join('');
}

function ndpStripReset() {
    const wrap = document.getElementById('ndpStrip');
    if (wrap) { wrap.innerHTML = ''; wrap.classList.add('hidden'); }
}

// 点缩略图条里的某一张 → 切到这个作品的第 k 张
function ndpPickSlide(k) {
    const s = ndPlay.slides[ndPlay.index];
    if (!s || !s.rels || !s.rels.length) return;
    const target = s.rels[Math.max(0, Math.min(k, s.rels.length - 1))];
    // slides 里这个作品是连续一段，找到对应那一条的下标
    for (let i = 0; i < ndPlay.slides.length; i++) {
        const t = ndPlay.slides[i];
        if (t.rels === s.rels && t.rel === target) { ndPlay.index = i; break; }
    }
    ndPlay.zoom = false;
    ndPlayRender();
}

// 点大图：原位放大 / 缩回。只改 .ndp-stage 的 class + 图片 object-fit，不动布局尺寸
function ndpToggleZoom() {
    const s = ndPlay.slides[ndPlay.index];
    if (!s || s.isVideo) return;                 // 视频有自带控件，不抢它的点击
    const stage = document.getElementById('ndStage');
    if (!stage) return;
    ndPlay.zoom = !ndPlay.zoom;
    stage.classList.toggle('zoomed', ndPlay.zoom);
}

async function ndPlayRender() {
    const s = ndPlay.slides[ndPlay.index];
    if (!s) return;
    const tk = ++ndPlay.token;
    ndpSet('ndpPos', (ndPlay.index + 1) + ' / ' + ndPlay.slides.length);
    ndpSet('ndpDate', s.date || '');
    ndpSet('ndpDesc', s.desc || '（这条作品没有描述）');
    const dEl = document.getElementById('ndpDesc');
    if (dEl) dEl.title = s.desc || '';
    ndpSet('ndpName', ndPlay.nick || '—');
    const raw = document.getElementById('ndpRaw');
    if (raw) raw.disabled = false;

    const stage = document.getElementById('ndStage');
    if (!stage) return;
    // 作者徽章（等级/频率/标签）：先填，panel 高度会变，下面的 fit 要等它落定
    const meta = document.getElementById('ndpMeta');
    if (meta) meta.innerHTML = ndpMetaHtml();

    // 这个作品的全部图（多图时给缩略图条用）+ 当前是第几张
    ndPlay.rels = (s.rels && s.rels.length) ? s.rels : [s.rel];
    ndPlay.cur = s.n || 0;
    // 换图/换作品就退出放大状态，否则下一张莫名其妙就是放大的
    ndPlay.zoom = false;
    stage.classList.remove('zoomed');

    if (s.isVideo) {
        stage.innerHTML = `<video id="ndpVideo" class="ndp-video" controls autoplay playsinline
            src="${mediaUrl(s.rel)}"></video>`;
        const v = document.getElementById('ndpVideo');
        // 跟详情页同一套兜底：报错、或 5 秒还读不到时长 → 认定解不了，转码
        v.addEventListener('error', () => ndPlayTranscode(s.rel, tk));
        setTimeout(() => {
            if (tk !== ndPlay.token) return;
            if (v.readyState === 0 && !v.error) ndPlayTranscode(s.rel, tk);
        }, 5000);
    } else {
        // 点图原位放大（需求③）。用 onclick 而不是监听器，因为这里是每次重建 DOM
        stage.innerHTML = `<img class="ndp-img" src="${mediaUrl(s.rel)}" alt=""
            onclick="ndpToggleZoom()" title="点一下放大 / 再点一下缩小"></img>`;
        // 图片不跟着比例调宽度：图文作品比例五花八门（3:4、1:1、横图都有），
        // 跟着调会让右栏忽宽忽窄、左边网格跟着重排，翻一张跳一下
    }
    ndpStripRender();
}

async function ndPlayTranscode(rel, tk) {
    if (tk !== ndPlay.token) return;
    const stage = document.getElementById('ndStage');
    if (!stage) return;
    const tip = (pct) => `<div class="ndp-tip">浏览器解不了这个视频，正在转成 H.264 ${pct}<br>
        <span>转一次以后就秒开（缓存里留着）</span></div>`;
    stage.innerHTML = tip('…');
    const t = await transcodeVideo(rel, (pct) => {
        if (tk !== ndPlay.token) return;
        const el = stage.querySelector('.ndp-tip');
        if (el) el.innerHTML = tip(pct >= 0 ? pct + '%' : '…');
    }, () => tk !== ndPlay.token);
    if (tk !== ndPlay.token) return;
    if (t && t.ok && t.url) {
        stage.innerHTML = `<video class="ndp-video" controls autoplay playsinline src="${t.url}"></video>`;
    } else {
        stage.innerHTML = `<div class="ndp-tip">转码失败${t && t.error ? '：' + escapeHtml(t.error) : ''}<br>
            <span>可以点右下 ↗ 用系统播放器看</span></div>`;
    }
}

function ndPlayOpenRaw() {
    const s = ndPlay.slides[ndPlay.index];
    if (s && s.rel) window.open(mediaUrl(s.rel), '_blank');
}

function ndPlayGoAuthor() {
    if (ndPlay.uid) openAuthor(ndPlay.uid);
}

// 键盘 ← → 连翻：只在这个页面生效；在输入框里打字时不抢方向键
document.addEventListener('keydown', (e) => {
    if (!ndPlay.active || state.view !== 'new') return;
    const t = e.target || {};
    if (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable) return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); ndPlayStep(-1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); ndPlayStep(1); }
});

// 只重排布局，**不重置播放状态、不丢滚动位置**（宽度变化专用）。
// 不能直接调 renderNewDownloads：它会 ndPlayReset()，一收侧边栏正在播的视频就没了。
function ndRelayout() {
    const body = document.getElementById('newBody');
    const groups = newDlState.rendered;
    if (!body || !groups || !groups.length) return;
    // 视图藏着（clientWidth = 0）就别重排：这时量出来的宽度是假的，
    // 排出来会是「一卡一整行」，返回时正好被用户看见（见 ndLayout 里的说明）
    if (!body.clientWidth) return;
    const lay = ndLayout();
    const scrollTop = body.scrollTop;          // 先记住滚到哪儿
    // 正在播的那张卡的 (gi, ii)，重建后重新点亮
    const act = body.querySelector('.nd-card.active');
    const actGi = act ? Number(act.dataset.gi) : -1;
    const actIi = act ? Number(act.dataset.ii) : -1;
    // **锚点**：记住当前视口顶部那条边落在哪个气泡上。
    // 只记 scrollTop 不够——重排后列数变了、总高度也变了，同一个 scrollTop
    // 指向的位置完全不同（实测 400 被压成 104，看起来像滚回了上面）。
    let anchorGi = -1;
    let anchorOffset = 0;
    const bodyTop = body.getBoundingClientRect().top;
    const gs = [].slice.call(body.querySelectorAll('.nd-group'));
    for (let i = 0; i < gs.length; i++) {
        const r = gs[i].getBoundingClientRect();
        if (r.bottom > bodyTop + 1) {           // 第一个还没滚出视口的气泡
            anchorGi = i;
            anchorOffset = r.top - bodyTop;
            break;
        }
    }
    body.style.setProperty('--nd-card-w', lay.cardW.toFixed(2) + 'px');
    body.innerHTML = groups
        .map((g, gi) => {
            const box = ndGroupBox(g.items.length, lay);
            return `<div class="nd-group${box.full ? ' full' : ''}" data-cols="${box.cols}" style="--nd-n:${box.cols}${box.full ? '' : ';--nd-w:' + box.w.toFixed(2) + 'px'}">
                ${ndAuthorHead(g, gi)}
                <div class="nd-grid">${g.items.map((it, ii) => ndCard(it, g.nickname, gi, ii)).join('')}</div>
            </div>`;
        })
        .join('')
        + ndFooterHtml();
    if (actGi >= 0) {
        const back = body.querySelector(`.nd-card[data-gi="${actGi}"][data-ii="${actIi}"]`);
        if (back) back.classList.add('active');
    }
    // 按锚点还原滚动：让那个气泡仍停在视口顶部同样的相对位置
    const gs2 = [].slice.call(body.querySelectorAll('.nd-group'));
    if (anchorGi >= 0 && gs2[anchorGi]) {
        const r2 = gs2[anchorGi].getBoundingClientRect();
        body.scrollTop += (r2.top - body.getBoundingClientRect().top) - anchorOffset;
    } else {
        body.scrollTop = scrollTop;
    }
    observeThumbs(body);
    ndTrimChips(body);
    ndArmMore();          // 重排后列表可能变得不满一屏，顺手判一次要不要续页
}

function renderNewDownloads(opts) {
    // keepScroll：继续加载下一页时用 —— 不重置右栏播放（正在播的视频不该被停掉），
    // 也不把左列表的滚动位置弹回顶部
    const keepScroll = !!(opts && opts.keepScroll);
    const body = document.getElementById('newBody');
    const sub = document.getElementById('newViewSub');
    const badge = document.getElementById('newDlBadge');
    if (!body) return;
    const d = newDlState.data || { groups: [], total_files: 0, has_more: false };

    // 续页重绘前先记下「滚到哪儿」和「正在播的那张卡」（作者 uid + 日期/描述）：
    // 新一页并进来后作品条数变多，同一作者块里的序号（ii）可能整体后移，
    // 旧的 gi/ii 不能直接拿来点亮
    const scrollTop = keepScroll ? body.scrollTop : 0;
    let actUid = '', actKey = '';
    if (keepScroll) {
        const c0 = body.querySelector('.nd-card.active');
        if (c0) {
            const g0 = (newDlState.rendered || [])[Number(c0.dataset.gi)];
            const it0 = g0 && g0.items[Number(c0.dataset.ii)];
            if (g0 && it0) {
                actUid = g0.uid;
                actKey = (it0.date || '') + '\u0000' + (it0.desc || '');
            }
        }
    }
    // 数据换了（切时间范围 / 点刷新）→ 旧的播放状态作废：列表都重排了，索引对不上
    if (!keepScroll && ndPlay.active) ndPlayReset();

    // 组内按作品发布时间从新到旧（「一个作者有多份就按顺序排好」）；
    // 同一天的用下载时间兜底，保证顺序稳定不会每次刷新乱跳
    const groups = (d.groups || []).map((g) => {
        const items = (g.items || []).slice().sort((a, b) =>
            String(b.date || '').localeCompare(String(a.date || '')) || (b.mtime || 0) - (a.mtime || 0));
        return { uid: g.uid, nickname: g.nickname, count: g.count, items,
                 level: g.level || '', freq: g.freq || '', status: g.status || '', tags: g.tags || [] };
    });
    const total = groups.reduce((n, g) => n + g.items.length, 0);
    // 灯箱要从这里拿「这个作者这次的全部作品」，所以渲染结果留一份
    newDlState.rendered = groups;

    if (sub) {
        // 卡片上那个日期是**作品发布时间**，而这一页是按**下载时间**切的，
        // 所以「今天」里出现 2025 年的作品是正常的 —— 说明文字写清楚，别让人以为筛错了。
        const filtered = ndFilterActive() ? ' · 已按作者筛选' : '';
        sub.textContent = total
            ? total + ' 个作品' + (d.has_more ? '（往下滚继续加载）' : '')
                + ' · ' + (d.total_files || 0) + ' 个文件 · ' + groups.length + ' 位作者' + filtered
            : (newDlState.watchOnly
                ? '追更还没下到过作品 —— 等它发现新作品就会自动出现在这里'
                : (ndFilterActive()
                    ? '筛选后这个范围里没有作品，把条件放宽试试'
                    : '这个范围内没有新下载'));
    }
    if (badge && !newDlState.watchOnly) {
        // 追更模式下别动侧栏「新下载」的角标 —— 那个数字是「新下载」自己的口径，
        // 被追更的条数盖掉会让人以为下载器下了 277 个（2026-11-08 实测踩到）
        badge.textContent = total || 0;
        badge.classList.toggle('zero', !total);
    }

    if (!total) {
        body.innerHTML = '<div class="nd-empty">' + (newDlState.range === 'today'
            ? '今天还没有新下载。<br>刚跑完 DouK 如果这里是空的，点右上角 <b>🔄 刷新</b>——服务端要等下载安静 5 秒后才会扫到新文件。'
            : '这个范围里没有新下载，换个时间范围看看。') + '</div>';
        return;
    }

    // 一位作者 = 一个块（.nd-group）：块上方一行表头写名字 + 等级/频率/状态/标签，
    // 下面是他的作品网格。用户 2026-10-31 标注要求「同一个作者一行就一个名字放在视频上方，
    // 同一个作者要连起来同时跟着分类、标签、等级哪些」——所以不再把所有卡摊平成一片。
    // 同一作者的作品在块内按发布时间倒序；块与块之间按「组内最新一条」倒序（顺序由后端给定）。
    // 一位作者 = 一个「大气泡」（.nd-group）：他的全部作品包在一起，
    // 气泡内优先横向排满、超出换行（用户 2026-10-31 20:36 要求）。
    // 列数与卡片宽度按**当前可用宽度**算（ndLayout），所以收起侧边栏后
    // 会自动多排几张、把右边的空白填满（用户 20:48 要求）。
    const lay = ndLayout();
    body.style.setProperty('--nd-card-w', lay.cardW.toFixed(2) + 'px');
    body.innerHTML = groups
        .map((g, gi) => {
            const box = ndGroupBox(g.items.length, lay);
            return `<div class="nd-group${box.full ? ' full' : ''}" data-cols="${box.cols}" style="--nd-n:${box.cols}${box.full ? '' : ';--nd-w:' + box.w.toFixed(2) + 'px'}">
                ${ndAuthorHead(g, gi)}
                <div class="nd-grid">${g.items.map((it, ii) => ndCard(it, g.nickname, gi, ii)).join('')}</div>
            </div>`;
        })
        .join('')
        + ndFooterHtml();

    observeThumbs(body);
    // 表头徽章「放不下就收成 ＋N」要在 DOM 落地、且容器有宽度之后才量得准
    ndTrimChips(body);
    ndWatchWidth();
    if (keepScroll) {
        body.scrollTop = scrollTop;
        if (actUid) {
            const gi = groups.findIndex((g) => g.uid === actUid);
            if (gi >= 0) {
                const ii = groups[gi].items.findIndex(
                    (it) => (it.date || '') + '\u0000' + (it.desc || '') === actKey);
                const card = ii >= 0
                    ? body.querySelector(`.nd-card[data-gi="${gi}"][data-ii="${ii}"]`) : null;
                if (card) card.classList.add('active');
            }
        }
    }
    // 首屏/续页落地后都判一次：内容没铺满一屏时要能自己接着往下取
    ndArmMore();
}

// ---------- 宽度变化时自动重排（收起侧边栏 / 拖窗口 / 缩放）----------
// 用户 2026-10-31 20:48：「点击最左边的那个把左边的栏收起来按钮还会动态调整
// 页面展示效果」。收栏后可用宽度变大，气泡要多排几张把右边填满，所以列数得重算。
//
// 为什么用 ResizeObserver 而不是只听 window.resize：
// 收起侧边栏**不触发 window.resize**（窗口尺寸没变，变的是内部布局），
// 只听 resize 会一点反应都没有。ResizeObserver 盯的是容器本身，最准。
let ndResizeObs = null;
let ndResizeTimer = 0;

function ndWatchWidth() {
    const list = document.getElementById('newBody');
    if (!list || typeof ResizeObserver !== 'function') return;
    if (ndResizeObs) ndResizeObs.disconnect();
    let lastW = list.clientWidth;
    ndResizeObs = new ResizeObserver(() => {
        const w = list.clientWidth;
        // 0 = 视图被藏起来了（进作者详情/切页），不是真的变窄。
        // 这时候**不能**记进 lastW、也不能排重排：否则会按 0 宽算出一列布局，
        // 返回时先看到「一张卡占满整行」的巨图（2026-11-02 用户反馈卡帧）。
        // 真显示出来时 w 会和 lastW 不一样，那次再重排就行。
        if (w <= 0) return;
        // 宽度没实质变化就跳过，否则滚动条出现/消失也会触发一轮重排，白白闪一下
        if (Math.abs(w - lastW) < 8) return;
        lastW = w;
        // 重排要重建 DOM，防抖 120ms，避免拖窗口时一路狂重建
        clearTimeout(ndResizeTimer);
        ndResizeTimer = setTimeout(() => {
            if (state.view === 'new' && newDlState.rendered) {
                ndRelayout();              // 只重排，不重置播放状态
            }
        }, 120);
    });
    ndResizeObs.observe(list);
}

// ============ 文件管理（2026-10-30 c83）============
// 两块：① 文件夹改名（让磁盘上的文件夹跟着「你标注的昵称」走）
//      ② 重复文件清理（改名合并时挑出来的重复副本，按作者删 / 全删）
// 为什么放网页：改名要连带改 data.json 里的作品路径、删文件必须有边界校验，
// 这些都只能由服务端做，页面只负责选和确认。
let filesState = { rename: [], dup: null, loaded: false };

async function openFilesView() {
    forceCloseDetail();
    rememberViewScroll();
    state.view = 'files';
    state.returnView = 'files';
    hideAllViews();
    const view = document.getElementById('filesView');
    if (view) view.classList.remove('hidden');
    animateViewEnter('filesView', 'neutral');
    clearSideActive();
    document.querySelectorAll('#featureList .side-item').forEach((i) => i.classList.toggle('active', i.dataset.action === 'files'));
    const eb = document.getElementById('exportBtn');
    if (eb) eb.classList.add('hidden');
    restoreViewScroll('files');
    await refreshFilesView();
}

async function refreshFilesView() {
    const body = document.getElementById('filesBody');
    if (!body) return;
    if (!filesState.loaded) body.innerHTML = '<div class="loading">读取中…</div>';
    try {
        const [d, rc] = await Promise.all([
            fetch('/api/files/overview').then((r) => r.json()),
            fetch('/api/recycle/list', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
                .then((r) => r.json()).catch(() => ({ batches: [] })),
        ]);
        filesState.rename = d.rename || [];
        filesState.dup = d.dup || { items: [], total_files: 0, total_bytes: 0 };
        filesState.recycle = (rc && rc.batches) || [];
        filesState.recycleSize = (rc && rc.total_size) || 0;
        filesState.loaded = true;
        renderFilesView();
    } catch (e) {
        body.innerHTML = '<div class="loading">读取失败：' + escapeHtml(e.message) + '</div>';
    }
}

function fmtBytes(n) {
    n = Number(n) || 0;
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return (i === 0 ? String(n) : n.toFixed(n >= 100 ? 0 : 1)) + ' ' + units[i];
}

function renderFilesView() {
    const body = document.getElementById('filesBody');
    const sub = document.getElementById('filesViewSub');
    if (!body) return;
    const rn = filesState.rename || [];
    const dup = filesState.dup || { items: [], total_files: 0, total_bytes: 0 };
    if (sub) sub.textContent = rn.length || dup.items.length
        ? '待改名 ' + rn.length + ' 位 · 重复文件 ' + dup.total_files + ' 个（' + fmtBytes(dup.total_bytes) + '）'
        : '都很干净';

    const renameRows = rn.length ? rn.map((a) => `
        <div class="fm-row" data-uid="${escapeHtml(a.uid)}" data-text="${escapeHtml((a.nickname + ' ' + a.current).toLowerCase())}">
            <div class="fm-main">
                <div class="fm-name">${escapeHtml(a.nickname)}${a.final_nick && a.final_nick !== a.nickname ? ' <span class="fm-note">实际用：' + escapeHtml(a.final_nick) + '（结尾的点 Windows 存不了）</span>' : ''}${a.alias ? ' <span class="fm-note">备注：' + escapeHtml(a.alias) + '</span>' : ''}</div>
                <div class="fm-cur">现在：${escapeHtml(a.folder)}</div>
            </div>
            <input class="fm-input" id="rn_${escapeHtml(a.uid)}" value="${escapeHtml(a.final_nick || a.nickname)}" spellcheck="false">
            <span class="fm-num">${a.works} 个作品</span>
            <button class="btn btn-mini" onclick="renameFolderTo('${escapeHtml(a.uid)}')"
                    ${a.conflict || a.problem ? 'disabled' : ''}
                    title="${escapeHtml(a.conflict ? '已经有同名文件夹' : (a.problem || '把文件夹改成这个名字'))}">改名</button>
        </div>`).join('')
        : '<div class="fm-empty">✓ 所有文件夹名都和你标注的昵称一致，没有要改的。</div>';

    const dupRows = dup.items.length ? dup.items.map((it, i) => `
        <div class="fm-row" data-name="${escapeHtml(it.name)}">
            <div class="fm-main">
                <div class="fm-name">${escapeHtml(it.old || it.name)} <span class="fm-note">旧名</span></div>
                <div class="fm-cur">${escapeHtml(it.name)}</div>
            </div>
            <span class="fm-num">${it.files} 份 / ${fmtBytes(it.bytes)}</span>
            <button class="btn btn-mini" id="dbtn_${i}" onclick="toggleDupGroup(${i})">逐份比较</button>
            <button class="btn btn-mini fm-del" onclick="deleteDup('${escapeHtml(it.name)}', ${it.files}, ${it.bytes})">整组删</button>
        </div>
        <div class="fm-panel hidden" id="fp_${i}" data-name="${escapeHtml(it.name)}"></div>`).join('')
        : '<div class="fm-empty">✓ 没有待清理的重复文件。</div>';

    const runnable = rn.filter((a) => !a.conflict && !a.problem).length;

    body.innerHTML = `
        <div class="settings-section">
            <div class="settings-title">文件夹改名：让文件夹跟着你标注的昵称走
                <span class="fm-count">${rn.length} 位待改</span></div>
            <div class="settings-desc">这些作者的<b>文件夹名</b>和<b>你标注的昵称</b>不一致（改过名但没再下载，文件夹还挂着旧名）。
                改完会同步索引，网页里照常能播，不用重新扫描。结尾带点/空格的名字 Windows 存不了，这些行是灰的。</div>
            <div class="fm-tools">
                <input id="fmFilter" class="fm-input fm-filter" placeholder="搜昵称 / 文件夹名…" oninput="filterFileRows()">
                <span class="fm-num">可改 ${runnable} 位</span>
                <button class="btn btn-mini" onclick="renameAllCandidates()" ${runnable ? '' : 'disabled'}>全部改名</button>
            </div>
            <div class="fm-list" id="fmRenameList">${renameRows}</div>
        </div>
        <div class="settings-section">
            <div class="dm-head">
                <div class="dm-head-main">
                    <div class="settings-title">重复作品清理
                        <span class="fm-count">${dup.items.length} 组 / ${dup.total_files} 份 / ${fmtBytes(dup.total_bytes)}</span></div>
                    <div class="settings-desc">每条作品都有两版：<b>升级后新名字的那份</b>（网页正在播它）和<b>旧名字的副本</b>（在隔离区）。
                        <b>两版完全相同的不用你管</b>——按「新名字那份」自动保留、把旧副本删掉；
                        只有<b>大小不同</b>的才会列出来，展开后并排给你挑，点封面就能直接播放比对。</div>
                </div>
                <button class="btn dm-apply" id="dupApplyBtn" disabled onclick="applyDupChoices(this)">删除重复</button>
            </div>
            <div class="dm-legend">
                <span class="dm-chip ok">✓ 保留</span>
                <span class="dm-chip dead">✕ 删除</span>
                <span class="dm-tip">圆圈选「保留这份」；点封面可以直接播视频。默认一律保留<b>新名字那份</b>（最稳）。</span>
            </div>
            <div class="fm-list" id="dupList">${dupRows}</div>
            ${dup.items.length ? `
            <div class="fm-foot">
                <span class="fm-num">共 ${dup.items.length} 组 / ${dup.total_files} 个文件 / ${fmtBytes(dup.total_bytes)}</span>
                <button class="btn btn-mini fm-del" onclick="deleteAllDups(${dup.total_files}, ${dup.total_bytes})">全部删除（不比较）</button>
            </div>` : ''}
        </div>
        ${recycleSectionHtml()}`;
    filesState.dupGroups = {};
    filesState.picks = {};
    filesState.openDup = filesState.openDup || {};
    Object.keys(filesState.openDup).forEach((name) => {
        const row = document.querySelector('#dupList .fm-row[data-name="' + cssName(name) + '"]');
        if (row) row.querySelector('button').click();
    });
    updateDupApplyBtn();
}

// ===== 回收站：删掉的作品都在这儿，可以整批恢复或彻底删 =====
// 2026-11-02：每批可点开看明细（删的到底是哪几条作品），展开状态和已拉到的明细
// 都放在 filesState.openBatch 里，重绘后还在，不用重复请求。
function recycleSectionHtml() {
    const list = filesState.recycle || [];
    const total = filesState.recycleSize || 0;
    filesState.openBatch = filesState.openBatch || {};
    const rows = list.length ? list.map((b) => {
        const st = filesState.openBatch[b.id] || {};
        const open = !!st.open;
        return `
        <div class="fm-row rb-row" data-batch="${escapeHtml(b.id)}">
            <div class="fm-main" onclick="toggleRecycleBatch('${escapeHtml(b.id)}')" title="点开看看这一批里有哪些作品">
                <div class="fm-name">
                    <span class="rb-chev${open ? ' open' : ''}">›</span>
                    ${escapeHtml(b.nickname || b.uid)}${b.exists ? '' : ' <span class="fm-note">文件已不在磁盘上</span>'}
                </div>
                <div class="fm-cur">${escapeHtml(b.time)} · ${b.works} 个作品 / ${b.files} 个文件 · ${fmtBytes(b.size)}</div>
            </div>
            <button class="btn btn-mini" onclick="restoreRecycleBatch('${escapeHtml(b.id)}')" ${b.exists ? '' : 'disabled'}
                    title="把这一批文件搬回原来的位置，网页里也会重新出现">恢复</button>
            <button class="btn btn-mini fm-del" onclick="purgeRecycleBatch('${escapeHtml(b.id)}', ${b.files}, ${b.size})"
                    title="真删掉，不能恢复">彻底删除</button>
            <div class="rb-sub${open ? '' : ' hidden'}" id="rb-${escapeHtml(b.id)}">${st.open ? recycleSubHtml(b.id) : ''}</div>
        </div>`;
    }).join('')
        : '<div class="fm-empty">✓ 回收站是空的。</div>';

    return `
        <div class="settings-section">
            <div class="settings-title rb-title">回收站
                <span class="fm-count">${list.length} 批 / ${fmtBytes(total)}</span>
                ${list.length ? `<button class="btn btn-mini fm-del rb-clear-all" onclick="purgeAllRecycle(${list.length}, ${total})"
                        title="把回收站里所有批次一起彻底删除，腾出空间（不可恢复）">一键清空</button>` : ''}
            </div>
            <div class="settings-desc">网页里删掉的作品不会真的消失：文件整份搬到 <b>browser\\recycle-bin\\</b>，
                作品从列表里摘掉。<b>点作者名那一行可以展开看具体删了哪些作品</b>；
                想反悔点「恢复」就搬回原位，和没删过一样；确实不要了再点「彻底删除」腾空间。</div>
            <div class="fm-list">${rows}</div>
        </div>`;
}

// 展开后那一块：作品清单（日期 / 描述 / 文件数 / 体积）
function recycleSubHtml(id) {
    const st = (filesState.openBatch || {})[id] || {};
    if (st.loading) return '<div class="rb-loading">读取中…</div>';
    if (st.error) return '<div class="rb-loading">读取失败：' + escapeHtml(st.error) + '</div>';
    const d = st.detail;
    if (!d) return '<div class="rb-loading">读取中…</div>';
    const works = d.works || [];
    if (!works.length) return '<div class="rb-loading">这一批没有作品记录。</div>';
    const rows = works.map((w) => `
        <div class="rb-work">
            <span class="rb-wdate">${escapeHtml(w.date || '—')}</span>
            <span class="rb-wdesc" title="${escapeHtml(w.desc || '')}">${escapeHtml(w.desc || '(无描述)')}</span>
            <span class="rb-wmeta">${w.file_count} 个文件${w.size ? ' · ' + fmtBytes(w.size) : ''}</span>
        </div>`).join('');
    return `<div class="rb-sub-head">这一批共 ${works.length} 个作品${d.size ? ' / ' + fmtBytes(d.size) : ''}</div>${rows}`;
}

async function toggleRecycleBatch(id) {
    filesState.openBatch = filesState.openBatch || {};
    const st = filesState.openBatch[id] || (filesState.openBatch[id] = {});
    const box = document.getElementById('rb-' + id);
    if (st.open) {                       // 收起
        st.open = false;
        if (box) box.classList.add('hidden');
        const ch = document.querySelector('.rb-row[data-batch="' + cssName(id) + '"] .rb-chev');
        if (ch) ch.classList.remove('open');
        return;
    }
    st.open = true;
    const ch = document.querySelector('.rb-row[data-batch="' + cssName(id) + '"] .rb-chev');
    if (ch) ch.classList.add('open');
    if (box) box.classList.remove('hidden');
    if (st.detail) { if (box) box.innerHTML = recycleSubHtml(id); return; }

    st.loading = true;
    if (box) box.innerHTML = recycleSubHtml(id);
    try {
        const r = await fetch('/api/recycle/detail', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: id }),
        }).then((x) => x.json());
        st.loading = false;
        if (!r.ok) { st.error = r.error || '读取失败'; }
        else { st.detail = r; }
    } catch (e) {
        st.loading = false;
        st.error = e.message;
    }
    if (box) box.innerHTML = recycleSubHtml(id);
}

async function restoreRecycleBatch(id) {
    showToast('正在恢复…');
    try {
        const r = await fetch('/api/recycle/restore', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: id }),
        }).then((x) => x.json());
        if (!r.ok) { showToast(r.error || '恢复失败'); return; }
        showToast('已恢复 ' + r.works + ' 个作品、' + r.files + ' 个文件');
        await refreshFilesView();
    } catch (e) {
        showToast('恢复失败：' + e.message);
    }
}

async function purgeRecycleBatch(id, files, size) {
    const ok = window.confirm('彻底删除这一批？\n\n· ' + files + ' 个文件（' + fmtBytes(size) + '）从硬盘上真删掉\n· 不能恢复\n\n确定吗？');
    if (!ok) return;
    try {
        const r = await fetch('/api/recycle/purge', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: id }),
        }).then((x) => x.json());
        if (!r.ok) { showToast(r.error || '删除失败'); return; }
        showToast('已彻底删除，释放 ' + fmtBytes(r.freed || 0));
        await refreshFilesView();
    } catch (e) {
        showToast('删除失败：' + e.message);
    }
}

// 一键清空：所有批次一起真删。真要删的量大（可能几十 GB），
// 先把批次数和体积报清楚，二次确认后才动手；删的过程中按钮置灰防连点。
async function purgeAllRecycle(batches, size) {
    const ok = window.confirm(
        '一键清空回收站？\n\n'
        + '· ' + batches + ' 批全部彻底删除（' + fmtBytes(size) + '）\n'
        + '· 文件从硬盘上真删掉，不能恢复\n'
        + '· 只想留几批的话，先点「恢复」，再清空\n\n确定清空吗？'
    );
    if (!ok) return;
    const btn = document.querySelector('.rb-clear-all');
    if (btn) { btn.disabled = true; btn.textContent = '正在删除…'; }
    showToast('正在彻底删除 ' + batches + ' 批，文件多的话要等一会儿…');
    try {
        const r = await fetch('/api/recycle/purge_all', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
        }).then((x) => x.json());
        if (!r.ok) { showToast(r.error || '清空失败'); return; }
        showToast('已清空 ' + r.batches + ' 批、' + r.works + ' 个作品，释放 ' + fmtBytes(r.freed || 0));
        if (r.failed && r.failed.length) {
            setTimeout(() => showToast(r.failed.length + ' 批没删掉（文件被占用？），已留在列表里'), 2400);
        }
        await refreshFilesView();
    } catch (e) {
        showToast('清空失败：' + e.message);
    } finally {
        const b2 = document.querySelector('.rb-clear-all');
        if (b2) { b2.disabled = false; b2.textContent = '一键清空'; }
    }
}

// 把组名安全地塞进 CSS 选择器（名字里可能有引号/反斜杠）
function cssName(s) {
    return String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

function filterFileRows() {
    const box = document.getElementById('fmFilter');
    const q = (box && box.value || '').trim().toLowerCase();
    document.querySelectorAll('#fmRenameList .fm-row').forEach((row) => {
        const t = row.dataset.text || '';
        row.classList.toggle('hidden', !!q && t.indexOf(q) < 0);
    });
}

async function renameAllCandidates() {
    const list = (filesState.rename || []).filter((a) => !a.conflict && !a.problem);
    if (!list.length) return;
    const ok = window.confirm('把下面 ' + list.length + ' 位作者的文件夹，改成「你标注的昵称」？\n\n'
        + '会改名的文件夹：' + list.length + ' 个\n'
        + '只改文件夹名 + 同步索引，不动任何作品文件。\n\n确定要改吗？');
    if (!ok) return;
    await postJson('/api/files/rename_batch', { all: true }, (r) => {
        showToast(r.message || ('已改名 ' + r.renamed + ' 个'));
        if (r.failed && r.failed.length) {
            setTimeout(() => showToast('有 ' + r.failed.length + ' 个没改成（名字非法/重名），已跳过'), 2200);
        }
        if (r.warning) setTimeout(() => showToast(r.warning), 4400);
    });
}

async function postJson(url, payload, onOk) {
    try {
        const r = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        }).then((x) => x.json());
        if (!r.ok) { showToast(r.error || '操作失败'); return; }
        if (onOk) onOk(r);
        await refreshFilesView();
    } catch (e) {
        showToast('操作失败：' + e.message);
    }
}

async function renameFolderTo(uid) {
    const input = document.getElementById('rn_' + uid);
    if (!input) return;
    const nick = (input.value || '').trim();
    if (!nick) { showToast('名字不能为空'); return; }
    const row = input.closest('.fm-row');
    if (row) row.classList.add('busy');
    try {
        const r = await fetch('/api/files/rename', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ uid: uid, nickname: nick }),
        }).then((x) => x.json());
        if (!r.ok) { showToast(r.error || '改名失败'); return; }
        showToast(r.message || '已改名');
        if (r.warning) setTimeout(() => showToast(r.warning), 2100);
        await refreshFilesView();
    } catch (e) {
        showToast('改名失败：' + e.message);
    } finally {
        if (row) row.classList.remove('busy');
    }
}

async function postDupDelete(payload) {
    try {
        const r = await fetch('/api/files/dup_delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        }).then((x) => x.json());
        if (!r.ok) { showToast(r.error || '删除失败'); return; }
        showToast('已删除 ' + r.removed + ' 组，释放 ' + fmtBytes(r.freed));
        if (r.failed && r.failed.length) showToast('有 ' + r.failed.length + ' 组没删掉（文件被占用？）');
        await refreshFilesView();
    } catch (e) {
        showToast('删除失败：' + e.message);
    }
}

async function toggleDupGroup(i) {
    const panel = document.getElementById('fp_' + i);
    if (!panel) return;
    const btn = document.getElementById('dbtn_' + i);
    const name = panel.dataset.name || '';
    filesState.openDup = filesState.openDup || {};
    if (!panel.classList.contains('hidden')) {
        panel.classList.add('hidden');
        if (btn) btn.textContent = '逐份比较';
        delete filesState.openDup[name];
        updateDupApplyBtn();
        return;
    }
    panel.classList.remove('hidden');
    if (btn) btn.textContent = '收起';
    filesState.openDup[name] = true;
    filesState.dupGroups = filesState.dupGroups || {};
    if (filesState.dupGroups[name]) {
        renderDupGroups(panel, filesState.dupGroups[name]);
        updateDupApplyBtn();
        return;
    }
    panel.innerHTML = '<div class="loading">读取中…</div>';
    try {
        const d = await fetch('/api/files/dup_group?name=' + encodeURIComponent(name)).then((r) => r.json());
        if (!d.ok) {
            panel.innerHTML = '<div class="fm-empty">' + escapeHtml(d.error || '读取失败') + '</div>';
            return;
        }
        filesState.dupGroups[name] = d;
        renderDupGroups(panel, d);
        updateDupApplyBtn();
    } catch (e) {
        panel.innerHTML = '<div class="fm-empty">读取失败：' + escapeHtml(e.message) + '</div>';
    }
}

// 每条作品一排气泡：左边是网页正在用的那份，右边是隔离区副本（反之也可能多份）。
// 每个气泡下面一个圆圈 = 「保留这份」，没选中的显红并标「删除」。
const DUP_PAGE = 40;      // 一次先渲染 40 条，其余按需追加

// 作品键归一成字符串，做「用户点了哪份」的内存映射
function dupKeyStr(key) { return (key || []).join('\u0001'); }

function dupPickOf(name) {
    filesState.picks = filesState.picks || {};
    if (!filesState.picks[name]) filesState.picks[name] = {};
    return filesState.picks[name];
}

// 这条作品最终保留哪份：用户点过就按用户的，没点过一律「保留新名字那份（在用）」
function dupKeepOf(name, g) {
    const p = dupPickOf(name)[dupKeyStr(g.key)];
    return p || 'current';
}

// 一条作品 = 一排气泡（在用那份 + 隔离区副本），下方各一个圆圈选「保留这份」
function dupGroupHTML(name, g, gi, pid) {
    // 没有「在用版本」的组先跳过：隔离区这份就是唯一一份，删了就没了
    const skip = !g.has_current;
    const picked = dupPickOf(name)[dupKeyStr(g.key)];
    const bubbles = g.versions.map((v, vi) => {
        const isCur = v.kind === 'current';
        let keep;
        if (skip) keep = true;
        else if (!picked) keep = isCur;
        else if (picked === 'current') keep = isCur;
        else keep = v.file === picked;
        const playUrl = '/' + mediaUrl(v.rel);
        const thumb = v.is_image ? playUrl : '/thumb?path=' + encodeURIComponent(v.rel);
        return `<div class="dm-bubble ${keep ? 'is-keep' : 'is-dead'}" data-ver="${vi}" data-kind="${v.kind}">
            <div class="dm-card">
                <div class="dm-media" data-url="${escapeHtml(playUrl)}" data-video="${v.is_image ? '0' : '1'}"
                     onclick="playDupMedia(this)" title="${v.is_image ? '点一下放大看全图' : '点一下就在这里播放'}">
                    <img src="${thumb}" loading="lazy" alt="" onerror="this.classList.add('broken')">
                    <span class="dm-play">${v.is_image ? '⤢' : '▶'}</span>
                    <span class="dm-flag">${keep ? '✓ 保留' : '✕ 删除'}</span>
                </div>
                <div class="dm-bub-body">
                    <div class="dm-bub-where">${isCur ? '升级后新名字的那份 · 网页正在播它' : '旧名字的副本 · 在隔离区'}</div>
                    <div class="dm-bub-size">${fmtBytes(v.size)}</div>
                    <div class="dm-bub-name" title="${escapeHtml(v.file)}">${escapeHtml(v.file)}</div>
                </div>
            </div>
            <label class="dm-pick">
                <input type="radio" class="dm-radio" name="${pid}_${gi}" value="${escapeHtml(v.file)}"
                       data-kind="${v.kind}" ${keep ? 'checked' : ''} ${skip ? 'disabled' : ''} onchange="pickDupVer(this)">
                <span class="dm-dot"></span><span class="dm-mark-txt">${skip ? '唯一一份 · 不删' : (keep ? '已选：保留这份' : '点这里保留这份')}</span>
            </label>
        </div>`;
    }).join('');
    return `<div class="dm-group${skip ? ' dm-skip' : ''}" data-gi="${gi}" data-key="${escapeHtml(JSON.stringify(g.key))}">
        <div class="dm-group-head">
            <span class="dm-date">${escapeHtml(g.date || '')}</span>
            <span class="dm-desc" title="${escapeHtml(g.desc)}">${escapeHtml(g.desc || '(无描述)')}</span>
            <span class="dm-badge ${skip ? 'diff' : (g.identical ? 'same' : 'diff')}">${skip ? '没有在用版本 · 跳过' : (g.identical ? '两版完全相同' : '两版大小不同 · 挑一份')}</span>
            <span class="dm-need">${skip ? '不动' : (g.versions.length - 1) + ' 份将被删'}</span>
        </div>
        <div class="dm-bubbles">${bubbles}</div>
    </div>`;
}

// 点封面：视频就地播放，图片开新页看原图
// 记住点开前的封面结构，收起时还原
const DUP_MEDIA_ORIG = new WeakMap();

// 点封面：视频就地播放、图片就地放大看全图；再点一次还原。
// 都不弹新窗口——以前图片走 window.open，弹出来的白底大图被当成「一大块白边」。
function playDupMedia(el) {
    if (!el) return;
    const url = el.dataset.url || '';
    if (!url) return;

    // 图片：开一个深色遮罩的大图浮层。不要就地 contain——
    // 白底的图文作品撑满整块会看成「一大块白边」，浮层有深色底就没这个问题。
    if (el.dataset.video !== '1') { openDupImage(url); return; }

    // 视频：再点一次收起
    if (el.dataset.playing) {
        if (DUP_MEDIA_ORIG.has(el)) {
            el.innerHTML = DUP_MEDIA_ORIG.get(el);
            DUP_MEDIA_ORIG.delete(el);
        }
        el.dataset.playing = '';
        el.classList.remove('playing');
        el.title = '点一下就在这里播放';
        return;
    }
    // 用当前缩略图当 poster：视频还没解出第一帧时不会闪一块白
    const img = el.querySelector('img');
    const poster = img ? img.getAttribute('src') : '';
    DUP_MEDIA_ORIG.set(el, el.innerHTML);
    el.dataset.playing = '1';
    el.classList.add('playing');
    el.title = '再点一下收起';
    el.innerHTML = '<video class="dm-video" src="' + url + '"'
        + (poster ? ' poster="' + poster + '"' : '')
        + ' controls autoplay playsinline preload="auto"></video>';
}

// 大图查看浮层：深色遮罩 + 居中图片，点任意处关闭
function openDupImage(url) {
    let ov = document.getElementById('dmLightbox');
    if (!ov) {
        ov = document.createElement('div');
        ov.id = 'dmLightbox';
        ov.className = 'dm-lightbox';
        ov.innerHTML = '<img alt=""><span class="dm-lb-close">点任意处关闭</span>';
        ov.addEventListener('click', () => ov.classList.add('hidden'));
        document.body.appendChild(ov);
    }
    ov.querySelector('img').src = url;
    ov.classList.remove('hidden');
}

function dupMoreHTML(name, left) {
    return `<button class="btn btn-mini" onclick="showMoreDup(this)">再显示 ${Math.min(DUP_PAGE, left)} 条（还有 ${left} 条）</button>`;
}

// 需要给用户看的行：大小不同的 + 没有在用版本的；完全相同的默认折叠
function dupRowsFor(name) {
    const d = filesState.dupGroups && filesState.dupGroups[name];
    if (!d) return [];
    return (d.groups || []).map((g, i) => ({ g, i }))
        .filter((x) => filesState.showSame || !(x.g.identical && x.g.has_current));
}

function renderDupGroups(panel, d) {
    const all = d.groups || [];
    if (!all.length) {
        panel.innerHTML = '<div class="fm-empty">✓ 这一组已经清空了。</div>';
        return;
    }
    const name = d.name;
    filesState.dupPid = filesState.dupPid || {};
    if (!filesState.dupPid[name]) {
        filesState.dupPid[name] = 'dg' + Math.random().toString(36).slice(2, 7);
    }
    const pid = filesState.dupPid[name];
    const sameCount = all.filter((g) => g.identical && g.has_current).length;
    const diffCount = all.length - sameCount;
    const rows = dupRowsFor(name);
    const shown = rows.slice(0, DUP_PAGE);
    const left = rows.length - shown.length;
    panel.innerHTML = `<div class="dm-sum">
            <span class="dm-sum-txt">这个作者共 <b>${all.length}</b> 条重复：<b class="ok">${sameCount}</b> 条两版完全相同
                （自动保留<b>新名字</b>那份，不用你看），<b class="warn">${diffCount}</b> 条大小不同${diffCount ? '，下面请挑一份' : '，没有要挑的'}。
                保留后文件名统一改成现在最新的昵称「<b>${escapeHtml(d.nick || '')}</b>」。</span>
            <label class="dm-showsame"><input type="checkbox" ${filesState.showSame ? 'checked' : ''} onchange="toggleShowSame(this)"> 也显示完全相同的</label>
        </div>
        ${rows.length ? `<div class="dm-groups">${shown.map((x) => dupGroupHTML(name, x.g, x.i, pid)).join('')}</div>`
            : '<div class="fm-empty">✓ 全部两版完全相同，不用挑——点右上角「删除重复」就按新名字那份清掉。</div>'}
        ${left > 0 ? '<div class="dm-more">' + dupMoreHTML(name, left) + '</div>' : ''}`;
}

function toggleShowSame(box) {
    filesState.showSame = !!(box && box.checked);
    const panel = box.closest('.fm-panel');
    if (!panel) return;
    const name = panel.dataset.name || '';
    const d = filesState.dupGroups && filesState.dupGroups[name];
    if (d) renderDupGroups(panel, d);
    updateDupApplyBtn();
}

function showMoreDup(btn) {
    const panel = btn.closest('.fm-panel');
    if (!panel) return;
    const name = panel.dataset.name || '';
    const rows = dupRowsFor(name);
    if (!rows.length) return;
    const wrap = panel.querySelector('.dm-groups');
    const cur = wrap.querySelectorAll('.dm-group').length;
    const next = rows.slice(cur, cur + DUP_PAGE);
    wrap.insertAdjacentHTML('beforeend',
        next.map((x) => dupGroupHTML(name, x.g, x.i, filesState.dupPid[name])).join(''));
    const left = rows.length - (cur + next.length);
    const box = btn.parentElement;
    if (left > 0) box.innerHTML = dupMoreHTML(name, left);
    else box.remove();
    updateDupApplyBtn();
}

function pickDupVer(input) {
    const group = input.closest('.dm-group');
    if (!group) return;
    const panel = input.closest('.fm-panel');
    const name = (panel && panel.dataset.name) || '';
    let key = [];
    try { key = JSON.parse(group.dataset.key || '[]'); } catch (e) { key = []; }
    if (name && key.length === 4) {
        dupPickOf(name)[dupKeyStr(key)] = input.dataset.kind === 'current' ? 'current' : input.value;
    }
    group.querySelectorAll('.dm-bubble').forEach((b) => {
        const r = b.querySelector('.dm-radio');
        const on = !!(r && r.checked);
        b.classList.toggle('is-keep', on);
        b.classList.toggle('is-dead', !on);
        const flag = b.querySelector('.dm-flag');
        if (flag) flag.textContent = on ? '✓ 保留' : '✕ 删除';
        const t = b.querySelector('.dm-mark-txt');
        if (t) t.textContent = on ? '已选：保留这份' : '点这里保留这份';
    });
    updateDupApplyBtn();
}

// 收集决策：按**数据**遍历已展开的作者（不是 DOM），所以没渲染出来的
// 「完全相同」那批也会一起处理——默认保留新名字那份。
function dupDecisions() {
    const out = [];
    let del = 0;
    let restore = 0;
    let auto = 0;
    let untouched = 0;      // 大小不同但用户没挑过的
    document.querySelectorAll('#dupList .fm-panel').forEach((panel) => {
        if (panel.classList.contains('hidden')) return;
        const name = panel.dataset.name || '';
        const d = (filesState.dupGroups || {})[name];
        if (!d) return;
        (d.groups || []).forEach((g) => {
            if (!g.has_current) return;            // 唯一一份，不能删
            const explicit = !!dupPickOf(name)[dupKeyStr(g.key)];
            const keep = dupKeepOf(name, g);
            out.push({ group: name, key: g.key, keep });
            del += g.versions.length - 1;
            if (keep !== 'current') restore += 1;
            else if (g.identical) auto += 1;
            else if (!explicit) untouched += 1;
        });
    });
    return { decisions: out, del, restore, auto, untouched };
}

function updateDupApplyBtn() {
    const btn = document.getElementById('dupApplyBtn');
    if (!btn) return;
    const { decisions, del, restore, auto } = dupDecisions();
    btn.disabled = !decisions.length;
    btn.textContent = decisions.length ? '删除重复 · 删 ' + del + ' 份' : '删除重复';
    btn.title = decisions.length
        ? ('保留 ' + decisions.length + ' 份，删除 ' + del + ' 份'
            + (auto ? '（其中 ' + auto + ' 条完全相同的直接留新名字那份）' : '')
            + (restore ? '，' + restore + ' 条用旧副本覆盖' : ''))
        : '先点「逐份比较」展开一组';
}

async function applyDupChoices(btn) {
    const { decisions, del, restore, auto, untouched } = dupDecisions();
    if (!decisions.length) { showToast('先点「逐份比较」展开一组'); return; }
    const msg = '按当前选择处理 ' + decisions.length + ' 条作品：\n\n'
        + (auto ? '· ' + auto + ' 条两版完全相同 → 保留新名字那份，删掉旧副本\n' : '')
        + (untouched ? '· ' + untouched + ' 条大小不同但你还没挑 → 先按「保留新名字那份」处理（想看就展开挑一下）\n' : '')
        + '· 共删除 ' + del + ' 份重复文件\n'
        + (restore ? '· 其中 ' + restore + ' 条会把旧副本搬进来（文件名改成最新昵称）\n' : '')
        + '\n留那份的文件名统一改成现在最新的昵称。只处理已展开的作者，删除不可恢复，确定吗？';
    if (!window.confirm(msg)) return;
    if (btn) { btn.disabled = true; btn.textContent = '处理中…'; }
    try {
        const r = await fetch('/api/files/dup_resolve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ decisions: decisions }),
        }).then((x) => x.json());
        if (!r.ok) {
            showToast(r.error || '处理失败');
            filesState.dupGroups = {};
            filesState.picks = {};
            await refreshFilesView();       // 可能已经处理了一部分，刷新看真实状态
            return;
        }
        let t = '已删除 ' + r.deleted + ' 份，释放 ' + fmtBytes(r.freed);
        if (r.renamed) t += '；' + r.renamed + ' 个文件改成最新名字';
        if (r.restored) t += '；' + r.restored + ' 条换成旧副本那份';
        showToast(t);
        if (r.warnings && r.warnings.length) {
            setTimeout(() => showToast(r.warnings[0]), 2600);
        }
        if (r.failed && r.failed.length) {
            setTimeout(() => showToast(r.failed.length + ' 条没处理成功：' + (r.failed[0].error || '')), 2400);
        }
        filesState.dupGroups = {};
        filesState.picks = {};
        await refreshFilesView();
    } catch (e) {
        // fetch 本身失败（服务停了 / 连接被掐断）：先说清楚，别只给个英文
        const tip = (e && e.name === 'TypeError')
            ? '请求没走通（服务可能已停止，或被系统保护拦下）。刷新页面看看已处理多少。'
            : ('处理失败：' + e.message);
        showToast(tip);
        try { await refreshFilesView(); } catch (_) { /* 忽略 */ }
    } finally {
        updateDupApplyBtn();
    }
}

function deleteDup(name, files, bytes) {
    const ok = window.confirm('删除这组重复文件？\n\n' + name + '\n' + files + ' 个文件 / ' + fmtBytes(bytes)
        + '\n\n只删隔离区里的副本，网页里正在用的那份不受影响。');
    if (!ok) return;
    if (filesState.openDup) delete filesState.openDup[name];
    postDupDelete({ names: [name] });
}

function deleteAllDups(files, bytes) {
    const ok = window.confirm('删除隔离区里【全部】重复文件？\n\n共 ' + files + ' 个文件 / ' + fmtBytes(bytes)
        + '\n\n只删隔离区里的副本，网页里正在用的那份不受影响。删完不可恢复。');
    if (ok) postDupDelete({ all: true });
}

// 收集当前表单里的值
function collectSettingsForm() {
    const body = document.getElementById('settingsBody');
    const disabled = [];
    body.querySelectorAll('input[data-status]').forEach((cb) => {
        if (cb.checked) disabled.push(cb.dataset.status);
    });
    return {
        disabled: disabled,
        data_dir: (document.getElementById('path_data_dir') || {}).value || '',
        ffmpeg: (document.getElementById('path_ffmpeg') || {}).value || '',
        downloader_dir: (document.getElementById('path_downloader_dir') || {}).value || '',
        fav_latest_mode: (document.getElementById('favLatestMode') || {}).value || 'none',
    };
}

// 判断当前表单是否和打开时的原始值有差异，据此决定「保存」亮不亮、要不要显示「取消」
function updateSettingsDirty() {
    const saveBtn = document.getElementById('settingsSaveBtn');
    const cancelBtn = document.getElementById('settingsCancelBtn');
    if (!saveBtn || !settingsBaseline) return;
    const cur = collectSettingsForm();
    const dirty = cur.data_dir !== settingsBaseline.data_dir
        || cur.ffmpeg !== settingsBaseline.ffmpeg
        || cur.downloader_dir !== settingsBaseline.downloader_dir
        || cur.fav_latest_mode !== settingsBaseline.fav_latest_mode
        || JSON.stringify(cur.disabled.sort()) !== JSON.stringify(settingsBaseline.disabled.slice().sort());
    saveBtn.disabled = !dirty;
    if (cancelBtn) cancelBtn.classList.toggle('hidden', !dirty);
}

// 点「修改」进入编辑态（显示「取消」，隐藏「修改」）
function togglePathEdit(key) {
    const input = document.getElementById('path_' + key);
    const row = input && input.closest('.settings-path-row');
    if (!input || !row) return;
    input.readOnly = false;
    input.dataset.prev = input.value;   // 记录进入编辑前的值，取消时还原
    input.focus();
    input.select();
    row.querySelector('[data-act="edit"]').classList.add('hidden');
    row.querySelector('[data-act="cancel"]').classList.remove('hidden');
    updateSettingsDirty();
}

// 点「取消」放弃这一行的修改，还原成编辑前的值
function cancelPathEdit(key) {
    const input = document.getElementById('path_' + key);
    const row = input && input.closest('.settings-path-row');
    if (!input || !row) return;
    input.value = input.dataset.prev || '';
    input.readOnly = true;
    row.querySelector('[data-act="cancel"]').classList.add('hidden');
    row.querySelector('[data-act="edit"]').classList.remove('hidden');
    updateSettingsDirty();
}

// 取消所有未保存的改动：恢复成打开时的原始值
function cancelSettings() {
    if (!settingsBaseline) return;
    const body = document.getElementById('settingsBody');
    // 还原状态勾选
    body.querySelectorAll('input[data-status]').forEach((cb) => {
        cb.checked = settingsBaseline.disabled.indexOf(cb.dataset.status) >= 0;
    });
    // 还原三个路径输入
    const pathMap = {
        data_dir: settingsBaseline.data_dir,
        ffmpeg: settingsBaseline.ffmpeg,
        downloader_dir: settingsBaseline.downloader_dir,
    };
    Object.entries(pathMap).forEach(([k, v]) => {
        const input = document.getElementById('path_' + k);
        const row = input && input.closest('.settings-path-row');
        if (!input) return;
        input.value = v || '';
        input.readOnly = true;
        if (row) {
            row.querySelector('[data-act="cancel"]').classList.add('hidden');
            row.querySelector('[data-act="edit"]').classList.remove('hidden');
        }
    });
    // 还原「最新作品时间口径」
    const favSel = document.getElementById('favLatestMode');
    if (favSel) favSel.value = settingsBaseline.fav_latest_mode || 'none';
    updateSettingsDirty();
    showToast('已取消改动');
}

async function saveSettings() {
    const saveBtn = document.getElementById('settingsSaveBtn');
    if (saveBtn && saveBtn.disabled) return;   // 没改动，不该能点到
    const form = collectSettingsForm();
    const paths = {
        data_dir: form.data_dir,
        ffmpeg: form.ffmpeg,
        downloader_dir: form.downloader_dir,
    };
    try {
        const r = await fetch('/api/settings', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                update_freq_disabled_status: form.disabled,
                fav_latest_mode: form.fav_latest_mode,
                paths,
            }),
        });
        const d = await r.json();
        if (!d.ok) { showToast('保存失败：' + (d.error || '未知错误')); return; }
        // 同步前端内存，让「更新频率」的禁用立即生效
        state.updateFreqDisabledStatus = d.settings.update_freq_disabled_status || [];
        // 保存成功后，把原始值刷新成刚保存的，避免再次提示 dirty
        settingsBaseline = {
            disabled: form.disabled.slice(),
            data_dir: form.data_dir,
            ffmpeg: form.ffmpeg,
            downloader_dir: form.downloader_dir,
            fav_latest_mode: form.fav_latest_mode,
        };
        // 把所有还在编辑态的行收成只读
        document.querySelectorAll('.settings-path-row').forEach((row) => {
            const input = row.querySelector('input');
            if (input) input.readOnly = true;
            row.querySelector('[data-act="cancel"]') && row.querySelector('[data-act="cancel"]').classList.add('hidden');
            row.querySelector('[data-act="edit"]') && row.querySelector('[data-act="edit"]').classList.remove('hidden');
        });
        updateSettingsDirty();
        showToast('设置已保存');
    } catch (e) {
        showToast('保存失败：' + e.message);
    }
}

// ===== 工具 =====
function escapeHtml(str) {
    return String(str ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

// 标签统一排序：同一个作者身上的几个标签，存进 user_data 的先后顺序取决于当初点的顺序，
// 于是「纹身/猪」和「猪/纹身」同时存在（用户会以为标签不一样）。这里固定成和侧边栏标签云
// 一致的字符序，任何界面显示都同一套顺序。
function sortTags(list) {
    return (list || []).slice().sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

// 时间戳（秒）→「今天 / 昨天 / N 天前 / N 个月前」，用于「多久没下载过」
function daysAgoText(ts) {
    const n = Number(ts) || 0;
    if (!n) return '';
    const now = Date.now() / 1000;
    const d = Math.floor((now - n) / 86400);
    if (d <= 0) return '今天';
    if (d === 1) return '昨天';
    if (d < 30) return d + ' 天前';
    if (d < 365) return Math.floor(d / 30) + ' 个月前';
    return Math.floor(d / 365) + ' 年前';
}

// 时间戳 → 本地 YYYY-MM-DD HH:MM
function tsText(ts, withTime) {
    const n = Number(ts) || 0;
    if (!n) return '';
    const d = new Date(n * 1000);
    const p = (x) => String(x).padStart(2, '0');
    const day = d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
    return withTime ? day + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) : day;
}

function showToast(msg) {
    const toast = document.getElementById('toast');
    toast.textContent = msg;
    toast.classList.remove('hidden');
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => toast.classList.add('hidden'), 2000);
}

// ===== 启动 =====
// 兜底：任何脚本错误都在页面上显示出来，不再静默停在「加载中…」
function showFatal(msg) {
    const bar = document.getElementById('fatalBar');
    if (!bar) return;
    bar.textContent = '脚本出错：' + msg + '（按 Ctrl+F5 强制刷新可解决旧缓存问题）';
    bar.classList.remove('hidden');
    const stat = document.getElementById('statInfo');
    if (stat && stat.textContent.indexOf('加载中') >= 0) stat.textContent = '加载失败';
}

window.addEventListener('error', (e) => {
    showFatal((e && e.message) || '未知错误');
});
window.addEventListener('unhandledrejection', (e) => {
    showFatal('请求失败 ' + ((e && e.reason && (e.reason.message || e.reason)) || ''));
});

// 幂等：DOMContentLoaded 有可能被触发两次（脚本重复加载、部分测试环境），
// 不拦的话事件会绑两遍，一个 Esc 会把选择器和详情页一起关掉。
let booted = false;

document.addEventListener('DOMContentLoaded', () => {
    if (booted) return;
    booted = true;
    // 事件绑定出错不该影响数据加载
    try {
        bindEvents();
    } catch (e) {
        console.error('bindEvents 失败', e);
        showFatal('页面初始化失败：' + (e && e.message));
    }
    init();
});

/* =====================================================================
   追更（watchView）
   ---------------------------------------------------------------------
   2026-11-08：原来「追更」是 /watch 一个独立页面，从顶栏按钮另开标签打开。
   现在并进主界面左侧「功能」栏，成为主界面里的一个视图（state.view='watch'）。
   数据接口完全复用，未改动服务端行为：
     GET  /api/watch/status  取状态（running / 名单 / 账号池 / 日志 / 档位统计）
     POST /api/watch         改名单、开关、设置、账号池
   函数一律用 wv 前缀，避免和主界面已有的 state / render / post 撞名。
   ===================================================================== */

// 名单筛选的档位定义。顺序 = 按钮在界面上的顺序，和作者详情页的作品分类同一套手感。
// 'down' / 'off' 不是档位，是两个常用的查看口径（有下载 / 已停用）。
const WV_FILTERS = [
    ['all', '全部', '名单里的全部作者'],
    ['warm', '温档', '7 天内发过作品'],
    ['cold', '冷档', '30 天内发过作品'],
    ['idle', '静档', '30 天以上没动静'],
    ['fixed', '定档', '手动指定了固定间隔'],
    ['down', '有下载', '追更过程中真的下到过作品的'],
    ['off', '已停用', '在名单里但暂时不查的'],
];

// 行内那颗小标签：档位 → [显示字, 鼠标悬停说明, 配色]
const WV_TIER_TAG = {
    hot: ['热', '2 天内发过', 'hot'],
    warm: ['温', '7 天内发过', 'warm'],
    cold: ['冷', '30 天内发过', 'cold'],
    idle: ['静', '更久没动静', 'cold'],
    fixed: ['定', '按指定间隔查', 'hot'],
};

const wvState = { data: null, filter: 'all', loading: false, bound: false, timer: 0 };

// 距离某个时间戳多久（未来 = 「N 分钟后」，过去 = 「N 分钟前」）
function wvAgo(ts) {
    if (!ts) return '—';
    const diff = ts * 1000 - Date.now();
    const s = Math.abs(diff) / 1000;
    let txt;
    if (s < 60) txt = Math.round(s) + ' 秒';
    else if (s < 3600) txt = Math.round(s / 60) + ' 分钟';
    else if (s < 86400) txt = (s / 3600).toFixed(1) + ' 小时';
    else txt = (s / 86400).toFixed(1) + ' 天';
    return diff >= 0 ? txt + '后' : txt + '前';
}

// 「2026-11-08 13:57:57」→「11-08 13:57」。名单那列窄，年份和秒都是噪音，
// 完整时间仍留在 title 里（鼠标悬停能看到）
function wvShortTime(s) {
    const v = String(s || '').trim();
    const m = v.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}:\d{2})/);
    return m ? (m[2] + '-' + m[3] + ' ' + m[4]) : (v || '—');
}

// 当前筛选条件是否命中这一行
function wvMatch(r, f) {
    if (f === 'all') return true;
    if (f === 'down') return (r.downloads || 0) > 0;
    if (f === 'off') return !r.enabled;
    return (r.tier || 'idle') === f;
}

// 切换筛选：只重绘，不重新请求（数据已经在手里，切换必须瞬时）
function wvSetFilter(k) {
    wvState.filter = k;
    wvRender();
}

// 侧边栏那颗数字：名单里有几位
function wvSyncBadge(n) {
    const el = document.getElementById('watchBadge');
    if (!el) return;
    const num = Number(n) || 0;
    el.textContent = String(num);
    el.classList.toggle('zero', num === 0);
}

// 打开追更视图（左侧「功能」栏点进来 / 地址栏 #watch 直接进来）
async function openWatchView() {
    forceCloseDetail();
    rememberViewScroll();
    state.view = 'watch';
    state.returnView = 'watch';
    hideAllViews();
    const v = document.getElementById('watchView');
    if (v) v.classList.remove('hidden');
    animateViewEnter('watchView', 'neutral');
    clearSideActive();
    document.querySelectorAll('#featureList .side-item').forEach((i) => {
        i.classList.toggle('active', i.dataset.action === 'watch');
    });
    const eb = document.getElementById('exportBtn');
    if (eb) eb.classList.add('hidden');
    wvBindOnce();
    // 第一次进来：先把骨架和筛选按钮摆出来。接口 200~800ms，只写一行「读取中…」会跳一下
    if (!wvState.data) {
        const vv = document.getElementById('watchView');
        if (vv) vv.classList.add('wv-booting');
        wvRenderFilter([]);
        wvSkeleton();
    }
    // 只在追更视图里轮询，切走就停（interval 还留着，靠 state.view 判断，省得反复建/清定时器）
    if (!wvState.timer) {
        wvState.timer = setInterval(() => {
            if (state.view === 'watch') wvPull({ silent: true });
        }, 5000);
    }
    await wvPull();
    // 滚动位置要等表格铺出来再恢复（先恢复的话内容还没那么高，会被夹到 0）
    restoreViewScroll('watch');
}

async function wvPull(opts) {
    const silent = !!(opts && opts.silent);
    if (wvState.loading) return;
    wvState.loading = true;
    try {
        const r = await fetch('/api/watch/status', { cache: 'no-store' });
        const j = await r.json();
        if (!j || j.ok === false) throw new Error((j && j.error) || '读取失败');
        wvState.data = j;
        wvSyncBadge(j.count);
        // 拉到数据时如果人已经切走了，就别去动 DOM（下次进来会重新拉）
        if (state.view === 'watch') wvRender();
    } catch (e) {
        if (!silent && state.view === 'watch') {
            showToast('追更状态读取失败：' + (e && e.message ? e.message : e));
            const hint = document.getElementById('wvTblHint');
            if (hint && !hint.textContent) hint.textContent = '读取失败';
        }
    } finally {
        wvState.loading = false;
    }
}

async function wvPost(body, okMsg) {
    try {
        const r = await fetch('/api/watch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const j = await r.json();
        if (!j.ok) { showToast(j.error || '操作失败'); return j; }
        if (okMsg) showToast(okMsg);
        await wvPull({ silent: true });
        return j;
    } catch (e) {
        showToast('请求失败：' + (e && e.message ? e.message : e));
        return null;
    }
}

// 表头吸顶的偏移量。
// 主界面的 .view-header 自己就是 sticky（top: -20px / z-index: 20，三个断点还不一样），
// 表头如果也贴 0 就会被标题条整块盖住，视觉上像「没有表头」。所以每次渲染量一次：
//   吸附位置 = 标题条自身高度 + 它的 top 偏移（负值）
// 结果写进 --wv-th-top，交给 CSS 的 `th { top: var(--wv-th-top) }`。
// 顺手在窗口尺寸变化时补一次（断点会改标题高度）。
function wvSyncStickyTop() {
    const root = document.getElementById('watchView');
    if (!root) return;
    const h = root.querySelector('.view-header');
    if (!h) return;
    const topOff = parseFloat(getComputedStyle(h).top) || 0;
    const v = Math.round(h.offsetHeight + topOff + 1);   // +1 留一条缝，别和标题边框粘在一起
    root.style.setProperty('--wv-th-top', Math.max(0, v) + 'px');
}
if (typeof window !== 'undefined' && !window.__wvStickyBound) {
    window.__wvStickyBound = true;
    window.addEventListener('resize', () => {
        if (state.view === 'watch') wvSyncStickyTop();
    });
}

// ---------- 整块渲染 ----------
// 首屏骨架：六行假数据占位，形状和真实行一致（名字 / 两个数字 / 检查 / 标签）
function wvSkeleton() {
    const tb = document.getElementById('wvTbody');
    if (!tb) return;
    const row = '<tr class="wv-skel">' +
        '<td><span class="sk sk-name"></span></td>' +
        '<td class="wv-num"><span class="sk sk-num"></span></td>' +
        '<td class="wv-num"><span class="sk sk-num"></span></td>' +
        '<td><span class="sk sk-date"></span></td>' +
        '<td><span class="sk sk-tag"></span></td>' +
        '<td></td></tr>';
    tb.innerHTML = row.repeat(6);
}

function wvRender() {
    const d = wvState.data;
    if (!d) return;
    const rowsAll = d.authors || [];
    // 档位按钮少了一个（热档 2026-11-09 撤掉）：会话里残留的 filter='hot' 要兜回「全部」，
    // 否则名单会一直按一个已经没有按钮的档位筛着
    if (!WV_FILTERS.some(([k]) => k === wvState.filter)) wvState.filter = 'all';
    const root = document.getElementById('watchView');
    if (root) root.classList.remove('wv-booting');
    wvSyncStickyTop();

    // 标题旁的状态文案
    const sub = document.getElementById('wvSub');
    if (sub) {
        if (d.running) {
            sub.textContent = '运行中 · 心跳 ' + wvAgo(d.heartbeat);
            sub.style.color = '#10b981';
        } else {
            sub.textContent = d.heartbeat ? ('已停止 · 最后心跳 ' + wvAgo(d.heartbeat)) : '未启动';
            sub.style.color = '';
        }
    }
    const bStart = document.getElementById('wvStart');
    const bStop = document.getElementById('wvStop');
    if (bStart) bStart.disabled = !!d.running;
    if (bStop) bStop.disabled = !d.running;

    // 三张概览卡（2026-11-09 布局重排：热号删掉，请求用量挪进「节奏与安全」）
    const setV = (id, html) => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = html;
    };
    setV('wvCount', d.count + '<small>位</small>');
    // 这两个数字现在是「节奏与安全」里那一行的小字，只换值、别把整行结构冲掉
    setV('wvReq', (d.req_today || 0) + (d.req_budget ? ' / ' + d.req_budget : '') + ' 次');
    const est = d.est_daily || 0;
    setV('wvEst', est + ' 次/天');
    const estCard = document.getElementById('wvEstCard');
    if (estCard) estCard.classList.toggle('over', !!(d.req_budget && est > d.req_budget));
    setV('wvDl', d.total_downloads + '<small>条</small>');
    const beat = document.getElementById('wvBeat');
    if (beat) beat.textContent = d.heartbeat ? wvAgo(d.heartbeat) : '—';

    // 风控提示条：冷却中 / 额度用尽 / 估算超上限，都在这里说清楚
    const gb = document.getElementById('wvGuard');
    if (gb) {
        const msgs = [];
        if (d.cool_left > 0) {
            msgs.push('抖音那边连续失败，已自动暂停 <b>' + Math.ceil(d.cool_left / 60) +
                      ' 分钟</b>。这是保护动作 —— 被限流时继续打才会真出事，停下来等没事。');
        }
        if (d.req_budget && d.req_today >= d.req_budget) {
            msgs.push('今日请求额度已用完（' + d.req_budget + ' 次），明天自动继续。想多查就调高「每日请求上限」。');
        }
        if (d.req_budget && est > d.req_budget) {
            msgs.push('按当前档位估算每天要发 <b>' + est + '</b> 次，超过上限 ' + d.req_budget +
                      ' 次。实际会先撞上限停下 —— 想全天不停就把档位调宽，或调高上限。');
        }
        if (msgs.length) { gb.innerHTML = msgs.join('<br>'); gb.classList.remove('hidden'); }
        else { gb.classList.add('hidden'); }
    }

    wvRenderSettings(d);
    wvRenderFilter(rowsAll);
    wvRenderTable(rowsAll);
    wvRenderAccounts(d);
    wvRenderLog(d);
}

// 设置项回填。用户正在改任意一个输入框时不要回填 —— 否则 5 秒轮询会把
// 刚敲进去的数字重置掉（老页面上踩过这个坑，这里保留同样的保护）。
function wvRenderSettings(d) {
    const IDS = ['wvSHot', 'wvSWarm', 'wvSCold', 'wvSIdle', 'wvSGap', 'wvSBudget', 'wvSMax',
                 'wvSAuto'];
    const editing = IDS.some((id) => document.activeElement === document.getElementById(id));
    if (editing) return;
    const s = d.settings || {};
    const put = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    put('wvSHot', s.interval_hot);
    put('wvSWarm', s.interval_warm);
    put('wvSCold', s.interval_cold);
    put('wvSIdle', s.interval_idle);
    put('wvSGap', s.gap);
    put('wvSMax', s.max_per_round);
    put('wvSBudget', s.daily_budget);
    // 「追更内容」下拉 2026-11-08 删了（用户不需要）。服务端的 content 一并保持原值，
    // 保存时继续把当前值带回去，别因为界面上没这个控件就把它重置成 all。
    wvState.content = s.content || 'all';
    const auto = document.getElementById('wvSAuto');
    if (auto) auto.checked = !!d.auto_start;
}

// 筛选按钮：观感 = 作者详情页的作品分类（灰底分段控件 + 白色选中块 + 计数胶囊）。
// 只在结构不对时重建，平时只更新数字和选中态 —— 否则 5 秒一次的重绘会把键盘焦点吃掉。
function wvRenderFilter(rows) {
    const box = document.getElementById('wvFilter');
    if (!box) return;
    if (box.children.length !== WV_FILTERS.length) {
        box.innerHTML = WV_FILTERS.map(([k, label, tip]) =>
            '<button class="btn work-filter-btn" role="tab" aria-selected="false" data-wvf="' + k + '"' +
            ' title="' + escapeHtml(tip) + '" onclick="wvSetFilter(\'' + k + '\')">' +
            escapeHtml(label) + '<b class="wf-n">0</b></button>'
        ).join('');
    }
    WV_FILTERS.forEach(([k], i) => {
        const b = box.children[i];
        if (!b) return;
        const n = rows.filter((r) => wvMatch(r, k)).length;
        const num = b.querySelector('.wf-n');
        if (num && num.textContent !== String(n)) num.textContent = String(n);
        const on = wvState.filter === k;
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
}

function wvRenderTable(rowsAll) {
    const tb = document.getElementById('wvTbody');
    if (!tb) return;
    const rows = rowsAll.filter((r) => wvMatch(r, wvState.filter));
    const hint = document.getElementById('wvTblHint');
    if (hint) {
        hint.textContent = rows.length === rowsAll.length
            ? ('共 ' + rowsAll.length + ' 位')
            : ('筛出 ' + rows.length + ' / ' + rowsAll.length + ' 位');
    }

    if (!rows.length) {
        const msg = rowsAll.length ? '当前筛选没有匹配的作者' : '名单是空的，先在上面加几位';
        tb.innerHTML = '<tr><td colspan="6" class="wv-empty">' + msg + '</td></tr>';
        return;
    }

    tb.innerHTML = rows.map((r) => {
        let t = WV_TIER_TAG[r.tier] || WV_TIER_TAG.idle;
        if (r.tier === 'fixed' && r.interval) {
            t = ['定', '每 ' + Math.round(r.interval / 60) + ' 分钟查一次', 'hot'];
        }
        let st = '<span class="wv-tag ' + t[2] + '" title="' + escapeHtml(t[1]) + '">' +
                 '<i class="wv-dot"></i>' + t[0] + '档</span>';
        if (!r.enabled) st = '<span class="wv-tag off"><i class="wv-dot"></i>已停用</span>';
        const extra = r.fails ? ' <span class="wv-tag off" title="失败重试中">' + r.fails + ' 失败</span>' : '';

        // 名字包成 <a>：中键 / Ctrl+点击可以「在新标签打开」，左键走应用内详情页
        const href = '/#author=' + encodeURIComponent(r.uid);
        const nm = r.in_lib
            ? '<a class="wv-nm" href="' + href + '" data-uid="' + escapeHtml(r.uid) + '" title="打开「' +
              escapeHtml(r.nickname) + '」的主页"><b>' + escapeHtml(r.nickname) + '</b><i class="go">↗</i></a>'
            : '<span class="wv-nm" title="还不在本地作品库里，暂时没有主页可跳"><b>' +
              escapeHtml(r.nickname) + '</b></span>';

        // 「检查」一列叠两行：上次查过 / 下次大概什么时候。
        // 名单收窄后（右边要让给日志）再拆成两列会把作者名挤成「乌…」，
        // 所以两列合一列、竖着排（2026-11-08）
        const when = '<div class="wv-when">' +
            '<span class="wv-when-l" title="上次检查：' + escapeHtml(r.last_scan || '—') + '">' +
                escapeHtml(wvShortTime(r.last_scan)) + '</span>' +
            '<span class="wv-when-n" title="下次检查">' +
                (r.next_check ? wvAgo(r.next_check) : '—') + '</span>' +
            '</div>';

        return '<tr class="wv-link' + (r.enabled ? '' : ' wv-off') + '" data-uid="' + escapeHtml(r.uid) + '" tabindex="0">' +
            '<td>' + nm + (r.in_lib ? '' : ' <span class="wv-tag off">不在库</span>') + '</td>' +
            '<td class="wv-num">' + (r.works || 0) + '</td>' +
            '<td class="wv-num">' + (r.downloads || '—') + '</td>' +
            '<td>' + when + '</td>' +
            '<td>' + st + extra + '</td>' +
            '<td class="wv-ops">' +
                '<button class="btn btn-mini" data-act="toggle" data-uid="' + escapeHtml(r.uid) + '">' +
                    (r.enabled ? '停用' : '启用') + '</button>' +
                '<button class="btn btn-mini" data-act="remove" data-uid="' + escapeHtml(r.uid) + '">移出</button>' +
            '</td></tr>';
    }).join('');
}

function wvRenderAccounts(d) {
    const box = document.getElementById('wvAccBody');
    if (!box) return;
    const list = d.accounts || [];
    if (!list.length) {
        box.innerHTML = '<div class="wv-empty" style="padding:16px">账号池是空的 —— 点右上「添加账号」，' +
            '把某个抖音号的 cookie 整段粘进来</div>';
        return;
    }
    box.innerHTML = '<table class="wv-tbl"><thead><tr>' +
        '<th style="width:30%">账号</th><th>今日请求</th><th>成功 / 失败</th><th>状态</th>' +
        '<th style="width:128px"></th></tr></thead><tbody>' +
        list.map((a) => {
            let st;
            if (!a.logged_in) st = '<span class="wv-tag off">没有登录态</span>';
            else if (!a.enabled) st = '<span class="wv-tag off">已停用</span>';
            else if (a.cool_left > 0) {
                st = '<span class="wv-tag off" title="' + escapeHtml(a.last_error || '') + '">歇 ' +
                     Math.ceil(a.cool_left / 60) + ' 分钟</span>';
            } else st = '<span class="wv-tag hot">可用</span>';
            return '<tr' + (a.enabled ? '' : ' class="wv-off"') + '>' +
                '<td><b>' + escapeHtml(a.label || a.id) + '</b>' +
                    (a.sid_tail ? ' <span class="wv-tag cold" title="sessionid 末 6 位">…' +
                                  escapeHtml(a.sid_tail) + '</span>' : '') + '</td>' +
                '<td>' + (a.req_today || 0) + '</td>' +
                '<td>' + (a.ok || 0) + ' / ' + (a.fail || 0) + '</td>' +
                '<td>' + st + '</td>' +
                '<td style="text-align:right;white-space:nowrap">' +
                    '<button class="btn btn-mini" data-aact="toggle" data-aid="' + escapeHtml(a.id) + '">' +
                        (a.enabled ? '停用' : '启用') + '</button>' +
                    '<button class="btn btn-mini" data-aact="remove" data-aid="' + escapeHtml(a.id) + '">移出</button>' +
                '</td></tr>';
        }).join('') + '</tbody></table>';
}

function wvRenderLog(d) {
    const box = document.getElementById('wvLog');
    if (!box) return;
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.innerHTML = (d.logs || []).map((ln) => {
        let cls = '';
        if (/↓/.test(ln)) cls = 'dl';
        else if (/★/.test(ln)) cls = 'new';
        else if (/✗|×/.test(ln)) cls = 'err';
        return '<div class="' + cls + '">' + escapeHtml(ln) + '</div>';
    }).join('') || '（还没有日志）';
    if (nearBottom) box.scrollTop = box.scrollHeight;
}

// ---------- 事件绑定（只绑一次） ----------
function wvBindOnce() {
    if (wvState.bound) return;
    wvState.bound = true;

    // 名单：整行可点进详情；行内两颗按钮优先
    const tb = document.getElementById('wvTbody');
    if (tb) {
        tb.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-act]');
            if (btn) {
                const uid = btn.getAttribute('data-uid');
                const act = btn.getAttribute('data-act');
                if (act === 'toggle') wvPost({ action: 'toggle', uid: uid }, '已切换');
                if (act === 'remove') {
                    const row = ((wvState.data && wvState.data.authors) || [])
                        .filter((r) => r.uid === uid)[0];
                    if (!confirm('把「' + (row ? row.nickname : uid) + '」移出追更名单？\n（已经下载的文件不受影响）')) return;
                    wvPost({ action: 'remove', uids: [uid] }, '已移出');
                }
                return;
            }
            const tr = e.target.closest('tr[data-uid]');
            if (!tr) return;
            const uid = tr.getAttribute('data-uid');
            const who = ((wvState.data && wvState.data.authors) || [])
                .filter((r) => r.uid === uid)[0];
            // 点在名字那根 <a> 上且按了修饰键 / 中键 → 交给浏览器开新标签
            if (e.target.closest('a.wv-nm') && (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0)) return;
            e.preventDefault();
            if (who && who.in_lib === false) {
                showToast('「' + who.nickname + '」还不在本地作品库里，没有主页可跳 —— 追更会照常下载，下完就能点进来');
                return;
            }
            openAuthor(uid);
        });
        // 键盘可达：回车/空格进详情（tabindex 已经在生成时加过）
        tb.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter' && e.key !== ' ') return;
            const tr = e.target.closest('tr[data-uid]');
            if (!tr) return;
            if (e.target.closest('button')) return;
            e.preventDefault();
            tr.click();
        });
    }

    // 账号池
    const acc = document.getElementById('wvAccBody');
    if (acc) {
        acc.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-aact]');
            if (!btn) return;
            const id = btn.getAttribute('data-aid');
            const act = btn.getAttribute('data-aact');
            if (act === 'toggle') wvPost({ action: 'account_toggle', id: id, enabled: btn.textContent === '停用' }, '已切换');
            if (act === 'remove') {
                if (!confirm('把这个号从账号池移出？\n（只是不再用它查询，已下载的作品不受影响）')) return;
                wvPost({ action: 'account_remove', id: id }, '已移出');
            }
        });
    }
}

// ---------- 名单 / 设置的写操作 ----------
// 「＋ 加作者」：默认收起，点一下在名单标题下面展开输入条（用户 2026-11-09：
// 加作者不重要，别单独占一整块）
function wvToggleAdd(force) {
    const bar = document.getElementById('wvAddBar');
    if (!bar) return;
    const show = (typeof force === 'boolean') ? force : bar.classList.contains('hidden');
    bar.classList.toggle('hidden', !show);
    const btn = document.getElementById('wvAddBtn');
    if (btn) btn.classList.toggle('active', show);
    if (show) {
        const inp = document.getElementById('wvAddInput');
        if (inp) inp.focus();
    }
}

async function wvAdd() {
    const inp = document.getElementById('wvAddInput');
    if (!inp) return;
    const v = (inp.value || '').trim();
    if (!v) { inp.focus(); return; }
    const j = await wvPost({ action: 'add', token: v });
    if (!j || !j.ok) return;
    const n = (j.added || []).length;
    let msg = n ? ('加入 ' + n + ' 位') : '没有新增';
    if ((j.skipped || []).length) msg += '，' + j.skipped.length + ' 位已在名单';
    if ((j.errors || []).length) msg += '；' + j.errors[0];
    showToast(msg);
    if (n) {
        inp.value = '';
        wvToggleAdd(false);      // 加完就收起来，把版面让回名单
    }
}

async function wvStart() {
    const b = document.getElementById('wvStart');
    if (!b || b.disabled) return;
    const old = b.textContent;
    b.disabled = true;
    b.textContent = '启动中…';
    try { await wvPost({ action: 'start' }, '追更已启动'); }
    finally { b.textContent = old; }
}

function wvStop() {
    if (!confirm('停止后就不再看新作品了，确定？')) return;
    wvPost({ action: 'stop' }, '已停止');
}

function wvRefresh() { wvPull(); }

function wvSaveCfg(okMsg) {
    const num = (id) => +((document.getElementById(id) || {}).value || 0);
    const auto = document.getElementById('wvSAuto');
    // 「追更内容」下拉已经删了，但服务端这个设置还在（默认 all）。保存时原样带回去，
    // 否则每存一次设置就把用户以前选过的值重置成 all。
    const content = wvState.content || 'all';
    wvPost({
        action: 'settings',
        auto_start: !!(auto && auto.checked),
        settings: {
            interval_hot: num('wvSHot'),
            interval_warm: num('wvSWarm'),
            interval_cold: num('wvSCold'),
            interval_idle: num('wvSIdle'),
            max_per_round: num('wvSMax'),
            gap: num('wvSGap'),
            daily_budget: num('wvSBudget'),
            content: content,
        },
    }, okMsg || '已保存');
}

// ---------- 账号池 ----------
function wvAccSync() { wvPost({ action: 'account_sync' }, '已同步下载器当前的号'); }

function wvAccToggleForm(hide) {
    const f = document.getElementById('wvAccForm');
    if (!f) return;
    const show = !hide && f.classList.contains('hidden');
    f.classList.toggle('hidden', !show);
    if (show) {
        const l = document.getElementById('wvAccLabel');
        if (l) l.focus();
    }
}

async function wvAccSave() {
    const label = (document.getElementById('wvAccLabel') || {}).value || '';
    const ck = ((document.getElementById('wvAccCookie') || {}).value || '').trim();
    if (!ck) { showToast('先把 cookie 粘进来'); return; }
    const j = await wvPost({ action: 'account_add', label: label.trim(), cookie: ck });
    if (!j || !j.ok) return;
    showToast('已加入账号池');
    document.getElementById('wvAccCookie').value = '';
    document.getElementById('wvAccLabel').value = '';
    wvAccToggleForm(true);
}

// 侧边栏那颗「追更」数字：进页面几秒后补一次，不用先点进追更才看到总数。
// 只发一次请求，失败就当没有（不弹提示，别为了一个角标打扰人）。
window.addEventListener('load', () => {
    setTimeout(() => {
        if (state.view === 'watch') return;    // 已经在追更页就不用重复拉
        fetch('/api/watch/status', { cache: 'no-store' })
            .then((r) => r.json())
            .then((j) => { if (j && j.ok !== false) wvSyncBadge(j.count); })
            .catch(() => { });
    }, 1200);
});
