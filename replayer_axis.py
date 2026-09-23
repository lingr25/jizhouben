# -*- coding: utf-8 -*-
"""战斗复现器 · 轴解析与标准化模块 (replayer_axis.py)。

职责：
1. 完整兼容 legacy `axis/*.jsonl` 格式；
2. 完整兼容 `calib/reminders.json` 格式；
3. 准确剔除 op_undo 撤销链并按 (frame, seq) 严格排序；
4. 导出为强类型的 NormalizedOp / AxisData 结构，供执行引擎调用。
"""
import argparse
import glob
import json
import os
import sys
from typing import Any, Dict, List, Optional

from replayer_types import (
    AxisData,
    AxisMetadata,
    Direction,
    NormalizedOp,
    OpType,
)


def parse_op_type(raw_op_str: Optional[str], note_str: str = "") -> OpType:
    """根据字符串推断操作类型。"""
    if raw_op_str:
        s = str(raw_op_str).strip().lower()
        if s in ("deploy", "dep"):
            return OpType.DEPLOY
        if s in ("skill", "sk"):
            return OpType.SKILL
        if s in ("retreat", "ret"):
            return OpType.RETREAT
        if s in ("pause",):
            return OpType.PAUSE
        if s in ("speed",):
            return OpType.SPEED

    # 从 note 中提取
    if note_str:
        n = note_str.strip()
        if n.startswith("部署") or "下" in n:
            return OpType.DEPLOY
        if n.startswith("技能") or "开技能" in n or "技能" in n:
            return OpType.SKILL
        if n.startswith("撤退") or "撤" in n:
            return OpType.RETREAT
        if n.startswith("暂停") or "停" in n:
            return OpType.PAUSE

    return OpType.NOTE


def load_jsonl_axis(path: str) -> AxisData:
    """解析 legacy 轴文件 (.jsonl) 为 AxisData。

    容忍末行不完整截断（针对强制关闭程序的场景）。
    """
    events: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if not line_str:
                continue
            try:
                events.append(json.loads(line_str))
            except ValueError:
                # 忽略残损行
                continue

    # 提取 Header
    header_evt = next((e for e in events if e.get("type") == "header"), None)
    if not header_evt:
        metadata = AxisMetadata(level_id="unknown")
    else:
        lvl = header_evt.get("level") or {}
        metadata = AxisMetadata(
            level_id=lvl.get("id", ""),
            level_w=lvl.get("w", 0),
            level_h=lvl.get("h", 0),
            frame_keys=header_evt.get("frame_keys") or {},
            clock=header_evt.get("clock", "ruler"),
            wall_time=header_evt.get("wall", ""),
            raw_header=header_evt,
        )

    # 处理撤销链
    undone_seqs = {e.get("seq") for e in events if e.get("type") == "op_undo" and e.get("seq") is not None}

    raw_ops = [
        e for e in events
        if e.get("type") == "op" and e.get("seq") not in undone_seqs
    ]

    normalized_ops: List[NormalizedOp] = []
    for e in raw_ops:
        frame = int(e.get("f", 0))
        seq = int(e.get("seq", 0))
        op_type = parse_op_type(e.get("op"), e.get("note", ""))
        col = int(e.get("col", -1)) if e.get("col") is not None else -1
        row = int(e.get("row", -1)) if e.get("row") is not None else -1
        slot = int(e.get("slot")) if e.get("slot") is not None else None
        direction = Direction.from_str(e.get("dir"))
        fill = int(e.get("fill")) if e.get("fill") is not None else None
        ncols = int(e.get("ncols")) if e.get("ncols") is not None else None
        shot = e.get("shot")
        note = e.get("note", "")

        normalized_ops.append(
            NormalizedOp(
                frame=frame,
                seq=seq,
                op_type=op_type,
                col=col,
                row=row,
                slot=slot,
                direction=direction,
                cost_fill=fill,
                cost_ncols=ncols,
                shot_name=shot,
                note=note,
                raw=e,
            )
        )

    # 严格按 (frame, seq) 排序
    normalized_ops.sort(key=lambda o: (o.frame, o.seq))

    anchors = [e for e in events if e.get("type") == "anchor"]

    return AxisData(
        metadata=metadata,
        ops=normalized_ops,
        anchors=anchors,
        raw_events=events,
    )


def load_reminders_json(path: str) -> AxisData:
    """解析 calib/reminders.json 为 AxisData。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        data = []

    metadata = AxisMetadata(level_id="reminder_store", clock="ruler")
    normalized_ops: List[NormalizedOp] = []

    for idx, item in enumerate(data):
        frame = int(item.get("frame", 0))
        note = str(item.get("note", ""))
        state = str(item.get("state", "pending"))
        if state == "archived":
            continue

        op_type = parse_op_type(None, note)
        normalized_ops.append(
            NormalizedOp(
                frame=frame,
                seq=idx + 1,
                op_type=op_type,
                note=note,
                raw=item,
            )
        )

    normalized_ops.sort(key=lambda o: (o.frame, o.seq))

    return AxisData(
        metadata=metadata,
        ops=normalized_ops,
        anchors=[],
        raw_events=data,
    )


def load_any_axis(path: str) -> AxisData:
    """根据文件后缀自动选择加载方式。"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"轴文件不存在: {path}")

    if path.endswith(".jsonl"):
        return load_jsonl_axis(path)
    elif path.endswith(".json"):
        return load_reminders_json(path)
    else:
        # 尝试按 jsonl 兜底
        return load_jsonl_axis(path)


def selftest() -> bool:
    """自测试：查找 legacy_timer_reviver/axis/ 下的所有轴并验证解析。"""
    print("[SelfTest] 开始测试 replayer_axis.py 轴文件解析...")
    patterns = [
        os.path.join("legacy_timer_reviver", "axis", "*.jsonl"),
        os.path.join("calib", "reminders.json"),
    ]
    files_tested = 0
    ops_total = 0

    for pat in patterns:
        matched = glob.glob(pat)
        for p in matched:
            try:
                axis = load_any_axis(p)
                files_tested += 1
                ops_total += len(axis.ops)
                print(f"  [OK] {os.path.basename(p):<30s} -> ops={len(axis.ops):<3d} level={axis.metadata.level_id or 'none'}")
            except Exception as ex:
                print(f"  [FAIL] {p}: {ex}")
                return False

    print(f"[SelfTest] 全部通过！共解析 {files_tested} 个文件，{ops_total} 条操作记录。")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="战斗复现器 · 轴解析工具")
    parser.add_argument("path", nargs="?", help="要解析的轴文件路径 (.jsonl / .json)")
    parser.add_argument("--selftest", action="store_true", help="运行自测试")
    args = parser.parse_args()

    if args.selftest or not args.path:
        success = selftest()
        sys.exit(0 if success else 1)
    else:
        ax = load_any_axis(args.path)
        print(ax.summary())
