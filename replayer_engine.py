# -*- coding: utf-8 -*-
"""战斗复现器 · 核心调度引擎 (replayer_engine.py)。

职责：
1. 以 BarRuler (ruler_client) 为基准时钟源，结合 AFA/原生微步实现帧级高精度推进；
2. 2x/1x 粗跑 + 预刹车 (Brake Lag 补偿) + 单帧 Creep 逼近；
3. 声明式 Op 执行流水线 (Deploy/Skill/Retreat/Pause)；
4. 异常回退、费用条对表纠偏与残差报告输出。
"""
import argparse
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from replayer_actuator import (
    DeployPipeline,
    execute_retreat_click,
    execute_skill_click,
    key_pulse,
    mouse_down,
    mouse_move,
    mouse_up,
    safe_cancel,
)
from replayer_axis import load_any_axis
from replayer_mapper import TileMapper
from replayer_types import AxisData, DeployStage, Direction, NormalizedOp, OpType, StageResult
from replayer_vision import compute_frame_diff, probe_cost_bar, probe_orient_wheel
from ruler_client import RulerClient, ensure_ruler_running, wait_frozen

# 调度与时序默认参数
LOGIC_FPS = 30
RUN_THRESHOLD = 18          # 距目标至少 18 帧才启动倍速粗跑
PRE_PAUSE_LEAD = 8          # 粗跑提前在目标前约 8 帧发起刹车
DEFAULT_BRAKE_LAG = 8       # 2x 速度下暂停键注入到游戏完全冻结的延迟预算(帧)
STEP_SETTLE_SEC = 0.06      # 过帧键按下后的防吞消化时间
STALL_MAX_RETRIES = 5       # 逼近过程中帧数连续未推进的最大重试次数


class ReplayEngine:
    """战斗复现主调度器。"""

    def __init__(
        self,
        axis_data: AxisData,
        win_rect: Tuple[int, int, int, int] = (0, 0, 1920, 1080),
        is_dry: bool = False,
        speed: int = 1,
        pause_key: str = "space",
        unpause_key: str = "space",
        pause_fallback_key: str = "esc",
        brake_lag: int = DEFAULT_BRAKE_LAG,
    ):
        self.axis = axis_data
        self.win_rect = win_rect
        self.is_dry = is_dry
        self.speed = speed
        self.pause_key = pause_key
        self.unpause_key = unpause_key
        self.pause_fallback_key = pause_fallback_key
        self.brake_lag = brake_lag

        self.mapper = TileMapper()
        self.actuator = DeployPipeline(is_dry=is_dry)
        self.ruler = RulerClient()
        self.residual_logs: List[Dict[str, Any]] = []
        self.events: List[str] = []

    def log(self, text: str):
        """记录一条结构化人话运行事件。"""
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {text}"
        self.events.append(line)
        print(line)

    def get_current_frame(self, fallback_frame: int = 0) -> int:
        """获取当前帧数。Dry 模式或尺子不可读时回退。"""
        if self.is_dry:
            return fallback_frame
        try:
            info = self.ruler.poll()
            if info and info.get("totalElapsedFrames") is not None:
                return int(info["totalElapsedFrames"])
        except Exception:
            pass
        return fallback_frame

    def step_forward_single_frame(self, step_key: str = "r") -> bool:
        """发送单次微步过帧键。"""
        if self.is_dry:
            return True
        key_pulse(step_key, hold_ms=30, is_dry=self.is_dry)
        time.sleep(STEP_SETTLE_SEC)
        return True

    def creep_to_frame(self, target_frame: int, current_frame: int) -> int:
        """使用微步过帧键精确逼近至目标帧。"""
        curr = current_frame
        stalls = 0

        while curr < target_frame:
            diff = target_frame - curr
            # 根据剩余帧数选择最优过帧档位
            if diff >= 5:
                step_key = "t"  # 166ms (~5帧)
            elif diff >= 2:
                step_key = "r"  # 33ms (~1帧)
            else:
                step_key = "y"  # 16ms (~0.5帧)

            if not self.is_dry:
                self.step_forward_single_frame(step_key)
                next_frame = self.get_current_frame(fallback_frame=curr + 1)
                if next_frame == curr:
                    stalls += 1
                    if stalls > STALL_MAX_RETRIES:
                        print(f"    [WARN] 过帧键连续 {stalls} 次未推进，可能游戏已卡死或失去焦点。")
                        break
                else:
                    stalls = 0
                curr = next_frame
            else:
                curr += 1

        return curr

    def advance_to_target_frame(self, target_frame: int, current_frame: int) -> int:
        """级联推进：粗跑 -> 提前刹车 -> 单帧 Creep 逼近。"""
        curr = current_frame
        delta = target_frame - curr

        if delta <= 0:
            return curr

        # 1. 粗跑阶段
        if delta > RUN_THRESHOLD and not self.is_dry:
            # 计算提前刹车点
            brake_frame = target_frame - PRE_PAUSE_LEAD - self.brake_lag
            print(f"  [Advance] 距离目标 {delta} 帧，启动倍速粗跑，计划在第 {brake_frame} 帧发起刹车...")

            # 解除暂停
            key_pulse(self.unpause_key, hold_ms=30, is_dry=self.is_dry)

            # 轮询等待到达刹车线
            while True:
                f_now = self.get_current_frame(fallback_frame=curr)
                if f_now >= brake_frame or f_now >= target_frame - 2:
                    break
                time.sleep(0.01)

            # 注入刹车
            key_pulse(self.pause_key, hold_ms=30, is_dry=self.is_dry)
            # 等待画面与时钟完全冻结
            wait_frozen(self.ruler, timeout=2.0)
            curr = self.get_current_frame(fallback_frame=target_frame - PRE_PAUSE_LEAD)
            print(f"  [Advance] 刹车冻结于第 {curr} 帧 (距目标差 {target_frame - curr} 帧)。")

        # 2. 精调逼近阶段
        if curr < target_frame:
            self.log(f"进入逐帧逼近: f={curr} -> f={target_frame} (差 {target_frame - curr} 帧)")
            curr = self.creep_to_frame(target_frame, curr)

        return curr

    def execute_single_op(self, op: NormalizedOp) -> bool:
        """执行单步标准化指令。"""
        level_id = self.axis.metadata.level_id
        map_size = (self.axis.metadata.level_w or 11, self.axis.metadata.level_h or 7)

        if op.op_type == OpType.DEPLOY:
            if op.slot is None or op.col < 0 or op.row < 0:
                self.log(f"f={op.frame} [FAIL] 部署参数不完整: slot={op.slot}, col={op.col}, row={op.row}")
                return False

            slot_px = self.mapper.get_slot_pixel(op.slot, self.win_rect)
            target_px = self.mapper.grid_to_pixel(
                level_id=level_id,
                col=op.col,
                row=op.row,
                win_rect=self.win_rect,
                is_tilt=True,
                map_size=map_size,
            )

            stages = self.actuator.execute_deploy(
                slot_xy=slot_px,
                target_xy=target_px,
                direction=op.direction,
                post_check=not self.is_dry,
            )
            success = all(s.success for s in stages)
            dur_str = " · ".join([f"{s.stage.value}:{s.duration_ms:.0f}ms" for s in stages])
            if success:
                self.log(f"f={op.frame} [DEPLOY] 成功部署卡槽{op.slot} 到 ({op.col},{op.row}) 朝向:{op.direction.value} ({dur_str})")
            else:
                self.log(f"f={op.frame} [FAIL] 部署卡槽{op.slot} 到 ({op.col},{op.row}) 失败 ({dur_str})")
            return success

        elif op.op_type == OpType.SKILL:
            if op.col < 0 or op.row < 0:
                self.log(f"f={op.frame} [FAIL] 技能目标地块无效: ({op.col},{op.row})")
                return False
            target_px = self.mapper.grid_to_pixel(
                level_id=level_id,
                col=op.col,
                row=op.row,
                win_rect=self.win_rect,
                is_tilt=False,
                map_size=map_size,
            )
            execute_skill_click(target_px, is_dry=self.is_dry)
            self.log(f"f={op.frame} [SKILL] 成功点击干员触发技能 ({op.col},{op.row})")
            return True

        elif op.op_type == OpType.RETREAT:
            if op.col < 0 or op.row < 0:
                self.log(f"f={op.frame} [FAIL] 撤退目标地块无效: ({op.col},{op.row})")
                return False
            target_px = self.mapper.grid_to_pixel(
                level_id=level_id,
                col=op.col,
                row=op.row,
                win_rect=self.win_rect,
                is_tilt=False,
                map_size=map_size,
            )
            execute_retreat_click(target_px, is_dry=self.is_dry)
            self.log(f"f={op.frame} [RETREAT] 成功撤退干员 ({op.col},{op.row})")
            return True

        elif op.op_type == OpType.NOTE:
            self.log(f"f={op.frame} [NOTE] 提示: {op.note}")
            return True

        return True

    def run(self, from_frame: int = 0) -> bool:
        """主回放执行入口。"""
        ops = [o for o in self.axis.ops if o.frame >= from_frame]
        mode_str = "仿真模拟 (Dry-Run)" if self.is_dry else "实机执行"
        self.log(f"战斗复现开始 | 关卡={self.axis.metadata.level_id or '通用'} | 待执行指令={len(ops)}条 ({mode_str})")

        current_frame = from_frame
        all_passed = True

        for idx, op in enumerate(ops):
            t_start = time.perf_counter()
            # 1. 推进到目标帧
            current_frame = self.advance_to_target_frame(op.frame, current_frame)
            frame_drift = current_frame - op.frame

            # 2. 执行动作
            success = self.execute_single_op(op)
            t_cost_ms = (time.perf_counter() - t_start) * 1000.0

            # 记录残差与质量日志
            self.residual_logs.append({
                "seq": op.seq,
                "op": op.op_type.value,
                "target_frame": op.frame,
                "actual_frame": current_frame,
                "frame_drift": frame_drift,
                "duration_ms": t_cost_ms,
                "success": success,
            })

            if not success:
                print(f"  [ERROR] 指令 #{op.seq} 执行失败！")
                all_passed = False
                # 安全兜底取消
                safe_cancel(is_dry=self.is_dry)

        print("\n=======================================================")
        print(f"战斗复现完成！总执行: {len(ops)} 条，结果: {'全部成功' if all_passed else '存在失败'}")
        print(f"=======================================================\n")
        return all_passed


def selftest() -> bool:
    """自测试：加载实际轴文件并运行 Dry-run 回放流水线。"""
    print("[SelfTest] 开始测试 replayer_engine.py 调度引擎...")
    sample_axis_path = os.path.join("legacy_timer_reviver", "axis", "20260802_155708.jsonl")
    if not os.path.exists(sample_axis_path):
        sample_axis_path = os.path.join("calib", "reminders.json")

    axis = load_any_axis(sample_axis_path)
    engine = ReplayEngine(
        axis_data=axis,
        win_rect=(0, 0, 1920, 1080),
        is_dry=True,
        speed=1,
    )
    ok = engine.run(from_frame=0)
    assert ok, "Dry-run 执行失败"
    assert len(engine.residual_logs) == len(axis.ops), "残差记录数量不匹配"
    print(f"  [OK] Dry-run 调度流水线校验通过 (共模拟执行 {len(engine.residual_logs)} 条指令)。")
    print("[SelfTest] replayer_engine.py 全部测试通过！")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="战斗复现器 · 主调度引擎")
    parser.add_argument("axis_path", nargs="?", help="要复现的轴文件路径 (.jsonl / .json)")
    parser.add_argument("--dry", action="store_true", help="只进行仿真模拟，不注入鼠标键盘操作")
    parser.add_argument("--from-frame", type=int, default=0, help="起始帧号 (默认 0)")
    parser.add_argument("--speed", type=int, default=1, choices=[1, 2], help="游戏倍速 (默认 1)")
    parser.add_argument("--selftest", action="store_true", help="运行自测试")
    args = parser.parse_args()

    if args.selftest or not args.axis_path:
        success = selftest()
        sys.exit(0 if success else 1)
    else:
        axis_obj = load_any_axis(args.axis_path)
        engine_inst = ReplayEngine(
            axis_data=axis_obj,
            is_dry=args.dry,
            speed=args.speed,
        )
        success = engine_inst.run(from_frame=args.from_frame)
        sys.exit(0 if success else 1)
