#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""user_data.json 的唯一普通写入入口。

所有进程都必须遵守：进程内锁 -> Windows 哨兵文件锁 -> 锁内重读校验 ->
字段级合并 -> 唯一临时文件 + fsync -> os.replace。文件损坏时直接失败，
绝不能按空库继续写。
"""

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager

import msvcrt

import paths


_WRITE_LOCK = threading.Lock()
_LOCK_WAIT_SECONDS = 10.0


class UserDataError(RuntimeError):
    """标注文件读不出或结构不合法，拒绝继续写盘。"""


def _target(path=None):
    return os.path.abspath(path or paths.USER_DATA)


def read_validated_user_data(path=None):
    """读取并校验标注；仅允许文件不存在时初始化为空结构。"""
    target = _target(path)
    try:
        with open(target, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except FileNotFoundError:
        return {"accounts": {}}
    except (OSError, ValueError) as exc:
        raise UserDataError("user_data.json 读取失败，已中止操作：%s" % exc) from exc

    if not isinstance(data, dict):
        raise UserDataError("user_data.json 顶层不是对象，已中止操作")
    # 兼容早期把 accounts 内容误写成顶层的结构；解析失败不在此处兜底。
    if "accounts" not in data and data:
        data = {"accounts": data}
    data.setdefault("accounts", {})
    if not isinstance(data["accounts"], dict):
        raise UserDataError("user_data.json 的 accounts 不是对象，已中止操作")
    for key in ("alt_group_names", "status_notes"):
        if key in data and not isinstance(data[key], dict):
            raise UserDataError("user_data.json 的 %s 不是对象，已中止操作" % key)
    return data


@contextmanager
def _interprocess_lock(target):
    """用永久保留的同目录哨兵文件做 Windows 跨进程互斥。"""
    lock_path = target + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+b") as fp:
        if os.fstat(fp.fileno()).st_size == 0:
            fp.write(b"\0")
            fp.flush()
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        delay = 0.05
        while True:
            try:
                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("等待 user_data.json 文件锁超时")
                time.sleep(delay)
                delay = min(delay * 1.5, 0.4)
        try:
            yield
        finally:
            fp.seek(0)
            msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)


def normalize_alt_groups(accounts):
    """把 alt_accounts 归一化成组内全连，返回被修正的条目数。"""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for uid, ua in accounts.items():
        find(uid)
        if not isinstance(ua, dict):
            continue
        arr = ua.get("alt_accounts")
        if not isinstance(arr, list):
            continue
        for value in arr:
            if isinstance(value, str) and value and value != uid:
                union(uid, value)

    groups = {}
    for uid in list(parent.keys()):
        groups.setdefault(find(uid), []).append(uid)

    changed = 0
    for members in groups.values():
        if len(members) < 2:
            ua = accounts.get(members[0])
            if (isinstance(ua, dict) and isinstance(ua.get("alt_accounts"), list)
                    and not ua["alt_accounts"]):
                ua.pop("alt_accounts", None)
                changed += 1
            continue
        members = sorted(members)
        for uid in members:
            others = [x for x in members if x != uid]
            ua = accounts.get(uid)
            if not isinstance(ua, dict):
                ua = accounts[uid] = {}
            current = ua.get("alt_accounts")
            if not isinstance(current, list) or sorted(current) != others:
                ua["alt_accounts"] = others
                changed += 1
    return changed


def _merge_named_mappings(disk, partial):
    if "alt_group_names" in partial and not isinstance(partial["alt_group_names"], dict):
        raise UserDataError("alt_group_names 补丁不是对象")
    if isinstance(partial.get("alt_group_names"), dict):
        names = dict(disk.get("alt_group_names") or {})
        for key, value in partial["alt_group_names"].items():
            if value is None or (isinstance(value, str) and not value.strip()):
                names.pop(key, None)
            else:
                names[key] = value
        disk["alt_group_names"] = names

    if "status_notes" in partial and not isinstance(partial["status_notes"], dict):
        raise UserDataError("status_notes 补丁不是对象")
    if isinstance(partial.get("status_notes"), dict):
        notes = dict(disk.get("status_notes") or {})
        for key, value in partial["status_notes"].items():
            if not isinstance(key, str) or not key:
                continue
            if value is None or (isinstance(value, str) and not value.strip()):
                notes.pop(key, None)
            elif isinstance(value, str):
                notes[key] = value
        if notes:
            disk["status_notes"] = notes
        else:
            disk.pop("status_notes", None)


def _atomic_write(target, data):
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(target), prefix=".user_data.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        for attempt in range(6):
            try:
                os.replace(tmp, target)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.1 * (attempt + 1))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def merge_save_user_data(partial, path=None, before_write=None):
    """在进程内锁和文件锁内重读最新标注，按字段合并后安全写回。"""
    if not isinstance(partial, dict):
        raise UserDataError("user_data 补丁顶层不是对象")
    if "accounts" in partial and not isinstance(partial["accounts"], dict):
        raise UserDataError("accounts 补丁不是对象")
    target = _target(path)
    with _WRITE_LOCK:
        with _interprocess_lock(target):
            disk = read_validated_user_data(target)
            merged = dict(disk["accounts"])
            for uid, patch in (partial.get("accounts") or {}).items():
                if patch is None:
                    merged.pop(uid, None)
                    continue
                if not isinstance(patch, dict):
                    raise UserDataError("账号 %s 的补丁不是对象" % uid)
                base = dict(merged.get(uid) or {})
                for key, value in patch.items():
                    if value is None:
                        base.pop(key, None)
                    else:
                        base[key] = value
                merged[uid] = base
            disk["accounts"] = merged
            _merge_named_mappings(disk, partial)
            fixed = normalize_alt_groups(disk["accounts"])
            if before_write is not None:
                before_write()
            _atomic_write(target, disk)
            return fixed
