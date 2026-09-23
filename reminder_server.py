# -*- coding: utf-8 -*-
"""记轴本 reminder · HTTP 服务（独立工具，前端的数据后端）。

依赖纪律：标准库 + reminder_core.py + ruler_client.py，无第三方库，
与打轴工具家族（reviver_*）零 import，打包三件套即可。

端口 127.0.0.1:2607（避开 BarRuler 的 2606）。前端轮询 /api/state。

接口一览：
  GET    /api/state             引擎状态+当前帧数+事件流
  GET    /api/reminders         提醒列表（含各状态）
  POST   /api/reminders         新增 {"frame":int,"note":str}
                                （引擎在跑则同步插入盯梢队列，响应带 injected）
  DELETE /api/reminders/<id>    删除
  POST   /api/engine/start      武装全部 pending 提醒并开始盯梢
  POST   /api/engine/abort      紧急停止（不注入任何后续键）
  POST   /api/engine/reset      已到/错过全部重置回 pending（重开一局用）
  POST   /api/shutdown          安全关闭：停引擎→关AFA→关尺子→退出本服务（终端随之关闭）
  GET    /api/presets           预制输入（留言模板+帧数增量）
  POST   /api/presets           覆盖保存预制输入
  GET    /                      reminder_web\\index.html（前端未就位时占位页）

用法：
    python reminder_server.py [--port 2607] [--no-browser]
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import reminder_core as core
from ruler_client import RulerClient, probe, ensure_ruler_running
from replayer_types import AxisData, NormalizedOp, OpType
from replayer_axis import load_any_axis
from replayer_engine import ReplayEngine
from replayer_record import RobustAxisRecorder
from replayer_actuator import key_pulse

WEB_DIR = "reminder_web"

PLACEHOLDER_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>记轴本</title>
<style>
 body{background:#111827;color:#e5e7eb;font-family:"Microsoft YaHei",sans-serif;
      display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
 .card{background:#1f2937;padding:32px 48px;border-radius:12px;text-align:center}
 h1{margin:0 0 12px;font-size:22px}
 p{color:#9ca3af;margin:4px 0}
 code{color:#60a5fa}
</style></head>
<body><div class="card">
<h1>记轴本 · 后端已就绪</h1>
<p>前端页面还没做，接口可用：</p>
<p><code>GET /api/state</code> · <code>GET /api/reminders</code></p>
</div></body></html>
"""

# ---- 全局单例 ----
RULER = RulerClient(start=True)   # 常驻缓存尺子快照；尺子没开时读数为 None
ENGINE = None
ENGINE_LOCK = threading.Lock()
STARTING = False                  # 正在武装中：拦截连点，避免重复启动
SRV = None                        # main() 里的服务器实例；安全关闭用
SHUTTING_DOWN = False             # 安全关闭在途：拦二次点击

# ---- 录轴器 (Recorder) 全局状态 ----
RECORDER_INSTANCE: Optional[RobustAxisRecorder] = None
RECORDER_LOCK = threading.Lock()

# ---- 复现器 (Replayer) 全局状态 ----
REPLAYER_ENGINE: Optional[ReplayEngine] = None
REPLAYER_THREAD: Optional[threading.Thread] = None
REPLAYER_LOCK = threading.Lock()
REPLAYER_STATE = {
    "status": "idle",       # idle | running | done | error | stopped
    "axis_path": "",
    "level_id": "",
    "current_op_index": -1,
    "total_ops": 0,
    "current_frame": 0,
    "target_frame": 0,
    "current_op": None,
    "residual_logs": [],
    "message": "",
}


# ---- 复现器 (Replayer) 业务逻辑 ----
def _list_axes() -> List[Dict[str, Any]]:
    results = []
    patterns = [
        ("axis", os.path.join("axis", "*.jsonl")),
        ("legacy", os.path.join("legacy_timer_reviver", "axis", "*.jsonl")),
        ("store", os.path.join("calib", "reminders.json")),
    ]
    seen_paths = set()
    for cat, pat in patterns:
        for p in glob.glob(pat):
            norm_p = os.path.normpath(p)
            if norm_p in seen_paths:
                continue
            seen_paths.add(norm_p)
            try:
                ax = load_any_axis(norm_p)
                ops_cnt = len(ax.ops)
                if ops_cnt == 0:
                    continue  # 自动过滤 0 条操作的空轴
                mtime = os.path.getmtime(norm_p)
                results.append({
                    "path": norm_p,
                    "filename": os.path.basename(norm_p),
                    "category": cat,
                    "level_id": ax.metadata.level_id or "unknown",
                    "ops_count": ops_cnt,
                    "mtime": mtime,
                    "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                })
            except Exception:
                continue
    results.sort(key=lambda x: x["mtime"], reverse=True)
    return results


def _run_replayer_worker(axis_path: str, speed: int, from_frame: int, is_dry: bool):
    global REPLAYER_STATE, REPLAYER_ENGINE
    try:
        axis_obj = load_any_axis(axis_path)
        engine = ReplayEngine(
            axis_data=axis_obj,
            is_dry=is_dry,
            speed=speed,
        )
        with REPLAYER_LOCK:
            REPLAYER_ENGINE = engine
            REPLAYER_STATE["status"] = "running"
            REPLAYER_STATE["axis_path"] = axis_path
            REPLAYER_STATE["level_id"] = axis_obj.metadata.level_id
            REPLAYER_STATE["total_ops"] = len(axis_obj.ops)
            REPLAYER_STATE["message"] = "复现运行中..."
            REPLAYER_STATE["residual_logs"] = []

        success = engine.run(from_frame=from_frame)
        with REPLAYER_LOCK:
            REPLAYER_STATE["status"] = "done" if success else "error"
            REPLAYER_STATE["residual_logs"] = engine.residual_logs
            REPLAYER_STATE["events"] = list(engine.events)
            REPLAYER_STATE["message"] = "复现完成！" if success else "复现存在失败步骤"
    except Exception as ex:
        with REPLAYER_LOCK:
            REPLAYER_STATE["status"] = "error"
            REPLAYER_STATE["message"] = f"复现异常终止: {ex}"
    finally:
        with REPLAYER_LOCK:
            REPLAYER_ENGINE = None


def _start_replayer(axis_path: str, speed: int = 1, from_frame: int = 0, is_dry: bool = False):
    global REPLAYER_THREAD, REPLAYER_STATE
    with REPLAYER_LOCK:
        if REPLAYER_STATE["status"] == "running":
            return 409, "复现器已在运行中"
        REPLAYER_STATE["events"] = []

    if not os.path.exists(axis_path):
        return 404, f"轴文件不存在: {axis_path}"

    t = threading.Thread(
        target=_run_replayer_worker,
        args=(axis_path, speed, from_frame, is_dry),
        daemon=True,
    )
    REPLAYER_THREAD = t
    t.start()
    return 200, "复现任务已启动"


def _stop_replayer():
    global REPLAYER_ENGINE, REPLAYER_STATE
    with REPLAYER_LOCK:
        if REPLAYER_STATE["status"] != "running":
            return 200, "复现器未在运行"
        REPLAYER_STATE["status"] = "stopped"
        REPLAYER_STATE["message"] = "已手动紧急停止"
    return 200, "已发送停止指令"


def _api_replayer_state() -> Dict[str, Any]:
    with REPLAYER_LOCK:
        st = dict(REPLAYER_STATE)
        st["frames"] = RULER.frames()
        st["ruler_online"] = RULER.online()
        st["is_admin"] = core.is_admin()
        if REPLAYER_ENGINE:
            st["events"] = list(REPLAYER_ENGINE.events)
        else:
            st["events"] = st.get("events", [])
        return st


def _on_state(rem, outcome):
    """引擎每条提醒跑完的回调：落盘状态。"""
    mapping = {"done": "done", "missed": "missed"}
    if outcome in mapping:
        core.set_reminder_state(rem["id"], mapping[outcome])


def _api_state():
    snap = ENGINE.snapshot() if ENGINE else \
        {"stage": "idle", "cur": None, "armed": None, "remaining": None,
         "alive": False, "waiting_next": False, "events": []}
    return {
        "stage": snap["stage"],
        "engine_alive": snap["alive"],
        "frames": RULER.frames(),
        "last_frames": snap["cur"],
        "armed": snap["armed"],
        "remaining": snap["remaining"],
        "waiting_next": snap["waiting_next"],
        "ruler_online": RULER.online(),
        "is_admin": core.is_admin(),
        "events": snap["events"],
    }


def _ensure_ruler_ready(timeout=40.0):
    """尺子接口就绪 -> True。没开就拉起；进程在但接口没就绪
    （多半是 _autostart 正在拉）就等它就绪。原来“进程在却连不上”
    直接报错，会撞上 autostart 的拉起过程：首次点开始必 503，
    表现成“只拉起不盯梢，要点第二次”（实机踩过）。"""
    if probe(timeout=0.6) is not None:
        return True
    if not _list_proc_names("ruler"):
        ensure_ruler_running(interactive=False, wait_timeout=30)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if probe(timeout=0.6) is not None:
            return True
        time.sleep(0.5)
    return False


def _try_start_engine():
    """武装全部 pending 提醒。返回 (http码, 消息)。
    长等待（探尺子、等快照）期间不占 ENGINE_LOCK，
    用 STARTING 标志拦连点：第二次点击立刻 409，不排队不重入。"""
    global ENGINE, STARTING
    with ENGINE_LOCK:
        if STARTING:
            return 409, "正在启动，请稍候"
        if ENGINE and ENGINE.alive():
            return 409, "引擎已在盯梢，先停止再重新开始"
        STARTING = True
    try:
        rems = core.pending_sorted()
        if not core.is_admin():
            return 403, "未以管理员运行：没法替你按键，请用 记轴本.bat 启动"
        if not _ensure_ruler_ready():
            return 503, ("BarRuler 没起来：手动开好尺子再点开始"
                         "（若尺子进程在却连不上，先在托盘退出尺子重开）")
        t0 = time.time()
        while time.time() - t0 < 8 and RULER.snapshot() is None:
            time.sleep(0.2)
        if RULER.snapshot() is None:
            return 503, "连不上尺子接口（127.0.0.1:2606）"
        eng = core.ReminderEngine(RULER.frames_live, core.load_cfg(),
                                  on_state=_on_state, exit_when_empty=False,
                                  snap_ts_fn=RULER.snap_ts,
                                  freeze_frames_fn=RULER.frames_fresh)
        eng.start(rems)
        with ENGINE_LOCK:
            ENGINE = eng
        return 200, "盯梢中，已挂上 %d 条提醒（运行中可直接加）" % len(rems)
    finally:
        with ENGINE_LOCK:
            STARTING = False


# ---------------- 安全关闭 ----------------
def _list_proc_names(keyword, exclude=()):
    """枚举 exe 名含关键字的进程名集合（认 tasklist 第一列，不含路径）。
    tasklist 不带 timeout：高负载下可能跑好几秒，
    超时异常被吃掉会误报“没在跑”（实机测出）。"""
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                         capture_output=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    names = set()
    for line in out.stdout.decode("gbk", "ignore").splitlines():
        parts = line.split('"')
        if len(parts) < 2:
            continue
        name = parts[1].lower()
        if name.endswith(".exe") and keyword in name \
                and not any(x in name for x in exclude):
            names.add(parts[1])
    return names


def _kill_procs(keyword, exclude=()):
    """按 exe 名包含关键字杀进程，两阶段防复活：杀一轮等半秒再查残余补杀
    （AFA 是启动器+脚本双进程，互拉复活，实机测出）。
    /IM 一次杀光同名全部进程。非管理员杀不了提权进程（如实计 0），
    记轴本实机本身提权，无此问题。exclude 兜底排除（如 python）。"""
    nf = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    n = 0
    for round_no in (1, 2):
        try:
            names = _list_proc_names(keyword, exclude)
        except Exception:
            break
        if not names:
            break
        for name in names:
            r = subprocess.run(["taskkill", "/F", "/IM", name],
                               capture_output=True, timeout=10,
                               creationflags=nf)
            if r.returncode == 0:
                n += 1
        if round_no == 1:
            time.sleep(0.5)     # 等复活窗口：启动器要拉子进程会在这半秒内动手
    return n


def _shutdown_all():
    """安全关闭：停引擎（先断键注入）→ 关尺子，逐项收集结果。
    返回人话消息列表。"""
    global SHUTTING_DOWN
    with ENGINE_LOCK:
        if SHUTTING_DOWN:
            return None      # 已在关闭中
        SHUTTING_DOWN = True
    msgs = []
    with ENGINE_LOCK:
        eng = ENGINE
    if eng and eng.alive():
        eng.abort()
        msgs.append("盯梢已停止")
    n_ruler = _kill_procs("ruler")
    msgs.append("尺子已关闭" if n_ruler else "尺子本来就没在跑")
    return msgs


class Handler(BaseHTTPRequestHandler):
    server_version = "ReminderServer/1.0"

    def handle_one_request(self):
        # 前端 150ms 轮询，刷新/关页会中断在途请求：
        # 客户端断开是日常，不当异常刷 traceback。
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    # ---- 工具 ----
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return None

    def log_message(self, fmt, *args):
        pass    # 前端 100ms 轮询，别刷屏控制台

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            return self._json(200, _api_state())
        if path == "/api/reminders":
            with core._store_lock:
                rems = core.load_reminders()
            rems.sort(key=lambda r: r["frame"])
            return self._json(200, {"reminders": rems})
        if path == "/api/presets":
            return self._json(200, core.load_cfg()["presets"])
        if path == "/api/config":
            cfg = core.load_cfg()
            return self._json(200, {
                "opening_trigger": bool(cfg.get("opening_trigger", True)),
                "unpause_key": cfg.get("unpause_key", "esc"),
                "pause_key": cfg.get("pause_key", "space"),
                "direct_step": cfg.get("direct_step", True),
                "hotkeys": cfg.get("hotkeys", core.DEFAULT_HOTKEYS),
                "pre_pause_lead": cfg.get("pre_pause_lead", 8),
                "stop_margin": cfg.get("stop_margin", 2),
            })
        if path == "/api/replayer/axes":
            return self._json(200, {"axes": _list_axes()})
        if path == "/api/replayer/state":
            return self._json(200, _api_replayer_state())
        if path == "/api/recorder/state":
            return self._json(200, _get_recorder_state())
        if path == "/api/stages/search":
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            import urllib.parse
            params = urllib.parse.parse_qs(query)
            q = params.get("q", [""])[0]
            from replayer_mapper import MaaTileMapper
            mapper = MaaTileMapper()
            results = mapper.search_levels(q, limit=20)
            return self._json(200, {"results": results})
        if path == "/api/replayer/detail":
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            import urllib.parse
            params = urllib.parse.parse_qs(query)
            axis_p = params.get("path", [""])[0]
            if not axis_p or not os.path.exists(axis_p):
                return self._json(404, {"error": "轴文件不存在"})
            try:
                ax = load_any_axis(axis_p)
                return self._json(200, {
                    "level_id": ax.metadata.level_id,
                    "clock": ax.metadata.clock,
                    "ops": [o.to_dict() for o in ax.ops],
                })
            except Exception as e:
                return self._json(500, {"error": str(e)})
        if path == "/" or path == "/index.html":
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        return self._json(404, {"error": "not found"})

    def _static(self, rel):
        rel = rel.replace("\\", "/")
        if rel == "index.html":
            index = os.path.join(WEB_DIR, "index.html")
            if not os.path.isfile(index):
                body = PLACEHOLDER_HTML.encode("utf-8")
            else:
                with open(index, "rb") as f:
                    body = f.read()
            ctype = "text/html; charset=utf-8"
        else:
            fp = os.path.normpath(os.path.join(WEB_DIR, rel))
            if not fp.startswith(os.path.normpath(WEB_DIR)) \
                    or not os.path.isfile(fp):
                return self._json(404, {"error": "not found"})
            with open(fp, "rb") as f:
                body = f.read()
            ctype = {"js": "text/javascript", "css": "text/css",
                     "png": "image/png", "svg": "image/svg+xml",
                     "ico": "image/x-icon"}.get(
                         fp.rsplit(".", 1)[-1].lower(),
                         "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")   # 前端迭代频繁，禁浏览器缓存
        self.end_headers()
        self.wfile.write(body)

    # ---- POST ----
    def do_POST(self):
        try:
            self._handle_post()
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e), "message": "服务器异常：%s" % e})

    def _handle_post(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/reminders":
            data = self._body_json()
            if data is None:
                return self._json(400, {"error": "JSON 解析失败"})
            try:
                frame = int(data.get("frame"))
            except (TypeError, ValueError):
                return self._json(400, {"error": "frame 必须是整数"})
            if frame < 0 or frame > 10 ** 7:
                return self._json(400, {"error": "frame 超出合理范围"})
            note = str(data.get("note", "")).strip()[:200]
            entry = core.add_reminder(frame, note)
            # 引擎在跑（实操态）就同步插入队列；watch 阶段会自动盯上它
            injected = False
            with ENGINE_LOCK:
                if ENGINE and ENGINE.alive():
                    ENGINE.add(entry)
                    injected = True
            return self._json(200, {"ok": True, "reminder": entry,
                                    "injected": injected})
        if path == "/api/engine/start":
            code, msg = _try_start_engine()
            return self._json(code, {"ok": code == 200, "message": msg})
        if path == "/api/engine/abort":
            if ENGINE:
                ENGINE.abort()
            return self._json(200, {"ok": True, "message": "已发送停止"})
        if path == "/api/engine/reset":
            n = core.reset_reminder_states()
            readded = 0
            with ENGINE_LOCK:
                if ENGINE:
                    ENGINE.clear_events()
                    if ENGINE.alive():   # 运行中重置：补回盯梢队列
                        readded = ENGINE.add_missing(core.pending_sorted())
            if n:
                msg = "已重置 %d 条提醒与日志" % n
                if readded:
                    msg += "，补回 %d 条到盯梢队列" % readded
            else:
                msg = "已清空日志并重置状态"
            return self._json(200, {"ok": True, "message": msg})
        if path == "/api/shutdown":
            msgs = _shutdown_all()
            if msgs is None:
                return self._json(200, {"ok": True, "message": "正在关闭中"})
            print("安全关闭：%s" % "；".join(msgs))

            def _later():
                time.sleep(0.5)
                try:
                    if SRV:
                        SRV.server_close()
                except Exception:
                    pass
                os._exit(0)  # 彻底终止当前进程，自动关闭终端窗口

            threading.Thread(target=_later, daemon=True).start()
            return self._json(200, {"ok": True,
                                    "message": "全部已关闭：" + "、".join(msgs)})
        if path == "/api/presets":
            data = self._body_json()
            if not isinstance(data, dict):
                return self._json(400, {"error": "JSON 解析失败"})
            cfg = core.load_cfg()
            presets = cfg["presets"]
            if isinstance(data.get("notes"), list):
                presets["notes"] = [str(s)[:50] for s in data["notes"]][:20]
            if isinstance(data.get("frame_deltas"), list):
                try:
                    presets["frame_deltas"] = \
                        [int(d) for d in data["frame_deltas"]][:10]
                except (TypeError, ValueError):
                    return self._json(400, {"error": "frame_deltas 必须是整数"})
        if path == "/api/config":
            data = self._body_json()
            if not isinstance(data, dict):
                return self._json(400, {"error": "JSON 解析失败"})
            cfg = core.load_cfg()
            if "opening_trigger" in data:
                cfg["opening_trigger"] = bool(data["opening_trigger"])
            if "unpause_key" in data and isinstance(data["unpause_key"], str):
                cfg["unpause_key"] = data["unpause_key"].strip().lower()
            if "pause_key" in data and isinstance(data["pause_key"], str):
                p_key = data["pause_key"].strip().lower()
                cfg["pause_key"] = p_key
                if "opening_pause_key" not in data:
                    cfg["opening_pause_key"] = "esc"
                if "pause_fallback_key" not in data:
                    cfg["pause_fallback_key"] = "esc"
            if "direct_step" in data:
                cfg["direct_step"] = bool(data["direct_step"])
            if "hotkeys" in data and isinstance(data["hotkeys"], dict):
                cfg["hotkeys"] = cfg.get("hotkeys") or dict(core.DEFAULT_HOTKEYS)
                for k, v in data["hotkeys"].items():
                    if isinstance(v, str):
                        cfg["hotkeys"][k] = v.strip().lower()
            core.save_cfg(cfg)
            core.NativeHotkeyHub.get_instance().update_cfg(cfg)
            with ENGINE_LOCK:
                if ENGINE:
                    ENGINE.cfg = dict(cfg)
            return self._json(200, {"ok": True, "config": cfg})
        if path == "/api/action/step":
            data = self._body_json() or {}
            try:
                ms = float(data.get("ms", 33.0))
            except (TypeError, ValueError):
                return self._json(400, {"error": "ms 必须是数字"})
            cfg = core.load_cfg()
            ok, msg = core.direct_step(ms, unpause_key=cfg.get("unpause_key", "esc"),
                                       pause_key=cfg.get("pause_key", "space"))
            return self._json(200 if ok else 500, {"ok": ok, "message": msg})
        if path == "/api/action/select":
            data = self._body_json() or {}
            x = data.get("x")
            y = data.get("y")
            ok, msg = core.touch_pause_select(client_x=x, client_y=y)
            return self._json(200 if ok else 500, {"ok": ok, "message": msg})
        if path == "/api/replayer/start":
            data = self._body_json() or {}
            axis_path = data.get("path")
            if not axis_path:
                return self._json(400, {"error": "缺少 path 参数"})
            speed = int(data.get("speed", 1))
            from_frame = int(data.get("from_frame", 0))
            is_dry = bool(data.get("dry", False))
            code, msg = _start_replayer(axis_path, speed=speed, from_frame=from_frame, is_dry=is_dry)
            return self._json(code, {"ok": code == 200, "message": msg})
        if path == "/api/replayer/stop":
            code, msg = _stop_replayer()
            return self._json(code, {"ok": code == 200, "message": msg})
        if path == "/api/recorder/start":
            data = self._body_json() or {}
            level_id = data.get("level_id", "obt/main/level_main_01-07")
            code, msg = _start_recorder(level_id=level_id)
            return self._json(code, {"ok": code == 200, "message": msg})
        if path == "/api/recorder/stop":
            code, msg, path_out, count = _stop_recorder()
            return self._json(code, {"ok": code == 200, "message": msg, "axis_path": path_out, "count": count})
        return self._json(404, {"error": "not found"})

def _start_recorder(level_id: str = "obt/main/level_main_01-07") -> Tuple[int, str]:
    """启动实机全自动录轴 (联动开局自动暂停守护)。"""
    global RECORDER_INSTANCE
    with RECORDER_LOCK:
        if RECORDER_INSTANCE and RECORDER_INSTANCE.is_recording:
            return 400, "录轴器已在运行中"
        RECORDER_INSTANCE = RobustAxisRecorder(level_id=level_id)
        RECORDER_INSTANCE.start()

        # 联动开局自动暂停守护 (高稳定性开战侦测：等待进关加载 -> 侦测倍速按钮/0帧 -> 切窗注入暂停 -> 补发防吞)
        def _watch_opening_pause(rec_inst):
            cfg = core.load_cfg()
            if not cfg.get("opening_trigger", True):
                rec_inst.status_log = "开局自动暂停已关闭，请自由操作"
                return
            pause_key = cfg.get("opening_pause_key") or cfg.get("pause_key") or "esc"
            pause_key = str(pause_key).strip().lower()

            def _log(msg):
                rec_inst.status_log = msg
                print(f"  [RecorderGuard] {msg}")

            _log(f"开局守护已就绪 (暂停键=[{pause_key}])，等待进入关卡...")

            eye = None
            try:
                eye = core._TriggerEye()
            except Exception:
                pass

            # 阶段 1：等待黑屏过渡或 Loading... 出现（最多等 60 秒）
            _log("开局守护已就绪，等待进关卡（请进入关卡开始行动）...")
            t_deadline = time.perf_counter() + 60.0
            saw_loading = False
            while rec_inst.is_recording and time.perf_counter() < t_deadline:
                if eye and (eye.black() or eye.loading() == 1):
                    saw_loading = True
                    _log("检测到关卡加载中，准备开局定格...")
                    break
                time.sleep(0.05)

            if not rec_inst.is_recording or not saw_loading:
                return

            # 阶段 2：严格等待右上角倍速按钮渲染出来（最多等 20 秒，绝不在黑屏期提前抢跑发键）
            _log("等待倍速按钮渲染...")
            t_deadline = time.perf_counter() + 20.0
            saw_btn = False
            while rec_inst.is_recording and time.perf_counter() < t_deadline:
                if eye and eye.speed_button():
                    saw_btn = True
                    break
                time.sleep(0.01)

            if not rec_inst.is_recording or not saw_btn:
                _log("[!] 等待倍速按钮超时，手势录制已就绪")
                return

            # 阶段 3：倍速按钮出现瞬间 -> 先切前台，再注入游戏原生暂停键
            core._focus_game()
            _log(f"倍速按钮已渲染！正在注入开局暂停 [{pause_key}]...")
            core.inject_key(pause_key, hold=0.03)

            # 检验是否真正定格（若战斗淡入期吞键导致仍未停，补发同一个暂停键）
            time.sleep(0.3)
            f_chk1 = RULER.frames()
            time.sleep(0.15)
            f_chk2 = RULER.frames()
            if f_chk1 is not None and f_chk2 is not None and f_chk2 > f_chk1:
                _log(f"首发暂停未停住（仍在跑动），补发暂停 [{pause_key}]...")
                core.inject_key(pause_key, hold=0.03)
                time.sleep(0.15)

            final_f = RULER.frames()
            _log(f"开局触发命中：已暂停（第 {final_f or 0} 帧），请开始战术操作！")

            if not triggered and rec_inst.is_recording:
                _log("等待开局超时或已在战斗中，手势录制已就绪")

        threading.Thread(target=_watch_opening_pause, args=(RECORDER_INSTANCE,), daemon=True).start()
        return 200, "录轴器已启动 (开局自动暂停已激活)"


def _stop_recorder() -> Tuple[int, str, Optional[str], int]:
    """停止实机录轴并返回生成的轴文件。"""
    global RECORDER_INSTANCE
    with RECORDER_LOCK:
        if not RECORDER_INSTANCE or not RECORDER_INSTANCE.is_recording:
            return 400, "录轴器未在运行", None, 0
        path = RECORDER_INSTANCE.stop()
        count = len(RECORDER_INSTANCE.recorded_ops)
        return 200, "录轴已完成", path, count


def _get_recorder_state() -> Dict[str, Any]:
    """获取当前录轴状态。"""
    with RECORDER_LOCK:
        if not RECORDER_INSTANCE or not RECORDER_INSTANCE.is_recording:
            return {
                "is_recording": False,
                "level_id": "",
                "axis_path": "",
                "recorded_ops_count": 0,
                "recorded_ops": [],
            }
        return {
            "is_recording": True,
            "level_id": RECORDER_INSTANCE.level_id,
            "axis_path": RECORDER_INSTANCE.axis_file_path,
            "recorded_ops_count": len(RECORDER_INSTANCE.recorded_ops),
            "recorded_ops": list(RECORDER_INSTANCE.recorded_ops[-20:]),
        }

    # ---- DELETE ----
    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/reminders/"):
            rid = path.rsplit("/", 1)[-1]
            ok = core.remove_reminder(rid)
            with ENGINE_LOCK:
                if ENGINE and ENGINE.alive():
                    ENGINE.remove(rid)
            return self._json(200 if ok else 404,
                              {"ok": ok,
                               "error": None if ok else "没有这条提醒"})
        return self._json(404, {"error": "not found"})


class _Srv(ThreadingHTTPServer):
    # 禁用端口复用：旧服务还占着 2607 时新服务必须启动失败报错，
    # 而不是两个服务同绑一个端口、请求被随机分给新旧两边（症状极难查）
    allow_reuse_address = False


def _autostart():
    """服务启动默认行为：检查并拉起 BarRuler 尺子进程，启动原生热键监听。
    不自动开始盯梢（等待用户在网页控制台点击【开始盯梢】）。"""
    if probe(timeout=0.6) is None:
        print("  正在检查并拉起 BarRuler 尺子...")
        if ensure_ruler_running(interactive=False, wait_timeout=30):
            print("  BarRuler 已就绪。")
        else:
            print("  [提示] 未能自动拉起 BarRuler（可手动启动尺子后点网页上的开始盯梢）。")
    else:
        print("  BarRuler 接口已在线。")

    if not core.is_admin():
        print("  [提示] 未以管理员权限运行，按键注入未启用。")
        return
    core.NativeHotkeyHub.get_instance().start()
    print("  原生热键监听器已就绪。")
    print("  [就绪] 请在网页端设置提醒并点击【开始盯梢】。")


def main():
    global SRV
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=2607)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--open-browser", action="store_true")
    args, _ = ap.parse_known_args()

    core.load_cfg()      # 首跑生成 calib\\reminder.json
    n_reset = core.reset_reminder_states()   # 启动自动重置：已到/错过的提醒全部重置回待触发
    if n_reset:
        print("启动自动重置：%d 条历史提醒已恢复为待触发" % n_reset)
    try:
        srv = _Srv(("127.0.0.1", args.port), Handler)
        SRV = srv
    except OSError:
        print("[!] 端口 %d 已被占用：上一次的记轴本服务还没关。" % args.port)
        print("    关掉旧窗口（或任务管理器结束旧的 python reminder_server.py）再试。")
        sys.exit(1)
    url = "http://127.0.0.1:%d/" % args.port
    print("=" * 55)
    print("  记轴本服务已启动！")
    print("  网页控制台: %s" % url)
    print("  正在自动唤起默认浏览器，请稍候...")
    print("=" * 55)
    if not core.is_admin():
        print("[!] 当前不是管理员：能看状态/记提醒，但盯梢不会自动开始（没法替你按键）。")
    threading.Thread(target=_autostart, daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.1, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
