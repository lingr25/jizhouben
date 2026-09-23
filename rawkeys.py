# -*- coding: utf-8 -*-
"""Raw Input 键盘监听：直接从驱动层收键盘事件。

AFA（AutoHotkey）注册 y/t/r 当热键时，会用键盘钩子把这几个键"吃掉"，
普通钩子监听（keyboard 库）排在它后面就什么都听不到。
Raw Input 不走钩子链，只监听、不拦截。
但注意 Windows 的 UIPI 隔离：前台窗口是管理员程序（本机的游戏和 AFA
都是）时，普通权限进程连 Raw Input 都收不到——本程序必须提权启动
（启动计时器.bat 已自动请求管理员权限）。
"""
import ctypes
import ctypes.wintypes as wt
import threading

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_INPUT = 0x00FF
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
WM_KEYUP, WM_SYSKEYUP = 0x0101, 0x0105
RIDEV_INPUTSINK = 0x00000100   # 不在前台也收
RID_INPUT = 0x10000003
RIM_TYPEKEYBOARD = 1

# 虚拟键码 -> 键名（与 keyboard 库/录像 meta 的叫法对齐）
VK_NAMES = {
    0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x10: "shift",
    0x11: "ctrl", 0x12: "alt", 0x13: "pause", 0x14: "caps_lock",
    0x1B: "esc", 0x20: "space", 0x21: "page_up", 0x22: "page_down",
    0x23: "end", 0x24: "home", 0x25: "left", 0x26: "up",
    0x27: "right", 0x28: "down", 0x2C: "print_screen", 0x2D: "insert",
    0x2E: "delete", 0xBC: "comma", 0xBE: "period",
}
for _i in range(10):
    VK_NAMES[0x30 + _i] = str(_i)                # 0-9
for _i in range(26):
    VK_NAMES[0x41 + _i] = chr(0x61 + _i)         # a-z
for _i in range(24):
    VK_NAMES[0x70 + _i] = "f%d" % (_i + 1)       # f1-f24
for _i in range(10):
    VK_NAMES[0x60 + _i] = "num_%d" % _i          # 小键盘 0-9


def vk_name(vk):
    return VK_NAMES.get(vk, "vk%d" % vk)


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT),
                ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD),
                ("hDevice", wt.HANDLE), ("wParam", wt.WPARAM)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", wt.USHORT), ("Flags", wt.USHORT),
                ("Reserved", wt.USHORT), ("VKey", wt.USHORT),
                ("Message", wt.UINT), ("ExtraInformation", wt.ULONG)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("keyboard", RAWKEYBOARD)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t,
                             wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CreateWindowExW.restype = wt.HWND

CLASS_NAME = "ArkTimerRawKeys"


class RawKeyListener:
    """后台线程开一个看不见的窗口收 WM_INPUT，每个按下事件回调 on_key(键名)。
    注意：同一进程再建一个监听器会顶掉旧的（Raw Input 目标窗口按进程算），
    本程序里"录入按键"和"实战监听"先后各建一个，正好是想要的交接行为。"""

    def __init__(self, on_key):
        self.on_key = on_key
        self._ok = False
        self._ready = threading.Event()
        self._down = set()          # 当前按住的键：滤掉长按自动重复
        self._wndproc = WNDPROC(self._on_msg)   # 持引用防垃圾回收

    def start(self, timeout=3.0):
        """启动监听线程；返回 True=通道已就绪。"""
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(timeout)
        return self._ok

    # ---- 以下都跑在监听线程里 ----
    def _on_msg(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                size = wt.UINT(0)
                user32.GetRawInputData(lparam, RID_INPUT, None,
                                       ctypes.byref(size),
                                       ctypes.sizeof(RAWINPUTHEADER))
                buf = ctypes.create_string_buffer(size.value)
                user32.GetRawInputData(lparam, RID_INPUT, buf,
                                       ctypes.byref(size),
                                       ctypes.sizeof(RAWINPUTHEADER))
                ri = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
                if ri.header.dwType == RIM_TYPEKEYBOARD:
                    vk = ri.keyboard.VKey
                    extra = getattr(ri.keyboard, 'ExtraInformation', 0)
                    if extra != 0x41524B54:  # 过滤系统内部注入按键 (INJECT_MAGIC)
                        if ri.keyboard.Message in (WM_KEYDOWN, WM_SYSKEYDOWN):
                            if vk not in self._down:
                                self._down.add(vk)
                                threading.Thread(target=self.on_key,
                                                 args=(vk_name(vk),),
                                                 daemon=True).start()
                        elif ri.keyboard.Message in (WM_KEYUP, WM_SYSKEYUP):
                            self._down.discard(vk)
            except Exception:
                pass   # 单个事件解析失败不能带崩消息循环
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self):
        try:
            hinst = kernel32.GetModuleHandleW(None)

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                            ("cbClsExtra", ctypes.c_int),
                            ("cbWndExtra", ctypes.c_int),
                            ("hInstance", wt.HINSTANCE), ("hIcon", wt.HANDLE),
                            ("hCursor", wt.HANDLE),
                            ("hbrBackground", wt.HANDLE),
                            ("lpszMenuName", wt.LPCWSTR),
                            ("lpszClassName", wt.LPCWSTR)]

            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = CLASS_NAME
            user32.RegisterClassW(ctypes.byref(wc))   # 已注册过会失败，无所谓
            # 必须是普通隐藏窗口：message-only 窗口收不到 Raw Input
            hwnd = user32.CreateWindowExW(0, CLASS_NAME, CLASS_NAME,
                                          0, 0, 0, 0, 0, None, None,
                                          hinst, None)
            if not hwnd:
                return
            rid = RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, hwnd)
            if not user32.RegisterRawInputDevices(
                    ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE)):
                return
            self._ok = True
        finally:
            self._ready.set()

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
