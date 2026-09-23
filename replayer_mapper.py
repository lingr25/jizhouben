# -*- coding: utf-8 -*-
"""战斗复现器 · 官方地块与摄像机透视映射器 (replayer_mapper.py)。

严格复刻 MAA (MaaAssistantArknights) 地图与 3D 摄像机投影体系：
1. 直接加载 resource/map/ (Arknights-Tile-Pos) 官方全关卡地图数据；
2. 读取官方真实的 view[0] (平视 [cx, cy, cz]) 与 view[1] (抓卡斜视 [cx, cy, cz])；
3. 读取格子的真实 heightType (0=低台, 1=高台附加 z=-0.4) 与 buildableType；
4. 实现双向映射：
   - 正投影：(col, row) -> 屏幕像素 (px, py)
   - 逆投影自校准：屏幕像素 (px, py) -> 自动吸附到最近的有效网格 (col, row) (用于实机录轴)。
"""
import glob
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

# 基础路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCE_DIR = os.path.join(BASE_DIR, "resource")
MAP_DIR = os.path.join(RESOURCE_DIR, "map")
SLOTS_PATH = os.path.join(BASE_DIR, "calib", "slots.json")

# MAA 官方 3D 摄像机物理模型常量
DEFAULT_FOV = 20.0
DEFAULT_X_DEG = 30.0
DEFAULT_Y_DEG = 10.0
DEFAULT_ASPECT_FACTOR = 9.0 / 16.0
DEFAULT_NEAR = 0.3
DEFAULT_FAR = 1000.0
HIGH_TILE_Z = -0.4    # 高台相对高度


def build_maa_mvp_matrix(
    camera_pos: Tuple[float, float, float],
    is_tilt: bool = False,
) -> np.ndarray:
    """构建与 MAA 官方一致的标准 MVP 投影变换矩阵。"""
    cx, cy, cz = camera_pos
    deg_to_rad = math.pi / 180.0
    fov_rad = DEFAULT_FOV * deg_to_rad
    x_deg_rad = DEFAULT_X_DEG * deg_to_rad
    y_deg_rad = DEFAULT_Y_DEG * deg_to_rad

    # 平移变换 Transform
    t_mat = np.array([
        [1.0, 0.0, 0.0, -cx],
        [0.0, 1.0, 0.0, -cy],
        [0.0, 0.0, 1.0, -cz],
        [0.0, 0.0, 0.0, 1.0],
    ], dtype=float)

    # 透视投影矩阵 Perspective
    p_mat = np.array([
        [DEFAULT_ASPECT_FACTOR / math.tan(fov_rad), 0.0, 0.0, 0.0],
        [0.0, 1.0 / math.tan(fov_rad), 0.0, 0.0],
        [0.0, 0.0, -(DEFAULT_FAR + DEFAULT_NEAR) / (DEFAULT_FAR - DEFAULT_NEAR),
         -(2.0 * DEFAULT_FAR * DEFAULT_NEAR) / (DEFAULT_FAR - DEFAULT_NEAR)],
        [0.0, 0.0, -1.0, 0.0],
    ], dtype=float)

    # 俯视 X 轴旋转 (30度)
    rx_mat = np.array([
        [1.0, 0.0, 0.0, 0.0],
        [0.0, math.cos(x_deg_rad), -math.sin(x_deg_rad), 0.0],
        [0.0, -math.sin(x_deg_rad), -math.cos(x_deg_rad), 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ], dtype=float)

    # 斜视 Y 轴旋转 (10度，抓卡部署状态)
    if is_tilt:
        ry_mat = np.array([
            [math.cos(y_deg_rad), 0.0, math.sin(y_deg_rad), 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [-math.sin(y_deg_rad), 0.0, math.cos(y_deg_rad), 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ], dtype=float)
    else:
        ry_mat = np.eye(4, dtype=float)

    return p_mat @ rx_mat @ ry_mat @ t_mat


class MaaTileMapper:
    """MAA 官方地块与摄像机映射器。"""
    _GLOBAL_SEARCH_INDEX: Optional[List[Dict[str, str]]] = None

    def __init__(self, map_dir: str = MAP_DIR):
        self.map_dir = map_dir
        self._map_cache: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def _ensure_search_index(cls, map_dir: str = MAP_DIR):
        """延迟加载并全局缓存全关卡搜索索引 (自动去重普通与突袭重复关卡)。"""
        if cls._GLOBAL_SEARCH_INDEX is not None:
            return
        index_list = []
        seen_keys = set()

        if os.path.exists(map_dir):
            # 排序：普通地图文件优先于突袭文件 (#f#)
            fnames = sorted(os.listdir(map_dir), key=lambda x: ("#f#" in x, x))
            for fname in fnames:
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(map_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    code = str(data.get("code", ""))
                    name = str(data.get("name", ""))
                    lvl_id = str(data.get("levelId", "") or data.get("stageId", ""))
                    stg_id = str(data.get("stageId", ""))

                    if not (lvl_id or code or name):
                        continue

                    # 唯一去重键：(代号, 关卡ID)
                    dedup_key = (code, lvl_id)
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)

                    index_list.append({
                        "code": code,
                        "name": name,
                        "level_id": lvl_id,
                        "stage_id": stg_id,
                        "display": f"[{code}] {name} ({lvl_id})" if name else f"[{code}] {lvl_id}",
                    })
                except Exception:
                    continue

        # 排序：主线 1-X, 7-X 等常用关卡靠前
        index_list.sort(key=lambda x: (
            not x["code"].startswith("1-"),
            not x["code"].startswith("7-"),
            not x["code"].startswith("main_"),
            len(x["code"])
        ))
        cls._GLOBAL_SEARCH_INDEX = index_list

    @classmethod
    def search_levels(cls, query: str = "", limit: int = 20, map_dir: Optional[str] = None) -> List[Dict[str, str]]:
        """根据关卡代号 (1-7, 7-16, H8-4) 或中文名 (暴君, 潮汐) 进行模糊搜索。"""
        cls._ensure_search_index(map_dir or MAP_DIR)
        if not cls._GLOBAL_SEARCH_INDEX:
            return []

        q = query.strip().lower()
        if not q:
            return cls._GLOBAL_SEARCH_INDEX[:limit]

        matches = []
        for item in cls._GLOBAL_SEARCH_INDEX:
            c = item["code"].lower()
            n = item["name"].lower()
            lvl = item["level_id"].lower()
            stg = item["stage_id"].lower()

            # 优先级打分
            score = 0
            if c == q:
                score = 100
            elif c.startswith(q):
                score = 80
            elif q in c:
                score = 60
            elif q in n:
                score = 50
            elif q in lvl or q in stg:
                score = 30

            if score > 0:
                matches.append((score, item))

        matches.sort(key=lambda x: x[0], reverse=True)
        return [m[1] for m in matches[:limit]]

    def _find_map_file(self, level_id: str) -> Optional[str]:
        """根据 level_id (如 'obt/main/level_main_01-07' 或 'main_01-07') 查找官方地图 json。"""
        if not level_id or not os.path.exists(self.map_dir):
            return None

        # 提取关键后缀
        key = level_id.replace("/", "-").replace("\\", "-")
        # 优先非 #f# 普通关，如果找不到再匹配 #f# 突袭关
        candidates = glob.glob(os.path.join(self.map_dir, f"*{key}*.json"))
        if not candidates:
            # 尝试直接按关卡名匹配
            short_id = level_id.split("/")[-1].replace("level_", "")
            candidates = glob.glob(os.path.join(self.map_dir, f"*{short_id}*.json"))

        if not candidates:
            return None

        candidates.sort(key=lambda p: ("#f#" in os.path.basename(p), len(p)))
        return candidates[0]

    def load_level_data(self, level_id: str) -> Optional[Dict[str, Any]]:
        """加载关卡官方地图元数据。"""
        if level_id in self._map_cache:
            return self._map_cache[level_id]

        p = self._find_map_file(level_id)
        if not p or not os.path.exists(p):
            return None

        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._map_cache[level_id] = data
            return data
        except Exception:
            return None

    def grid_to_norm_xy(
        self,
        level_id: str,
        col: int,
        row: int,
        is_tilt: bool = True,
        map_size: Optional[Tuple[int, int]] = None,
    ) -> Tuple[float, float]:
        """将 (col, row) 转换为归一化屏幕坐标 (nx, ny) (0.0~1.0)。"""
        map_data = self.load_level_data(level_id)
        if not map_data:
            w, h = map_size if map_size else (11, 7)
            return (0.15 + col * 0.065, 0.20 + row * 0.08)

        w = int(map_data.get("width", 11))
        h = int(map_data.get("height", 7))
        views = map_data.get("view", [[0.0, -4.81, -7.76], [0.6, -5.31, -8.64]])

        cam_pos = views[1 if is_tilt and len(views) > 1 else 0]
        matrix = build_maa_mvp_matrix(cam_pos, is_tilt=is_tilt)

        # 获取高度类型 (0=低台, 1=高台)
        height_type = 0
        tiles = map_data.get("tiles")
        inv_row = h - 1 - row
        if tiles and 0 <= inv_row < len(tiles) and 0 <= col < len(tiles[inv_row]):
            height_type = tiles[inv_row][col].get("heightType", 0)

        z = HIGH_TILE_Z if height_type == 1 else 0.0

        pt = np.array([
            col - (w - 1) / 2.0,
            inv_row - (h - 1) / 2.0,
            z,
            1.0,
        ], dtype=float)

        view_pt = matrix @ pt
        view_pt /= view_pt[3]

        nx = float((view_pt[0] + 1.0) / 2.0)
        ny = float(1.0 - (view_pt[1] + 1.0) / 2.0)
        return nx, ny

    def grid_to_pixel(
        self,
        level_id: str,
        col: int,
        row: int,
        win_rect: Tuple[int, int, int, int],  # (left, top, width, height)
        is_tilt: bool = True,
        map_size: Optional[Tuple[int, int]] = None,
    ) -> Tuple[int, int]:
        """将网格坐标转换为实机屏幕绝对像素 (px, py)。"""
        left, top, width, height = win_rect
        nx, ny = self.grid_to_norm_xy(level_id, col, row, is_tilt=is_tilt, map_size=map_size)
        px = int(round(left + nx * width))
        py = int(round(top + ny * height))
        return px, py

    def pixel_to_grid(
        self,
        level_id: str,
        px: int,
        py: int,
        win_rect: Tuple[int, int, int, int],
        is_tilt: bool = True,
    ) -> Optional[Tuple[int, int, float]]:
        """逆透视吸附：给定屏幕像素坐标，自动吸附到最近的有效网格。

        返回: (col, row, distance_error_px) 或 None
        """
        map_data = self.load_level_data(level_id)
        if not map_data:
            return None

        w = int(map_data.get("width", 11))
        h = int(map_data.get("height", 7))

        best_col, best_row = -1, -1
        min_dist_sq = float("inf")

        for r in range(h):
            for c in range(w):
                target_px, target_py = self.grid_to_pixel(
                    level_id, col=c, row=r, win_rect=win_rect, is_tilt=is_tilt
                )
                dist_sq = (target_px - px) ** 2 + (target_py - py) ** 2
                if dist_sq < min_dist_sq:
                    min_dist_sq = dist_sq
                    best_col, best_row = c, r

        if best_col >= 0 and best_row >= 0:
            return best_col, best_row, math.sqrt(min_dist_sq)
        return None

    def get_slot_pixel(
        self,
        slot_idx: int,
        win_rect: Tuple[int, int, int, int],
        total_slots: int = 12,
    ) -> Tuple[int, int]:
        """计算手牌区卡槽像素位置 (对齐 MAA 底部手牌卡槽分布)。"""
        left, top, width, height = win_rect
        # MAA 底部卡牌标准分布：X 轴 0.22 ~ 0.88，Y 轴 0.945
        x_start = 0.22
        x_end = 0.88
        y_pos = 0.945
        step = (x_end - x_start) / max(1, total_slots - 1)
        nx = x_start + (slot_idx - 1) * step
        ny = y_pos
        return int(round(left + nx * width)), int(round(top + ny * height))


# 兼容 TileMapper 别名
TileMapper = MaaTileMapper


def selftest() -> bool:
    """自测试：加载 MAA 官方关卡地图与双向透视投影。"""
    print("[SelfTest] 开始测试 replayer_mapper.py (MAA 官方地图与投影体系)...")
    mapper = MaaTileMapper()

    # 1. 验证 1-7 官方地图数据加载
    level_data = mapper.load_level_data("obt/main/level_main_01-07")
    assert level_data is not None, "未读取到 1-7 官方地图数据"
    assert "view" in level_data and "tiles" in level_data, "地图数据字段缺失"
    print(f"  [OK] 成功读取 1-7 官方地图: {level_data.get('name')} (尺寸={level_data.get('width')}x{level_data.get('height')})")

    # 2. 正投影测试
    nx, ny = mapper.grid_to_norm_xy("obt/main/level_main_01-07", col=1, row=3, is_tilt=True)
    assert 0.0 < nx < 1.0 and 0.0 < ny < 1.0, f"正投影坐标越界: ({nx}, {ny})"
    print(f"  [OK] MAA 官方 3D 摄像机正投影校验通过: (1,3) -> norm=({nx:.4f}, {ny:.4f})")

    # 3. 逆投影自动吸附测试
    win = (0, 0, 1920, 1080)
    target_px, target_py = mapper.grid_to_pixel("obt/main/level_main_01-07", col=2, row=4, win_rect=win)
    # 在目标点附近加 10px 扰动，测试是否能精准吸附回 (2, 4)
    adsorb_res = mapper.pixel_to_grid("obt/main/level_main_01-07", target_px + 8, target_py - 6, win_rect=win)
    assert adsorb_res is not None, "逆投影吸附失败"
    ac, ar, err_dist = adsorb_res
    assert (ac, ar) == (2, 4), f"逆投影吸附网格错误: ({ac}, {ar})"
    print(f"  [OK] 逆透视自动吸附校验通过: 扰动输入 -> 精准还原网格 (2,4) 误差={err_dist:.1f}px")

    print("[SelfTest] replayer_mapper.py 全部测试通过！")
    return True


if __name__ == "__main__":
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        success = selftest()
        sys.exit(0 if success else 1)
