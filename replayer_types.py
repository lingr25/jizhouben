# -*- coding: utf-8 -*-
"""战斗复现器数据类型与模型定义 (replayer_types.py)。

定义复现器全局通用的数据结构、枚举、状态探针返回结构与执行阶段契约。
纯标准库，无第三方重依赖。
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class OpType(str, Enum):
    DEPLOY = "deploy"      # 部署干员/召唤物
    SKILL = "skill"        # 点击干员开技能
    RETREAT = "retreat"    # 撤退干员
    PAUSE = "pause"        # 暂停/解除暂停
    SPEED = "speed"        # 切换倍速 (1x / 2x)
    NOTE = "note"          # 纯提醒/旁白


class Direction(str, Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    NONE = "none"

    @classmethod
    def from_str(cls, s: Optional[str]) -> "Direction":
        if not s:
            return cls.NONE
        s_lower = str(s).strip().lower()
        for member in cls:
            if member.value == s_lower:
                return member
        return cls.NONE


@dataclass
class NormalizedOp:
    """标准化的单步操作指令。完全兼容旧版 axis jsonl 与 reminders.json。"""
    frame: int                          # 触发帧号 (f)
    seq: int = 0                        # 操作序列号 (用于撤销链判定与同帧次序)
    op_type: OpType = OpType.DEPLOY     # 操作类型
    col: int = -1                       # 目标地块列 (0-indexed)
    row: int = -1                       # 目标地块行 (0-indexed)
    slot: Optional[int] = None          # 手牌卡槽位置 (1-indexed, 从左到右)
    direction: Direction = Direction.NONE  # 部署朝向
    cost_fill: Optional[int] = None     # 费用条填充列数 (对表纠偏)
    cost_ncols: Optional[int] = None    # 费用条总列数
    shot_name: Optional[str] = None     # 录制时的截图名 (诊断对齐)
    note: str = ""                      # 备注信息
    raw: Dict[str, Any] = field(default_factory=dict)  # 原始事件快照

    @property
    def cost_ratio(self) -> Optional[float]:
        if self.cost_fill is not None and self.cost_ncols and self.cost_ncols > 0:
            return float(self.cost_fill) / float(self.cost_ncols)
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "f": self.frame,
            "seq": self.seq,
            "op": self.op_type.value,
            "col": self.col,
            "row": self.row,
            "slot": self.slot,
            "dir": self.direction.value if self.direction != Direction.NONE else None,
            "fill": self.cost_fill,
            "ncols": self.cost_ncols,
            "shot": self.shot_name,
            "note": self.note,
        }


@dataclass
class AxisMetadata:
    """轴文件的元数据头信息。"""
    level_id: str = ""
    level_w: int = 0
    level_h: int = 0
    frame_keys: Dict[str, float] = field(default_factory=dict)  # 过帧键映射: {'y': 16.0, ...}
    clock: str = "ruler"
    wall_time: str = ""
    raw_header: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AxisData:
    """完整轴文件解析对象。"""
    metadata: AxisMetadata
    ops: List[NormalizedOp] = field(default_factory=list)
    anchors: List[Dict[str, Any]] = field(default_factory=list)
    raw_events: List[Dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"=== 轴元数据: level={self.metadata.level_id or 'unknown'} "
            f"({self.metadata.level_w}x{self.metadata.level_h}) "
            f"ops={len(self.ops)} anchors={len(self.anchors)} clock={self.metadata.clock} ==="
        ]
        for o in self.ops:
            dir_str = f"dir={o.direction.value}" if o.direction != Direction.NONE else "dir=-"
            slot_str = f"slot={o.slot}" if o.slot is not None else "slot=-"
            coord_str = f"({o.col},{o.row})" if o.col >= 0 else "(-,-)"
            lines.append(
                f"  f={o.frame:<6d} seq={o.seq:<3d} {o.op_type.value:<8s} {coord_str:<8s} {slot_str:<8s} {dir_str:<10s} fill={o.cost_fill}"
            )
        return "\n".join(lines)


class SpeedMode(str, Enum):
    SPEED_1X = "1x"
    SPEED_2X = "2x"
    UNKNOWN = "unknown"


@dataclass
class StateProbeResult:
    """状态探针 (VisionHub) 返回结果。"""
    is_paused: Optional[bool] = None        # 是否处于暂停状态
    speed_mode: SpeedMode = SpeedMode.UNKNOWN
    orient_wheel_visible: bool = False      # 朝向盘是否已展开
    orient_wheel_center: Optional[Tuple[int, int]] = None  # 朝向盘中心像素坐标 (px, py)
    battle_ended: bool = False             # 是否进入结算/失败页面
    battle_won: Optional[bool] = None      # True=通关, False=失败, None=未结束
    operator_ready: Optional[bool] = None  # 指定卡槽/干员是否可用
    confidence: float = 0.0                # 探针综合置信度 (0.0 ~ 1.0)
    details: Dict[str, Any] = field(default_factory=dict)


class DeployStage(str, Enum):
    """部署六阶段枚举。"""
    PICK = "pick"          # 阶段1: 按下手牌区卡片
    DRAG = "drag"          # 阶段2: 平滑拖拽至目标地块
    HOVER = "hover"        # 阶段3: 目标地块悬停与高亮校验
    RELEASE = "release"    # 阶段4: 释放鼠标并确认朝向盘弹起
    ORIENT = "orient"      # 阶段5: 滑动选择朝向
    CONFIRM = "confirm"    # 阶段6: 释放确认干员下场与费用扣除


@dataclass
class StageResult:
    """执行器单阶段执行结果与诊断。"""
    stage: DeployStage
    success: bool
    duration_ms: float = 0.0
    error_msg: str = ""
    diff_mean: Optional[float] = None
    diff_ratio: Optional[float] = None
    snapshot_path: Optional[str] = None
