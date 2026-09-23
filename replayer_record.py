# -*- coding: utf-8 -*-
"""战斗复现器 · 高鲁棒性实机全自动录轴器 (replayer_record.py)。

核心鲁棒性设计：
1. 窗口与 DPI 动态适配：通过 Win32 GetClientRect + ClientToScreen 动态获取游戏画面真实客户区（去除黑边与标题栏）；
2. 双手势模式支持：
   - 一段式手势：手牌拖到地块连贯滑向直接松手；
   - 两段式手势：手牌拖到地块松手 -> 看到朝向盘后再次按下滑动选向；
3. MAA 官方 3D 逆透视矩阵 + 地形合法性吸附：结合关卡 buildableType 与 heightType 自动吸附到最近合法网格 (col, row)；
4. 角度象限解算 (atan2) 与死区过滤：精准识别 UP/DOWN/LEFT/RIGHT；
5. BarRuler 内存直读帧数与费用条快照实时绑定；
6. 实时落盘至 axis/YYYYMMDD_HHMMSS.jsonl。
"""
import argparse
import ctypes
import ctypes.wintypes
import json
import math
import os
import queue
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from replayer_mapper import MaaTileMapper
from replayer_types import Direction, NormalizedOp, OpType
from ruler_client import RulerClient

# Win32 API 与常量
WH_MOUSE_LL = 14
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
WM_RBUTTONDOWN = 0x0204

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


# 64 位与 32 位系统通用 Windows 句柄与回调签名
LRESULT = ctypes.c_ssize_t
_user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
_user32.CallNextHookEx.restype = LRESULT

_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.wintypes.HINSTANCE, ctypes.wintypes.DWORD]
_user32.SetWindowsHookExW.restype = ctypes.c_void_p

_user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
_user32.UnhookWindowsHookEx.restype = ctypes.wintypes.BOOL


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)


def get_window_rect_from_point(screen_x: int, screen_y: int) -> Optional[Tuple[int, int, int, int]]:
    """根据鼠标点击的屏幕物理坐标，动态获取并锁定被点击窗口的真实客户区。"""
    pt = POINT(screen_x, screen_y)
    hwnd = _user32.WindowFromPoint(pt)
    if not hwnd:
        return None

    # 向上追溯根窗口 (GA_ROOT = 2)
    root_hwnd = _user32.GetAncestor(hwnd, 2)
    target_hwnd = root_hwnd if root_hwnd else hwnd

    rect = RECT()
    _user32.GetClientRect(target_hwnd, ctypes.byref(rect))
    origin = POINT(0, 0)
    _user32.ClientToScreen(target_hwnd, ctypes.byref(origin))
    w = rect.right - rect.left
    h = rect.bottom - rect.top

    if w >= 320 and h >= 180:
        return (origin.x, origin.y, w, h)
    return None


def find_game_window_rect() -> Optional[Tuple[int, int, int, int]]:
    """查找游戏窗口真实客户区，自动避让腾讯会议 (Wemeet) 等浮动工具栏。"""
    titles = ["明日方舟", "Arknights", "MuMu", "LDPlayer", "Nox", "BlueStacks", "雷电", "夜神"]
    exclude_keywords = ["腾讯会议", "TencentMeeting", "WeMeet", "共享屏幕"]

    hwnd = None

    def enum_cb(h, extra):
        length = _user32.GetWindowTextLengthW(h)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(h, buf, length + 1)
            title = buf.value
            if any(ex in title for ex in exclude_keywords):
                return True
            if any(k.lower() in title.lower() for k in titles):
                extra.append(h)
                return False
        return True

    found = []
    _user32.EnumWindows(
        ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(enum_cb),
        ctypes.byref(ctypes.py_object(found)),
    )
    if found:
        hwnd = found[0]

    if not hwnd:
        sw = _user32.GetSystemMetrics(0)
        sh = _user32.GetSystemMetrics(1)
        return (0, 0, sw, sh)

    rect = RECT()
    _user32.GetClientRect(hwnd, ctypes.byref(rect))
    pt = POINT(0, 0)
    _user32.ClientToScreen(hwnd, ctypes.byref(pt))
    width = rect.right - rect.left
    height = rect.bottom - rect.top

    return (pt.x, pt.y, width, height)


class RobustAxisRecorder:
    """高鲁棒性实机全自动录轴引擎。"""

    # 阈值配置 (放宽判定区间，极大提高实战抓取率)
    DRAG_MIN_DIST = 20        # 判定为有效拖拽的最小像素位移
    ORIENT_DEADZONE = 18      # 朝向判定死区像素阈值
    HAND_AREA_Y_MIN = 0.65    # 手牌区相对高度下界 (从 65% 高度开始即可抓卡)
    DEADZONE_GRID_DIST = 100.0 # 逆透视吸附允许的最大像素误差

    def __init__(
        self,
        level_id: str = "obt/main/level_main_01-07",
        out_dir: str = "axis",
        win_rect: Optional[Tuple[int, int, int, int]] = None,
        on_op_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.level_id = level_id
        self.out_dir = out_dir
        self.win_rect_override = win_rect
        self.on_op_callback = on_op_callback

        self.mapper = MaaTileMapper()
        self.ruler = RulerClient()

        self.axis_file_path: Optional[str] = None
        self.seq = 0
        self.is_recording = False
        self.recorded_ops: List[Dict[str, Any]] = []
        self.status_log: str = "录轴已就绪，请在模拟器中操作..."

        # 手势生命周期状态机
        self._lock = threading.Lock()
        self.is_mouse_down = False
        self.down_pos: Optional[Tuple[int, int]] = None
        self.down_frame: int = 0
        self.down_slot: Optional[int] = None
        self.is_hand_drag = False

        # 两段式朝向盘等待状态
        self.pending_wheel_grid: Optional[Tuple[int, int]] = None
        self.pending_wheel_slot: Optional[int] = None
        self.pending_wheel_frame: int = 0
        self.wheel_down_pos: Optional[Tuple[int, int]] = None

        # Win32 钩子句柄、异步事件队列与工作线程
        self._hook = None
        self._proc = None
        self._hook_tid = 0
        self._event_queue = queue.Queue(maxsize=2000)
        self._hook_thread: Optional[threading.Thread] = None
        self._worker_thread: Optional[threading.Thread] = None

    def start(self, level_id: Optional[str] = None):
        """开启录轴会话。"""
        with self._lock:
            if self.is_recording:
                return
            if level_id:
                self.level_id = level_id

            os.makedirs(self.out_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            self.axis_file_path = os.path.join(self.out_dir, f"{ts}.jsonl")

            map_data = self.mapper.load_level_data(self.level_id)
            w = map_data.get("width", 11) if map_data else 11
            h = map_data.get("height", 7) if map_data else 7

            header = {
                "type": "header",
                "ver": 2,
                "wall": time.strftime("%Y-%m-%d %H:%M:%S"),
                "level": {"id": self.level_id, "w": w, "h": h},
                "clock": "ruler",
            }
            with open(self.axis_file_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(header, ensure_ascii=False) + "\n")

            self.seq = 0
            self.recorded_ops.clear()
            while not self._event_queue.empty():
                try:
                    self._event_queue.get_nowait()
                except Exception:
                    break

            self.is_recording = True
            self._start_worker_thread()
            self._start_hook_thread()
            print(f"\n[RobustRecorder] 录轴已启动 -> {self.axis_file_path} (关卡={self.level_id})")

    def stop(self) -> Optional[str]:
        """停止录轴会话并返回生成的轴文件路径 (0 条操作自动删除不留垃圾)。"""
        with self._lock:
            if not self.is_recording:
                return self.axis_file_path
            self.is_recording = False
            self._stop_hook_thread()
            self._stop_worker_thread()

            # 0 条操作自动丢弃并删除文件
            if len(self.recorded_ops) == 0:
                if self.axis_file_path and os.path.exists(self.axis_file_path):
                    try:
                        os.remove(self.axis_file_path)
                    except Exception:
                        pass
                print("[RobustRecorder] 录制操作数为 0，已自动放弃并清理空文件。")
                return None

            print(f"[RobustRecorder] 录轴已停止，共录制 {len(self.recorded_ops)} 条操作。")
            return self.axis_file_path

    def _get_current_frame_and_cost(self) -> Tuple[Optional[int], Optional[int]]:
        """获取当前 BarRuler 内存直读帧数。战斗未开始(选关/进关加载)时返回 None。"""
        try:
            info = self.ruler.poll()
            if info and "totalElapsedFrames" in info:
                f = info.get("totalElapsedFrames")
                if f is not None:
                    fill = info.get("costFillRatio")
                    return int(f), int(fill * 100) if fill is not None else None
        except Exception:
            pass
        return None, None

    def _emit_op(
        self,
        op_type: OpType,
        frame: int,
        col: int = -1,
        row: int = -1,
        slot: Optional[int] = None,
        direction: Direction = Direction.NONE,
        fill: Optional[int] = None,
        note: str = "",
    ):
        """落盘并广播单条 Op。"""
        if not self.axis_file_path:
            return

        self.seq += 1
        op_dict = {
            "t": time.time(),
            "f": frame,
            "type": "op",
            "seq": self.seq,
            "op": op_type.value,
            "col": col,
            "row": row,
            "slot": slot,
            "dir": direction.value if direction != Direction.NONE else None,
            "fill": fill,
            "note": note,
        }
        self.recorded_ops.append(op_dict)

        with open(self.axis_file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(op_dict, ensure_ascii=False) + "\n")

        print(f"  [Op Captured] #{self.seq} f={frame:<5d} {op_type.value.upper():<7s} 格子=({col},{row}) 卡槽={slot} 朝向={direction.value}")

        if self.on_op_callback:
            try:
                self.on_op_callback(op_dict)
            except Exception:
                pass

    def _calculate_slot_index(self, rel_x: float, total_slots: int = 12) -> int:
        """根据 X 比例计算底部手牌卡槽 (1 ~ 12)。"""
        x_start, x_end = 0.20, 0.90
        step = (x_end - x_start) / max(1, total_slots - 1)
        idx = int(round(1 + (rel_x - x_start) / step))
        return max(1, min(total_slots, idx))

    def _vector_to_direction(self, dx: float, dy: float) -> Direction:
        """使用 atan2 象限角将滑动位移向量解算为严格的四向朝向。"""
        if math.hypot(dx, dy) < self.ORIENT_DEADZONE:
            return Direction.NONE

        angle = math.atan2(dy, dx)
        deg = math.degrees(angle)

        if -45.0 <= deg < 45.0:
            return Direction.RIGHT
        elif 45.0 <= deg < 135.0:
            return Direction.DOWN
        elif -135.0 <= deg < -45.0:
            return Direction.UP
        else:
            return Direction.LEFT

    def handle_mouse_event(self, event_type: int, screen_x: int, screen_y: int):
        """核心鼠标事件处理状态机 (严格受控于战斗生命周期)。"""
        if not self.is_recording:
            return

        # 1. 严格门禁：只有在游戏真正开战（BarRuler 帧数就绪且有效）后才录入操作
        f_now, fill = self._get_current_frame_and_cost()
        if f_now is None:
            # 尚在选人、编队、点击进关或结算加载，自动静默忽略，绝不污染轴文件
            self.is_mouse_down = False
            self.is_hand_drag = False
            return

        # 2. 动态锁定：若未锁定窗口，优先通过当前点击点获取真实窗口矩形
        if not self.win_rect_override:
            dyn_rect = get_window_rect_from_point(screen_x, screen_y)
            win_rect = dyn_rect or find_game_window_rect()
        else:
            win_rect = self.win_rect_override

        if not win_rect:
            return

        win_x, win_y, win_w, win_h = win_rect
        # 边界检查
        if not (win_x <= screen_x <= win_x + win_w and win_y <= screen_y <= win_y + win_h):
            self.is_mouse_down = False
            self.is_hand_drag = False
            return

        # 转换为游戏窗口内相对坐标 (0.0 ~ 1.0)
        rel_x = (screen_x - win_x) / float(win_w)
        rel_y = (screen_y - win_y) / float(win_h)

        # ---------------- 1. 鼠标按下 (WM_LBUTTONDOWN) ----------------
        if event_type == WM_LBUTTONDOWN:
            self.is_mouse_down = True

            # A. 底部手牌区按下 (进入抓卡态)
            if rel_y >= self.HAND_AREA_Y_MIN:
                slot_idx = self._calculate_slot_index(rel_x)
                self.down_pos = (screen_x, screen_y)
                self.down_frame = f_now
                self.down_slot = slot_idx
                self.is_hand_drag = True
                self.pending_wheel_grid = None
                print(f"  [Recorder] 手牌抓卡按下: slot={slot_idx} (rel_x={rel_x:.2f}, rel_y={rel_y:.2f}) f={f_now}")

            # B. 两段式朝向盘中心按下
            elif self.pending_wheel_grid is not None:
                self.wheel_down_pos = (screen_x, screen_y)
                print(f"  [Recorder] 朝向盘按下: grid={self.pending_wheel_grid} f={f_now}")

            # C. 战场内普通点击 (可能开技能/选干员)
            else:
                self.down_pos = (screen_x, screen_y)
                self.down_frame = f_now
                self.is_hand_drag = False
                print(f"  [Recorder] 战场点击按下: ({screen_x},{screen_y}) rel=({rel_x:.2f},{rel_y:.2f}) f={f_now}")

        # ---------------- 2. 鼠标释放 (WM_LBUTTONUP) ----------------
        elif event_type == WM_LBUTTONUP:
            if not self.is_mouse_down:
                return
            self.is_mouse_down = False

            # A. 手牌拖拽释放 (部署动作)
            if self.is_hand_drag and self.down_pos is not None:
                dx = screen_x - self.down_pos[0]
                dy = screen_y - self.down_pos[1]
                dist = math.hypot(dx, dy)

                # 有效向上拖入战场
                if dist >= self.DRAG_MIN_DIST and rel_y < self.HAND_AREA_Y_MIN:
                    adsorb = self.mapper.pixel_to_grid(
                        self.level_id, px=screen_x, py=screen_y, win_rect=win_rect, is_tilt=True
                    )
                    if adsorb and adsorb[2] <= self.DEADZONE_GRID_DIST:
                        col, row, _ = adsorb
                        target_px, target_py = self.mapper.grid_to_pixel(
                            self.level_id, col=col, row=row, win_rect=win_rect, is_tilt=True
                        )
                        orient_dx = screen_x - target_px
                        orient_dy = screen_y - target_py

                        direction = self._vector_to_direction(orient_dx, orient_dy)
                        if direction != Direction.NONE:
                            self._emit_op(
                                op_type=OpType.DEPLOY,
                                frame=self.down_frame,
                                col=col,
                                row=row,
                                slot=self.down_slot,
                                direction=direction,
                                note=f"部署卡槽 {self.down_slot}",
                            )
                            self.pending_wheel_grid = None
                        else:
                            self.pending_wheel_grid = (col, row)
                            self.pending_wheel_slot = self.down_slot
                            self.pending_wheel_frame = self.down_frame

                self.is_hand_drag = False
                self.down_pos = None

            # B. 两段式朝向滑动释放
            elif self.pending_wheel_grid is not None and self.wheel_down_pos is not None:
                dx = screen_x - self.wheel_down_pos[0]
                dy = screen_y - self.wheel_down_pos[1]
                direction = self._vector_to_direction(dx, dy)

                self._emit_op(
                    op_type=OpType.DEPLOY,
                    frame=self.pending_wheel_frame,
                    col=self.pending_wheel_grid[0],
                    row=self.pending_wheel_grid[1],
                    slot=self.pending_wheel_slot,
                    direction=direction,
                    note=f"部署卡槽 {self.pending_wheel_slot}",
                )
                self.pending_wheel_grid = None
                self.wheel_down_pos = None

            # C. 战场点击 (技能/撤退识别)
            elif self.down_pos is not None:
                dx = screen_x - self.down_pos[0]
                dy = screen_y - self.down_pos[1]
                if math.hypot(dx, dy) < 20 and rel_y < self.HAND_AREA_Y_MIN:
                    adsorb = self.mapper.pixel_to_grid(
                        self.level_id, px=screen_x, py=screen_y, win_rect=win_rect, is_tilt=False
                    )
                    if adsorb and adsorb[2] <= self.DEADZONE_GRID_DIST:
                        col, row, _ = adsorb
                        self._emit_op(
                            op_type=OpType.SKILL,
                            frame=self.down_frame,
                            col=col,
                            row=row,
                            note=f"操作干员 ({col},{row})",
                        )
                self.down_pos = None

    # ---------------- 异步无阻塞 Worker 线程 ----------------
    def _start_worker_thread(self):
        def _worker_loop():
            while self.is_recording:
                try:
                    event = self._event_queue.get(timeout=0.05)
                    wParam, px, py = event
                    self.handle_mouse_event(wParam, px, py)
                except queue.Empty:
                    continue
                except Exception as e:
                    print(f"[RecorderWorker] 异步处理异常: {e}")

        t = threading.Thread(target=_worker_loop, daemon=True)
        self._worker_thread = t
        t.start()

    def _stop_worker_thread(self):
        if self._worker_thread:
            self._worker_thread.join(timeout=0.3)
            self._worker_thread = None

    # ---------------- Win32 底层钩子事件循环 (0 延迟极速返回) ----------------
    def _start_hook_thread(self):
        def _hook_loop():
            self._hook_tid = _kernel32.GetCurrentThreadId()

            def _low_level_mouse_proc(nCode, wParam, lParam):
                # 极速入队 (耗时 < 0.001ms)，完全与后台解耦
                try:
                    if nCode >= 0 and wParam in (WM_LBUTTONDOWN, WM_LBUTTONUP):
                        info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                        self._event_queue.put_nowait((wParam, info.pt.x, info.pt.y))
                except Exception:
                    pass
                try:
                    return _user32.CallNextHookEx(self._hook, nCode, wParam, lParam)
                except Exception:
                    return 0

            self._proc = HOOKPROC(_low_level_mouse_proc)
            self._hook = _user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, None, 0)
            if not self._hook:
                err = _kernel32.GetLastError()
                print(f"[RecorderHook] SetWindowsHookExW 失败, 错误码={err}")
                return

            msg = ctypes.wintypes.MSG()
            while self.is_recording:
                # 消息泵处理
                b_ret = _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)
                if b_ret:
                    if msg.message == 0x0012:  # WM_QUIT
                        break
                    _user32.TranslateMessage(ctypes.byref(msg))
                    _user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    time.sleep(0.001)

            if self._hook:
                _user32.UnhookWindowsHookEx(self._hook)
                self._hook = None

        t = threading.Thread(target=_hook_loop, daemon=True)
        self._hook_thread = t
        t.start()

    def _stop_hook_thread(self):
        self.is_recording = False
        if self._hook_tid:
            try:
                _user32.PostThreadMessageW(self._hook_tid, 0x0012, 0, 0)
            except Exception:
                pass
        if self._hook_thread:
            self._hook_thread.join(timeout=0.5)
            self._hook_thread = None


def selftest() -> bool:
    """自测试：模拟一段式与两段式实机部署录制。"""
    print("[SelfTest] 开始测试 replayer_record.py (高鲁棒性录轴引擎)...")
    test_dir = os.path.join("axis", "test_records")
    win = (0, 0, 1920, 1080)
    rec = RobustAxisRecorder(level_id="obt/main/level_main_01-07", out_dir=test_dir, win_rect=win)
    # Mock BarRuler 处于战斗状态 (f=0)
    rec._get_current_frame_and_cost = lambda: (0, 100)
    rec.start()

    # 1. 模拟一段式快速连贯部署：
    # 在卡槽 3 按下 (X=0.34, Y=0.92) -> 向上拖拽到 (1, 2) 地块并在松手时向右滑出向量 (dx=+60, dy=0)
    slot_x = int(1920 * 0.34)
    slot_y = int(1080 * 0.92)

    # 获取 (1, 2) 地块的像素位置
    grid_x, grid_y = rec.mapper.grid_to_pixel("obt/main/level_main_01-07", col=1, row=2, win_rect=win, is_tilt=True)

    rec.handle_mouse_event(WM_LBUTTONDOWN, slot_x, slot_y)
    rec.handle_mouse_event(WM_LBUTTONUP, grid_x + 60, grid_y)

    # 验证一段式落盘
    assert len(rec.recorded_ops) == 1, f"一段式录制失败: ops={len(rec.recorded_ops)}"
    op1 = rec.recorded_ops[0]
    assert op1["op"] == "deploy" and op1["dir"] == "right"
    print(f"  [OK] 一段式连贯拖拽录制校验通过: 卡槽={op1['slot']} 格子=({op1['col']},{op1['row']}) 朝向={op1['dir']}")

    # 2. 模拟两段式部署：
    # 卡槽 6 按下 -> 拖到 (2, 3) 垂直松手 (无向量) -> 朝向盘再次按下向下滑动 80px 松手
    slot6_x = int(1920 * 0.52)
    grid2_x, grid2_y = rec.mapper.grid_to_pixel("obt/main/level_main_01-07", col=2, row=3, win_rect=win, is_tilt=True)

    rec.handle_mouse_event(WM_LBUTTONDOWN, slot6_x, slot_y)
    rec.handle_mouse_event(WM_LBUTTONUP, grid2_x, grid2_y)  # 垂直松手

    assert rec.pending_wheel_grid is not None, "两段式状态挂起失败"
    # 朝向盘向下滑动
    rec.handle_mouse_event(WM_LBUTTONDOWN, grid2_x, grid2_y)
    rec.handle_mouse_event(WM_LBUTTONUP, grid2_x, grid2_y + 80)

    assert len(rec.recorded_ops) == 2, f"两段式录制失败: ops={len(rec.recorded_ops)}"
    op2 = rec.recorded_ops[1]
    assert op2["op"] == "deploy" and op2["dir"] == "down"
    print(f"  [OK] 两段式朝向盘录制校验通过: 卡槽={op2['slot']} 格子=({op2['col']},{op2['row']}) 朝向={op2['dir']}")

    rec.stop()
    print("[SelfTest] replayer_record.py 全部测试通过！")
    return True


if __name__ == "__main__":
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        success = selftest()
        sys.exit(0 if success else 1)
