#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""严格判定「两组文件是不是同一个作品」——只看画面结构，不看接口、不看时长。

为什么不用像素绝对差：
  同一作品重编码后亮度/对比度会整体偏移，128x128 灰度 MAE 能到 20+，
  但人眼一看就是同一条。所以改用**归一化相关系数 r**（减均值 / 除标准差），
  亮度、对比度差异不影响，只比结构。

为什么用密集采样：
  同一条作品被重新导出后，动作时间轴可能平移 0.3~1 秒。按比例抽 8 帧会整段错位。
  这里用 ffmpeg 一次性解码成 32x32 灰度序列（A 每秒 2 帧、B 每秒 6 帧），
  对 A 的每一帧在 B 的整条时间轴上找最像的一帧 —— 相当于自带 ±0.17s 的对齐搜索。

结论：
  SAME   —— 中位 r ≥ 0.93 且 ≥85% 的帧能配上（r ≥ 0.85）
  DIFF   —— 中位 r ≤ 0.75 或配不上的帧超过一半
  其余    UNSURE（要人看）

用法：python tools/dup_visual_check.py [作者过滤] [输入json]
"""
import os
import sys
import json
import struct
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from paths import FFMPEG, FFPROBE     # noqa: E402

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".flv", ".mkv"}
SZ = 32
SAME_R = 0.93
SAME_FIT = 0.85
DIFF_R = 0.75
DIFF_FIT = 0.50


def duration(path):
    try:
        o = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, text=True, timeout=60)
        return float(o.stdout.strip())
    except Exception:
        return 0.0


def frames(path, fps):
    """返回 [bytes(1024)]，每项是一帧 32x32 灰度。图片直接读。"""
    ext = os.path.splitext(path)[1].lower()
    if ext not in VIDEO_EXT:
        from PIL import Image
        im = Image.open(path).convert("L").resize((SZ, SZ), Image.BILINEAR)
        return [im.tobytes()]
    cmd = [FFMPEG, "-v", "error", "-i", path,
           "-vf", "fps=%s,scale=%d:%d,format=gray" % (fps, SZ, SZ),
           "-f", "rawvideo", "-"]
    p = subprocess.run(cmd, capture_output=True, timeout=600)
    buf = p.stdout
    n = SZ * SZ
    return [buf[i:i + n] for i in range(0, len(buf) - n + 1, n)]


def prep(fr):
    """转成 (去均值后的向量, 模长)。灰度值 0-255。"""
    v = list(fr)
    m = sum(v) / float(len(v))
    d = [x - m for x in v]
    norm = sum(x * x for x in d) ** 0.5
    return d, norm


def corr(a, b, na, nb):
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    s = 0
    for i in range(len(a)):
        s += a[i] * b[i]
    return s / (na * nb)


def compare(a, b):
    fa = frames(a, 2)
    fb = frames(b, 6)
    if not fa or not fb:
        return {"verdict": "UNSURE", "note": "抽帧失败", "n": [len(fa), len(fb)]}
    pb = [prep(f) for f in fb]
    best = []
    for f in fa:
        d, n = prep(f)
        best.append(round(max(corr(d, x, n, xm) for x, xm in pb), 3))
    fit = sum(1 for r in best if r >= SAME_FIT) / float(len(best))
    med = sorted(best)[len(best) // 2]
    if med >= SAME_R and fit >= 0.85:
        v = "SAME"
    elif med <= DIFF_R or fit <= DIFF_FIT:
        v = "DIFF"
    else:
        v = "UNSURE"
    return {"n": [len(fa), len(fb)], "median_r": round(med, 3),
            "min_r": min(best), "fit": round(fit, 2), "verdict": v}


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    src = sys.argv[2] if len(sys.argv) > 2 else "dup_candidates.json"
    groups = json.load(open(os.path.join(here, src), encoding="utf-8"))
    results = []
    for g in groups:
        if only and only not in g["author"]:
            continue
        pairs = [compare(g["files"][i + 1], g["files"][0])
                 for i in range(len(g["files"]) - 1)]
        vs = [p["verdict"] for p in pairs]
        overall = "SAME" if vs and all(v == "SAME" for v in vs) else (
            "DIFF" if any(v == "DIFF" for v in vs) else "UNSURE")
        rec = dict(g, pairs=pairs, verdict=overall)
        results.append(rec)
        print("[%s] %s | %s | %s" % (overall, g["author"], g["date"], g["key"]))
        for p, f in zip(pairs, g["files"][1:]):
            print("    %-6s med_r=%-6s min_r=%-6s fit=%-4s 帧数=%s  %s" % (
                p["verdict"], p.get("median_r", "-"), p.get("min_r", "-"),
                p.get("fit", "-"), p.get("n", "-"), os.path.basename(f)))
        sys.stdout.flush()
    out_name = src.replace(".json", "_visual.json")
    json.dump(results, open(os.path.join(here, out_name), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    c = {}
    for r in results:
        c[r["verdict"]] = c.get(r["verdict"], 0) + 1
    print("\n合计 %s -> %s" % (c, out_name))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
