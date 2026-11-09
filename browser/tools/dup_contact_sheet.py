#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一组里的两个视频/图片按相同时间点拼成上下两排对比图，供人眼确认。

用法：python tools/dup_contact_sheet.py <json> <组序号>
输出：browser/tmp/dup_sheet_-<序号>.jpg（上排 = 第 1 个文件，下排 = 第 2 个文件）
"""
import os
import sys
import json
import subprocess
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from paths import FFMPEG, FFPROBE, APP_ROOT     # noqa: E402
from PIL import Image                            # noqa: E402

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".flv", ".mkv"}
N = 8
W = 200


def dur(p):
    try:
        o = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", p], capture_output=True, text=True)
        return float(o.stdout.strip())
    except Exception:
        return 0.0


def frames(path, tmp, tag):
    ext = os.path.splitext(path)[1].lower()
    if ext not in VIDEO_EXT:
        im = Image.open(path).convert("RGB")
        im.thumbnail((W, W * 3))
        out = os.path.join(tmp, tag + ".jpg")
        im.save(out)
        return [out]
    d = dur(path)
    outs = []
    for i in range(N):
        t = d * (0.03 + 0.94 * i / (N - 1))
        o = os.path.join(tmp, "%s_%d.jpg" % (tag, i))
        subprocess.run([FFMPEG, "-v", "error", "-ss", "%.3f" % t, "-i", path,
                        "-frames:v", "1", "-vf", "scale=%d:-1" % W, "-y", o],
                       capture_output=True, timeout=120)
        if os.path.exists(o):
            outs.append(o)
    return outs


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    groups = json.load(open(os.path.join(here, sys.argv[1]), encoding="utf-8"))
    gi = int(sys.argv[2])
    g = groups[gi]
    tmp = tempfile.mkdtemp(prefix="sheet_")
    rows = [frames(p, tmp, "a") for p in g["files"]]
    imgs = [[Image.open(p).convert("RGB") for p in r] for r in rows]
    h = max(im.height for row in imgs for im in row)
    cols = max(len(r) for r in imgs)
    canvas = Image.new("RGB", (cols * (W + 4), h * len(imgs) + 4 * len(imgs)), "white")
    for ri, row in enumerate(imgs):
        for ci, im in enumerate(row):
            canvas.paste(im, (ci * (W + 4), ri * (h + 4)))
    outdir = os.path.join(APP_ROOT, "tmp")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, "dup_sheet_%d.jpg" % gi)
    canvas.save(out, quality=88)
    print(out)
    print("上排:", os.path.basename(g["files"][0]))
    print("下排:", os.path.basename(g["files"][1]))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
