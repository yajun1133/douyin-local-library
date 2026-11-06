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

FAIL = []


def chk(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + ("" if cond else ("  <- " + str(extra))))
    if not cond:
        FAIL.append(name)


class FakeDy:
    """假的抖音客户端：按 sec_uid → 作品列表（新→旧）应答。

    pages=N 时最多返回 N*20 条，模拟真实翻页；fail_all=True 时全部抛错。
    """

    def __init__(self, works, fail_all=False):
        self.works = works
        self.fail_all = fail_all
        self.calls = []
        self.reqs = 0

    def author_works(self, sec_uid, pages=1, retry=2):
        self.calls.append((sec_uid, pages))
        self.reqs += 1
        if self.fail_all:
            raise RuntimeError("HTTP 444")
        return (self.works.get(sec_uid) or [])[:max(1, pages) * 20]


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

print("\n[1] 四档边界")
for days, want in [(0.5, "hot"), (1.99, "hot"), (2.01, "warm"), (6.9, "warm"),
                   (7.1, "cold"), (29.9, "cold"), (30.1, "idle")]:
    t, _ = W.tier_of({"last_post": now - days * 86400}, CFG, now)
    chk("%.2f 天 -> %s" % (days, want), t == want, t)
chk("没有 last_post -> idle", W.tier_of({}, CFG, now)[0] == "idle")

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

print("\n[9] 作者还不在本地库：跳过，但绝不标记已处理")
st = fresh_state()
st["authors"]["e"] = {"last_ts": int(now - 86400), "seen": [], "downloads": 0,
                      "fails": {}, "last_post": int(now - 86400)}
dy = FakeDy({"SECe": [mk_aweme("e1", int(now - 3600)), mk_aweme("e2", int(now - 1800))]})
w = base_watch(["e"])
r = W.scan_round(dy, w, st, {"e": set()}, {})     # 故意不给文件夹
sa = st["authors"]["e"]
chk("既没下也没记成失败", r["downloaded"] == 0 and r["failed"] == 0,
    "dl=%d failed=%d" % (r["downloaded"], r["failed"]))
chk("两条都没进 seen（作者进库后还能补）", sa["seen"] == [], sa["seen"])
chk("基线没被推进", int(sa["last_ts"]) == int(now - 86400), sa["last_ts"])
chk("失败计数没被污染", not (sa.get("fails") or {}), sa.get("fails"))

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
w_old = mk_aweme("old1", int(now - 3 * 86400), "老作品")
w_new = mk_aweme("new1", int(now - 2 * 3600), "本地还没有的新作品")
st = fresh_state()
dy = FakeDy({"SECe": [w_new, w_old]})
w = base_watch(["e"])
r = W.scan_round(dy, w, st, {"e": {(d_old, W.work_key("老作品"))}}, {"e": "dirE"})
se = st["authors"]["e"]
chk("基线 = 本地最新那天，不是现在",
    abs(int(se["last_ts"]) - (time.mktime(time.strptime(d_old, "%Y-%m-%d")) + 86399)) < 2,
    se["last_ts"])
chk("本地缺的那条被当候选", r["new"] == 1, "new=%d" % r["new"])
chk("这一轮就动手下了（不是等到下一轮）", (r["downloaded"] + r["failed"]) == 1,
    "dl=%d failed=%d" % (r["downloaded"], r["failed"]))
chk("老作品没被重下", "old1" in se["seen"], se["seen"])

print("\n========================")
if FAIL:
    print("失败 %d 项：" % len(FAIL))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
