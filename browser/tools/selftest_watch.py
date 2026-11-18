"""追更模块自检：不联网、不碰真实状态文件，用假数据撞边界。

跑法：
    PYTHONPATH= C:/Python312/python.exe tools/selftest_watch.py

覆盖：
  1. 四档判定的边界（正好 2 天 / 7 天 / 30 天）
  2. 每日额度用完后一个请求都不发
  3. 连续失败到阈值 → 冷却，且冷却期间不再发请求
  4. 关机一夜后补漏：基线之后的作品全被挑出来，一条不漏
  5. 补漏时"整页都比基线新"→ 自动多翻页
  6. 判重：库里已有的作品不会再下
  7. 额度按真实请求数记（含重试）
  8. 试运行（--dry）不改动任何状态
  9. 作者还不在本地库：跳过但绝不标记已处理
 10. 账号池：一个号连续失败就换另一个，只有一个号时不会死循环
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import watch_core as W  # noqa: E402

TMP = tempfile.mkdtemp(prefix="watchtest_")
W.STATE_FILE = os.path.join(TMP, "state.json")
W.LOG_FILE = os.path.join(TMP, "watch.log")
# 下载明细也指到临时目录：2026-11-12 起 scan_round 会读它做「自己下过的 ID」判重，
# 不隔离的话自检会去读真实账本，结果就不可重复了
W.DOWNLOADS_FILE = os.path.join(TMP, "watch_downloads.jsonl")
# 库根也指到临时目录：2026-11-10 起「作者不在本地库」会自动建作者目录，
# 不改这里的话自检会往真实库根（E 盘）里撒一堆测试目录
_REAL_SCAN_ROOT = W.paths.SCAN_ROOT
W.paths.SCAN_ROOT = os.path.join(TMP, "lib")
os.makedirs(W.paths.SCAN_ROOT, exist_ok=True)

FAIL = []


def chk(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + ("" if cond else ("  <- " + str(extra))))
    if not cond:
        FAIL.append(name)


class FakeDy:
    """假的抖音客户端：按 sec_uid → 作品列表（新→旧）应答。

    pages=N 时最多返回 N*20 条，模拟真实翻页；fail_all=True 时全部抛错。
    """

    def __init__(self, works, fail_all=False, empty_first=0):
        self.works = works
        self.fail_all = fail_all
        self.empty_first = empty_first      # 前 N 枪返回空列表（模拟软限流）
        self.calls = []
        self.cursors = []                   # 每次调用用的游标（全量接着翻的用例要看它）
        self.reqs = 0

    def author_works(self, sec_uid, pages=1, retry=2, with_more=False, start_cursor="0"):
        self.calls.append((sec_uid, pages))
        self.cursors.append(str(start_cursor))
        self.reqs += 1
        if self.fail_all:
            raise RuntimeError("HTTP 444")
        if len(self.calls) <= self.empty_first:
            return ([], False, "0") if with_more else []
        allw = self.works.get(sec_uid) or []
        n = max(1, pages) * 20
        # start_cursor：模拟抖音的 max_cursor = 已经翻过的条数（真实接口给的是时间游标，
        # 这里只关心「从上次停下的地方继续」，用条数等价即可）
        off = int(str(start_cursor or "0") or 0)
        out = allw[off:off + n]
        nxt = str(off + len(out))
        # with_more：翻满页数后还有没有更早的作品（全量扫要它）+ 下次从哪接着翻
        return (out, len(allw) > off + len(out), nxt) if with_more else out


def mk_aweme(aid, ts, desc="d"):
    return {"aweme_id": aid, "create_time": ts, "desc": desc}


CFG = dict(W.DEFAULT_CFG)


def base_watch(uids):
    return {"authors": [{"uid": u, "sec_uid": "SEC" + u, "nickname": "N" + u,
                         "enabled": True} for u in uids],
            "settings": dict(CFG)}


def fresh_state():
    return {"authors": {}, "req_day": time.strftime("%Y-%m-%d"), "req_today": 0,
            "consec_fail": 0, "cool_until": 0, "cool_level": 0}


now = time.time()

print("\n[1] 六档边界（沸/热/温/凉/寒/冻）+ 「沸」的密度门槛")
for days, want in [(1.5, "hot"), (2.9, "hot"), (3.1, "warm"), (6.9, "warm"),
                   (7.1, "cool"), (29.9, "cool"), (30.1, "chill"),
                   (89.9, "chill"), (90.1, "frozen"), (200, "frozen")]:
    t, _ = W.tier_of({"last_post": now - days * 86400}, CFG, now)
    chk("%.2f 天 -> %s" % (days, want), t == want, t)

# 24 小时内：光有时间不够，还得有「确实在日更」的密度证据
chk("24h 内只发 1 条 -> 热（不算日更）",
    W.tier_of({"last_post": now - 3600, "cad24": 1, "cad3": 1}, CFG, now)[0] == "hot")
chk("24h 内 2 条 -> 沸",
    W.tier_of({"last_post": now - 3600, "cad24": 2, "cad3": 2}, CFG, now)[0] == "boil")
chk("24h 内 1 条但 72h 内 3 条 -> 沸",
    W.tier_of({"last_post": now - 3600, "cad24": 1, "cad3": 3}, CFG, now)[0] == "boil")
chk("停更很久后刚发一条（老状态没有密度数据）-> 热，不会直接沸",
    W.tier_of({"last_post": now - 600}, CFG, now)[0] == "hot")
chk("没有 last_post -> 热（刚加进来的按热档先查）", W.tier_of({}, CFG, now)[0] == "hot")

# 2026-11-13 用户口径：分档看**作品发布时间**，不是我们下载它的时间。
# 停更 200 天的人今天被我们补下一批老作品，last_new 是今天 —— 不能因此变沸/热。
chk("200 天前的作品 + 今天刚下载 -> 冻档（不被下载时间拉热）",
    W.tier_of({"last_post": now - 200 * 86400, "last_new": now,
               "cad24": 5, "cad3": 5}, CFG, now)[0] == "frozen")
chk("只有 last_ts（已处理到的作品发布时间）也能定档",
    W.tier_of({"last_ts": now - 10 * 86400}, CFG, now)[0] == "cool")

# 六档间隔：越冷越大，且必须严格递增（配错就是把作者晾在那儿）
_seq = ["boil", "hot", "warm", "cool", "chill", "frozen"]
_ivs = [CFG["interval_%s" % k] for k in _seq]
chk("六档间隔严格递增", all(a < b for a, b in zip(_ivs, _ivs[1:])), _ivs)
chk("沸 15 分钟 / 热 30 分钟 / 温 1 小时 / 凉 4 小时 / 寒 12 小时 / 冻 3 天",
    _ivs == [900, 1800, 3600, 14400, 43200, 259200], _ivs)

print("\n[2] 每日额度用完")
st = fresh_state()
st["req_today"] = CFG["daily_budget"]
dy = FakeDy({})
w = base_watch(["1", "2"])
r = W.scan_round(dy, w, st, {}, {})
chk("不发任何请求", len(dy.calls) == 0, dy.calls)
chk("返回 paused 原因", "额度" in (r.get("paused") or ""), r.get("paused"))

print("\n[3] 连续失败熔断")
st = fresh_state()
dy = FakeDy({}, fail_all=True)
w = base_watch([str(i) for i in range(30)])
r = W.scan_round(dy, w, st, {}, {})
chk("失败到阈值就停（不是跑完 30 位）", len(dy.calls) <= CFG["break_fails"] + 1, len(dy.calls))
chk("写入了冷却时间", float(st.get("cool_until") or 0) > time.time(), st.get("cool_until"))
chk("冷却级别 +1", int(st.get("cool_level") or 0) == 1, st.get("cool_level"))
# 冷却期内再来一轮：一个请求都不许发
n0 = len(dy.calls)
W.scan_round(dy, w, st, {}, {})
chk("冷却期零请求", len(dy.calls) == n0, len(dy.calls) - n0)

print("\n[4] 关机一夜后的补漏")
night = now - 8 * 3600
works = {"SECa": [mk_aweme("a3", int(night + 3000)), mk_aweme("a2", int(night + 1200)),
                  mk_aweme("a1", int(night - 60))]}
st = fresh_state()
# 基线停在睡前：a1 是睡前就已处理过的，a2/a3 是夜里新发的
st["authors"]["a"] = {"last_ts": int(night), "seen": ["a1"], "downloads": 0,
                      "fails": {}, "last_post": int(night)}
dy = FakeDy(works)
w = base_watch(["a"])
r = W.scan_round(dy, w, st, {}, {"a": ""})   # 给了文件夹才会真下；这里只验候选数
chk("夜里两条都被发现（睡前那条不重复）", r["new"] == 2, "new=%d" % r["new"])
chk("睡前那条仍记为已处理（不会被重下）", "a1" in st["authors"]["a"]["seen"],
    st["authors"]["a"]["seen"])

print("\n[5] 整页都比基线新 → 自动多翻页")
# 30 条全新作品；只翻 1 页只能拿到最新 20 条，补翻后应该 30 条全拿到
many = [mk_aweme("p%d" % i, int(now - i * 60)) for i in range(30)]
st = fresh_state()
st["authors"]["b"] = {"last_ts": int(now - 86400), "seen": [], "downloads": 0,
                      "fails": {}, "last_post": int(now - 86400)}
dy = FakeDy({"SECb": many})
w = base_watch(["b"])
r = W.scan_round(dy, w, st, {}, {"b": ""})
chk("触发多页请求", any(p > 1 for _s, p in dy.calls), dy.calls)
chk("30 条全拿到（不只第一页 20 条）", r["new"] == 30, "new=%d" % r["new"])

print("\n[6] 判重：库里已有的不再下")
st = fresh_state()
st["authors"]["c"] = {"last_ts": int(now - 86400), "seen": [], "downloads": 0,
                      "fails": {}, "last_post": int(now - 86400)}
dy = FakeDy({"SECc": [mk_aweme("c1", int(now - 3600), "同一个描述")]})
w = base_watch(["c"])
from datetime import datetime as _dt
day = _dt.fromtimestamp(now - 3600).strftime("%Y-%m-%d")
lib = {"c": {(day, W.work_key("同一个描述", CFG["desc_len"]))}}
r = W.scan_round(dy, w, st, lib, {"c": ""})
chk("算作跳过而不是下载", r["skipped"] == 1 and r["downloaded"] == 0,
    "skip=%d dl=%d" % (r["skipped"], r["downloaded"]))

print("\n[7] 额度按真实请求数记，不是按作者数")
st = fresh_state()
dy = FakeDy({"SECx": [mk_aweme("x1", int(now - 3600))]}, fail_all=True)
w = base_watch(["x"])
W.scan_round(dy, w, st, {}, {})
chk("req_today == 实际发出的请求数", int(st["req_today"]) == dy.reqs,
    "req_today=%s reqs=%s" % (st.get("req_today"), dy.reqs))
import inspect  # noqa: E402
chk("真实 Douyin.get 里有计数", "self.reqs += 1" in inspect.getsource(W.Douyin.get))

print("\n[8] 试运行（--dry）不能改动状态")
st = fresh_state()
st["authors"]["d"] = {"last_ts": int(now - 86400), "seen": [], "downloads": 0,
                      "fails": {}, "last_post": int(now - 86400)}
KEY = ("seen", "last_ts", "next_check", "downloads")
before = {k: st["authors"]["d"].get(k) for k in KEY}
dy = FakeDy({"SECd": [mk_aweme("d1", int(now - 3600))]})
w = base_watch(["d"])
r = W.scan_round(dy, w, st, {"d": set()}, {"d": "somedir"}, dry=True)
after = {k: st["authors"]["d"].get(k) for k in KEY}
chk("没记成已处理（seen/last_ts/下载数/下轮时间都没动）", before == after,
    "前=%s 后=%s" % (before, after))
chk("如实报告会下载 1 条", int(r.get("would") or 0) == 1, r.get("would"))

print("\n[9] 作者还不在本地库：按下载器的规则自动建目录并下载")
st = fresh_state()
st["authors"]["e"] = {"last_ts": int(now - 86400), "seen": [], "downloads": 0,
                      "fails": {}, "last_post": int(now - 86400)}
dy = FakeDy({"SECe": [mk_aweme("e1", int(now - 3600)), mk_aweme("e2", int(now - 1800))]})
w = base_watch(["e"])
r = W.scan_round(dy, w, st, {"e": set()}, {})     # 故意不给文件夹
sa = st["authors"]["e"]
made = os.path.join(W.paths.SCAN_ROOT, "UIDe_Ne_发布作品")
chk("自动建了 UIDe_Ne_发布作品", os.path.isdir(made),
    os.listdir(W.paths.SCAN_ROOT)[:3])
chk("两条都去下了（假下载器必失败，所以计在 failed）", r["failed"] == 2,
    "failed=%d" % r["failed"])
chk("建目录后不再算 number '作者不在库' 跳过", r["skipped"] == 0, r["skipped"])

# 库根不可用（盘掉线 / 没权限）时：仍然只能跳过，且绝不能标记成已处理
_ok_root = W.paths.SCAN_ROOT
W.paths.SCAN_ROOT = os.path.join(TMP, "no_such_lib")     # 不存在 → 建不出来
W._dir_map = None
st2 = fresh_state()
st2["authors"]["e"] = {"last_ts": int(now - 86400), "seen": [], "downloads": 0,
                       "fails": {}, "last_post": int(now - 86400)}
r2 = W.scan_round(dy, w, st2, {"e": set()}, {})
sa2 = st2["authors"]["e"]
chk("库根不可用：不下也不记失败", r2["downloaded"] == 0 and r2["failed"] == 0,
    "dl=%d failed=%d" % (r2["downloaded"], r2["failed"]))
chk("库根不可用：两条都没进 seen（进库后还能补）", sa2["seen"] == [], sa2["seen"])
chk("库根不可用：基线没被推进", int(sa2["last_ts"]) == int(now - 86400), sa2["last_ts"])
chk("库根不可用：失败计数没被污染", not (sa2.get("fails") or {}), sa2.get("fails"))
W.paths.SCAN_ROOT = _ok_root
W._dir_map = None

print("\n[10] 账号池：一个号失败就换另一个")
W.ACCOUNTS_FILE = os.path.join(TMP, "accounts.json")


def _acc(i):
    return {"id": "a%d" % i, "label": "号%d" % i, "cookie": {"sessionid": "sid%d" % i},
            "enabled": True, "ok": 0, "fail": 0, "cool_until": 0, "ban_level": 0,
            "req_today": 0, "req_day": ""}


def _write_pool(n):
    with open(W.ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"accounts": [_acc(i) for i in range(1, n + 1)]}, f)


_write_pool(2)
p = W.AccountPool()
chk("初始用第 1 个号", p.current()["label"] == "号1", p.current()["label"])
p.mark_fail("HTTP 444")
chk("失败 1 次还不歇（可能是偶发）", p.accounts[0]["cool_until"] == 0,
    p.accounts[0]["cool_until"])
p.mark_fail("HTTP 444")
chk("连续 2 次不成 → 这个号先歇", p.accounts[0]["cool_until"] > time.time(),
    p.accounts[0]["cool_until"])
chk("这个号一歇，下一个请求就自动用号 2", p.current()["label"] == "号2",
    p.current()["label"])
chk("只剩一个可用号时，再换也换不动（不会死循环）", p.rotate("测试") is False)
chk("可用号只剩 1 个", len(p.usable()) == 1, len(p.usable()))

print("\n[15] 两个号必须交替出手（旧逻辑是「第一个用到死」）")
_write_pool(2)
pb = W.AccountPool()
pb.reset_day()
seq = []
for _ in range(6):
    a = pb.balance()
    a["req_today"] = int(a.get("req_today") or 0) + 1
    seq.append(a.get("label"))
chk("两个号都轮到了", len(set(seq)) == 2, seq)
chk("严格交替，不会连着两次同一个号",
    all(seq[i] != seq[i + 1] for i in range(len(seq) - 1)), seq)
chk("各打 3 次（真正的均摊）", [seq.count(x) for x in sorted(set(seq))] == [3, 3], seq)

_write_pool(1)
p1 = W.AccountPool()
p1.mark_fail("x")
p1.mark_fail("x")
chk("只有一个号时无处可换（不会死循环）", p1.rotate("测试") is False)

_write_pool(2)
st_q = {"req_today": 25000, "req_day": time.strftime("%Y-%m-%d")}
chk("单号 2 万额度：2.5 万已超", W.guard(dict(st_q), W.DEFAULT_CFG, accounts=1)[0] is False)
chk("两个号 4 万额度：2.5 万没超",
    W.guard(dict(st_q), W.DEFAULT_CFG, accounts=2)[0] is True)

print("\n[11] 新加作者：基线定在「本地最新那件」，不跳过本地缺的作品")
d_old = time.strftime("%Y-%m-%d", time.localtime(now - 3 * 86400))
w_old = mk_aweme("old1", int(time.mktime(time.strptime(d_old, "%Y-%m-%d"))) - 3600, "老作品")
w_new = mk_aweme("new1", int(now - 2 * 3600), "本地还没有的新作品")
st = fresh_state()
dy = FakeDy({"SECe": [w_new, w_old]})
w = base_watch(["e"])
r = W.scan_round(dy, w, st, {"e": {(d_old, W.work_key("老作品"))}}, {"e": "dirE"})
se = st["authors"]["e"]
chk("基线 = 本地最新那天的 00:00（当天作品全部重核，靠判重挡）",
    abs(int(se["last_ts"]) - time.mktime(time.strptime(d_old, "%Y-%m-%d"))) < 2,
    se["last_ts"])
chk("本地缺的那条被当候选", r["new"] == 1, "new=%d" % r["new"])
chk("这一轮就动手下了（不是等到下一轮）", (r["downloaded"] + r["failed"]) == 1,
    "dl=%d failed=%d" % (r["downloaded"], r["failed"]))
chk("老作品没被重下", "old1" in se["seen"], se["seen"])

print("\n[12] 接口返回空但本地有作品：同号再试一次 + 换号再试一次，三次都空才列进「扫不到」")
cfg_fast = dict(CFG)
cfg_fast["gap"] = 0.01
cfg_fast["gap_jitter"] = 0
w_fast = base_watch(["x"])
w_fast["settings"] = cfg_fast
lib_x = {"x": {("2026-01-01", W.work_key("旧作品"))}}

st = fresh_state()
dy = FakeDy({"SECx": []})
W.scan_round(dy, w_fast, st, lib_x, {"x": "dirX"}, douk_ids=set())
chk("一共打了 3 枪（第 1 次 + 重试 2 次）", len(dy.calls) == 3, len(dy.calls))
chk("记进 no_api", "x" in (st.get("no_api") or {}), list(st.get("no_api") or {}))
chk("记了本地条数", (st.get("no_api") or {}).get("x", {}).get("local") == 1,
    (st.get("no_api") or {}).get("x"))

print("\n[13] 换号后查到了：不进「扫不到」，还会把旧记录摘掉")
st = fresh_state()
st["no_api"] = {"x": {"nickname": "Nx", "local": 1, "tries": 3, "ts": 0, "reason": "旧的"}}
dy = FakeDy({"SECx": [mk_aweme("x1", int(now - 100))]}, empty_first=2)
W.scan_round(dy, w_fast, st, lib_x, {"x": "dirX"}, douk_ids=set())
chk("第 3 枪才拿到作品", len(dy.calls) == 3, len(dy.calls))
chk("旧的「扫不到」记录被摘掉", "x" not in (st.get("no_api") or {}), st.get("no_api"))

print("\n[14] 已判定扫不到的作者：12 小时内只打一枪（别把额度耗在这种作者身上）")
st = fresh_state()
st["no_api"] = {"x": {"nickname": "Nx", "local": 1, "tries": 3,
                      "ts": time.time() - 60, "reason": "连查三次都返回空"}}
dy = FakeDy({"SECx": []})
W.scan_round(dy, w_fast, st, lib_x, {"x": "dirX"}, douk_ids=set())
chk("只打 1 枪", len(dy.calls) == 1, len(dy.calls))
chk("仍在「扫不到」里", "x" in (st.get("no_api") or {}), st.get("no_api"))

print("\n[16] 新加进追更：立刻把 TA 现有作品整体扫一遍（不只下新的）")
# 库里已有 d1/d2（老作品），抖音上还有 d3/d4/d5 没下过。
# 正常追更只会看「比基线新的」，全量必须把老的也捞出来。
day_a = _dt.fromtimestamp(now - 3 * 86400).strftime("%Y-%m-%d")
day_b = _dt.fromtimestamp(now - 2 * 86400).strftime("%Y-%m-%d")
lib_d = {"d": {(day_a, W.work_key("老作品A", CFG["desc_len"])),
               (day_b, W.work_key("老作品B", CFG["desc_len"]))}}
st = fresh_state()
# 已经追了一段时间、基线推到最新 —— 这种情况下普通逻辑一条都不会再下
st["authors"]["d"] = {"last_ts": int(now), "seen": ["d1", "d2"], "downloads": 2,
                      "fails": {}, "last_post": int(now)}
works = {"SECd": [mk_aweme("d5", int(now - 3600), "新作品E"),
                  mk_aweme("d4", int(now - 86400), "新作品D"),
                  mk_aweme("d3", int(now - 2 * 86400), "新作品C"),
                  mk_aweme("d2", int(now - 2 * 86400), "老作品B"),
                  mk_aweme("d1", int(now - 3 * 86400), "老作品A")]}
dy = FakeDy(works)
w = base_watch(["d"])
w["authors"][0]["full_scan"] = True
w["authors"][0]["added_at"] = "2026-11-10 13:00:00"
r = W.scan_round(dy, w, st, lib_d, {"d": "dirD"})
sd = st["authors"]["d"]
chk("三条没下过的都进了候选（全量不看基线）", r["new"] == 5, "new=%d" % r["new"])
chk("本地缺的那 3 条都去下了（假下载器必失败，所以计在 failed）",
    r["failed"] == 3, "failed=%d" % r["failed"])
chk("本地已有的那两条算跳过（不重下）", r["skipped"] >= 2, "skip=%d" % r["skipped"])
chk("记下「已做过全量」的标记", sd.get("full_done_for") == "2026-11-10 13:00:00",
    sd.get("full_done_for"))
chk("收尾后不再是 bulk", not sd.get("bulk"), sd.get("bulk"))

# 同一个作者再来一轮：不能又全量一遍
n0 = len(dy.calls)
W.scan_round(dy, w, st, lib_d, {"d": "dirD"})
chk("第二轮不再重复全量（没有新候选）", st["authors"]["d"]["downloads"] == sd["downloads"],
    st["authors"]["d"]["downloads"])

print("\n[16b] 作品多：全量翻页要接着上次的游标往下翻（否则永远翻不到底）")
# 60 条作品、每页 20、full_pages=1：要 3 轮才翻完。
# 改之前每轮都从 "0" 开始，has_more 永远为真 → 每 60 秒重翻一轮，日志天天「还没翻到底」。
w5 = base_watch(["e"])
w5["authors"][0]["full_scan"] = True
w5["authors"][0]["added_at"] = "T-E"
w5["settings"] = dict(CFG)
w5["settings"]["full_pages"] = 1
many = {"SECe": [mk_aweme("e%02d" % i, int(now - i * 3600), "作品%d" % i) for i in range(60)]}
st5 = fresh_state()
dy5 = FakeDy(many)
for _ in range(4):
    W.scan_round(dy5, w5, st5, {"e": set()}, {"e": "dirE"})
se = st5["authors"]["e"]
# 每轮除了「全量深翻」还会先打一枪常规页（游标 0），所以只看非 0 的游标
_nonzero = [c for c in dy5.cursors if c != "0"]
chk("深翻用了 20、40 两个续翻游标（0 → 20 → 40）",
    _nonzero == ["20", "40"], dy5.cursors)
chk("翻完之后不再深翻（没有更多续翻游标）", len(_nonzero) == 2, _nonzero)
chk("翻到底后收尾（bulk 结束、留 full_done_for）",
    not se.get("bulk") and se.get("full_done_for") == "T-E",
    (se.get("bulk"), se.get("full_done_for")))

print("\n[17] 新加的作者插到队首；补不完时不会被立刻反复叫起来")
w2 = base_watch(["p", "q"])
w2["authors"][1]["full_scan"] = True
w2["authors"][1]["added_at"] = "T-NEW"
due = W.which_authors_due(w2, {"authors": {}})
chk("新加的那位排在最前", str(due[0]["uid"]) == "q", [x["uid"] for x in due])
st2 = {"authors": {"q": {"bulk": True, "full_done_for": "",
                         "next_check": time.time() + 300}}}
due2 = W.which_authors_due(w2, st2)
chk("正在补（bulk=True）时不插队，按 next_check 等",
    all(str(x["uid"]) != "q" for x in due2), [x["uid"] for x in due2])

print("\n[18] 连续扫不到：先只在面板写着，到阈值才打标签 + 停用（人还留在名单里）")
# 这条会写 watch.json 和 user_data.json，两条路径都改到临时目录，
# 绝不能碰真实名单和真实标注（set_watch_enabled 会整份重写 watch.json）
W.WATCH_FILE = os.path.join(TMP, "watch.json")
W.paths.USER_DATA = os.path.join(TMP, "user_data.json")
with open(W.WATCH_FILE, "w", encoding="utf-8") as f:
    json.dump({"authors": [{"uid": "z", "sec_uid": "SECz", "nickname": "Nz",
                           "enabled": True, "added_at": "T"}],
               "settings": dict(CFG)}, f, ensure_ascii=False)
with open(W.paths.USER_DATA, "w", encoding="utf-8") as f:
    json.dump({"accounts": {"z": {"nickname": "Nz", "tags": ["可爱"]}}}, f, ensure_ascii=False)

wz = W.load_watch()
lib_z = {"z": {("2026-01-01", "k")}}      # 本地有 1 条作品


def _tags():
    d = json.load(open(W.paths.USER_DATA, encoding="utf-8"))
    return (d["accounts"].get("z") or {}).get("tags") or []


def _in_watch():
    d = json.load(open(W.WATCH_FILE, encoding="utf-8"))
    return any(str(a.get("uid")) == "z" for a in d.get("authors") or [])


def _enabled():
    """名单里这位现在是不是启用状态（None = 人不在名单里）。"""
    d = json.load(open(W.WATCH_FILE, encoding="utf-8"))
    for a in d.get("authors") or []:
        if str(a.get("uid")) == "z":
            return bool(a.get("enabled", True))
    return None


st = fresh_state()
dy = FakeDy({"SECz": []})
W.scan_round(dy, wz, st, lib_z, {"z": "dirZ"}, douk_ids=set())
chk("第 1 次扫不到：进「扫不到」面板", "z" in (st.get("no_api") or {}))
chk("第 1 次扫不到：不打标签", "扫不到" not in _tags(), _tags())
chk("第 1 次扫不到：还在追更名单、还是启用", _in_watch() and _enabled() is True,
    (_in_watch(), _enabled()))
chk("第 1 次扫不到：已经排好下次探测时间",
    float((st.get("no_api") or {}).get("z", {}).get("next_probe") or 0) > 0,
    (st.get("no_api") or {}).get("z"))

W.scan_round(dy, wz, st, lib_z, {"z": "dirZ"}, douk_ids=set())
chk("第 2 次：仍不打标签", "扫不到" not in _tags(), _tags())
chk("第 2 次：rounds=2", (st.get("no_api") or {}).get("z", {}).get("rounds") == 2,
    (st.get("no_api") or {}).get("z"))

W.scan_round(dy, wz, st, lib_z, {"z": "dirZ"}, douk_ids=set())
chk("第 3 次：打上「扫不到」标签", "扫不到" in _tags(), _tags())
chk("原有标签没被覆盖（可爱 还在）", "可爱" in _tags(), _tags())
chk("第 3 次：停用但**没有**移出名单", _in_watch() and _enabled() is False,
    (_in_watch(), _enabled()))
chk("第 3 次：仍留在「扫不到」面板（能看见、能回探）",
    "z" in (st.get("no_api") or {}), st.get("no_api"))

print("\n[18b] 停用之后：不到点不查，到点回探；探到作品自动恢复启用并摘标签")
wz2 = W.load_watch()
due = W.which_authors_due(wz2, st)
chk("没到探测时间：不派活", all(str(x.get("uid")) != "z" for x in due),
    [x.get("uid") for x in due])
(st["no_api"]["z"])["next_probe"] = 0          # 到点了
due = W.which_authors_due(wz2, st)
probe = [x for x in due if str(x.get("uid")) == "z"]
chk("到点才派一枪探测（标了 _probe）", bool(probe) and probe[0].get("_probe") is True,
    [(x.get("uid"), x.get("_probe")) for x in due])
# 停用中的人排在队尾，不能挤掉正常作者
chk("探测排在正常作者后面", due and str(due[-1].get("uid")) == "z",
    [x.get("uid") for x in due])

W.scan_round(dy, {"authors": probe, "settings": dict(CFG)}, st, lib_z,
             {"z": "dirZ"}, douk_ids=set())
chk("探测还是空：继续停用", _enabled() is False, _enabled())
chk("探测还是空：还在「扫不到」面板", "z" in (st.get("no_api") or {}), st.get("no_api"))
chk("探测还是空：下次探测时间又往后推了",
    float((st.get("no_api") or {}).get("z", {}).get("next_probe") or 0) > time.time(),
    (st.get("no_api") or {}).get("z"))

# 探测这一枪查到了作品（就是本地那条，不会再下一份）：自动恢复 + 摘标签
dy2 = FakeDy({"SECz": [{"aweme_id": "OLD1", "desc": "k",
                        "create_time": int(time.mktime(time.strptime("2026-01-01", "%Y-%m-%d")))}]})
W.scan_round(dy2, {"authors": probe, "settings": dict(CFG)}, st, lib_z,
             {"z": "dirZ"}, douk_ids=set())
chk("探测到了：自动恢复启用", _enabled() is True, _enabled())
chk("探测到了：摘掉「扫不到」标签", "扫不到" not in _tags(), _tags())
chk("探测到了：从「扫不到」面板移出", "z" not in (st.get("no_api") or {}), st.get("no_api"))
chk("恢复后还是启用中的人，不会被再探一次",
    all(str(x.get("uid")) != "z" or not x.get("_probe")
        for x in W.which_authors_due(W.load_watch(), st)))

print("\n[18c] 手动「探测」：名单里的 probe_now 能立刻派活，消费后摘掉")
with open(W.WATCH_FILE, "w", encoding="utf-8") as f:
    json.dump({"authors": [{"uid": "p", "sec_uid": "SECp", "nickname": "Np",
                           "enabled": False, "added_at": "T"},
                          {"uid": "q", "sec_uid": "SECq", "nickname": "Nq",
                           "enabled": True, "added_at": "T"}],
               "settings": dict(CFG)}, f, ensure_ascii=False)
st3 = fresh_state()
st3["authors"]["q"] = {"next_check": 0, "last_ts": 0, "seen": [], "fails": {}}
st3["no_api"] = {"p": {"nickname": "Np", "local": 3, "rounds": 3,
                       "ts": time.time() - 86400,
                       "next_probe": time.time() + 7 * 86400}}
chk("停用且没到回探时间：不派活",
    all(str(x.get("uid")) != "p" for x in W.which_authors_due(W.load_watch(), st3)))
chk("request_probe 写进名单", W.request_probe("p") is True)
_seq2 = [str(x.get("uid")) for x in W.which_authors_due(W.load_watch(), st3)]
_hit = [x for x in W.which_authors_due(W.load_watch(), st3) if str(x.get("uid")) == "p"]
chk("点了探测：立刻派一枪（不管到没到点、停没停用）",
    bool(_hit) and _hit[0].get("_probe") is True, [(x.get("uid"), x.get("_probe")) for x in _hit])
chk("手动探测排在到点的普通作者前面（一轮几百位，排后面要等十几分钟）",
    _seq2 and _seq2[0] == "p", _seq2)
W.clear_probe_flag("p")
chk("消费掉之后不再重复触发",
    all(str(x.get("uid")) != "p" for x in W.which_authors_due(W.load_watch(), st3)))

print("\n[18d] 一键探测停用：只排停用的，启用的不重复排，没 SEC_UID 的跳过")
with open(W.WATCH_FILE, "w", encoding="utf-8") as f:
    json.dump({"authors": [
        {"uid": "on1", "sec_uid": "SECon1", "nickname": "N-on", "enabled": True},
        {"uid": "off1", "sec_uid": "SECoff1", "nickname": "N-off1", "enabled": False},
        {"uid": "off2", "sec_uid": "SECoff2", "nickname": "N-off2", "enabled": False},
        {"uid": "nosec", "nickname": "N-nosec", "enabled": False},
    ], "settings": dict(CFG)}, f, ensure_ascii=False)
_n = W.request_probe_all(only_off=True)
_w = W.load_watch()
chk("只排了 2 位（没 SEC_UID 的跳过）", _n == 2, _n)
chk("两位停用的都排上了",
    all(any(a["uid"] == u and a.get("probe_now") for a in _w["authors"])
        for u in ("off1", "off2")))
chk("启用的那位没被排", not any(a["uid"] == "on1" and a.get("probe_now")
                                for a in _w["authors"]))

print("\n[19] 空描述同日两条：第二条不能被同名文件吞掉（2026-11-12 线上丢过一条）")
DAY = "2026-11-11"
NICK = "K"
AID_OLD = "7700000000000000001"
AID_NEW = "7706922566221432497"
n_old = W.douk_detail_name(DAY, NICK, "", AID_OLD)
n_new = W.douk_detail_name(DAY, NICK, "", AID_NEW)
chk("空描述的名字里必须带作品 ID", AID_OLD in n_old and AID_NEW in n_new, (n_old, n_new))
chk("两条无文案作品不再同名", n_old != n_new, (n_old, n_new))
chk("空描述不做文件名判重（交给 ID 层）",
    W.is_known({(DAY, "")}, {DAY: [""]}, DAY, "", 64) is False)

# 盘上已经躺着一条修复前生成的 `日期-昵称-.mp4`：新作品必须换名字落盘，而不是被当已下
AD = os.path.join(W.paths.SCAN_ROOT, "UIDcoll_K_发布作品")
os.makedirs(AD, exist_ok=True)
legacy = os.path.join(AD, "%s-%s-.mp4" % (DAY, NICK))
with open(legacy, "wb") as f:
    f.write(b"x" * 2048)

_saved = []
_orig_save, _orig_mark = W.save_item, W.douk_mark_downloaded


def _fake_save(cr, urls, base, referer, retry=3):
    _saved.append(base)
    with open(base + ".mp4", "wb") as f:
        f.write(b"y" * 2048)
    return 2048, ".mp4", ""


W.save_item = _fake_save
W.douk_mark_downloaded = lambda a: True
try:
    ct = int(time.mktime(time.strptime(DAY + " 22:35", "%Y-%m-%d %H:%M")))
    aw = {"aweme_id": AID_NEW, "create_time": ct, "desc": "",
          "video": {"play_addr": {"url_list": ["http://127.0.0.1/x"]}}}

    class _FakeCR:
        pass

    class _FakeDy:
        cr = _FakeCR()

    files, size, err = W.download_aweme(_FakeDy(), aw, AD, NICK, 64)
    chk("撞名时换名字照样下下来（不是静默跳过）", files == 1 and err == "", (files, err))
    chk("落盘的名字里带作品 ID", bool(_saved) and _saved[-1].endswith(AID_NEW), _saved)
    chk("老文件没被覆盖", os.path.getsize(legacy) == 2048)

    # 同一件作品再下一遍：这次名字里有 ID、文件也在 → 必须认作「已经下过」
    _saved.clear()
    files2, _s2, err2 = W.download_aweme(_FakeDy(), aw, AD, NICK, 64)
    chk("同一条作品不会重复下", files2 == 0 and err2 == "", (files2, err2))
finally:
    W.save_item, W.douk_mark_downloaded = _orig_save, _orig_mark

print("\n[20] 描述只差表情 + 短描述：必须认作已有（2026-11-13 线上重下过 1 条）")
# 线上那条：库里 `2025-11-24-昵称-可爱.mp4`（DouK 生名时删掉了表情），
# 接口返回 `可爱☺️` → 老口径「剥完要 ≥4 字」把 2 字的 `可爱` 挡在门外 → 又下一份。
D20 = "2025-11-24"
loc20 = {(D20, "可爱")}
di20 = W.build_date_index(loc20)
chk("短描述 + 表情 → 认作已有", W.is_known(loc20, di20, D20, "可爱☺️", 64) is True)
chk("表情在库里、接口没有，也要认", 
    W.is_known({(D20, "可爱😊")}, W.build_date_index({(D20, "可爱😊")}), D20, "可爱", 64) is True)
chk("同一天两条剥完一样 → 有歧义不认（宁可重下也不张冠李戴）",
    W.is_known({(D20, "可爱"), (D20, "可爱😊")},
               W.build_date_index({(D20, "可爱"), (D20, "可爱😊")}), D20, "可爱☺️", 64) is False)
chk("接口侧同日也有两条剥完一样 → 不认（方向二同样看唯一性）",
    W.is_known(loc20, di20, D20, "可爱☺️", 64, bare_total=2) is False)
chk("两边都唯一才认（bare_total=1）",
    W.is_known(loc20, di20, D20, "可爱☺️", 64, bare_total=1) is True)
chk("完全不同的短描述不认", W.is_known(loc20, di20, D20, "好嘛", 64) is False)
chk("单字描述不参与（区分度太低）",
    W.is_known({(D20, "哈")}, W.build_date_index({(D20, "哈")}), D20, "哈😄", 64) is False)
chk("跨天不认（模糊比对只在同一天内做）",
    W.is_known(loc20, di20, "2025-11-25", "可爱☺️", 64) is False)

print("\n[21] 撞名后内容级兜底：同一件作品不会再落一份重复文件")
AD2 = os.path.join(W.paths.SCAN_ROOT, "UIDdup_T_发布作品")
os.makedirs(AD2, exist_ok=True)
D21, NICK21, AID21 = "2025-11-24", "T", "7500000000000000001"
BODY = b"z" * 8192
orig21 = os.path.join(AD2, "%s-%s-可爱.mp4" % (D21, NICK21))
with open(orig21, "wb") as f:
    f.write(BODY)


def _save_same(cr, urls, base, referer, retry=3):
    with open(base + ".mp4", "wb") as f:      # 内容与老文件完全相同
        f.write(BODY)
    return len(BODY), ".mp4", ""


def _save_other(cr, urls, base, referer, retry=3):
    with open(base + ".mp4", "wb") as f:      # 内容不同 = 真的是另一条作品
        f.write(b"q" * len(BODY))
    return len(BODY), ".mp4", ""


ctor = int(time.mktime(time.strptime(D21 + " 21:42", "%Y-%m-%d %H:%M")))
aw21 = {"aweme_id": AID21, "create_time": ctor, "desc": "可爱☺️",
        "video": {"play_addr": {"url_list": ["http://127.0.0.1/x"]}}}


class _FakeCR21:
    pass


class _FakeDy21:
    cr = _FakeCR21()


_mark21 = []
W.douk_mark_downloaded = lambda a: _mark21.append(a) or True
try:
    W.save_item = _save_same
    _out21 = []
    files21, size21, err21 = W.download_aweme(_FakeDy21(), aw21, AD2, NICK21, 64, out=_out21)
    gone21 = not os.path.exists(os.path.join(AD2, "%s-%s-可爱_%s.mp4" % (D21, NICK21, AID21)))
    chk("内容相同 → 不重复落盘", files21 == 0 and err21 == "", (files21, err21))
    chk("重下的副本已删掉", gone21, os.listdir(AD2))
    chk("明细里不记这个已删的文件", _out21 == [], _out21)
    chk("老文件还在", os.path.getsize(orig21) == len(BODY))
    chk("照样记进 DouK 记录（不然以后还会再判一次新）", AID21 in _mark21, _mark21)

    W.save_item = _save_other
    files22, _s22, err22 = W.download_aweme(_FakeDy21(), aw21, AD2, NICK21, 64)
    chk("内容不同 → 当作另一条作品保留（11-12 那条保护不能破）",
        files22 == 1 and err22 == "", (files22, err22))
finally:
    W.save_item, W.douk_mark_downloaded = _orig_save, _orig_mark

print("\n[22] scan_round 真的把「同日接口侧条数」传进判重（接线检查）")
# 只有一条 `可爱☺️`、库里是 `可爱` → 必须跳过；同日再来一条剥完一样的 → 不能跳
# （否则作者同一天两条 `可爱☺️`/`可爱😊`、本地只有一条时，另一条会被永远漏下）
_orig_save2 = W.save_item
W.save_item = lambda *a, **k: (0, "", "自检里不许真联网")
try:
    day22 = _dt.fromtimestamp(now - 3600).strftime("%Y-%m-%d")
    lib22 = {"y": {(day22, "可爱")}}
    st22 = fresh_state()
    dy22 = FakeDy({"SECy": [mk_aweme("y1", int(now - 3600), "可爱☺️")]})
    r22 = W.scan_round(dy22, base_watch(["y"]), st22, lib22, {"y": ""}, douk_ids=set())
    chk("一条且唯一 → 算跳过、不去下", r22["skipped"] == 1 and r22["downloaded"] == 0,
        "skip=%d dl=%d" % (r22["skipped"], r22["downloaded"]))

    st23 = fresh_state()
    dy23 = FakeDy({"SECy": [mk_aweme("y2", int(now - 3600), "可爱😊"),
                            mk_aweme("y3", int(now - 3500), "可爱☺️")]})
    r23 = W.scan_round(dy23, base_watch(["y"]), st23, lib22, {"y": ""}, douk_ids=set())
    chk("同日两条剥完一样 → 都当候选（不冒认已有）", r23["new"] == 2,
        "new=%d skip=%d" % (r23["new"], r23["skipped"]))
finally:
    W.save_item = _orig_save2

print("\n========================")

if FAIL:
    print("失败 %d 项：" % len(FAIL))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
