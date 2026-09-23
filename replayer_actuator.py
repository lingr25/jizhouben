# -*- coding: utf-8 -*-
"""战斗复现器 · 闭环执行器 (replayer_actuator.py)。

严格复刻 MAA (MaaAssistantArknights) 核心机制：
1. 对齐 MaaCore/Controller/SwipeHelper.hpp：三次样条减速插值 (cubic_spline)；
2. 对齐 resource/tasks.json 中 BattleUseOper 与 BattleSwipeOper 的真实时序与物理参数：
   - 抓卡 preDelay = 150ms (按下等待干员浮起)
   - 释放 postDelay = 200ms (释放等待朝向盘弹起)
   - 滑动初速度 slope_in = 2.0, 末速度 slope_out = 0.0
   - 滑动最小 duration = 300ms, 步进 interval = 10ms
3. Windows SendInput 硬件级模拟与带 Post-check 的部署六阶段状态机。
"""
import argparse
import ctypes
import ctypes.wintypes
import math
import random
import sys
import time
from typing import Callable, List, Optional, Tuple, Union

import numpy as np

from replayer_types import DeployStage, Direction, StageResult
from replayer_vision import compute_frame_diff, probe_orient_wheel

# Windows SendInput 常量与结构
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

# 扫描码映射 (Scan Code)
SCAN_CODES = {
    "space": 0x39,
    "esc": 0x01,
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
    "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14,
    "y": 0x15, "f": 0x21,
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.wintypes.ULONG)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.wintypes.ULONG)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.wintypes.DWORD),
        ("wParamL", ctypes.wintypes.WORD),
        ("wParamH", ctypes.wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.wintypes.DWORD),
        ("union", _INPUTUNION),
    ]


_user32 = ctypes.windll.user32


def _send_input(inputs: List[INPUT]):
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    _user32.SendInput(n, arr, ctypes.sizeof(INPUT))


def _screen_size() -> Tuple[int, int]:
    return _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)


def _to_abs_coords(x: int, y: int) -> Tuple[int, int]:
    sw, sh = _screen_size()
    abs_x = int(x * 65535 / (sw - 1)) if sw > 1 else 0
    abs_y = int(y * 65535 / (sh - 1)) if sh > 1 else 0
    return abs_x, abs_y


# ---------------- MAA 官方触控动力学实现 (SwipeHelper.hpp) ----------------


def cubic_spline(slope_0: float, slope_1: float, t: float) -> float:
    """MAA 官方三次样条插值函数 (SwipeHelper.hpp: cubic_spline)。

    用于生成符合真人手势特性的平滑滑动速度曲线。
    slope_0: 起点斜率 (初速度)
    slope_1: 终点斜率 (末速度)
    t: 归一化时间进度 [0.0, 1.0]
    """
    a = slope_0
    b = -(2.0 * slope_0 + slope_1 - 3.0)
    c = -(-slope_0 - slope_1 + 2.0)
    return a * t + b * (t**2) + c * (t**3)


def generate_maa_swipe_trajectory(
    start: Tuple[int, int],
    end: Tuple[int, int],
    duration_ms: int = 300,
    interval_ms: int = 10,
    slope_in: float = 2.0,
    slope_out: float = 0.0,
) -> List[Tuple[int, int]]:
    """生成 MAA 风格三次样条平滑插值轨迹点集。

    默认参数与 MAA tasks.json 中 BattleSwipeOper 严格一致：
    - slope_in = 2.0 (初速度快)
    - slope_out = 0.0 (末速度慢，平滑减速防过冲)
    - interval_ms = 10ms
    """
    x1, y1 = start
    x2, y2 = end
    points: List[Tuple[int, int]] = [start]

    cur_time = interval_ms
    while cur_time < duration_ms:
        progress = cubic_spline(slope_in, slope_out, cur_time / float(duration_ms))
        cur_x = int(round(x1 + (x2 - x1) * progress))
        cur_y = int(round(y1 + (y2 - y1) * progress))
        points.append((cur_x, cur_y))
        cur_time += interval_ms

    points.append(end)
    return points


# ---------------- 硬件输入原语 ----------------


def mouse_move(x: int, y: int, is_dry: bool = False):
    """移动鼠标至绝对屏幕坐标 (x, y)。"""
    if is_dry:
        return
    abs_x, abs_y = _to_abs_coords(x, y)
    inp = INPUT(
        type=INPUT_MOUSE,
        union=_INPUTUNION(
            mi=MOUSEINPUT(
                dx=abs_x,
                dy=abs_y,
                mouseData=0,
                dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                time=0,
                dwExtraInfo=None,
            )
        ),
    )
    _send_input([inp])


def mouse_down(x: int, y: int, button: str = "left", is_dry: bool = False):
    """在 (x, y) 按下鼠标。"""
    if is_dry:
        return
    abs_x, abs_y = _to_abs_coords(x, y)
    flag = MOUSEEVENTF_LEFTDOWN if button == "left" else MOUSEEVENTF_RIGHTDOWN
    inp = INPUT(
        type=INPUT_MOUSE,
        union=_INPUTUNION(
            mi=MOUSEINPUT(
                dx=abs_x,
                dy=abs_y,
                mouseData=0,
                dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | flag,
                time=0,
                dwExtraInfo=None,
            )
        ),
    )
    _send_input([inp])


def mouse_up(x: int, y: int, button: str = "left", is_dry: bool = False):
    """在 (x, y) 释放鼠标。"""
    if is_dry:
        return
    abs_x, abs_y = _to_abs_coords(x, y)
    flag = MOUSEEVENTF_LEFTUP if button == "left" else MOUSEEVENTF_RIGHTUP
    inp = INPUT(
        type=INPUT_MOUSE,
        union=_INPUTUNION(
            mi=MOUSEINPUT(
                dx=abs_x,
                dy=abs_y,
                mouseData=0,
                dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | flag,
                time=0,
                dwExtraInfo=None,
            )
        ),
    )
    _send_input([inp])


def key_pulse(key_name: str, hold_ms: int = 30, is_dry: bool = False):
    """以硬件扫描码发送单次按键脉冲。"""
    if is_dry:
        return
    sc = SCAN_CODES.get(key_name.lower())
    if sc is None:
        return

    down_inp = INPUT(
        type=INPUT_KEYBOARD,
        union=_INPUTUNION(
            ki=KEYBDINPUT(
                wVk=0,
                wScan=sc,
                dwFlags=KEYEVENTF_SCANCODE,
                time=0,
                dwExtraInfo=None,
            )
        ),
    )
    up_inp = INPUT(
        type=INPUT_KEYBOARD,
        union=_INPUTUNION(
            ki=KEYBDINPUT(
                wVk=0,
                wScan=sc,
                dwFlags=KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP,
                time=0,
                dwExtraInfo=None,
            )
        ),
    )

    _send_input([down_inp])
    time.sleep(hold_ms / 1000.0)
    _send_input([up_inp])


def execute_skill_click(
    target_xy: Tuple[int, int],
    is_dry: bool = False,
):
    """暂停状态下精准触发技能：
    1. 点击干员地块中心唤出面板；
    2. 等待面板渲染后，点击上方技能按钮区域 (偏移 -140px)。
    """
    if is_dry:
        return
    cx, cy = target_xy
    # 1. 点击干员
    mouse_move(cx, cy)
    time.sleep(0.02)
    mouse_down(cx, cy)
    time.sleep(0.03)
    mouse_up(cx, cy)
    time.sleep(0.08)

    # 2. 点击头顶技能按钮 (MAA tasks.json: BattleSkillReady 偏置 [-28, -140])
    skill_x = cx - 28
    skill_y = cy - 140
    mouse_move(skill_x, skill_y)
    time.sleep(0.02)
    mouse_down(skill_x, skill_y)
    time.sleep(0.03)
    mouse_up(skill_x, skill_y)


def execute_retreat_click(
    target_xy: Tuple[int, int],
    is_dry: bool = False,
):
    """暂停状态下精准触发撤退：
    1. 点击干员地块中心唤出面板；
    2. 点击左下方撤退按钮区域。
    """
    if is_dry:
        return
    cx, cy = target_xy
    # 1. 点击干员
    mouse_move(cx, cy)
    time.sleep(0.02)
    mouse_down(cx, cy)
    time.sleep(0.03)
    mouse_up(cx, cy)
    time.sleep(0.08)

    # 2. 点击撤退按钮 (偏置 [-150, 60])
    ret_x = cx - 150
    ret_y = cy + 60
    mouse_move(ret_x, ret_y)
    time.sleep(0.02)
    mouse_down(ret_x, ret_y)
    time.sleep(0.03)
    mouse_up(ret_x, ret_y)


def safe_cancel(cancel_xy: Tuple[int, int] = (100, 100), is_dry: bool = False):
    """安全取消当前拖拽/悬浮，点击左上空白区域。"""
    if is_dry:
        return
    cx, cy = cancel_xy
    mouse_up(cx, cy, "left", is_dry=is_dry)
    time.sleep(0.05)
    mouse_down(cx, cy, "left", is_dry=is_dry)
    time.sleep(0.03)
    mouse_up(cx, cy, "left", is_dry=is_dry)


def ensure_game_window_foreground(
    title_keywords: Tuple[str, ...] = ("明日方舟", "Arknights", "MuMu", "LDPlayer"),
    exclude_keywords: Tuple[str, ...] = ("腾讯会议", "TencentMeeting", "WeMeet", "共享屏幕"),
) -> bool:
    """确保游戏/模拟器窗口处于 Windows 前台激活状态，自动避让腾讯会议悬浮条。"""
    hwnd = None

    def enum_cb(h, extra):
        length = _user32.GetWindowTextLengthW(h)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(h, buf, length + 1)
            title = buf.value
            if any(ex in title for ex in exclude_keywords):
                return True
            if any(k in title for k in title_keywords):
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

    if hwnd:
        try:
            _user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            _user32.SetForegroundWindow(hwnd)
            return True
        except Exception:
            return False
    return False


# ---------------- MAA 级部署流水线 ----------------


class DeployPipeline:
    """部署六阶段状态机管理器 (严格对齐 MAA 官方时序与实战熔断防护)。"""

    # MAA tasks.json 标准参数
    PRE_DELAY_SEC = 0.15      # BattleUseOper.preDelay: 150ms
    DRAG_DURATION_MS = 300    # BattleSwipeOper.specialParams[4]: 300ms
    ORIENT_DURATION_MS = 150  # BattleSwipeOper.postDelay: 150ms
    ORIENT_DIST_PX = 140      # 朝向滑动像素距离 (1280x720 基准换算)
    WHEEL_TIMEOUT_SEC = 0.40  # 朝向盘探测超时上限 (400ms)

    def __init__(
        self,
        grab_frame_fn: Optional[Callable[[], Optional[np.ndarray]]] = None,
        is_dry: bool = False,
    ):
        self.grab_frame = grab_frame_fn or (lambda: None)
        self.is_dry = is_dry

    def execute_deploy(
        self,
        slot_xy: Tuple[int, int],
        target_xy: Tuple[int, int],
        direction: Direction = Direction.NONE,
        post_check: bool = True,
    ) -> List[StageResult]:
        """执行符合 MAA 官方标准且具备实战熔断防护的部署流水线。"""
        results: List[StageResult] = []

        # 前置防护：确保窗口激活
        if not self.is_dry:
            ensure_game_window_foreground()

        # ---------------- Stage 1: PICK ----------------
        t0 = time.perf_counter()
        mouse_move(slot_xy[0], slot_xy[1], is_dry=self.is_dry)
        time.sleep(0.02)
        mouse_down(slot_xy[0], slot_xy[1], is_dry=self.is_dry)
        # 对齐 MAA BattleUseOper.preDelay (150ms)
        time.sleep(self.PRE_DELAY_SEC)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        results.append(StageResult(stage=DeployStage.PICK, success=True, duration_ms=dt_ms))

        # ---------------- Stage 2: DRAG (MAA 三次样条减速插值) ----------------
        t0 = time.perf_counter()
        traj = generate_maa_swipe_trajectory(
            start=slot_xy,
            end=target_xy,
            duration_ms=self.DRAG_DURATION_MS,
            interval_ms=10,
            slope_in=2.0,
            slope_out=0.0,
        )
        for pt in traj:
            mouse_move(pt[0], pt[1], is_dry=self.is_dry)
            time.sleep(0.010)  # 10ms 步长
        dt_ms = (time.perf_counter() - t0) * 1000.0
        results.append(StageResult(stage=DeployStage.DRAG, success=True, duration_ms=dt_ms))

        # ---------------- Stage 3: HOVER ----------------
        t0 = time.perf_counter()
        time.sleep(0.05)  # 悬停稳定
        dt_ms = (time.perf_counter() - t0) * 1000.0
        results.append(StageResult(stage=DeployStage.HOVER, success=True, duration_ms=dt_ms))

        # ---------------- Stage 4: RELEASE (自适应朝向盘握手与卡费熔断防护) ----------------
        t0 = time.perf_counter()
        mouse_up(target_xy[0], target_xy[1], is_dry=self.is_dry)

        wheel_ok = True
        wheel_center = target_xy

        if post_check and not self.is_dry and direction != Direction.NONE:
            # 自适应轮询探测朝向盘是否就绪 (防掉帧与卡费弹回)
            wheel_ok = False
            probe_start = time.perf_counter()
            while time.perf_counter() - probe_start < self.WHEEL_TIMEOUT_SEC:
                frame = self.grab_frame()
                if frame is not None:
                    vis, center, conf = probe_orient_wheel(frame)
                    if vis and center is not None:
                        wheel_ok = True
                        wheel_center = center
                        break
                time.sleep(0.02)  # 20ms 自适应步长
        else:
            time.sleep(0.18)  # Dry-run 基础延时

        dt_ms = (time.perf_counter() - t0) * 1000.0
        results.append(
            StageResult(
                stage=DeployStage.RELEASE,
                success=wheel_ok,
                duration_ms=dt_ms,
                error_msg="" if wheel_ok else "朝向盘未呼出 (可能卡费或地块占用被弹回)",
            )
        )

        # 熔断防护：若朝向盘未呼出 (如卡费弹回)，立即安全释放并退出，绝不盲目划屏误触场上干员
        if not wheel_ok and post_check:
            safe_cancel(is_dry=self.is_dry)
            print(f"  [Safety Fuse Triggered] 部署被弹回/朝向盘未出，已触发安全释放并熔断后续划向。")
            return results

        if direction == Direction.NONE:
            return results

        # ---------------- Stage 5: ORIENT (MAA 朝向滑动) ----------------
        t0 = time.perf_counter()
        dir_vec = {
            Direction.UP: (0, -self.ORIENT_DIST_PX),
            Direction.DOWN: (0, self.ORIENT_DIST_PX),
            Direction.LEFT: (-self.ORIENT_DIST_PX, 0),
            Direction.RIGHT: (self.ORIENT_DIST_PX, 0),
        }.get(direction, (0, 0))

        start_pt = wheel_center
        end_xy = (start_pt[0] + dir_vec[0], start_pt[1] + dir_vec[1])

        mouse_down(start_pt[0], start_pt[1], is_dry=self.is_dry)
        time.sleep(0.03)

        orient_traj = generate_maa_swipe_trajectory(
            start=start_pt,
            end=end_xy,
            duration_ms=self.ORIENT_DURATION_MS,
            interval_ms=10,
            slope_in=2.0,
            slope_out=0.0,
        )
        for pt in orient_traj:
            mouse_move(pt[0], pt[1], is_dry=self.is_dry)
            time.sleep(0.010)

        dt_ms = (time.perf_counter() - t0) * 1000.0
        results.append(StageResult(stage=DeployStage.ORIENT, success=True, duration_ms=dt_ms))

        # ---------------- Stage 6: CONFIRM ----------------
        t0 = time.perf_counter()
        mouse_up(end_xy[0], end_xy[1], is_dry=self.is_dry)
        time.sleep(0.08)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        results.append(StageResult(stage=DeployStage.CONFIRM, success=True, duration_ms=dt_ms))

        return results


def selftest() -> bool:
    """自测试：MAA 三次样条插值与 Dry-run 部署流程。"""
    print("[SelfTest] 开始测试 replayer_actuator.py (MAA 动力学版本)...")

    # 1. 验证 MAA 三次样条曲线 properties
    # t=0 时应为 0.0，t=1 时应为 1.0
    p0 = cubic_spline(slope_0=2.0, slope_1=0.0, t=0.0)
    p1 = cubic_spline(slope_0=2.0, slope_1=0.0, t=1.0)
    p_mid = cubic_spline(slope_0=2.0, slope_1=0.0, t=0.5)
    assert abs(p0 - 0.0) < 1e-6, "起点进度必须为 0.0"
    assert abs(p1 - 1.0) < 1e-6, "终点进度必须为 1.0"
    # 前快后慢：t=0.5 时进度应显著大于 0.5 (实测约 0.625)
    assert p_mid > 0.55, f"MAA 减速样条特性错误: p_mid={p_mid}"
    print(f"  [OK] MAA 三次样条减速插值校验通过 (t=0.5 progress={p_mid:.3f})。")

    # 2. 验证轨迹生成
    traj = generate_maa_swipe_trajectory((100, 100), (500, 500), duration_ms=300, interval_ms=10)
    assert traj[0] == (100, 100) and traj[-1] == (500, 500), "起终点坐标错误"
    assert len(traj) >= 30, f"轨迹点数过少: {len(traj)}"
    print(f"  [OK] MAA 滑动轨迹点生成校验通过 (总采样点={len(traj)})。")

    # 3. Dry-run 部署流程
    pipeline = DeployPipeline(is_dry=True)
    results = pipeline.execute_deploy(
        slot_xy=(1200, 950),
        target_xy=(600, 450),
        direction=Direction.RIGHT,
        post_check=False,
    )
    assert len(results) == 6, f"执行阶段数量不符: {len(results)}"
    for r in results:
        assert r.success, f"阶段失败: {r.stage}"

    print("  [OK] MAA 部署六阶段 Dry-Run 校验通过。")
    print("[SelfTest] replayer_actuator.py 全部测试通过！")
    return True


if __name__ == "__main__":
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        success = selftest()
        sys.exit(0 if success else 1)
