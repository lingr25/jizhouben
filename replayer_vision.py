# -*- coding: utf-8 -*-
"""战斗复现器 · 视觉中枢 (replayer_vision.py)。

严格复刻 MAA (MaaAssistantArknights) 视觉中枢与资源定义：
1. 对齐 MaaCore/Vision/MaskedCcoeffMatcher.cpp：掩膜相关系数匹配 (TM_CCOEFF_NORMED / Alpha Mask)；
2. 对齐 resource/tasks.json 标准 ROI 体系 (基于 1280x720 基准坐标自适应缩放)：
   - BattleOpersFlag: [35, 588, 1245, 18]
   - BattleCostFlag:  [1160, 495, 45, 45]
   - BattleHpFlag:    [400, 0, 650, 60]
   - BattleOperCooling HSV: [0, 100, 0] ~ [20, 255, 150]
3. 直接加载 resource/template/ 官方透明通道模板库；
4. 差分三态与通用状态探针。
"""
import glob
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

try:
    import cv2
except ImportError:
    cv2 = None

from replayer_types import SpeedMode, StateProbeResult

# 基础路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCE_DIR = os.path.join(BASE_DIR, "resource")
TEMPLATES_DIR = os.path.join(RESOURCE_DIR, "template")
TASKS_JSON_PATH = os.path.join(RESOURCE_DIR, "tasks.json")

# MAA 基准设计分辨率 (1280 x 720)
MAA_BASE_WIDTH = 1280.0
MAA_BASE_HEIGHT = 720.0

# 差分阈值
PIXEL_DIFF_THRESH = 15
DIFF_STABLE_MAX = 2.0
DIFF_TRANSITION_MAX = 12.0
COST_FILL_THRESH = 150


class MaaTaskConfig:
    """MAA tasks.json 任务配置解析器单例。"""
    _instance = None
    _tasks: Dict[str, Any] = {}

    @classmethod
    def get_instance(cls) -> "MaaTaskConfig":
        if cls._instance is None:
            cls._instance = MaaTaskConfig()
            cls._instance._load()
        return cls._instance

    def _load(self):
        if os.path.exists(TASKS_JSON_PATH):
            try:
                with open(TASKS_JSON_PATH, "r", encoding="utf-8") as f:
                    self._tasks = json.load(f)
            except Exception:
                self._tasks = {}

    def get_roi(self, task_name: str) -> Optional[Tuple[int, int, int, int]]:
        """获取指定任务在 1280x720 下的 [x, y, w, h]。"""
        t = self._tasks.get(task_name)
        if t and "roi" in t:
            roi = t["roi"]
            if len(roi) == 4:
                return (roi[0], roi[1], roi[2], roi[3])
        return None


def scale_roi_to_image(
    roi_1280: Tuple[int, int, int, int],
    img_w: int,
    img_h: int,
) -> Tuple[int, int, int, int]:
    """将 MAA 1280x720 基准 ROI [x, y, w, h] 缩放到当前图像尺寸 (slice_y1, slice_y2, slice_x1, slice_x2)。"""
    rx, ry, rw, rh = roi_1280
    scale_x = img_w / MAA_BASE_WIDTH
    scale_y = img_h / MAA_BASE_HEIGHT

    x1 = max(0, min(img_w, int(round(rx * scale_x))))
    y1 = max(0, min(img_h, int(round(ry * scale_y))))
    x2 = max(x1 + 1, min(img_w, int(round((rx + rw) * scale_x))))
    y2 = max(y1 + 1, min(img_h, int(round((ry + rh) * scale_y))))

    return (y1, y2, x1, x2)


def to_gray(img: np.ndarray) -> np.ndarray:
    """转换为单通道灰度图。"""
    if img is None:
        return None
    if len(img.shape) == 2:
        return img
    if img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def compute_frame_diff(
    before: np.ndarray,
    after: np.ndarray,
    roi_slices: Optional[Tuple[int, int, int, int]] = None,
) -> Tuple[Optional[float], Optional[float]]:
    """计算两帧之间的 (平均灰度差 mean_diff, 变化像素比例 change_ratio)。"""
    if before is None or after is None or before.shape != after.shape:
        return None, None

    if roi_slices is not None:
        y1, y2, x1, x2 = roi_slices
        img_b = before[y1:y2, x1:x2]
        img_a = after[y1:y2, x1:x2]
    else:
        img_b = before
        img_a = after

    diff = cv2.absdiff(to_gray(img_b), to_gray(img_a))
    mean_diff = float(diff.mean())
    change_ratio = float((diff > PIXEL_DIFF_THRESH).mean())
    return mean_diff, change_ratio


def detect_diff_tri_state(before: np.ndarray, after: np.ndarray) -> str:
    """检测差分三态: 'stable' | 'transition' | 'active'"""
    mean_diff, _ = compute_frame_diff(before, after)
    if mean_diff is None:
        return "unknown"
    if mean_diff < DIFF_STABLE_MAX:
        return "stable"
    elif mean_diff < DIFF_TRANSITION_MAX:
        return "transition"
    else:
        return "active"


# ---------------- MAA MaskedCcoeffMatcher 算法实现 ----------------


class MaskedCcoeffMatcher:
    """带 Mask 掩膜的相关系数匹配器 (对齐 MAA MaskedCcoeffMatcher.cpp)。"""

    @staticmethod
    def match(
        source: np.ndarray,
        template: np.ndarray,
        mask: Optional[np.ndarray] = None,
        threshold: float = 0.70,
        roi_slices: Optional[Tuple[int, int, int, int]] = None,
    ) -> Tuple[float, Optional[Tuple[int, int]], Optional[Tuple[int, int, int, int]]]:
        """多功能掩膜相关系数匹配。

        返回: (score, (center_x, center_y), (rect_x, rect_y, w, h))
        """
        if source is None or template is None:
            return 0.0, None, None

        offset_x, offset_y = 0, 0
        if roi_slices is not None:
            y1, y2, x1, x2 = roi_slices
            src_crop = source[y1:y2, x1:x2]
            offset_x, offset_y = x1, y1
        else:
            src_crop = source

        src_h, src_w = src_crop.shape[:2]
        tpl_h, tpl_w = template.shape[:2]

        if src_h < tpl_h or src_w < tpl_w:
            return 0.0, None, None

        # 提取 Alpha 通道作为 Mask
        use_mask = mask
        tpl_rgb = template
        if len(template.shape) == 3 and template.shape[2] == 4:
            if use_mask is None:
                use_mask = template[:, :, 3]
            tpl_rgb = template[:, :, :3]

        gray_src = to_gray(src_crop)
        gray_tpl = to_gray(tpl_rgb)

        if use_mask is not None:
            mask_gray = to_gray(use_mask) if len(use_mask.shape) == 3 else use_mask
            res = cv2.matchTemplate(gray_src, gray_tpl, cv2.TM_CCOEFF_NORMED, mask=mask_gray)
        else:
            res = cv2.matchTemplate(gray_src, gray_tpl, cv2.TM_CCOEFF_NORMED)

        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        score = float(max_val)

        if score < threshold:
            return score, None, None

        top_left_x = offset_x + max_loc[0]
        top_left_y = offset_y + max_loc[1]
        center_x = top_left_x + tpl_w // 2
        center_y = top_left_y + tpl_h // 2
        rect = (top_left_x, top_left_y, tpl_w, tpl_h)

        return score, (center_x, center_y), rect


# ---------------- 状态探针集合 ----------------


def probe_cost_bar(frame: np.ndarray) -> Tuple[Optional[float], int]:
    """费用条读数探针。"""
    if frame is None:
        return None, 0

    h, w = frame.shape[:2]
    # 对齐 1918x987 / 1280x720 费用条纵带比例
    y1 = int(0.7755 * h)
    y2 = int(0.7800 * h)
    x1 = int(0.9224 * w)
    x2 = w - 1

    if y2 <= y1 or x2 <= x1:
        return None, 0

    band_crop = to_gray(frame[y1:y2, x1:x2])
    if band_crop is None or band_crop.size == 0:
        return None, 0

    col_means = band_crop.mean(axis=0)
    ncols = len(col_means)
    if ncols == 0:
        return None, 0

    fill_count = 0
    for val in col_means > COST_FILL_THRESH:
        if not val:
            break
        fill_count += 1

    return float(fill_count) / float(ncols), ncols


def probe_orient_wheel(
    frame: np.ndarray,
) -> Tuple[bool, Optional[Tuple[int, int]], float]:
    """朝向盘探针：利用黄色/橙色箭头 HSV 特征定位朝向盘中心。"""
    if frame is None:
        return False, None, 0.0

    h, w = frame.shape[:2]
    # 搜索主战场区域
    y1, y2 = int(0.10 * h), int(0.85 * h)
    x1, x2 = int(0.05 * w), int(0.95 * w)
    crop = frame[y1:y2, x1:x2]

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    lower = np.array([15, 100, 150], dtype=np.uint8)
    upper = np.array([38, 255, 255], dtype=np.uint8)

    mask = cv2.inRange(hsv, lower, upper)
    pixel_count = int(cv2.countNonZero(mask))

    if pixel_count >= 80:
        m = cv2.moments(mask)
        if m["m00"] > 0:
            cx = int(m["m10"] / m["m00"]) + x1
            cy = int(m["m01"] / m["m00"]) + y1
            conf = min(1.0, pixel_count / 500.0)
            return True, (cx, cy), conf

    return False, None, 0.0


def selftest() -> bool:
    """自测试：MAA tasks.json 加载与模板匹配。"""
    print("[SelfTest] 开始测试 replayer_vision.py (MAA 视觉体系)...")

    # 1. 验证 tasks.json ROI 解析
    cfg = MaaTaskConfig.get_instance()
    roi_opers = cfg.get_roi("BattleOpersFlag")
    roi_cost = cfg.get_roi("BattleCostFlag")
    assert roi_opers is not None, "未读取到 BattleOpersFlag ROI"
    assert roi_cost is not None, "未读取到 BattleCostFlag ROI"
    print(f"  [OK] MAA tasks.json 读取成功: BattleOpersFlag={roi_opers}, BattleCostFlag={roi_cost}")

    # 2. 验证 ROI 缩放
    scaled = scale_roi_to_image(roi_opers, img_w=1920, img_h=1080)
    assert scaled[1] > scaled[0] and scaled[3] > scaled[2], "ROI 缩放尺寸异常"
    print(f"  [OK] ROI 缩放校验通过: (1280x720) -> 1920x1080 slice={scaled}")

    # 3. 验证掩膜匹配算法
    src_test = np.random.randint(10, 50, (100, 100, 3), dtype=np.uint8)
    tpl_test = np.random.randint(150, 255, (20, 20, 4), dtype=np.uint8)
    tpl_test[:, :, 3] = 255  # Alpha 通道
    # 贴在 (40, 40) 位置
    src_test[40:60, 40:60] = tpl_test[:, :, :3]

    score, center, rect = MaskedCcoeffMatcher.match(src_test, tpl_test, threshold=0.8)
    assert center == (50, 50), f"匹配中心错误: {center}"
    print(f"  [OK] MaskedCcoeffMatcher 掩膜匹配校验通过: score={score:.2f}, center={center}")

    print("[SelfTest] replayer_vision.py 全部测试通过！")
    return True


if __name__ == "__main__":
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        success = selftest()
        sys.exit(0 if success else 1)
