# -*- coding: utf-8 -*-

"""记轴本 reminder · 后端核心（独立工具）。

依赖纪律：只依赖标准库 + ruler_client.py（BarRuler 客户端），

不 import main/recognizer/reviver_* 等计时器/打轴家族模块。

打包 = reminder_core.py + reminder_server.py + ruler_client.py，

过帧键规划、SendInput 注入、键名表都自带拷贝，与打轴工具互不牵连。

功能：盯一串「目标帧+留言」。每条提醒的执行流程：

  trigger  小目标（刹车点算成负数）借鉴 AFA 开源配方，

           密轮询屏幕捕捉开战瞬间，开战即暂停（冻结 0~2 帧）；

  watch    游戏正常跑，盯着尺子帧数；

  brake    距目标 pre_pause_lead(默认8) 帧时注入暂停键暂停；

  creep    用 AFA 过帧键（calib\\keys.json）逐键慢速逼近；

  notify   停在目标前 stop_margin(默认2) 帧，提醒「还差X帧」+留言，

           自动武装下一条最近的 pending 提醒。

时钟源：BarRuler（totalElapsedFrames），帧级直读，暂停=帧数冻结。

安全护栏（照抄回放执行器的纪律）：未提权拒跑；尺子读不到=安全停止

不注入乱键；逼近中过帧键连续不生效判失败；失败时尽力补一次暂停。

用法：

    python reminder_core.py --selftest

    python reminder_core.py 120 "开技能" --dry     # 只打印计划不注入

    python reminder_core.py 120 "开技能"           # 实跑（管理员+尺子）

"""

import argparse

import ctypes

import ctypes.wintypes

import glob

import json

import os

import random

import subprocess

import threading

import time

from ruler_client import RulerClient, ensure_ruler_running, wait_frozen

LOGIC_FPS = 30

CFG_PATH = os.path.join("calib", "reminder.json")

STORE_PATH = os.path.join("calib", "reminders.json")

KEYS_PATH = os.path.join("calib", "keys.json")

EVENT_LOG_PATH = os.path.join("calib", "reminder_events.log")

STEP_MARGIN = 0.08      # 每个过帧键后额外消化时间，防后续键被吞（同 reviver_play）

SETTLE = 0.25           # 重新暂停后等画面稳定再逼近

STALL_MAX = 3           # 过帧键连续这么多次没推进 -> 判失败

LOST_TIMEOUT = 3.0      # 读到过帧后尺子连续读不到这么久 -> 安全停止

                        # （从头一帧都没读到=没碰过游戏，无限等进关卡）

OUTCOME_CN = {"done": "完成，到点了", "missed": "错过，目标帧已过",

              "clock_lost": "读不到帧数", "failed": "异常停止",

              "aborted": "手动停止"}

BRAKE_LAG = 10          # 暂停键注入到游戏真停的延迟预算（帧）：

                        # 刹车要在 目标-lead-该延迟 时就发键，落点才在 lead 附近；

                        # 实机偏晚可在 calib\reminder.json 调大 brake_lag

_kernel32 = ctypes.windll.kernel32

DEFAULT_HOTKEYS = {

    "step_16ms": "y",

    "step_33ms": "r",

    "step_166ms": "t",

    "pause_select": "w",

}

DEFAULT_CFG = {

    "unpause_key": "space",        # 解除暂停键（默认 Esc）

    "pause_key": "space",        # 暂停/刹车键：直接注入游戏原生 Space（空格）

    "pause_fallback_key": "esc", # 备用暂停键：2x 运行中游戏会忽略空格的

                                 # 暂停请求，ESC 呼出暂停菜单不受倍速影响

                                 # （AFA「按下暂停」同款配方）；置空串禁用

    "pre_pause_lead": 8,         # 距目标多少帧时先暂停

    "stop_margin": 2,            # 停在目标前多少帧，给用户留反应空间

    "brake_lag": BRAKE_LAG,      # 刹车延迟预算（帧），实机偏晚就调大

    "opening_trigger": True,     # 小目标开局触发：开战那帧就暂停（滑动开关）

    "opening_pause_key": "space",# 游戏原生暂停键（开局触发用）；游戏内默认空格

    "hotkeys": dict(DEFAULT_HOTKEYS),

    "direct_step": True,         # 逼近时使用原生高精度脉冲过帧（不依赖外部 AFA 宏，根治跑飞）

    "presets": {

        "notes": ["部署：", "技能：", "撤退：", "注意：", "概率："],

        "frame_deltas": [1, 10, 30, 150, 600],  # 帧数快捷增量（30帧=1秒）

    },

}

# ---------------- 配置 / 存储（calib\reminder.json / reminders.json） ----------------

def load_cfg():

    """读配置；首跑生成默认文件。用户手改的字段保留，缺的补默认。"""

    if not os.path.exists(CFG_PATH):

        save_cfg(DEFAULT_CFG)

        return dict(DEFAULT_CFG)

    try:

        with open(CFG_PATH, encoding="utf-8") as f:

            cfg = json.load(f)

    except Exception:

        return dict(DEFAULT_CFG)

    merged = dict(DEFAULT_CFG)

    merged.update(cfg)

    merged["presets"] = dict(DEFAULT_CFG["presets"])

    merged["presets"].update(cfg.get("presets") or {})

    merged["hotkeys"] = dict(DEFAULT_CFG["hotkeys"])

    merged["hotkeys"].update(cfg.get("hotkeys") or {})

    if "unpause_key" not in cfg:

        merged["unpause_key"] = "space"

    # 注：此处曾有"pause_key 为 f/esc 时强制改回 space"的一次性迁移逻辑，
    # 但它没有退出条件：用户把暂停键设成 esc/f 后，每次读配置都会被静默
    # 打回默认并写盘，表现为"设置页保存后跳回默认键"。用户改什么就存什么，
    # 合法性/冲突校验在 HTTP 层做。

    return merged

def save_cfg(cfg):

    os.makedirs("calib", exist_ok=True)

    with open(CFG_PATH, "w", encoding="utf-8") as f:

        json.dump(cfg, f, ensure_ascii=False, indent=1)

_store_lock = threading.Lock()   # HTTP 线程与引擎线程都会碰存储

def load_reminders():

    try:

        with open(STORE_PATH, encoding="utf-8") as f:

            data = json.load(f)

        return data if isinstance(data, list) else []

    except Exception:

        return []

def save_reminders(rems):

    os.makedirs("calib", exist_ok=True)

    with open(STORE_PATH, "w", encoding="utf-8") as f:

        json.dump(rems, f, ensure_ascii=False, indent=1)

def add_reminder(frame, note):

    with _store_lock:

        rems = load_reminders()

        entry = {"id": "%08x" % random.getrandbits(32),

                 "frame": int(frame), "note": str(note),

                 "state": "pending"}

        rems.append(entry)

        save_reminders(rems)

        return entry

def remove_reminder(rid):

    with _store_lock:

        rems = load_reminders()

        kept = [r for r in rems if r["id"] != rid]

        if len(kept) == len(rems):

            return False

        save_reminders(kept)

        return True

def set_reminder_state(rid, state):

    with _store_lock:

        rems = load_reminders()

        for r in rems:

            if r["id"] == rid:

                r["state"] = state

        save_reminders(rems)

def reset_reminder_states():

    """已到/错过全部重置回 pending（重开一局：错过的帧能再触发）。

    返回改动条数。"""

    with _store_lock:

        rems = load_reminders()

        n = 0

        for r in rems:

            if r.get("state") not in (None, "pending"):

                r["state"] = "pending"

                n += 1

        if n:

            save_reminders(rems)

        return n

def pending_sorted():

    """待触发的提醒，按帧号升序。"""

    with _store_lock:

        rems = load_reminders()

    return sorted((r for r in rems if r.get("state") == "pending"),

                  key=lambda r: r["frame"])

# ---------------- 键注入（自带拷贝，口径同 reviver_inject，不 import 它） ----------------

NAME_VK = {}

for _i in range(26):

    NAME_VK[chr(ord("a") + _i)] = 65 + _i

for _i in range(10):

    NAME_VK[str(_i)] = 48 + _i

for _i in range(12):

    NAME_VK["f%d" % (_i + 1)] = 112 + _i

NAME_VK.update({"space": 32, "enter": 13, "esc": 27, "tab": 9,

                "shift": 16, "ctrl": 17, "alt": 18, "backspace": 8})

INJECT_MAGIC = 0x41524B54   # "ARKT"，与打轴家族同魔数，录制端认得出是自己人

INPUT_KEYBOARD = 1

KEYEVENTF_KEYUP = 0x0002

MAPVK_VK_TO_VSC = 0

ULONG_PTR = ctypes.c_size_t

_user32 = ctypes.windll.user32

_gdi32 = ctypes.windll.gdi32

# 句柄是 64 位指针：不声明 restype 会被截成 32 位 int（x64 上碰巧能用但危险）

_user32.GetDC.restype = ctypes.wintypes.HDC

_gdi32.CreateCompatibleDC.restype = ctypes.wintypes.HDC

_gdi32.CreateCompatibleBitmap.restype = ctypes.wintypes.HBITMAP

_gdi32.SelectObject.restype = ctypes.wintypes.HGDIOBJ

# 传句柄进这些函数同样要 argtypes：不声明时 ctypes 按 32 位 int 收参，

# 64 位句柄高位非零直接 OverflowError，引擎线程当场暴毙（实机踩过）

_user32.GetDC.argtypes = [ctypes.wintypes.HWND]

_user32.ReleaseDC.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HDC]

_user32.PrintWindow.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HDC,

                                ctypes.wintypes.UINT]

_user32.PrintWindow.restype = ctypes.wintypes.BOOL

_gdi32.CreateCompatibleDC.argtypes = [ctypes.wintypes.HDC]

_gdi32.CreateCompatibleBitmap.argtypes = [ctypes.wintypes.HDC,

                                          ctypes.c_int, ctypes.c_int]

_gdi32.SelectObject.argtypes = [ctypes.wintypes.HDC, ctypes.wintypes.HGDIOBJ]

_gdi32.DeleteObject.argtypes = [ctypes.wintypes.HGDIOBJ]

_gdi32.DeleteDC.argtypes = [ctypes.wintypes.HDC]

_gdi32.BitBlt.argtypes = [ctypes.wintypes.HDC, ctypes.c_int, ctypes.c_int,

                          ctypes.c_int, ctypes.c_int, ctypes.wintypes.HDC,

                          ctypes.c_int, ctypes.c_int, ctypes.wintypes.DWORD]

_gdi32.GetDIBits.argtypes = [ctypes.wintypes.HDC, ctypes.wintypes.HBITMAP,

                             ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,

                             ctypes.c_void_p, ctypes.c_uint]

class _KEYBDINPUT(ctypes.Structure):

    _fields_ = [("wVk", ctypes.wintypes.WORD),

                ("wScan", ctypes.wintypes.WORD),

                ("dwFlags", ctypes.wintypes.DWORD),

                ("time", ctypes.wintypes.DWORD),

                ("dwExtraInfo", ULONG_PTR)]

class _MOUSEINPUT(ctypes.Structure):

    # 注入用不到鼠标，但 union 必须和 Windows 的 INPUT 同尺寸（x64=40字节），

    # 少了它 sizeof 偏小，SendInput 会直接返回 0（实机踩过）

    _fields_ = [("dx", ctypes.wintypes.LONG), ("dy", ctypes.wintypes.LONG),

                ("mouseData", ctypes.wintypes.DWORD),

                ("dwFlags", ctypes.wintypes.DWORD),

                ("time", ctypes.wintypes.DWORD),

                ("dwExtraInfo", ULONG_PTR)]

class _INPUTU(ctypes.Union):

    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]

class _INPUT(ctypes.Structure):

    _anonymous_ = ("u",)

    _fields_ = [("type", ctypes.wintypes.DWORD), ("u", _INPUTU)]

def _send_key_event(name, flags):

    vk = NAME_VK.get(name)

    if vk is None:

        return False

    scan = _user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)

    inp = _INPUT(type=INPUT_KEYBOARD)

    inp.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags,

                         time=0, dwExtraInfo=INJECT_MAGIC)

    return _user32.SendInput(1, ctypes.byref(inp),

                             ctypes.sizeof(_INPUT)) == 1

def inject_key(name, hold=0.03):

    """按名字注入一次按下+抬起。False 多半是没提权（UIPI）。"""

    ok = _send_key_event(name, 0)

    time.sleep(hold)

    return _send_key_event(name, KEYEVENTF_KEYUP) and ok

def is_admin():

    try:

        return bool(ctypes.windll.shell32.IsUserAnAdmin())

    except Exception:

        return False

# ---------------- 原生高精度脉冲过帧（QPC 微秒忙等） ----------------

def qpc_sleep(delay_ms):

    """QPC 高精度微秒忙等：避开 Windows 上 time.sleep 15.6ms 粗粒度抖动。"""

    if delay_ms <= 0:

        return

    freq = ctypes.c_int64()

    _kernel32.QueryPerformanceFrequency(ctypes.byref(freq))

    f = float(freq.value)

    if f <= 0:

        time.sleep(delay_ms / 1000.0)

        return

    start = ctypes.c_int64()

    _kernel32.QueryPerformanceCounter(ctypes.byref(start))

    target = start.value + int(delay_ms * f / 1000.0)

    cur = ctypes.c_int64()

    while True:

        _kernel32.QueryPerformanceCounter(ctypes.byref(cur))

        if cur.value >= target:

            break

        rem = (target - cur.value) * 1000.0 / f

        if rem > 3.0:

            time.sleep(0.001)

_DIRECT_STEP_LOCK = threading.Lock()

def direct_step(ms, unpause_key="esc", pause_key="space", inject_fn=None):
    """原生脉冲过帧（对齐 AFA 与 Unity 输入队列）：
    若 unpause_key != pause_key (如 unpause=esc, pause=space，AFA 标准模式)：
      1. ESC Down (解除暂停，游戏立刻开始走帧)
      2. QPC 精准等待 ms 毫秒（游戏净跑动时长严格等于 ms）
      3. Space Down (重新暂停游戏)
      4. 保持 50ms (对齐 AFA USleep(50))
      5. ESC Up + Space Up 释放两键
    若 unpause_key == pause_key (如两者皆为 space)：
      1. Space Down -> 保持 15ms -> Space Up (解暂点按)
      2. QPC 精准等待剩余时长
      3. Space Down -> 保持 50ms -> Space Up (重新暂停点按)
    返回 (ok, msg)。"""
    if inject_fn is not None and inject_fn is not inject_key:
        # Mock / 外部测试注入器兼容通道
        inject_fn(unpause_key)
        qpc_sleep(ms)
        inject_fn(pause_key)
        return True, "已原生过帧 %.1fms" % ms

    if not is_admin():
        return False, "未以管理员运行"

    # 防连击与多线程重入冲突：一次过帧未完结前拒绝并发注入
    if not _DIRECT_STEP_LOCK.acquire(blocking=False):
        return False, "过帧进行中，已忽略连击"

    try:
        u_key = str(unpause_key).lower()
        p_key = str(pause_key).lower()

        if u_key != p_key:
            # AFA 经典双键交叠模式 (ESC解暂 + Space暂停)
            if not _send_key_event(u_key, 0):
                return False, "解暂停键 %s 注入失败" % u_key
            qpc_sleep(ms)
            if not _send_key_event(p_key, 0):
                _send_key_event(u_key, KEYEVENTF_KEYUP)
                return False, "暂停键 %s 注入失败" % p_key
            qpc_sleep(50.0)
            _send_key_event(u_key, KEYEVENTF_KEYUP)
            _send_key_event(p_key, KEYEVENTF_KEYUP)
        else:
            # 单键双脉冲模式 (如 Space解暂 + Space暂停)
            hold1 = min(20.0, max(8.0, ms * 0.4))
            rem_sleep = max(15.0, ms - hold1)
            if not _send_key_event(u_key, 0):
                return False, "解暂停键 %s 注入失败" % u_key
            qpc_sleep(hold1)
            _send_key_event(u_key, KEYEVENTF_KEYUP)
            qpc_sleep(rem_sleep)
            if not _send_key_event(p_key, 0):
                return False, "暂停键 %s 注入失败" % p_key
            qpc_sleep(50.0)
            _send_key_event(p_key, KEYEVENTF_KEYUP)

        return True, "已原生过帧 %.1fms" % ms
    finally:
        _DIRECT_STEP_LOCK.release()

# ---------------- Win32 Touch Injection (原生暂停选中干员) ----------------

class POINT(ctypes.Structure):

    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

class RECT(ctypes.Structure):

    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),

                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

class POINTER_INFO(ctypes.Structure):

    _fields_ = [

        ("pointerType", ctypes.c_uint32),

        ("pointerId", ctypes.c_uint32),

        ("frameId", ctypes.c_uint32),

        ("pointerFlags", ctypes.c_uint32),

        ("sourceDevice", ctypes.c_void_p),

        ("hwndTarget", ctypes.c_void_p),

        ("ptPixelLocation", POINT),

        ("ptHimetricLocation", POINT),

        ("ptPixelLocationRaw", POINT),

        ("ptHimetricLocationRaw", POINT),

        ("dwTime", ctypes.c_uint32),

        ("historyCount", ctypes.c_uint32),

        ("InputData", ctypes.c_int32),

        ("dwKeyStates", ctypes.c_uint32),

        ("PerformanceCount", ctypes.c_uint64),

        ("ButtonChangeType", ctypes.c_uint32),

    ]

class POINTER_TOUCH_INFO(ctypes.Structure):

    _fields_ = [

        ("pointerInfo", POINTER_INFO),

        ("touchFlags", ctypes.c_uint32),

        ("touchMask", ctypes.c_uint32),

        ("rcContact", RECT),

        ("rcContactRaw", RECT),

        ("orientation", ctypes.c_uint32),

        ("pressure", ctypes.c_uint32),

    ]

class TouchInjector:
    _init_done = False
    _lock = threading.Lock()

    @classmethod
    def init(cls, max_count=3, feedback_mode=1):
        with cls._lock:
            if cls._init_done:
                return True
            try:
                _user32.InitializeTouchInjection.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
                _user32.InitializeTouchInjection.restype = ctypes.c_int
                _user32.InjectTouchInput.argtypes = [ctypes.c_uint32, ctypes.POINTER(POINTER_TOUCH_INFO)]
                _user32.InjectTouchInput.restype = ctypes.c_int
                res = _user32.InitializeTouchInjection(max_count, feedback_mode)
                cls._init_done = bool(res)
                return cls._init_done
            except Exception:
                return False

    @classmethod
    def tap(cls, screen_x, screen_y):
        """对齐 AFA TouchInjector.Tap：零 Sleep 瞬发，耗时 <0.05ms，单帧内闭环"""
        if not cls.init():
            return False
        # Touch Down
        t_down = POINTER_TOUCH_INFO()
        t_down.pointerInfo.pointerType = 2    # PT_TOUCH
        t_down.pointerInfo.pointerFlags = 0x00010006  # INRANGE | INCONTACT | DOWN
        t_down.pointerInfo.ptPixelLocation.x = int(screen_x)
        t_down.pointerInfo.ptPixelLocation.y = int(screen_y)
        t_down.touchMask = 7
        t_down.rcContact.left = int(screen_x) - 2
        t_down.rcContact.top = int(screen_y) - 2
        t_down.rcContact.right = int(screen_x) + 2
        t_down.rcContact.bottom = int(screen_y) + 2
        t_down.orientation = 90
        t_down.pressure = 32000
        _user32.InjectTouchInput(1, ctypes.byref(t_down))

        # Touch Update
        t_up = POINTER_TOUCH_INFO()
        t_up.pointerInfo.pointerType = 2
        t_up.pointerInfo.pointerFlags = 0x00020006  # INRANGE | INCONTACT | UPDATE
        t_up.pointerInfo.ptPixelLocation.x = int(screen_x)
        t_up.pointerInfo.ptPixelLocation.y = int(screen_y)
        t_up.touchMask = 7
        t_up.rcContact = t_down.rcContact
        t_up.orientation = 90
        t_up.pressure = 32000
        _user32.InjectTouchInput(1, ctypes.byref(t_up))

        # Touch Up
        t_release = POINTER_TOUCH_INFO()
        t_release.pointerInfo.pointerType = 2
        t_release.pointerInfo.pointerFlags = 0x00040000  # UP
        t_release.pointerInfo.ptPixelLocation.x = int(screen_x)
        t_release.pointerInfo.ptPixelLocation.y = int(screen_y)
        _user32.InjectTouchInput(1, ctypes.byref(t_release))
        return True

def _grab_rect(sx, sy, w, h):

    """抓屏幕矩形像素 -> (BGRA bytes, w, h)；失败返回 (None, 0, 0)。

    口径同 _TriggerEye._pixels：GetDC(None)+BitBlt+GetDIBits。"""

    dc = _user32.GetDC(None)

    if not dc:

        return None, 0, 0

    mem = _gdi32.CreateCompatibleDC(dc)

    bmp = _gdi32.CreateCompatibleBitmap(dc, w, h)

    old = _gdi32.SelectObject(mem, bmp)

    try:

        if not _gdi32.BitBlt(mem, 0, 0, w, h, dc, sx, sy, 0x00CC0020):

            return None, 0, 0

        bmi = _BITMAPINFOHEADER()

        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)

        bmi.biWidth, bmi.biHeight = w, -h          # 负高=自上而下

        bmi.biPlanes, bmi.biBitCount = 1, 32

        buf = ctypes.create_string_buffer(w * h * 4)

        if _gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0) != h:

            return None, 0, 0

        return buf.raw, w, h

    finally:

        _gdi32.SelectObject(mem, old)

        _gdi32.DeleteObject(bmp)

        _gdi32.DeleteDC(mem)

        _user32.ReleaseDC(None, dc)


def _grab_window_client(hwnd, w, h):

    """按窗口句柄抓客户区，避免共享软件覆盖桌面时污染像素。

    PrintWindow 是首选；失败时返回 None，由调用方回退到桌面 BitBlt。
    该函数只负责客户区，不改变现有桌面截图实现。"""

    if not hwnd or w <= 0 or h <= 0:

        return None

    dc = _user32.GetDC(hwnd)

    if not dc:

        return None

    mem = _gdi32.CreateCompatibleDC(dc)

    bmp = _gdi32.CreateCompatibleBitmap(dc, w, h)

    old = _gdi32.SelectObject(mem, bmp)

    try:

        # PW_CLIENTONLY keeps the returned coordinates aligned with GetClientRect.
        if not _user32.PrintWindow(hwnd, mem, 0x00000001 | 0x00000002):

            return None

        bmi = _BITMAPINFOHEADER()

        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)

        bmi.biWidth, bmi.biHeight = w, -h

        bmi.biPlanes, bmi.biBitCount = 1, 32

        buf = ctypes.create_string_buffer(w * h * 4)

        if _gdi32.GetDIBits(mem, bmp, 0, h, buf,

                            ctypes.byref(bmi), 0) != h:

            return None

        return buf.raw

    finally:

        _gdi32.SelectObject(mem, old)

        _gdi32.DeleteObject(bmp)

        _gdi32.DeleteDC(mem)

        _user32.ReleaseDC(hwnd, dc)


def _scan_content_rect(w, h, lum):

    """黑边检测（纯函数，便于单测）：lum(x,y)->0..255 或 None（读取失败按非黑处理）。

    返回内容矩形 (x0, y0, cw, ch)；检测结果不合理时退回整个客户区。"""

    if w < 40 or h < 40:

        return 0, 0, w, h

    th = 26                              # 黑边亮度阈值（近黑才算边）

    rows = [int(h * f) for f in (0.2, 0.35, 0.5, 0.65, 0.8)]

    cols = [int(w * f) for f in (0.2, 0.35, 0.5, 0.65, 0.8)]

    step_x = max(1, w // 240)

    step_y = max(1, h // 240)

    def col_dark(x):

        for y in rows:

            v = lum(x, y)

            if v is None or v >= th:

                return False

        return True

    def row_dark(y):

        for x in cols:

            v = lum(x, y)

            if v is None or v >= th:

                return False

        return True

    x0 = 0

    while x0 < int(w * 0.42) and col_dark(x0):

        x0 += step_x

    x1 = w - 1

    while w - 1 - x1 < int(w * 0.42) and col_dark(x1):

        x1 -= step_x

    y0 = 0

    while y0 < int(h * 0.42) and row_dark(y0):

        y0 += step_y

    y1 = h - 1

    while h - 1 - y1 < int(h * 0.42) and row_dark(y1):

        y1 -= step_y

    # 太窄的"边"当噪声抹掉，避免暗色地图边角被误判

    if x0 < w * 0.02:

        x0 = 0

    if w - 1 - x1 < w * 0.02:

        x1 = w - 1

    if y0 < h * 0.02:

        y0 = 0

    if h - 1 - y1 < h * 0.02:

        y1 = h - 1

    cw, ch = x1 + 1 - x0, y1 + 1 - y0

    if cw < w * 0.5 or ch < h * 0.5:

        return 0, 0, w, h

    return x0, y0, cw, ch


def _content_rect(hwnd):

    """客户区 -> (sx, sy, x0, y0, cw, ch)：屏幕原点 + 游戏内容矩形。

    游戏内容固定 16:9：窗口客户区就是 16:9 时直接用（零开销、行为不变）；

    比例不符时抓屏检测黑边，把比例坐标锚定到实际内容区，防止非 16:9

    窗口（带黑边/异形窗口）里触控点击偏移。任何失败退回整个客户区。"""

    r = ctypes.wintypes.RECT()

    if not _user32.GetClientRect(hwnd, ctypes.byref(r)):

        return None

    w, h = r.right, r.bottom

    if w <= 0 or h <= 0:

        return None

    pt = ctypes.wintypes.POINT(0, 0)

    if not _user32.ClientToScreen(hwnd, ctypes.byref(pt)):

        return None

    sx, sy = pt.x, pt.y

    if abs(w / h - 16 / 9) <= 0.02:

        return sx, sy, 0, 0, w, h

    raw, bw, bh = _grab_rect(sx, sy, w, h)

    if raw is None:

        return sx, sy, 0, 0, w, h

    def lum(x, y):

        i = (y * bw + x) * 4

        return max(raw[i + 2], raw[i + 1], raw[i])   # BGRA 最大通道当亮度

    x0, y0, cw, ch = _scan_content_rect(w, h, lum)

    return sx, sy, x0, y0, cw, ch


def touch_pause_select(client_x=None, client_y=None):
    """原生暂停选中干员（完全对齐 AFA ActionPauseSelect）：
    通过极速 Win32 客户端转换（<0.1ms）与零延时三点触控穿透暂停界面激活干员。
    全流程在 0.2ms 内注入 Windows 触控队列，游戏在同一渲染帧内消化解暂+选中+重暂，
    实现秒选且过帧为 0。返回 (ok, msg)。"""
    hwnd = _find_game_hwnd()
    if not hwnd:
        return False, "未找到游戏窗口"

    r = ctypes.wintypes.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(r)):
        return False, "获取客户区失败"
    w, h = r.right, r.bottom
    if w <= 0 or h <= 0:
        return False, "游戏窗口尺寸无效"

    pt = ctypes.wintypes.POINT(0, 0)
    if not _user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return False, "屏幕坐标转换失败"
    sx, sy = pt.x, pt.y

    cur_pt = ctypes.wintypes.POINT(0, 0)
    _user32.GetCursorPos(ctypes.byref(cur_pt))

    if client_x is None or client_y is None:
        target_sx, target_sy = cur_pt.x, cur_pt.y
    else:
        target_sx, target_sy = sx + int(client_x), sy + int(client_y)

    # 左右暂停按钮屏幕坐标（AFA 官方黄金比例）：
    pl_sx = sx + int(w * 0.9400)
    pl_sy = sy + int(h * 0.0700)
    pr_sx = sx + int(w * 0.9650)
    pr_sy = sy + int(h * 0.0700)

    # 瞬间三点触控：左暂停 -> 干员 -> 右暂停（对齐 AFA 零 Sleep 瞬发）
    TouchInjector.tap(pl_sx, pl_sy)
    TouchInjector.tap(target_sx, target_sy)
    TouchInjector.tap(pr_sx, pr_sy)

    # 恢复鼠标指针
    _user32.SetCursorPos(cur_pt.x, cur_pt.y)
    return True, "已瞬间暂停选中"

# ---------------- 原生热键监听器（游戏前台守护） ----------------

class NativeHotkeyHub:

    """全局热键监听器（原生三档过帧与暂停选中）。

    仅在游戏窗口处于前台时响应热键，不干扰其他应用。"""

    _instance = None

    _lock = threading.Lock()

    def __init__(self, cfg=None):

        self.cfg = cfg or load_cfg()

        self._running = False

        self._thread = None

        self._lock = threading.Lock()

        self._last_trigger_ts = 0.0

    @classmethod

    def get_instance(cls):

        with cls._lock:

            if cls._instance is None:

                cls._instance = cls()

            return cls._instance

    def update_cfg(self, cfg):

        with self._lock:

            self.cfg = dict(cfg)

    def start(self):

        with self._lock:

            if self._running:

                return True

            self._running = True

            self._thread = threading.Thread(target=self._run, daemon=True)

            self._thread.start()

            return True

    def stop(self):

        with self._lock:

            self._running = False

    def _run(self):

        from rawkeys import RawKeyListener

        def on_key(name):

            if not self._running:

                return

            now = time.time()

            if now - self._last_trigger_ts < 0.12:

                return

            hwnd = _find_game_hwnd()

            if not hwnd:

                return

            fg = _user32.GetForegroundWindow()
            meeting_fg = _is_meeting_foreground(fg)

            if fg != hwnd and not meeting_fg:

                return

            with self._lock:

                cfg = self.cfg

                hotkeys = cfg.get("hotkeys") or DEFAULT_HOTKEYS

                unpause_key = cfg.get("unpause_key", "esc")

                pause_key = cfg.get("pause_key", "space")

            name_low = str(name).lower()

            action_keys = {
                str(hotkeys.get("step_16ms", "y")).lower(),
                str(hotkeys.get("step_33ms", "r")).lower(),
                str(hotkeys.get("step_166ms", "t")).lower(),
                str(hotkeys.get("pause_select", "w")).lower(),
            }

            # When Tencent Meeting owns focus during sharing, move focus to the
            # game only for a configured action key. Other applications retain
            # the original strict foreground behavior above.
            if fg != hwnd and name_low in action_keys:

                _switched, found, _prev = _focus_game()

                if not found or _user32.GetForegroundWindow() != hwnd:

                    return

            if name_low == str(hotkeys.get("step_16ms", "y")).lower():

                self._last_trigger_ts = now

                direct_step(16.0, unpause_key=unpause_key, pause_key=pause_key)

            elif name_low == str(hotkeys.get("step_33ms", "r")).lower():

                self._last_trigger_ts = now

                direct_step(30.0, unpause_key=unpause_key, pause_key=pause_key)

            elif name_low == str(hotkeys.get("step_166ms", "t")).lower():

                self._last_trigger_ts = now

                direct_step(166.0, unpause_key=unpause_key, pause_key=pause_key)

            elif name_low == str(hotkeys.get("pause_select", "w")).lower():

                self._last_trigger_ts = now

                touch_pause_select()

        listener = RawKeyListener(on_key)

        if listener.start():

            while self._running:

                time.sleep(0.5)

# ---------------- 游戏窗口聚焦（自带 ctypes 拷贝，口径同 game_window.py） ----------------

GAME_KEYWORDS = ("明日方舟", "arknights", "mumu", "nemu", "ldplayer", "nox", "bluestacks", "雷电", "夜神", "逍遥", "模拟器")

MEETING_KEYWORDS = ("腾讯会议", "Tencent Meeting")

def _find_game_hwnd():

    """按标题找游戏窗口：恰叫『明日方舟』的优先，其次最大可见窗口。"""

    u = _user32

    console = ctypes.windll.kernel32.GetConsoleWindow()

    hits = []

    enum = ctypes.WINFUNCTYPE(ctypes.c_bool,

                              ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def cb(hwnd, _):

        if hwnd == console or not u.IsWindowVisible(hwnd):

            return True

        n = u.GetWindowTextLengthW(hwnd)

        if n <= 0:

            return True

        buf = ctypes.create_unicode_buffer(n + 1)

        u.GetWindowTextW(hwnd, buf, n + 1)

        title = buf.value

        low = title.lower()

        if not any(k.lower() in low for k in GAME_KEYWORDS):

            return True

        rect = ctypes.wintypes.RECT()

        u.GetWindowRect(hwnd, ctypes.byref(rect))

        w = rect.right - rect.left

        h = rect.bottom - rect.top

        if w < 200 or h < 200:      # 最小化/小得不像游戏画面

            return True

        hits.append((title.strip() == "明日方舟", w * h, hwnd))

        return True

    u.EnumWindows(enum(cb), 0)

    if not hits:

        return None

    hits.sort(key=lambda x: (x[0], x[1]), reverse=True)

    return hits[0][2]


def _is_meeting_foreground(hwnd):

    """共享兼容窄例外：只识别腾讯会议前台，不放宽到任意应用。"""

    if not hwnd:

        return False

    n = _user32.GetWindowTextLengthW(hwnd)

    if n <= 0:

        return False

    buf = ctypes.create_unicode_buffer(n + 1)

    _user32.GetWindowTextW(hwnd, buf, n + 1)

    title = buf.value.lower()

    return any(keyword.lower() in title for keyword in MEETING_KEYWORDS)

def _focus_game():

    """把游戏窗口切到前台。返回 (是否切换, 是否找到, 切前前台句柄)。"""

    u = _user32

    hwnd = _find_game_hwnd()

    if not hwnd:

        return False, False, None

    prev = u.GetForegroundWindow()

    if prev == hwnd:

        return False, True, None

    if u.IsIconic(hwnd):

        u.ShowWindow(hwnd, 9)               # SW_RESTORE

    u.keybd_event(0x12, 0, 0, 0)            # 按一下 Alt 换取前台权限

    u.keybd_event(0x12, 0, 2, 0)

    u.SetForegroundWindow(hwnd)

    time.sleep(0.2)                         # 等切换落定再发键

    return True, True, prev

# ---------------- 开局触发：屏幕采样器（借鉴 AFA 开源配方，GPLv3） ----------------

# 配方来源 CloudTracey/arknights-frame-assistant（本地备查 vendor/afa-src）：

# 黑屏 -> Loading 白字 -> 等 2s -> 密轮询右上「倍速按钮」，

# 它出现的那一瞬间发游戏原生暂停键。反应式刹车有 ~8 帧延迟，

# 毫秒级触发能冻在 0~2 帧——小目标（危机合约 2 帧/0 帧放置）只能靠它。

_BLACK_PTS = [(x, y) for y in (.05, .95) for x in (.05, .25, .5, .75, .95)] + \
    [(.05, .5), (.95, .5),                       # 左右边中点

     (.25, .25), (.75, .25), (.5, .5),           # 内部四点+正中心

     (.25, .75), (.75, .75)]

_LOAD_LINES = [(.750000, .980000, .953472),      # 右下 Loading... 文字（放宽扫描区间自适应多比例）
               (.400000, .600000, .953472),      # 底部中央
               (.400000, .600000, .520833)]      # 屏幕中央

_LOAD_RED = (0xA6, 0x00, 0x00)                   # 红/蓝按钮 = 非战斗场景，中止识别
_LOAD_BLUE = (0x00, 0x70, 0xA3)

def _speed_rect(x, y, w, h):
    """右上角倍速按钮（1X/2X）全分辨率与全比例大一统自适应采样区：
    严格对齐 Unity CanvasScaler (Match Width or Height) 动态缩放模型：
    基准设计分辨率 1920x1080 (16:9)，UI 缩放因子 S = min(w / 1920, h / 1080)。
    
    - 宽屏 (>=16:9，如 16:9, 17:9, 21:9)：S = h / 1080（按高度等比缩放）；
    - 窄屏 (<16:9，如 16:10, 4:3, 5:4)：S = w / 1920（按宽度等比缩放，防止卡牌溢出）；
    
    统一采样几何包围盒：
      x0 = (x + w) - int(340 * S)
      x1 = (x + w) - int(175 * S)
      y0 = y + int(40 * S)
      y1 = y + int(125 * S)
    """
    scale = min(w / 1920.0, h / 1080.0) if (w > 0 and h > 0) else 1.0
    right_x = x + w
    x0 = right_x - int(340 * scale)
    x1 = right_x - int(175 * scale)
    
    # 窗口安全边界保护
    x0 = max(x + int(0.40 * w), x0)
    x1 = min(right_x - 1, max(x0 + 4, x1))
    
    y0 = y + int(40 * scale)
    y1 = y + int(125 * scale)
    y0 = max(y, y0)
    y1 = min(y + h - 1, max(y0 + 4, y1))
    
    return x0, y0, max(1, x1 - x0), max(1, y1 - y0)

class _BITMAPINFOHEADER(ctypes.Structure):

    """GetDIBits 用：整块读倍速按钮区像素（逐点 GetPixel 太慢，见 speed_button）"""

    _fields_ = [("biSize", ctypes.wintypes.DWORD),

                ("biWidth", ctypes.c_long),

                ("biHeight", ctypes.c_long),

                ("biPlanes", ctypes.wintypes.WORD),

                ("biBitCount", ctypes.wintypes.WORD),

                ("biCompression", ctypes.wintypes.DWORD),

                ("biSizeImage", ctypes.wintypes.DWORD),

                ("biXPelsPerMeter", ctypes.c_long),

                ("biYPelsPerMeter", ctypes.c_long),

                ("biClrUsed", ctypes.wintypes.DWORD),

                ("biClrImportant", ctypes.wintypes.DWORD)]

class _TriggerEye:

    """开局触发的屏幕采样器：直接读屏幕像素，毫秒级。

    坐标用游戏窗口客户区比例（不依赖分辨率/窗口位置）；读屏幕 DC，

    要求游戏在前台（引擎在触发前段负责把游戏切到前台）。

    DPI：线程切 per-monitor 感知，防缩放下比例坐标与物理像素错位。"""

    def __init__(self):

        self._hwnd = None

        self._hwnd_ts = 0.0

        self._window_raw = None

        self._window_raw_ts = 0.0

        try:

            _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-3))

        except Exception:

            pass

    def _client(self):

        """游戏客户区 -> (left, top, w, h) 屏幕坐标；找不到窗口返回 None。

        句柄缓存 1s：密轮询段每几毫秒一次，EnumWindows 不能次次跑。"""

        now = time.time()

        if self._hwnd and now - self._hwnd_ts < 1.0 \
                and _user32.IsWindow(self._hwnd):

            hwnd = self._hwnd

        else:

            hwnd = _find_game_hwnd()

            self._hwnd, self._hwnd_ts = hwnd, now

        if not hwnd:

            return None

        r = ctypes.wintypes.RECT()

        if not _user32.GetClientRect(hwnd, ctypes.byref(r)):

            return None

        pt = ctypes.wintypes.POINT(0, 0)

        _user32.ClientToScreen(hwnd, ctypes.byref(pt))

        return pt.x, pt.y, r.right, r.bottom

    def _window_frame(self, c):

        """共享控制窗抢焦点时按窗口抓帧，其余情况保持原桌面采样。"""

        if not _is_meeting_foreground(_user32.GetForegroundWindow()):

            return None

        now = time.perf_counter()

        if self._window_raw is not None and now - self._window_raw_ts < 0.03:

            return self._window_raw

        raw = _grab_window_client(self._hwnd, c[2], c[3])

        if raw is None:

            self._window_raw = None

            return None

        self._window_raw = raw

        self._window_raw_ts = now

        return raw

    def _pixels(self, pts):

        """批量读客户区比例坐标点 -> [(r,g,b)...]；窗口不在返回 None。

        一次 BitBlt 抓点集包围盒再内存取色：逐点 GetPixel 慢，

        且句柄不声明 argtypes 会在 64 位上 OverflowError 崩线程

        （实机踩过：盯梢状态无声消失）。"""

        c = self._client()

        if not c:

            return None

        x, y, w, h = c

        xs = [int(rx * w) for rx, _ry in pts]

        ys = [int(ry * h) for _rx, ry in pts]

        bx0, by0 = min(xs), min(ys)

        bw = max(xs) - bx0 + 1

        bh = max(ys) - by0 + 1

        dc = _user32.GetDC(None)

        mem = _gdi32.CreateCompatibleDC(dc)

        bmp = _gdi32.CreateCompatibleBitmap(dc, bw, bh)

        old = _gdi32.SelectObject(mem, bmp)

        try:

            if not _gdi32.BitBlt(mem, 0, 0, bw, bh, dc, x + bx0, y + by0,

                                 0x00CC0020):            # SRCCOPY

                return None

            bmi = _BITMAPINFOHEADER()

            bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)

            bmi.biWidth, bmi.biHeight = bw, -bh          # 负高=自上而下

            bmi.biPlanes, bmi.biBitCount = 1, 32

            buf = ctypes.create_string_buffer(bw * bh * 4)

            if _gdi32.GetDIBits(mem, bmp, 0, bh, buf,

                                ctypes.byref(bmi), 0) != bh:

                return None

            raw = buf.raw

            out = []

            for rx, ry in pts:

                i = ((int(ry * h) - by0) * bw + (int(rx * w) - bx0)) * 4

                out.append((raw[i + 2], raw[i + 1], raw[i]))   # BGRA -> RGB

            return out

        finally:

            _gdi32.SelectObject(mem, old)

            _gdi32.DeleteObject(bmp)

            _gdi32.DeleteDC(mem)

            _user32.ReleaseDC(None, dc)

    def black(self, tol=10):

        """17 点全屏黑屏判定，允许 1 点被游戏鼠标遮挡（AFA 口径）。"""

        vals = self._pixels(_BLACK_PTS)

        if vals is None:

            return False

        miss = sum(1 for v in vals if v is None or

                   v[0] > tol or v[1] > tol or v[2] > tol)

        return miss <= 1

    def _line_has(self, x0, x1, y, rgb, tol, n=24):

        """水平扫描线上是否找得到指定颜色。"""

        vals = self._pixels([(x0 + (x1 - x0) * i / (n - 1), y)

                             for i in range(n)])

        if vals is None:

            return False

        return any(v is not None and abs(v[0] - rgb[0]) <= tol and

                   abs(v[1] - rgb[1]) <= tol and abs(v[2] - rgb[2]) <= tol

                   for v in vals)

    def loading(self):

        """Loading 扫描：1=白字 Loading；2=红/蓝按钮（非战斗场景，中止）；0=都不是。"""

        x0, x1, y = _LOAD_LINES[0]

        if self._line_has(x0, x1, y, _LOAD_RED, 50) or \
                self._line_has(x0, x1, y, _LOAD_BLUE, 50):

            return 2

        for x0, x1, y in _LOAD_LINES:

            if not self._line_has(x0, x1, y, (255, 255, 255), 10):

                return 0

        return 1

    def speed_button(self):

        """右上倍速按钮区找白像素：按钮出现=战斗开始的那一渲染帧。

        必须 BitBlt 整块截图再内存扫描（AFA PixelSearch 同机理）：

        屏幕 DC 上逐点 GetPixel 单点就是毫秒级，几百个点扫一轮要

        秒级，开战瞬间根本抓不住（实机踩过：拖到第 51 帧才暂停）。"""

        c = self._client()

        if not c:

            return False

        x0, y0, bw, bh = _speed_rect(*c)

        # PrintWindow captures the target HWND rather than the shared desktop.
        # Keep the existing ROI scan and desktop BitBlt path as fallback.
        window_raw = self._window_frame(c)

        if window_raw is not None:

            x, y, w, h = c

            local_x0 = max(0, x0 - x)

            local_y0 = max(0, y0 - y)

            local_x1 = min(w, local_x0 + bw)

            local_y1 = min(h, local_y0 + bh)

            n = 0

            for row in range(local_y0, local_y1):

                start = (row * w + local_x0) * 4

                for i in range(start, start + (local_x1 - local_x0) * 4, 4):

                    if window_raw[i] >= 225 and window_raw[i + 1] >= 225 \
                            and window_raw[i + 2] >= 225:

                        n += 1

                        if n >= 12:

                            return True

            # PrintWindow can return a blank frame for some DirectX windows;
            # fall through to the desktop path rather than losing detection.

        dc = _user32.GetDC(None)

        mem = _gdi32.CreateCompatibleDC(dc)

        bmp = _gdi32.CreateCompatibleBitmap(dc, bw, bh)

        old = _gdi32.SelectObject(mem, bmp)

        try:

            if not _gdi32.BitBlt(mem, 0, 0, bw, bh, dc, x0, y0,

                                 0x00CC0020):          # SRCCOPY

                return False

            bmi = _BITMAPINFOHEADER()

            bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)

            bmi.biWidth, bmi.biHeight = bw, -bh        # 负高=自上而下

            bmi.biPlanes, bmi.biBitCount = 1, 32

            buf = ctypes.create_string_buffer(bw * bh * 4)

            if _gdi32.GetDIBits(mem, bmp, 0, bh, buf,

                                ctypes.byref(bmi), 0) != bh:   # DIB_RGB_COLORS

                return False

            raw = buf.raw

            n = 0

            for i in range(0, len(raw), 4):            # BGRA：三通道阈值对称
                if raw[i] >= 225 and raw[i + 1] >= 225 and raw[i + 2] >= 225:
                    n += 1
                    if n >= 12:     # 要凑够几颗亮像素：字形实测数百颗，在扫描带中凑够 12 颗纯白笔画像素即确认倍速按钮出现
                        return True

            return False

        finally:

            _gdi32.SelectObject(mem, old)

            _gdi32.DeleteObject(bmp)

            _gdi32.DeleteDC(mem)

            _user32.ReleaseDC(None, dc)

# ---------------- 过帧键规划（自带拷贝，同 reviver_play.plan_steps） ----------------

def plan_steps(frames_needed, frame_keys, speed=1.0):

    """贪心组合过帧键推进 frames_needed 帧（不过冲，允许 1/4 帧贴边）。

    返回 (键序列, 残差帧)。键账本: 帧 = ms*speed*30/1000。"""

    per = sorted(((ms * speed * LOGIC_FPS / 1000.0, k)

                  for k, ms in frame_keys.items()), reverse=True)

    seq, acc = [], 0.0

    for fpk, k in per:

        while acc + fpk <= frames_needed + 0.25:

            seq.append(k)

            acc += fpk

    return seq, frames_needed - acc

# ---------------- 引擎 ----------------

class ReminderEngine:

    """提醒执行引擎：后台线程按序跑一串提醒

    （trigger->watch->brake->creep->notify）。

    frames_fn()  -> int|None  当前局内帧数（尺子读不到返回 None；

                              watch 阶段用这个严格口径判掉线）

    freeze_frames_fn          冻结检测的宽松口径（暂停态也要可读）；

                              不给就用 frames_fn 兜底（selftest mock）

    inject(name) -> bool      注入按键（selftest 用 mock 顶替）

    on_state(rem, outcome)    每条提醒跑完的回调（落盘状态用），

                              outcome ∈ done/missed/failed/aborted/clock_lost

    exit_when_empty           队列跑空就结束（CLI 用 True；

                              服务端 False，挂着等实操中动态插入）

    运行中可 add(rem) 动态插入：watch 阶段发现更近的提醒会自动切换；

    已进入刹车/逼近的提醒不抢（正在动的不能半途而废），跑完再挑。"""

    def __init__(self, frames_fn, cfg=None, frame_keys=None,

                 inject=inject_key, on_state=None, exit_when_empty=True,

                 snap_ts_fn=None, freeze_frames_fn=None, eye=None):

        self.frames_fn = frames_fn

        # 冻结检测用宽松口径：frames_live 在暂停时（isRunning=False）返回

        # None 会让检测器变盲，真暂停被认成钟丢（实机踩过）

        self.freeze_fn = freeze_frames_fn or frames_fn

        self.snap_ts_fn = snap_ts_fn   # 快照到达时间戳；冻结判定要活证据

        self._eye = eye      # 开局触发采样器：None=实机懒建 GDI；selftest 注 mock

        self.cfg = cfg or load_cfg()

        self.frame_keys = frame_keys or {}

        self._raw_inject = inject

        # 真注入才包焦点守护；selftest 的 mock 不动（不抢焦点不 EnumWindows）

        self.inject = self._guarded_inject if inject is inject_key else inject

        self._pre_focus = None        # 注入前用户所在窗口，到点还焦点用

        self._focus_warned = False

        self._brake_pause_key = None  # 刹车实测停得住游戏的键，逼近段复暂复用

        self.on_state = on_state

        self.exit_when_empty = exit_when_empty

        self.stage = "idle"          # idle/trigger/watch/brake/creep/notify/failed

        self.cur = None              # 最近读到的帧数

        self.waiting_next = False    # 本局已过目标，自动等下一局开战

        self.armed = None            # 当前武装中的提醒
        self._armed_cancelled = False # 当前武装目标是否被用户中途删除
        self._is_paused = False        # 游戏当前是否处于引擎或已知暂停态

        self.events = []             # [(时间串, 消息)]，最新在后

        self._queue = []             # 待触发提醒（引擎自己的副本）

        self._new = threading.Event()  # 动态插入时唤醒空转等待

        self._stop = threading.Event()

        self._thread = None

        self._lock = threading.Lock()

    # ---- 对外 ----

    def start(self, rems):

        """按序执行提醒列表（入队即武装，运行中还能 add 追加）。"""

        if self._thread and self._thread.is_alive():

            return False

        self._stop.clear()

        try:
            cur_f = self.frames_fn()
        except Exception:
            cur_f = None

        with self._lock:
            q_rems = []
            for r in rems:
                rc = dict(r)
                if "_f_added" not in rc:
                    rc["_f_added"] = cur_f if cur_f is not None else 0
                q_rems.append(rc)
            self._queue = q_rems

        self._thread = threading.Thread(target=self._run, daemon=True)

        self._thread.start()

        return True

    def remove(self, rid):
        """从引擎运行队列中实时移除指定 ID 的提醒。
        若该提醒正是当前正在执行的目标 (self.armed)，则实时打断并触发切轨。
        """
        with self._lock:
            orig_len = len(self._queue)
            self._queue = [q for q in self._queue if q.get("id") != rid]
            is_armed = bool(self.armed and self.armed.get("id") == rid)
            if is_armed:
                self._armed_cancelled = True
        self._new.set()
        if is_armed:
            self.log("当前目标已被用户删除，正在切换下一目标...")
        return (orig_len != len(self._queue)) or is_armed

    def add(self, rem):

        """盯梢中途插入新提醒（实操快速加）。只进引擎队列，

        持久化与否由调用方决定（存储层的事）。"""

        try:
            cur_f = self.frames_fn()
        except Exception:
            cur_f = None

        rc = dict(rem)
        if "_f_added" not in rc:
            rc["_f_added"] = cur_f

        with self._lock:

            self._queue.append(rc)

        self._new.set()

    def add_missing(self, rems):

        """重置后批量补入：已在队列/正在盯的跳过，避免重复触发。

        返回实际补入条数。"""

        with self._lock:

            known = {q["id"] for q in self._queue}

            if self.armed:

                known.add(self.armed["id"])

            added = [dict(r) for r in rems if r["id"] not in known]

            self._queue.extend(added)

        if added:

            self._new.set()

        return len(added)

    def _peek_nearest(self):

        """队列里帧号最小的提醒；空了返回 None。"""

        with self._lock:

            if not self._queue:

                return None

            return min(self._queue, key=lambda r: r["frame"])

    def _pick_next(self):

        """挑下一条武装：优先本局还够得着的（帧号在当前读数之后）里

        最近的；都够不着/帧数读不到（局间）才退到全局最小帧——后者

        武装后进「等下一局」，下局开战自然接手。若不看够得着，已过

        本局帧数的小帧提醒会反复抢位等下一局，把本局还在前面的提醒

        全部晾死（实机踩过：盯 700 时插入 f=5，引擎停转，700 白等）。"""

        with self._lock:

            q = list(self._queue)

        if not q:

            return None

        try:

            f = self.frames_fn()

        except Exception:

            f = None

        if f is not None:

            reach = [r for r in q if r["frame"] > f]

            if reach:

                return min(reach, key=lambda r: r["frame"])

        return min(q, key=lambda r: r["frame"])

    def _peek_reachable(self, f):

        """队列里帧号在当前帧数之后（本局还够得着）的最近一条。"""

        with self._lock:

            cands = [q for q in self._queue if q["frame"] > f]

        if not cands:

            return None

        return min(cands, key=lambda r: r["frame"])

    def _discard(self, rem):

        with self._lock:

            self._queue = [q for q in self._queue if q["id"] != rem["id"]]

    def abort(self):

        self._stop.set()

    def alive(self):

        return bool(self._thread and self._thread.is_alive())

    def join(self, timeout=None):

        if self._thread:

            self._thread.join(timeout)

    def log(self, msg):

        with self._lock:

            self.events.append((time.strftime("%H:%M:%S"), msg))

            del self.events[:-100]

        # 同步落盘：远端用户报问题时只回传这一个文件就能定位

        # （页面事件流只留最近 12 条且刷新即没，实机排障吃过亏）

        try:

            with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:

                f.write("%s %s\n" % (time.strftime("%m-%d %H:%M:%S"), msg))

        except Exception:

            pass

    def clear_events(self):
        """清空事件流日志"""
        with self._lock:
            self.events.clear()

    def snapshot(self):

        with self._lock:

            armed = None

            remaining = None

            if self.armed and self.cur is not None:

                remaining = self.armed["frame"] - self.cur

                armed = {"id": self.armed["id"], "frame": self.armed["frame"],

                         "note": self.armed["note"]}

            return {"stage": self.stage, "cur": self.cur, "armed": armed,

                    "remaining": remaining, "alive": self.alive(),

                    "waiting_next": self.waiting_next,

                    "events": list(self.events[-12:])}

    def _guarded_inject(self, name, *args, **kwargs):

        """实机注入：先把游戏切到前台再发键，否则键飞进别的窗口。"""

        switched, found, prev = _focus_game()

        if switched:

            if self._pre_focus is None:

                self._pre_focus = prev

            self.log("切到游戏窗口发键")

        elif not found and not self._focus_warned:

            self._focus_warned = True

            self.log("[!] 没找到游戏窗口（标题含 明日方舟/Arknights），键可能送不进游戏")

        return self._raw_inject(name, *args, **kwargs)

    def _already_paused(self, rate=None):
        """双采样差分预检：帧数不动=已暂停。
        注意：在关卡刚开局（f <= 1）时，刚开跑的 0 帧和 1 帧会有极短的未跳帧窗口，
        此时不能当作已暂停（否则会误判已暂停而漏发暂停键导致跑飞）。"""
        a = self.freeze_fn()
        if a is None or a <= 1:
            return False
        gap = 0.25
        if rate:
            gap = max(0.1, min(0.35, 2.0 / rate))
        time.sleep(gap)
        b = self.freeze_fn()
        return b is not None and b == a

    def _probe_running(self, rate=None):
        """三态探测当前是否在跑：帧数在动=run，冻结=frozen，读不到=unknown。
        补按暂停键（开关键）前的安全门：探明真没停住才按下一发，
        避免第一发其实已停、确认慢了时把刚停的游戏解暂停。"""
        a = self.freeze_fn()
        if a is None:
            return "unknown"
        gap = 0.25
        if rate:
            gap = max(0.1, min(0.35, 2.0 / rate))
        time.sleep(gap)
        b = self.freeze_fn()
        if b is None:
            return "unknown"
        return "run" if b != a else "frozen"

    def _opening_trigger(self, target, native_pause_key="esc"):
        if not native_pause_key:
            native_pause_key = self.cfg.get("opening_pause_key", "esc")
        native_pause_key = str(native_pause_key).strip().lower()
        """开局触发（等待黑屏/Loading -> 开战瞬间倍速按钮出现 -> 毫秒级暂停）：
        严格等待进入关卡的加载流程，绝不在主菜单或编队界面误判触发。"""
        eye = self._eye
        if eye is None:
            try:
                eye = _TriggerEye()
            except Exception:
                self.log("[!] 开局触发不可用，回落普通刹车")
                return "fallback"
            self._eye = eye

        # 如果战斗已经在跑动中且已经走帧（>1 帧），说明不在关卡外，直接回落普通盯梢
        f_init = self.frames_fn()
        if f_init is not None and f_init > 1:
            self.log("战斗已进入第 %d 帧，回落普通盯梢" % f_init)
            return "fallback"

        self.log("开局触发：已就绪，等待进关卡（请进入关卡开始行动）...")

        # 阶段 1：等待黑屏过渡或 Loading... 出现（最多等 60 秒）
        t_deadline = time.perf_counter() + 60.0
        saw_loading = False
        while not self._stop.is_set() and time.perf_counter() < t_deadline:
            f = self.frames_fn()
            if f is not None and f > 1:
                self.log("战斗已在跑动中（当前 %d 帧），回落普通盯梢" % f)
                return "fallback"

            if eye.black() or eye.loading() == 1:
                saw_loading = True
                self.log("检测到关卡加载中，准备开局暂停...")
                break
            time.sleep(0.05)

        if self._stop.is_set():
            return "aborted"
        if not saw_loading:
            self.log("[!] 未检测到关卡加载，回落普通盯梢")
            return "fallback"

        # 阶段 2：等待倍速按钮在关卡第一帧渲染出来（最多等 20 秒）
        t_deadline = time.perf_counter() + 20.0
        saw_btn = False
        while not self._stop.is_set() and time.perf_counter() < t_deadline:
            f = self.frames_fn()
            if f is not None and f > 1:
                self.log("战斗已在跑动中（当前 %d 帧），回落普通盯梢" % f)
                return "fallback"

            if eye.speed_button():
                saw_btn = True
                break
            time.sleep(0.01)

        if self._stop.is_set():
            return "aborted"
        if not saw_btn:
            self.log("[!] 等待倍速按钮超时，回落普通盯梢")
            return "fallback"

        # 阶段 3：倍速按钮出现瞬间 -> 发游戏原生暂停键。
        if not self.inject(native_pause_key):
            self.log("[!] 开局触发按键失败")
            return "fallback"

        # 检验是否真正定格（若战斗淡入期吞键导致仍未停，补发同一个暂停键）
        time.sleep(0.3)
        f_chk1 = self.freeze_fn()
        time.sleep(0.15)
        f_chk2 = self.freeze_fn()
        if f_chk1 is not None and f_chk2 is not None and f_chk2 > f_chk1:
            self.log("首发暂停未停住（仍在跑动），补发暂停...")
            self.inject(native_pause_key)
            time.sleep(0.15)

        pf = self.freeze_fn()
        self.log("开局触发命中：已暂停（第 %s 帧）" % (pf if pf is not None else 0))
        return "paused"


    def _run(self):
        while not self._stop.is_set():
            try:
                cur_f = self.frames_fn()
            except Exception:
                cur_f = None

            rem = self._pick_next()
            if rem is None:
                if self.exit_when_empty:
                    break
                self._new.wait(0.3)      # 挂起等实操中动态插入
                self._new.clear()
                continue

            # 如果当前已经选定了一个未来的目标 rem（如 f=60），且当前战局正在推进（cur_f 可读且 > 0），
            # 那么队列中所有入队时在未来但在本局推进中已被越过（_f_added < r['frame'] <= cur_f < rem['frame']）的节点，
            # 说明在本次战斗推进中已被越过（错过）。将其标记为 missed 并通知落盘与前端更新。
            if cur_f is not None and cur_f > 0 and rem["frame"] > cur_f:
                with self._lock:
                    passed = [r for r in self._queue if r["id"] != rem["id"]
                              and r.get("_f_added") is not None
                              and r["_f_added"] < r["frame"] <= cur_f]
                    for pr in passed:
                        self._queue = [q for q in self._queue if q["id"] != pr["id"]]
                for pr in passed:
                    self.log("节点 f=%d 目标帧已在当前读数（%d）之前：标记为错过" % (pr["frame"], cur_f))
                    if self.on_state:
                        try:
                            self.on_state(pr, "missed")
                        except Exception:
                            pass

            self.armed = rem

            try:

                outcome = self._run_one(rem)

            except Exception as e:

                # 线程不许静默死：不兜底时异常只打到 stderr，

                # 页面上只看到盯梢状态“没了”不知为何（实机踩过：

                # GDI 句柄溢出把引擎当场打死）

                self.log("[!] 引擎内部出错，本次盯梢安全停止：%s: %s"

                         % (type(e).__name__, e))

                self.stage = "failed"

                outcome = "failed"

            if outcome == "switched":    # 主动让位（来了更近的/这条等下一局

                continue                 # 先让够得着的），rem 还在队列里，重挑

            self._discard(rem)

            self.log("提醒 f=%d 结束：%s" % (rem["frame"], OUTCOME_CN.get(outcome, outcome)))

            if self.on_state:

                try:

                    self.on_state(rem, outcome)

                except Exception:

                    pass

            if outcome in ("aborted", "clock_lost", "failed"):

                break        # 出错/叫停：不再碰后面的提醒

        self.stage = "idle"

        self.armed = None

    def _run_one(self, rem):
        self._armed_cancelled = False
        target = rem["frame"]
        lead = int(self.cfg.get("pre_pause_lead", 8))
        margin = int(self.cfg.get("stop_margin", 2))
        brake_lag = int(self.cfg.get("brake_lag", BRAKE_LAG))
        pause_key = str(self.cfg.get("pause_key", "space")).strip().lower()
        unpause_key = str(self.cfg.get("unpause_key", "esc")).strip().lower()
        open_pause_key = str(self.cfg.get("opening_pause_key", "esc")).strip().lower()
        self.log("开始盯第 %d 帧：%s" % (target, rem.get("note", "")))
        self._pre_focus = None
        self._focus_warned = False
        self._brake_pause_key = None
        trigger_done = False     # 开局触发只试一次（小目标专用）
        trigger_hit = False      # 开局触发已把开局停住
        seen_frame = False       # 本条提醒是否读到过帧数
        self.waiting_next = False

        # 玩家恢复门禁 (WaitUserResume Gate)：
        # 如果当前处于暂停态（上一节点刚刚刹车完成 self._is_paused 为 True，或实测当前处于静止状态）：
        # 进入门禁守护状态。在玩家主动解暂让游戏跑起来之前，绝不自发发起任何刹车或逼近！
        need_resume_gate = bool(self._is_paused)
        if need_resume_gate:
            self.log("游戏处于暂停静止态，等待玩家手动操作或解除暂停...")

        # ---- watch：游戏正常跑，盯到距目标 lead+刹车预算 帧就发暂停键 ----
        self.stage = "watch"
        lost_since = None
        brake_at = target - lead - brake_lag
        rate = None                 # 帧/秒 EMA
        last_move_ts = None         # 帧数最近一次变化的时刻
        seen_move = False           # watch 期间是否亲眼看到帧数变化
        prev_f, prev_t = None, None
        safety_paused = False
        seen_below = False          # 本条盯梢见过帧数低于目标（跑着经过的证据）
        deferred_noted = set()      # 已过本局帧数的插入提醒：只提示一次排队
        manual_hit = False

        while not self._stop.is_set():
            if self._armed_cancelled:
                self.log("目标 f=%d 已被用户删除，停止盯梢" % target)
                return "cancelled"

            # 无论游戏是否在跑，优先读取静止帧数
            pf = self.freeze_fn()
            if pf is not None:
                with self._lock:
                    self.cur = pf
                # 门禁检查 1：如果玩家在暂停中通过手动步进（Y/R）到达或超过了目标
                if need_resume_gate and pf >= target:
                    self.log("目标帧 %d 已由玩家手动步进到达（当前 f=%d）" % (target, pf))
                    seen_below = True
                    manual_hit = True
                    break

            f = self.frames_fn()
            now = time.perf_counter()

            if f is None:
                # 处于门禁守护中且读不到活动帧（游戏在暂停中）：安静等待，不触发开局盲打
                if need_resume_gate:
                    time.sleep(0.05)
                    continue

                # 开局触发：小目标（刹车点算成负数）反应式刹车没救，
                # 趁进关卡前这段读不到帧的窗口捕捉开战瞬间暂停。
                if (self.waiting_next
                        or (not trigger_done and not seen_frame)) \
                        and self.cfg.get("opening_trigger", True):
                    trigger_done = True
                    self.stage = "trigger"
                    t = self._opening_trigger(
                        target, open_pause_key)
                    if t == "aborted":
                        return "aborted"
                    if t == "paused":
                        pf = self.freeze_fn()
                        with self._lock:
                            if pf is not None:
                                self.cur = pf
                        trigger_hit = True
                        if target - lead - brake_lag <= 0:
                            break      # 小目标：直接交给刹车/逼近段
                        else:
                            self.log("开局已自动暂停（第 %d 帧），等待战斗运行..."
                                     % (pf if pf is not None else 0))
                    self.stage = "watch"

                if lost_since is None:
                    lost_since = now
                    if self.waiting_next:
                        self.log("等下一局中：帧数读不到（结算/加载正常），继续等")
                    elif self.cur is None:
                        self.log("读不到帧数：游戏可能不在关卡内、没开尺子。"
                                 "会一直等，进关卡自动认")
                    else:
                        self.log("帧数读不到了，等尺子恢复...")

                # 补救刹车
                if not safety_paused and seen_below and self.cur is not None \
                        and self.cur >= brake_at - 30 \
                        and now - lost_since > 0.35:
                    self.log("[!] 临近目标时丢帧，先补一脚暂停！")
                    self.inject(pause_key)
                    safety_paused = True

                if seen_below and self.cur is not None \
                        and now - lost_since > LOST_TIMEOUT:
                    self.stage = "failed"
                    self.log("[!] 帧数一直没恢复，为免游戏跑过本次放弃")
                    return "clock_lost"

                time.sleep(0.02)
                continue

            if lost_since is not None and not self.waiting_next:
                self.log("帧数已可读（当前 %d），继续盯梢" % f)
            lost_since = None
            seen_frame = True

            if prev_f is None:
                prev_f, prev_t = f, now
                last_move_ts = now
            elif f != prev_f:
                seen_move = True
                if trigger_hit:
                    # 开局暂停已经被玩家解除；后续到目标前必须重新刹车，
                    # 不能再把“曾经开局暂停”当成“当前仍暂停”。
                    trigger_hit = False
                dt = now - prev_t
                if dt > 1e-3:
                    r = (f - prev_f) / dt
                    rate = r if rate is None else 0.7 * rate + 0.3 * r
                    # 门禁检查 2：检测到玩家主动解除暂停，游戏恢复正常跑动（连续运动且 rate >= 8 FPS）
                    if need_resume_gate and (r >= 8.0 or (rate is not None and rate >= 8.0)):
                        need_resume_gate = False
                        self._is_paused = False
                        self.log("检测到战斗已恢复推进（%.1f FPS），解除门禁，开启自动拦截"
                                 % (rate or r))
                prev_f, prev_t = f, now
                last_move_ts = now

            with self._lock:
                self.cur = f

            # 【核心门禁拦截】：只要 need_resume_gate 为 True（游戏还未解暂跑动），绝不进入刹车和逼近！
            if need_resume_gate:
                time.sleep(0.05)
                continue

            cushion = min(int((rate or 0) * 0.25 + 0.5), 15)
            speed_now = max(1.0, min(2.6, (rate or 0) / LOGIC_FPS))
            brake_at = target - lead - int(brake_lag * speed_now) - cushion

            nearer = self._peek_nearest()
            if nearer is not None and nearer["id"] != rem["id"] \
                    and nearer["frame"] < target:
                if nearer["frame"] > f:
                    self.log("插进来一条更近的提醒 f=%d，先盯它" % nearer["frame"])
                    return "switched"
                if nearer["id"] not in deferred_noted:
                    deferred_noted.add(nearer["id"])
                    self.log("插进来的 f=%d 已过本局帧数：排队等下一局，先继续盯 f=%d"
                             % (nearer["frame"], target))

            if f < target:
                if self.waiting_next:
                    self.waiting_next = False
                    self.log("下一局开战了（当前 %d），恢复盯梢" % f)
                seen_below = True
            elif seen_below:
                self.log("目标帧 %d 已经过了（现在 %d），这条不盯了" % (target, f))
                return "missed"
            else:
                if not self.waiting_next:
                    self.waiting_next = True
                    prev_f, prev_t = None, None
                    rate = None
                    seen_move = False
                    last_move_ts = None
                    self.log("帧数（%d）已在目标帧 %d 之后：这局赶不上，自动等下一局开战"
                             % (f, target))
                if self._peek_reachable(f) is not None:
                    self.log("队列里还有本局够得着的提醒：先让它，这条排队等下一局")
                    self.waiting_next = False
                    return "switched"
                time.sleep(0.1)
                continue

            if f >= brake_at:
                break
            time.sleep(0.01)

        if self._stop.is_set():
            return "aborted"

        # 如果是玩家在门禁期间手动步进到达，直接进入 notify，不执行任何刹车注入或逼近
        if manual_hit:
            self.stage = "notify"
            self._is_paused = True
            f = self.freeze_fn()
            with self._lock:
                if f is not None:
                    self.cur = f
            left = ("还差 %d 帧" % (target - f)) if f is not None else "帧数暂不可读"
            self.log("== 到点：%s 到目标帧 %d：%s ==" %
                     (left, target, rem.get("note", "")))
            return "done"

        # ---- brake：watch 正常拦截后注入暂停键 ----
        if self._armed_cancelled:
            self.log("目标 f=%d 已被用户删除，取消刹车" % target)
            return "cancelled"
        self.stage = "brake"
        self.log("还差 %d 帧到目标，先帮你暂停" % (target - self.cur
                                             if self.cur is not None else -1))

        moved_recently = seen_move and last_move_ts is not None and \
            time.perf_counter() - last_move_ts < 0.5

        if trigger_hit:
            if not wait_frozen(None, hold=0.15, timeout=2.0,
                               frames_fn=self.freeze_fn,
                               ts_fn=self.snap_ts_fn):
                self.log("[!] 开局触发确认已暂停不通过，为免安全停止")
                self.stage = "failed"
                return "failed"
            self._brake_pause_key = open_pause_key
        elif not moved_recently and self._already_paused(rate):
            self.log("游戏本来就在暂停，直接开始逼近")
        else:
            ok = False
            speed_brake = max(1.0, min(2.6, (rate or 0) / LOGIC_FPS))
            fallback = self.cfg.get("pause_fallback_key", "esc")
            if fallback:
                fallback = str(fallback).strip().lower()

            if pause_key == "space":
                # 默认空格模式：2x 速度下 ESC 响应更敏捷稳妥，优先尝试 ESC
                if speed_brake >= 1.5 and fallback and fallback != "space":
                    keys = [fallback, pause_key]
                else:
                    keys = [pause_key, fallback] if fallback and fallback != pause_key else [pause_key]
            else:
                # 用户自定义按键模式（如 F 键）：100% 优先且信任用户的自定义按键
                if fallback and fallback != pause_key:
                    keys = [pause_key, fallback]
                else:
                    keys = [pause_key]
            pressed = None
            for i, key in enumerate(keys, 1):
                if i > 1:
                    probe = self._probe_running(rate)
                    if probe == "frozen":
                        if wait_frozen(None, hold=0.15, timeout=2.0,
                                       frames_fn=self.freeze_fn,
                                       ts_fn=self.snap_ts_fn):
                            ok = True
                            break
                        self.log("[!] 帧数读取确认已暂停不通过，为免安全停止")
                        self.stage = "failed"
                        return "failed"
                    if probe == "unknown":
                        self.log("[!] 暂停后帧数确认读不到，为免安全停止，请重试")
                        self.stage = "failed"
                        return "clock_lost"
                    if not key:
                        break
                self.log("按下暂停键 %s（第%d次尝试%s）" % (
                    key, i, "：2x时ESC稳停，先试它" if i == 1 and key != pause_key else ""))
                if not self.inject(key):
                    self.log("[!] 暂停键按不下去（没权限？请以管理员运行）")
                    self.stage = "failed"
                    return "failed"
                pressed = key
                if wait_frozen(None, hold=0.15, timeout=2.0,
                               frames_fn=self.freeze_fn,
                               ts_fn=self.snap_ts_fn):
                    ok = True
                    self._is_paused = True
                    break
                if self.freeze_fn() is None:
                    self.log("[!] 暂停后帧数确认读不到，为免安全停止，请重试")
                    self.stage = "failed"
                    return "clock_lost"
            if not ok:
                self.log("[!] 暂停键（%s/%s）都没停住游戏，为免安全停止，请重试" % (keys[0], keys[1] or "无"))
                self.stage = "failed"
                return "failed"
            self._brake_pause_key = pressed

        f = self.freeze_fn()
        if f is None:
            self.stage = "failed"
            return "clock_lost"
        with self._lock:
            self.cur = f

        if f > target:
            self.log("停住时已过目标帧（现在 %d）" % f)
            return "missed"

        time.sleep(SETTLE)

        # ---- creep：微调逼近 target-margin ----
        est = max(1.0, min(2.6, rate / LOGIC_FPS)) if rate else 2.0
        calibrated = False
        self.stage = "creep"
        stop_at = target - margin
        stall = 0
        use_direct = self.cfg.get("direct_step", True)
        unpause_key = str(self.cfg.get("unpause_key", "esc")).strip().lower()
        if self._brake_pause_key:
            repause_key = self._brake_pause_key
        elif pause_key == "space" and est >= 1.5:
            repause_key = str(self.cfg.get("pause_fallback_key", "esc")).strip().lower()
        else:
            repause_key = pause_key
        unpause_eff = unpause_key

        while not self._stop.is_set():
            if self._armed_cancelled:
                self.log("目标 f=%d 已被用户删除，取消逼近" % target)
                return "cancelled"
            f = self.freeze_fn()
            if f is None:
                self.stage = "failed"
                self.log("[!] 逼近时帧数读不到，为免安全停止")
                return "clock_lost"
            with self._lock:
                self.cur = f
            if f >= stop_at:
                break

            f_diff = stop_at - f
            # 不能把容许量放宽到 f_diff + 1：二倍速下重新暂停还会经过
            # Unity 输入队列，额外多走约 1~2 帧，最后一击就可能越过目标。
            # 只按到 stop_at 的真实缺口选脉冲，宁可多按一次小键。
            budget = f_diff * 1000.0 / (LOGIC_FPS * est)
            if calibrated and budget >= 166.0:
                step_ms = 166.0
            elif budget >= 83.0:
                step_ms = 83.0
            elif budget >= 33.0:
                step_ms = 33.0
            else:
                # 1x 下一逻辑帧约 33.3ms，16ms 常读不到整数帧变化；
                # 2x 下 16ms 才接近一逻辑帧，适合最后贴边。
                step_ms = 16.0 if est >= 1.5 else 33.0

            f0 = f
            ok, msg = direct_step(step_ms, unpause_key=unpause_eff,
                                  pause_key=repause_key, inject_fn=self.inject)
            if not ok:
                self.log("[!] 原生过帧失败：%s" % msg)
                self.stage = "failed"
                return "failed"

            moved = False
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 1.0:
                v = self.freeze_fn()
                if v is not None and v > f0:
                    moved = True
                    break
                time.sleep(0.02)
            if not moved:
                stall += 1
                self.log("原生过帧 %.0fms 帧数未动（%d/%d）" % (step_ms, stall, STALL_MAX))
                if unpause_eff != "esc" and repause_key == "esc":
                    unpause_eff = "esc"
                    self.log("改用 ESC 解暂（空格可能被暂停遮挡）")
                if stall >= STALL_MAX:
                    self.log("[!] 原生过帧多次无响应，为免安全停止")
                    self.stage = "failed"
                    return "failed"
                continue

            if not wait_frozen(None, hold=0.1, timeout=1.5,
                               frames_fn=self.freeze_fn,
                               ts_fn=self.snap_ts_fn):
                probe = self._probe_running(rate)
                if probe == "frozen":
                    if not wait_frozen(None, hold=0.1, timeout=1.0,
                                       frames_fn=self.freeze_fn,
                                       ts_fn=self.snap_ts_fn):
                        self.log("[!] 帧数读取确认已停不通过，为免安全停止")
                        self.stage = "failed"
                        return "failed"
                elif probe == "run":
                    self.log("[!] 过帧后没停住，补一脚 %s 暂停" % repause_key)
                    self.inject(repause_key)
                    if not wait_frozen(None, hold=0.1, timeout=1.0,
                                       frames_fn=self.freeze_fn,
                                       ts_fn=self.snap_ts_fn):
                        self.log("[!] 补暂停后未确认冻结，为免安全停止")
                        self.stage = "failed"
                        return "failed"
                else:
                    self.log("[!] 补暂停后帧数读不到，为免安全停止")
                    return "clock_lost"

            f1 = self.freeze_fn()
            if f1 is None:
                self.stage = "failed"
                self.log("[!] 逼近后帧数读不到，为免安全停止")
                return "clock_lost"
            stall = 0
            adv = f1 - f0
            nominal = step_ms * LOGIC_FPS / 1000.0
            if adv > 0 and nominal >= 2.4:
                meas = adv / nominal + 0.5
                est = min(2.6, meas if not calibrated else max(est, meas))
                calibrated = True
            elif adv >= 2:
                est = max(est, 2.0)
            with self._lock:
                self.cur = f1
            if f1 > target:
                self.log("逼近时过目标帧了（现在 %d）" % f1)
                return "missed"

        if self._stop.is_set():
            return "aborted"

        # ---- notify：报剩余帧数 + 出声 ----
        if self._armed_cancelled:
            self.log("目标 f=%d 已被用户删除，取消提醒" % target)
            return "cancelled"
        if self._pre_focus:
            _user32.SetForegroundWindow(self._pre_focus)
            self._pre_focus = None

        self.stage = "notify"
        self._is_paused = True
        f = self.freeze_fn()
        with self._lock:
            if f is not None:
                self.cur = f
        left = ("还差 %d 帧" % (target - f)) if f is not None else "帧数暂不可读"
        self.log("== 到点：%s 到目标帧 %d：%s ==" %
                 (left, target, rem.get("note", "")))
        return "done"

# ---------------- 命令行 ----------------

def _dry_plan(target, cfg, frame_keys):

    lead = int(cfg.get("pre_pause_lead", 8))

    margin = int(cfg.get("stop_margin", 2))

    seq, resid = plan_steps(lead - margin, frame_keys)

    print("== dry 计划（目标帧 %d）==" % target)

    print("  watch : 游戏正常跑，距目标 %d 帧（f=%d）时动手"

          % (lead, target - lead))

    print("  brake : 注入暂停键 %s，等帧数冻结确认暂停"

          % cfg.get("pause_key", "esc"))

    print("  creep : 过帧键 %s 逼近约 %d 帧 -> %s（残差 %.2f 帧）"

          % (frame_keys and ",".join("%s=%gms" % kv

                                     for kv in frame_keys.items()),

             lead - margin, "".join(seq) or "(无需按键)", resid))

    print("  notify: 停在 f=%d 附近（留 %d 帧），提示留言"

          % (target - margin, margin))

def selftest():

    """离线自测：mock 帧数+注入走完状态机，不碰游戏不碰网络。写 _t.txt。"""

    OUT = []

    def say(m):

        print(m)

        OUT.append(m)

    # 倍速按钮 ROI 必须随 CanvasScaler 缩放，并保持右缘锚定。
    roi_cases = [
        ((100, 50, 1024, 715), (943, 71, 88, 45)),    # 4:3 客户区
        ((0, 0, 1920, 1200), (1580, 40, 165, 85)),    # 16:10
        ((20, 30, 1920, 1080), (1600, 70, 165, 85)),  # 16:9
        ((0, 0, 3440, 1440), (2987, 53, 220, 113)),   # 21:9
    ]
    roi_ok = all(_speed_rect(*args) == expected
                 for args, expected in roi_cases)
    say("[ROI] CanvasScaler 多比例采样框: %s"
        % ("通过" if roi_ok else "不通过"))
    if not roi_ok:
        for args, expected in roi_cases:
            say("    %s -> %s（期望 %s）"
                % (args, _speed_rect(*args), expected))

    cfg = dict(DEFAULT_CFG)

    fkeys = {"r": 33.0, "t": 166.0}

    class MockGame:

        """假游戏：running 时按真实时间 30fps 走帧（贴近实机，

        引擎轮询频率不会把帧数读飞）；paused 冻结；

        暂停键 toggle；过帧键只在暂停时按名义帧数推进。

        hidden=True 模拟战斗开始前：帧数读不到（尺子 isRunning=False）。

        lag>0 模拟尺子推送滞后：读数永远是 lag 秒前的值。"""

        def __init__(self, f0, pause_key="esc", fps=LOGIC_FPS, hidden=False,

                     pause_keys=(), lag=0.0, key_speed=1.0,

                     space_dead_running=False, repause_lag_frames=0):

            self.f = float(f0)

            self.paused = False

            self.pause_key = pause_key

            self.pause_keys = set(pause_keys)   # 其它可暂停的键（如原生 space）

            self.hidden = hidden

            self.battle = not hidden            # 战斗开始前帧数不可读

            self.fps = fps

            self.lag = lag

            self.key_speed = key_speed   # 倍速局：过帧键推进翻倍（同实机）

            self.space_dead_running = space_dead_running

            self.repause_lag_frames = repause_lag_frames

            self._t = time.perf_counter()

            self._hist = [(self._t, int(self.f))]

        def battle_on(self):

            self.battle = True

            self._t = time.perf_counter()

        def frames(self):

            if self.hidden and not self.battle:

                return None

            now = time.perf_counter()

            if not self.paused:

                self.f += (now - self._t) * self.fps

            self._t = now

            v = int(self.f)

            self._hist.append((now, v))

            if self.lag <= 0:

                return v

            cutoff = now - self.lag     # 只报 lag 秒前的值：尺子推送滞后

            out = self._hist[0][1]

            for ts, hv in self._hist:

                if ts <= cutoff:

                    out = hv

                else:

                    break

            if len(self._hist) > 4096:

                self._hist = self._hist[-2048:]

            return out

        def inject(self, name, hold=0.03):

            now = time.perf_counter()

            if not self.paused:

                self.f += (now - self._t) * self.fps

            self._t = now

            self._hist.append((now, int(self.f)))

            if self.space_dead_running and name == "space":

                if not self.paused and self.fps >= 45:

                    return True   # 模拟实机：2x 运行中游戏忽略空格的暂停请求

            if name in ("esc", "space", self.pause_key) or name in self.pause_keys:
                if not self.paused and self.repause_lag_frames:
                    self.f += self.repause_lag_frames
                self.paused = not self.paused
            elif name in fkeys and self.paused:

                self.f += max(1, int(fkeys[name] * LOGIC_FPS / 1000.0

                                     * self.key_speed + 0.5))

                self._hist.append((now, int(self.f)))

            return True

    # ① plan_steps 不变量（同 reviver_play 口径）

    ok1, worst = True, 0.0

    for needed in (0.0, 0.3, 0.5, 1.0, 4.98, 5.0, 33.3, 100.0):

        seq, resid = plan_steps(needed, {"t": 166.0, "r": 33.0, "y": 16.0})

        worst = max(worst, abs(resid))

        if not (-0.25 <= resid <= 0.5):

            ok1 = False

    say("① plan_steps: %s（8 档目标，最坏残差 %.2f 帧）"

        % ("通过" if ok1 else "不通过", worst))

    # ② 正常流：watch->brake->creep->notify；刹车有延迟预算，落点只断言

    #    不越过目标帧，留白 1~margin+1 帧

    g = MockGame(100)

    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)

    rem = {"id": "t1", "frame": 120, "note": "开技能", "state": "pending"}

    eng.start([rem])

    eng.join(15)

    snap = eng.snapshot()

    left = 120 - g.f

    ok2 = (snap["stage"] == "idle" and g.paused and 1 <= left <= cfg["stop_margin"] + 1)

    say("② 正常流: %s（停 f=%d，距目标 %d 帧，游戏处于暂停）"

        % ("通过" if ok2 else "不通过", int(g.f), left))

    # ③ 连续两条提醒自动接力（第一条到点后游戏暂停，用户处理完在

    #    两条提醒之间的 notify 等待期手动解除暂停，引擎应继续盯第二条。

    #    解暂停用事件驱动：等 a 到点（on_state 回调）再放手，不用固定

    #    Timer——Timer 闭包引用的是全局 g，负载高时 ③ 跑得慢，

    #    定时到点会落进后面的测试项里，把别人的暂停解掉）

    g = MockGame(100)

    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)

    outcomes = []

    eng.on_state = lambda r, o: outcomes.append((r["id"], o))

    eng.start([{"id": "a", "frame": 120, "note": "", "state": "pending"},

               {"id": "b", "frame": 135, "note": "", "state": "pending"}])

    t0 = time.time()

    while time.time() - t0 < 20:

        if any(i == "a" for i, _o in outcomes):

            g.paused = False

            break

        time.sleep(0.05)

    eng.join(30)

    ok3 = [o for _i, o in outcomes] == ["done", "done"]

    say("③ 连续提醒: %s（结果 %s）" % ("通过" if ok3 else "不通过", outcomes))

    # ④ 首读已过目标（重置备下一局/武装慢了）-> 不判错过、不按键，

    #    等下一局开战；帧数回落后自动恢复并到点

    g = MockGame(200)

    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)

    outcomes4 = []

    eng.on_state = lambda r, o: outcomes4.append(o)

    eng.start([{"id": "m", "frame": 120, "note": "", "state": "pending"}])

    time.sleep(1.0)

    wait_ok = not g.paused and eng.alive() and not outcomes4

    g.f = 0.0

    g._t = time.perf_counter()   # 模拟下一局开战：帧数归零重计

    eng.join(30)

    ok4 = wait_ok and outcomes4 == ["done"]

    say("④ 过目标等下一局: %s（等待期未按键=%s，结果 %s）"

        % ("通过" if ok4 else "不通过", wait_ok, outcomes4))

    if not ok4:

        for ts, msg in eng.snapshot()["events"]:

            say("    %s  %s" % (ts, msg))

    # ⑤ 盯梢中尺子掉线：离刹车点还远时断 -> 只等不按键；

    #    掉线时已逼近刹车点 -> 最多补一记暂停保底，绝不碰过帧键

    g = MockGame(100)

    _real_frames = g.frames

    _pressed5 = []

    def _flaky():

        if g.f >= 101:      # 刚读到帧就断：还没到刹车点，只等不按键；

            return None     # 逼近保底带（刹车点前 30 帧）后最多补一记暂停

        return _real_frames()

    def _inj5(name):

        _pressed5.append(name)

        return g.inject(name)

    eng = ReminderEngine(_flaky, cfg, fkeys, inject=_inj5)

    eng.start([{"id": "c", "frame": 120, "note": "", "state": "pending"}])

    eng.join(15)

    ok5 = (all(k == cfg["pause_key"] for k in _pressed5)

           and len(_pressed5) <= 1 and g.f < 115)

    say("⑤ 掉线安全停: %s（停 f=%d，按键 %s）"

        % ("通过" if ok5 else "不通过", int(g.f), _pressed5 or "无"))

    # ⑥ abort 能随时叫停

    g = MockGame(0)

    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)

    eng.start([{"id": "x", "frame": 100000, "note": "", "state": "pending"}])

    time.sleep(0.1)

    eng.abort()

    eng.join(3)

    ok6 = not eng.alive()

    say("⑥ abort: %s" % ("通过" if ok6 else "不通过"))

    # ⑦ 盯梢中动态插入更近的提醒 -> 自动切换，先近的后远的。

    #    解暂停用事件驱动（等引擎武装到 far 再放手），不用固定延时：

    #    自适应刹车点会随版本前移，固定延时可能撞进刹车窗口里把

    #    mock 的 toggle 暂停按成解除，制造假失败。

    #    mock 用 15fps：刹车逻辑按帧算不受影响，只是给负载高的机器

    #    更长的刹车跑道，避免线程拖顶造成假失败。

    g = MockGame(100, fps=15)

    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)

    outcomes = []

    eng.on_state = lambda r, o: outcomes.append((r["id"], o))

    eng.start([{"id": "far", "frame": 200, "note": "", "state": "pending"}])

    time.sleep(0.3)                       # 盯 far 期间插入更近的 near

    eng.add({"id": "near", "frame": 120, "note": "", "state": "pending"})

    t0 = time.time()                      # near 到点（on_state 回调）即视为

    while time.time() - t0 < 20:          # 用户处理完，解暂停让 far 继续盯：

        if any(i == "near" for i, _o in outcomes):

            g.paused = False              # 用结果回调作信号，不看引擎内部

            break                         # 状态，避免和刹车段竞态

        time.sleep(0.05)

    eng.join(30)

    ok7 = [i for i, _o in outcomes] == ["near", "far"] and \
        all(o == "done" for _i, o in outcomes)

    say("⑦ 动态插入: %s（触发顺序 %s）"

        % ("通过" if ok7 else "不通过", outcomes))

    if not ok7:

        for ts, msg in eng.snapshot()["events"]:

            say("    %s  %s" % (ts, msg))

    # ⑧ 小目标开局触发：黑屏->Loading->倍速按钮出现即开战，

    #    引擎同帧发原生 space 暂停，冻结点 ≤2 帧，全程走到 notify

    class MockEye:

        """假采样器：脚本推进场景；phase 3（按钮出现）= 开战。"""

        def __init__(self, game):

            self.game = game

            self.phase = 0

        def black(self):

            return self.phase >= 1

        def loading(self):

            return 1 if self.phase >= 2 else 0

        def speed_button(self):

            if self.phase >= 3:

                if not self.game.battle:

                    self.game.battle_on()

                return True

            return False

    g = MockGame(0, hidden=True, pause_keys=("space",))

    eye = MockEye(g)

    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject, eye=eye)

    outcomes8 = []

    eng.on_state = lambda r, o: outcomes8.append((r["id"], o))

    eng.start([{"id": "trig", "frame": 2, "note": "部署", "state": "pending"}])

    time.sleep(0.4)          # 引擎应在 trigger 阶段等黑屏

    eye.phase = 1

    time.sleep(0.4)          # 黑屏确认，等 Loading

    eye.phase = 2            # 白 Loading -> 引擎内部再等 2s 才密轮询

    time.sleep(2.6)

    eye.phase = 3            # 倍速按钮出现=开战，引擎应立即发键

    eng.join(20)

    ok8 = g.paused and g.f <= 2 and outcomes8 == [("trig", "done")]

    say("⑧ 开局触发: %s（冻在 f=%d，结果 %s）"

        % ("通过" if ok8 else "不通过", int(g.f), outcomes8))

    if not ok8:

        for ts, msg in eng.snapshot()["events"]:

            say("    %s  %s" % (ts, msg))

    # ⑨ 触发回落：采样器什么场景都见不到，战斗却直接开始（帧数可读）

    #    → 引擎放弃触发回正常 watch/brake，照样能落位。

    #    mock 用 15fps：给高负载机器更长的刹车跑道（开战到按键的

    #    反应窗口按帧算），避免线程拖顶造成假失败（同 ⑦ 口径）

    g = MockGame(0, hidden=True, fps=15)

    eye = MockEye(g)         # phase 恒 0：黑屏/Loading/按钮全无

    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject, eye=eye)

    outcomes9 = []

    eng.on_state = lambda r, o: outcomes9.append(o)

    eng.start([{"id": "fb", "frame": 15, "note": "", "state": "pending"}])

    time.sleep(0.8)          # 引擎在等黑屏（永远等不到）

    g.battle_on()            # 战斗突然开始：帧数可读 -> 触发让位

    eng.join(30)

    snap = eng.snapshot()

    left9 = 15 - g.f

    ok9 = (snap["stage"] == "idle" and g.paused and outcomes9 == ["done"]

           and 1 <= left9 <= cfg["stop_margin"] + 1)

    say("⑨ 触发回落: %s（停 f=%d，距目标 %d 帧）"

        % ("通过" if ok9 else "不通过", int(g.f), left9))

    if not ok9:

        for ts, msg in snap["events"]:

            say("    %s  %s" % (ts, msg))

    # ⑩ 尺子读数滞后（快照慢半拍）：过帧键必须等帧数真动了再认冻结，

    #    否则拿旧值误判“没动”会多补大键、冲过目标。

    #    游戏预先暂停，直接走逼近段（stop_at=104，需推进 4 帧）

    g = MockGame(100, lag=0.4)

    g.paused = True

    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)

    outcomes10 = []

    eng.on_state = lambda r, o: outcomes10.append(o)

    eng.start([{"id": "lag", "frame": 106, "note": "", "state": "pending"}])

    eng.join(30)

    snap = eng.snapshot()

    ok10 = (snap["stage"] == "idle" and outcomes10 == ["done"]

            and int(g.f) == 104)        # 正好落 stop_at，无过冲

    say("⑩ 慢读数逼近: %s（停 f=%d，目标 106）"

        % ("通过" if ok10 else "不通过", int(g.f)))

    if not ok10:

        for ts, msg in snap["events"]:

            say("    %s  %s" % (ts, msg))

    # 11 倍速局（fps=60：解暂停脉冲期间帧钟同样走两倍速）：逼近段

    #    必须把脉冲时长按标定倍速折算、未标定时禁用大键，不能按

    #    1x 账本硬推（实机踩过：2x 局 166ms 大键一击推约10帧冲过目标）

    g = MockGame(51, fps=60)

    g.paused = True

    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)

    outcomes11 = []

    eng.on_state = lambda r, o: outcomes11.append(o)

    eng.start([{"id": "s2", "frame": 65, "note": "", "state": "pending"}])

    eng.join(30)

    snap = eng.snapshot()

    ok11 = (snap["stage"] == "idle" and outcomes11 == ["done"]

            and 63 <= int(g.f) <= 65)   # 落 stop_at~目标之间，不许冲过

    say("[11] 倍速局逼近: %s（停 f=%d，目标 65）"

        % ("通过" if ok11 else "不通过", int(g.f)))

    if not ok11:

        for ts, msg in snap["events"]:

            say("    %s  %s" % (ts, msg))

    # 11b 倍速局缺口恰好4帧：修复前 166ms 大键一击推约10帧冲到 69，

    #     直接判错过——正是「2倍速停不住、1倍速正常」的案发现场

    g = MockGame(59, fps=60)

    g.paused = True

    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)

    outcomes11b = []

    eng.on_state = lambda r, o: outcomes11b.append(o)

    eng.start([{"id": "s2b", "frame": 65, "note": "", "state": "pending"}])

    eng.join(30)

    snap = eng.snapshot()

    ok11b = (snap["stage"] == "idle" and outcomes11b == ["done"]

             and 63 <= int(g.f) <= 65)

    say("[11b] 倍速局缺口4帧: %s（停 f=%d，目标 65）"

        % ("通过" if ok11b else "不通过", int(g.f)))

    if not ok11b:

        for ts, msg in snap["events"]:

            say("    %s  %s" % (ts, msg))

    # 11c 二倍速且重新暂停晚生效2帧：缺口4帧时旧预算(f_diff+1)
    #     会选83ms，脉冲约5帧+暂停延迟2帧，最终越目标1帧。
    g = MockGame(596, fps=60, repause_lag_frames=2)
    g.paused = True
    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)
    outcomes11c = []
    eng.on_state = lambda r, o: outcomes11c.append(o)
    eng.start([{"id": "s2lag", "frame": 602, "note": "", "state": "pending"}])
    eng.join(30)
    snap = eng.snapshot()
    ok11c = (snap["stage"] == "idle" and outcomes11c == ["done"]
             and 600 <= int(g.f) <= 602)
    say("[11c] 2x暂停延迟逼近: %s（停 f=%d，目标 602）"
        % ("通过" if ok11c else "不通过", int(g.f)))
    if not ok11c:
        for ts, msg in snap["events"]:
            say("    %s  %s" % (ts, msg))

    # 12 正好落目标帧=到点不是错过（实机口径：f=55 停在 55 被误判

    #    错过）。落 stop_at~目标之间均算到点

    g = MockGame(54)

    g.paused = True

    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)

    outcomes12 = []

    eng.on_state = lambda r, o: outcomes12.append(o)

    eng.start([{"id": "eq", "frame": 55, "note": "", "state": "pending"}])

    eng.join(30)

    snap = eng.snapshot()

    ok12 = (snap["stage"] == "idle" and outcomes12 == ["done"]

            and 53 <= int(g.f) <= 55)

    say("[12] 落目标算到点: %s（停 f=%d，目标 55）"

        % ("通过" if ok12 else "不通过", int(g.f)))

    if not ok12:

        for ts, msg in snap["events"]:

            say("    %s  %s" % (ts, msg))

    if not ok12:

        for ts, msg in snap["events"]:

            say("    %s  %s" % (ts, msg))

    # [13] 重置后帧数停在目标之后（上局残留/结算画面，引擎有旧读数）：

    #     自动等下一局，不判错过（实机踩过：重置补回队列被立刻写回 missed）

    g = MockGame(500)

    g.paused = True

    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)

    eng.cur = 500            # 上局残留读数

    outcomes13 = []

    eng.on_state = lambda r, o: outcomes13.append(o)

    eng.start([{"id": "hold", "frame": 55, "note": "", "state": "pending"}])

    time.sleep(1.2)

    ok13 = eng.alive() and not outcomes13 \
        and eng.snapshot()["stage"] == "watch"

    eng.abort()

    eng.join(3)

    say("[13] 重置后不误杀: %s（停在 f=500 等下一局，未判错过）"

        % ("通过" if ok13 else "不通过"))

    # [14] 等下一局后重开：帧数回到低位，盯梢自动恢复并到点

    g = MockGame(500)

    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)

    eng.cur = 500            # 同上：残留读数

    outcomes14 = []

    eng.on_state = lambda r, o: outcomes14.append(o)

    eng.start([{"id": "rs", "frame": 20, "note": "", "state": "pending"}])

    time.sleep(0.6)      # 此时在等下一局（帧数过目标且在跑）

    g.f = 0.0            # 模拟重开一局：帧数归零重计

    g._t = time.perf_counter()

    eng.join(30)

    ok14 = outcomes14 == ["done"]

    say("[14] 重开后恢复盯梢: %s（结果 %s）"

        % ("通过" if ok14 else "不通过", outcomes14))

    if not ok14:

        for ts, msg in eng.snapshot()["events"]:

            say("    %s  %s" % (ts, msg))

    # [15] 实操中插入已过本局帧数的提醒：应排队等下一局，

    #      不能劫持本局还够得着的提醒的岗（实机踩过：盯 700 时

    #      插入 f=5 -> 引擎进等下一局，700 白等停转）

    g = MockGame(300)

    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)

    outcomes15 = []

    eng.on_state = lambda r, o: outcomes15.append((r["id"], o))

    eng.start([{"id": "big", "frame": 400, "note": "", "state": "pending"}])

    time.sleep(0.3)

    eng.add({"id": "past", "frame": 25, "note": "", "state": "pending"})

    t0 = time.time()

    while time.time() - t0 < 30:

        if any(i == "big" for i, _o in outcomes15):

            g.paused = False

            g.f = 0.0

            g._t = time.perf_counter()   # 模拟下一局开战：帧数归零

            break

        time.sleep(0.05)

    eng.join(30)

    ok15 = outcomes15 == [("big", "done"), ("past", "done")]

    say("[15] 插入已过帧不劫持: %s（结果 %s）"

        % ("通过" if ok15 else "不通过", outcomes15))

    if not ok15:

        for ts, msg in eng.snapshot()["events"]:

            say("    %s  %s" % (ts, msg))

    # [16] 反序：先插已过帧（进等下一局占岗），再插够得着的 ->

    #      等待方要让位；够得着的跑完后已过帧在下局接手

    g = MockGame(300)

    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)

    outcomes16 = []

    eng.on_state = lambda r, o: outcomes16.append((r["id"], o))

    eng.start([{"id": "past", "frame": 25, "note": "", "state": "pending"}])

    time.sleep(0.5)      # 此时应在等下一局

    wait_ok16 = eng.waiting_next and not outcomes16

    eng.add({"id": "big", "frame": 400, "note": "", "state": "pending"})

    t0 = time.time()

    while time.time() - t0 < 30:

        if any(i == "big" for i, _o in outcomes16):

            g.paused = False

            g.f = 0.0

            g._t = time.perf_counter()

            break

        time.sleep(0.05)

    eng.join(30)

    ok16 = wait_ok16 and outcomes16 == [("big", "done"), ("past", "done")]

    say("[16] 等下一局时让位: %s（在等待=%s，结果 %s）"

        % ("通过" if ok16 else "不通过", wait_ok16, outcomes16))

    if not ok16:

        for ts, msg in eng.snapshot()["events"]:

            say("    %s  %s" % (ts, msg))

    # [17] 原生触控注入与 QPC 微秒精度自测

    t0_qpc = time.perf_counter()

    qpc_sleep(33.0)

    qpc_elapsed = (time.perf_counter() - t0_qpc) * 1000.0

    touch_sz = ctypes.sizeof(POINTER_TOUCH_INFO)

    touch_init = TouchInjector.init()

    ok17 = (25.0 <= qpc_elapsed <= 100.0) and (touch_sz == 144) and touch_init

    say("[17] 原生触控与QPC微秒计时: %s（耗时 %.2fms, struct=%d 字节, init=%s）"

        % ("通过" if ok17 else "不通过", qpc_elapsed, touch_sz, touch_init))

    # [18] 2x 且空格暂停被游戏忽略（实机踩过：2倍速一路跑不停）：

    #     刹车应直接用 ESC 呼出暂停菜单停住，逼近脉冲换 空格解暂+ESC

    #     复暂 双键模式，全程照常落位。修复前：两发空格都被吞，

    #     判 failed 收手，游戏一路跑完。从 f=20 起跑让 watch 先实测

    #     出 60fps 速率（真实流程：有速率证据刹车才走 ESC 优先）

    g = MockGame(20, fps=60, space_dead_running=True)

    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)

    outcomes18 = []

    eng.on_state = lambda r, o: outcomes18.append(o)

    eng.start([{"id": "s2x", "frame": 130, "note": "", "state": "pending"}])

    eng.join(30)

    snap = eng.snapshot()

    left18 = 130 - g.f

    ok18 = (snap["stage"] == "idle" and outcomes18 == ["done"]

            and g.paused and 1 <= left18 <= cfg["stop_margin"] + 2)

    say("[18] 2x空格失效ESC兜底: %s（停 f=%d，距目标 %d 帧，结果 %s）"

        % ("通过" if ok18 else "不通过", int(g.f), left18, outcomes18))

    if not ok18:

        for ts, msg in snap["events"]:

            say("    %s  %s" % (ts, msg))

    # [19] 运行中动态删除节点立即切轨
    #     队列中有 f=57 和 f=200。引擎开始追踪 f=57。
    #     在运行中动态删除 f=57，验证引擎立即取消 f=57 并切轨追踪 f=200，
    #     并在 f=200 正常刹车逼近完成。
    g = MockGame(10, fps=30)
    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)
    outcomes19 = []
    eng.on_state = lambda r, o: outcomes19.append((r["id"], o))
    eng.start([
        {"id": "r57", "frame": 57, "note": "删除项", "state": "pending"},
        {"id": "r200", "frame": 200, "note": "最终目标", "state": "pending"}
    ])
    time.sleep(0.1)
    eng.remove("r57")
    eng.join(30)
    snap = eng.snapshot()
    left19 = 200 - g.f
    ok19 = (snap["stage"] == "idle" and outcomes19 == [("r57", "cancelled"), ("r200", "done")]
            and g.paused and 1 <= left19 <= cfg["stop_margin"] + 2)
    say("[19] 运行中动态删除节点立即切轨: %s（停在 f=%d，距目标 %d 帧，完成 %s）"
        % ("通过" if ok19 else "未通过", int(g.f), left19, outcomes19))
    if not ok19:
        for ts, msg in snap["events"]:
            say("    %s  %s" % (ts, msg))

    # [20] 超近相邻节点玩家恢复门禁
    #     队列中有 f=55 和 f=57。在 f=55 刹停后，游戏处于暂停态。
    #     验证引擎在 f=57 时进入门禁守护（绝不自动逼近冲过 57），
    #     直到玩家手动步进到 f=57，精准触发 f=57 提醒。
    g = MockGame(10, fps=30)
    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject)
    outcomes20 = []
    eng.on_state = lambda r, o: outcomes20.append((r["id"], o))
    eng.start([
        {"id": "n55", "frame": 55, "note": "节点1", "state": "pending"},
        {"id": "n57", "frame": 57, "note": "超近节点2", "state": "pending"}
    ])
    # 等待 n55 完成（自动刹停在 53 帧附近）
    t0 = time.time()
    while time.time() - t0 < 5.0:
        if any(r[0] == "n55" for r in outcomes20):
            break
        time.sleep(0.05)
    
    # 模拟玩家在暂停中观察，此时引擎绝不自动逼近到 57
    time.sleep(0.3)
    eng_creeped_before_user = bool(g.f >= 57)
    
    # 模拟玩家手动按步进键走 2 帧到达 57
    g.f = 57.0
    eng.join(10)
    snap = eng.snapshot()
    ok20 = (snap["stage"] == "idle" and outcomes20 == [("n55", "done"), ("n57", "done")]
            and not eng_creeped_before_user and g.paused)
    say("[20] 超近相邻节点玩家恢复门禁: %s（门禁生效时自发过帧=%s，完成 %s）"
        % ("通过" if ok20 else "未通过", eng_creeped_before_user, outcomes20))
    if not ok20:
        for ts, msg in snap["events"]:
            say("    %s  %s" % (ts, msg))

    # [21] 开局暂停只是历史事件：玩家解暂停后，未来目标必须重新刹车。
    g = MockGame(0, hidden=True, pause_keys=("space",))
    eye = MockEye(g)
    eng = ReminderEngine(g.frames, cfg, fkeys, inject=g.inject, eye=eye)
    outcomes21 = []
    eng.on_state = lambda r, o: outcomes21.append(o)
    eng.start([{"id": "opening_then_brake", "frame": 55,
                "note": "", "state": "pending"}])
    time.sleep(0.2)
    eye.phase = 1
    time.sleep(0.2)
    eye.phase = 2
    time.sleep(2.3)
    eye.phase = 3
    t0 = time.time()
    while time.time() - t0 < 5:
        if any("等待战斗运行" in msg for _ts, msg in eng.snapshot()["events"]):
            break
        time.sleep(0.02)
    opening_paused = g.paused and any(
        "等待战斗运行" in msg for _ts, msg in eng.snapshot()["events"])
    if opening_paused:
        g.inject("space")               # 玩家手动解除开局暂停
    eng.join(20)
    snap = eng.snapshot()
    ok21 = (opening_paused and g.paused and outcomes21 == ["done"]
            and int(g.f) < 55
            and not any("开局触发确认已暂停不通过" in msg
                        for _ts, msg in snap["events"]))
    say("[21] 开局解暂后重新刹车: %s（停 f=%d，结果 %s）"
        % ("通过" if ok21 else "不通过", int(g.f), outcomes21))
    if not ok21:
        for ts, msg in snap["events"]:
            say("    %s  %s" % (ts, msg))

    ok = ok1 and ok2 and ok3 and ok4 and ok5 and ok6 and ok7 and ok8 and ok9 \
        and ok10 and ok11 and ok11b and ok11c and ok12 and ok13 and ok14 and ok15 and ok16 and ok17 \
        and ok18 and ok19 and ok20 and ok21

    say("总结: %s" % ("全部通过" if ok else "有失败项"))

    with open("_t.txt", "w", encoding="utf-8") as f:

        f.write("记轴本自检 %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))

        f.write("\n".join(OUT) + "\n")

    return ok

def main():

    ap = argparse.ArgumentParser()

    ap.add_argument("frame", nargs="?", type=int)

    ap.add_argument("note", nargs="?", default="")

    ap.add_argument("--dry", action="store_true")

    ap.add_argument("--selftest", action="store_true")

    args = ap.parse_args()

    if args.selftest:

        import sys

        sys.exit(0 if selftest() else 1)

    if args.frame is None:

        print(__doc__)

        return

    cfg = load_cfg()

    frame_keys = load_frame_keys() or {"y": 16.0, "r": 33.0, "t": 166.0}

    if args.dry:

        _dry_plan(args.frame, cfg, frame_keys)

        return

    if not is_admin():

        print("[!] 未提权！注入会被 UIPI 拦，请管理员运行（记轴本.bat）。")

        return

    if not ensure_ruler_running(interactive=True):

        print("[!] BarRuler 没开也启动不了，记轴本只认尺子的帧数。")

        return

    ruler = RulerClient()

    t0 = time.time()

    while time.time() - t0 < 10 and ruler.snapshot() is None:

        time.sleep(0.2)

    if ruler.snapshot() is None:

        print("[!] 连不上尺子接口（127.0.0.1:2606）。")

        return

    done = threading.Event()

    results = []

    def _on_state(rem, outcome):

        results.append(outcome)

        done.set()

    eng = ReminderEngine(ruler.frames_live, cfg, frame_keys,

                         on_state=_on_state, snap_ts_fn=ruler.snap_ts,

                         freeze_frames_fn=ruler.frames_fresh)

    rem = {"id": "cli", "frame": args.frame, "note": args.note,

           "state": "pending"}

    eng.start([rem])

    print("已武装：目标帧 %d「%s」。现在让游戏跑起来，Ctrl+C 退出。"

          % (args.frame, args.note))

    try:

        while not done.wait(0.5):

            pass

    except KeyboardInterrupt:

        eng.abort()

    eng.join(5)

    print("结果: %s" % (results[0] if results else "aborted"))

    for ts, msg in eng.snapshot()["events"]:

        print("  %s  %s" % (ts, msg))

if __name__ == "__main__":

    main()

