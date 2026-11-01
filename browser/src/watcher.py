#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载目录文件事件监听（Windows ReadDirectoryChangesW）。

为什么需要它：以前靠「每 5 分钟列一次根目录、比 mtime」来发现新下载。列一次只要 0.09 秒，
但它是**主动去问盘**——E: 机械盘本来能睡，被这么问就一直醒着。事件监听反过来：
由系统在文件真正落盘时通知我们，没下载时一次 IO 都不发。

设计要点：
- 只监听一个根目录（subtree），把事件里的相对路径取第一段（作者文件夹名）就够；
  真正的「哪个文件夹变了」仍由 scanner 自己按目录 mtime 判断（它本来就只扫变了的），
  这里的事件只负责回答一个问题：**现在该不该跑一趟扫描**。
- 合并：事件不断来时不会每来一个就跑扫描，而是等「安静 5 秒」或「最多攒 120 秒」再交付一批，
  这样 DouK 一边下一边写，我们只在它停下来时扫一次。
- 缓冲区溢出（事件多到读不完）时不猜，直接交付「有变化、但不知道是谁」，
  让调用方跑一趟普通增量扫描（scanner 自己会算出变了哪些文件夹）。
- 盘掉线 / 句柄失效：自动重连，不抛异常打死线程。

对外只有三个方法：start() / wait_change(timeout) / stop()。
"""

import ctypes
import os
import re
import threading
import time
from ctypes import wintypes

# ---------- Win32 常量 ----------
FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OVERLAPPED = 0x40000000

FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
FILE_NOTIFY_CHANGE_SIZE = 0x00000008

# 新建 / 改名 / 目录变化 / 文件还在写（大小在变）都要：SIZE 让「下载中」一直算「没安静」，
# 不会在写到一半时抢着扫，从而不会把半截文件的大小记进下载记录库。
NOTIFY_FILTER = (FILE_NOTIFY_CHANGE_FILE_NAME
                 | FILE_NOTIFY_CHANGE_DIR_NAME
                 | FILE_NOTIFY_CHANGE_SIZE)

WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
INFINITE = 0xFFFFFFFF
ERROR_OPERATION_ABORTED = 995
ERROR_ACCESS_DENIED = 5
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

BUF_SIZE = 256 * 1024

UID_FOLDER_RE = re.compile(r"^UID(\d+)_(.*)_发布作品$")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ULONG_PTR),
        ("InternalHigh", ULONG_PTR),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class FILE_NOTIFY_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", wintypes.DWORD),
        ("Action", wintypes.DWORD),
        ("FileNameLength", wintypes.DWORD),
        ("FileName", ctypes.c_wchar * 1),
    ]


_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                             wintypes.HANDLE]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                       wintypes.BOOL, wintypes.DWORD, wintypes.LPDWORD,
                                       wintypes.LPVOID, wintypes.LPVOID]
_k32.ReadDirectoryChangesW.restype = wintypes.BOOL
_k32.GetOverlappedResult.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.LPDWORD,
                                     wintypes.BOOL]
_k32.GetOverlappedResult.restype = wintypes.BOOL
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
_k32.CancelIoEx.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.ResetEvent.argtypes = [wintypes.HANDLE]
_k32.ResetEvent.restype = wintypes.BOOL


class Watcher:
    """监听 root 下所有作者文件夹的变化。

    QUIET   ：事件停下来多久才算这一批结束（秒）
    MAX_WAIT：万一 DouK 一直不停地写，最多攒这么久就先交付一批
    """

    QUIET = 5.0
    MAX_WAIT = 120.0
    ERROR_BACKOFF = 10.0

    def __init__(self, root, log=None):
        self.root = root
        self._log = log or (lambda m: None)
        self._cond = threading.Condition()
        self._changed = set()       # 变化过的顶层文件夹名
        self._unknown = False       # 溢出：知道有变化，但不知道是谁
        self._last_event = 0.0
        self._stop = threading.Event()
        self._thread = None
        self._handle = None
        self._ov = None
        self.alive = False          # 句柄当前是否打开着
        self.error = ""

    # ---------- 对外 ----------
    def start(self):
        self._thread = threading.Thread(target=self._run, name="dir-watcher", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        h, ov = self._handle, self._ov
        if h:
            try:
                if ov is not None:
                    _k32.CancelIoEx(h, ctypes.byref(ov))
            except Exception:
                pass

    def wait_change(self, timeout=None):
        """阻塞等到有一批变化（合并安静期后返回）。
        返回 (folders:set, unknown:bool)；超时无变化返回 (set(), False)。"""
        deadline = None if timeout is None else time.time() + timeout
        with self._cond:
            if not self._changed and not self._unknown:
                remain = None if deadline is None else max(0.0, deadline - time.time())
                if remain == 0:
                    return set(), False
                self._cond.wait(remain)
            if not self._changed and not self._unknown:
                return set(), False

            # 等安静期：下载还在写就别急着扫
            hard = time.time() + self.MAX_WAIT
            while not self._stop.is_set():
                quiet_left = self.QUIET - (time.time() - self._last_event)
                if quiet_left <= 0 or time.time() >= hard:
                    break
                self._cond.wait(min(quiet_left, 0.5))
            out, unknown = set(self._changed), self._unknown
            self._changed.clear()
            self._unknown = False
            return out, unknown

    # ---------- 内部 ----------
    def _push(self, folders, unknown=False):
        with self._cond:
            if unknown:
                self._unknown = True
            if folders:
                self._changed |= folders
            self._last_event = time.time()
            self._cond.notify_all()

    def _open(self):
        h = _k32.CreateFileW(
            self.root,
            FILE_LIST_DIRECTORY,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED,
            None,
        )
        if h == INVALID_HANDLE_VALUE or h is None:
            err = ctypes.get_last_error()
            self.error = "打开目录失败（错误码 %d）" % err
            return None
        ev = _k32.CreateEventW(None, True, False, None)
        if not ev:
            err = ctypes.get_last_error()
            _k32.CloseHandle(h)
            self.error = "建事件失败（错误码 %d）" % err
            return None
        ov = OVERLAPPED()
        ov.hEvent = ev
        return h, ev, ov

    def _close(self, h, ev):
        try:
            if h:
                _k32.CloseHandle(h)
        except Exception:
            pass
        try:
            if ev:
                _k32.CloseHandle(ev)
        except Exception:
            pass

    def _run(self):
        while not self._stop.is_set():
            opened = self._open()
            if not opened:
                self._log("[watch] %s；%.0f 秒后重连" % (self.error, self.ERROR_BACKOFF))
                self._stop.wait(self.ERROR_BACKOFF)
                continue
            h, ev, ov = opened
            self._handle, self._ov = h, ov
            self.alive = True
            self.error = ""
            try:
                self._loop(h, ev, ov)
            except Exception as e:                     # 不打死线程
                self.error = "监听异常：%s" % e
                self._log("[watch] %s" % self.error)
            finally:
                self.alive = False
                self._handle, self._ov = None, None
                self._close(h, ev)
            if not self._stop.is_set():
                self._stop.wait(2.0)

    def _loop(self, h, ev, ov):
        buf = ctypes.create_string_buffer(BUF_SIZE)
        returned = wintypes.DWORD(0)
        while not self._stop.is_set():
            ctypes.memset(ctypes.byref(ov), 0, ctypes.sizeof(ov))
            ov.hEvent = ev
            _k32.ResetEvent(ev)
            ok = _k32.ReadDirectoryChangesW(
                h, buf, BUF_SIZE, True, NOTIFY_FILTER,
                ctypes.byref(returned), ctypes.byref(ov), None,
            )
            if not ok:
                err = ctypes.get_last_error()
                if err in (ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
                    raise OSError(err, "目录不可用")
                self._stop.wait(1.0)
                continue

            # 一直等到真有事件（或 stop() 把这次读取消掉）。
            # 这里**不能**用「每秒超时再重发」的写法：取消和重发之间有个空档，
            # 落在那几十微秒里的事件就永远丢了。停止由 stop() 调 CancelIoEx 触发。
            rc = _k32.WaitForSingleObject(ev, INFINITE)
            if rc != WAIT_OBJECT_0:
                if self._stop.is_set():
                    break
                self._stop.wait(1.0)
                continue

            if not _k32.GetOverlappedResult(h, ctypes.byref(ov), ctypes.byref(returned), False):
                err = ctypes.get_last_error()
                if err == ERROR_OPERATION_ABORTED:
                    if self._stop.is_set():
                        break
                    continue
                self._stop.wait(1.0)
                continue

            if returned.value == 0:
                # 缓冲区溢出：知道有变化，但这一批事件读不全了
                self._push(None, unknown=True)
                continue

            self._push(self._parse(buf, returned.value))

    @staticmethod
    def _parse(buf, nbytes):
        """从 FILE_NOTIFY_INFORMATION 链里取顶层文件夹名。"""
        out = set()
        offset = 0
        while offset < nbytes:
            fni = FILE_NOTIFY_INFORMATION.from_buffer(buf, offset)
            if fni.FileNameLength:
                raw = ctypes.string_at(ctypes.addressof(buf) + offset
                                       + FILE_NOTIFY_INFORMATION.FileName.offset,
                                       fni.FileNameLength)
                rel = raw.decode("utf-16-le", "replace")
                head = rel.replace("\\", "/").split("/")[0]
                if head and UID_FOLDER_RE.match(head):
                    out.add(head)
            if fni.NextEntryOffset == 0:
                break
            offset += fni.NextEntryOffset
        return out
