# -*- coding: utf-8 -*-
"""BarRuler 外接时钟客户端：打轴器的第二块表。

timer 与 BarRuler 是同一职能（局内帧钟）的两套实现，运行时择一，绝不混跑。
选 BarRuler 时，打轴器不再跑 TimerCore/画面识别，帧号全部来自本模块：
  · 尺子在本机 127.0.0.1:2606 起服务（WebSocket 推送 + HTTP 快照，apiVersion=2）；
  · totalElapsedFrames = 战斗内累计逻辑帧，正是轴要的 f，帧级精度，
    动态费率/负费/边界亚帧全由尺子自己解决。

RulerClient：后台线程维持 WebSocket 连接缓存最新快照，断线自动重连；
websockets 库缺失或连接失败时降级 HTTP 轮询（只读，send 不可用）。
frames() 在掉线 / isRunning=False / 数据过期 >2s 时返回 None，调用方据此
判"表不可读"（录轴回落旧值保持、回放安全停止或回落兜底）。

ensure_ruler_running()：探活 → 没开就自动启动尺子 exe → 等接口就绪。
exe 路径存 calib\\ruler.json；首跑按顺序找：
  ① reviver_for_barruler\\target\\release\\ 下的编译产物；
  ② 问用户输路径并记住。
"""
import ctypes
import glob
import json
import os
import socket
import subprocess
import threading
import time

HTTP_URL = "http://127.0.0.1:2606/"
WS_URL = "ws://127.0.0.1:2606/"
RULER_CFG = os.path.join("calib", "ruler.json")
STALE_SEC = 2.0          # 快照超过这么久没更新视为不可读（掉线/尺子卡死）

try:
    from websockets.sync.client import connect as _ws_connect
except Exception:
    _ws_connect = None   # 没装 websockets：降级 HTTP 轮询（只读）


def probe(timeout=1.0):
    """探活：拿到尺子当前快照返回 dict，服务不在返回 None。
    用裸 socket 发 HTTP：不走系统代理（代理软件常把 127.0.0.1 也劫走，
    urllib 会超时），也不用 urllib 的读体方式（尺子 Connection: close
    早关连接时 read() 会被 RST）。"""
    try:
        s = socket.create_connection(("127.0.0.1", 2606), timeout=timeout)
        try:
            s.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1:2606\r\n"
                      b"Connection: close\r\n\r\n")
            s.settimeout(timeout)
            buf = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    break
                except ConnectionResetError:
                    break    # 尺子发完就 RST：数据已收全，拿到手里的继续用
                if not chunk:
                    break
                buf += chunk
        finally:
            s.close()
        body = buf.split(b"\r\n\r\n", 1)[-1]
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


class RulerClient:
    """常驻后台的尺子快照缓存。线程安全，随取随用。"""

    def __init__(self, start=True):
        self._snap = None
        self._snap_ts = 0.0
        self._lock = threading.Lock()
        self._stop = False
        self._ws = None
        self._ws_lock = threading.Lock()   # send 与收包线程共用一条连接
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    # ---- 后台线程 ----
    def _run(self):
        while not self._stop:
            ok = False
            if _ws_connect is not None:
                try:
                    self._run_ws()
                    ok = True
                except Exception:
                    self._ws = None
            if self._stop:
                break
            if not ok:
                # WS 不可用/刚断：HTTP 单次轮询兜底，稍后还会再试 WS
                self._poll_http_once()
                time.sleep(0.05)

    def _run_ws(self):
        with _ws_connect(WS_URL) as ws:
            self._ws = ws
            while not self._stop:
                try:
                    # 尺子会主动推快照；长时间没动静（暂停冻结期可能不推）
                    # 就主动要一份，保证快照新鲜度
                    raw = ws.recv(timeout=0.5)
                    self._ingest(raw)
                except TimeoutError:
                    try:
                        with self._ws_lock:
                            ws.send(json.dumps({"type": "getSnapshot"}))
                    except Exception:
                        return          # 连接已坏，出圈重连
                except Exception:
                    return
        self._ws = None

    def _poll_http_once(self):
        s = probe(timeout=0.8)
        if s is not None:
            self._ingest_obj(s)

    def _ingest(self, raw):
        try:
            d = json.loads(raw if isinstance(raw, str)
                           else raw.decode("utf-8"))
        except Exception:
            return
        # 主动推送是顶层快照；请求响应是 typed envelope（payload 里装快照）
        if isinstance(d, dict) and d.get("type") == "snapshot":
            d = d.get("payload") or {}
        self._ingest_obj(d)

    def _ingest_obj(self, d):
        if not isinstance(d, dict) or \
                ("totalElapsedFrames" not in d and "isRunning" not in d):
            return
        with self._lock:
            self._snap = d
            self._snap_ts = time.time()

    # ---- 对外 ----
    def snapshot(self):
        """最近一份快照 dict（可能过期，调用方自己判时效）；没收到过返回 None。"""
        with self._lock:
            return dict(self._snap) if self._snap else None

    def snapshot_fresh(self):
        """快照 + 是否新鲜：(dict|None, bool)。"""
        with self._lock:
            if self._snap is None:
                return None, False
            return dict(self._snap), (time.time() - self._snap_ts) <= STALE_SEC

    def frames(self):
        """战斗内累计逻辑帧（totalElapsedFrames）；
        掉线 / 尺子没识别到费用条(isRunning=False) / 数据过期 → None。"""
        s, fresh = self.snapshot_fresh()
        if not fresh or s is None or not s.get("isRunning"):
            return None
        f = s.get("totalElapsedFrames")
        return f if isinstance(f, int) else None

    def frames_live(self, max_age=1.0):
        """frames() 加通道活性：快照到达超 max_age 秒就当不可读。
        引擎用这个口径：缓存旧值冒充活读数（把通道卡死看成游戏暂停）
        比短暂读不到危险得多；正常时请求/推送周期 ≤0.6s，1s 足够宽松。"""
        with self._lock:
            if self._snap is None or time.time() - self._snap_ts > max_age:
                return None
            if not self._snap.get("isRunning"):
                return None
            f = self._snap.get("totalElapsedFrames")
            return f if isinstance(f, int) else None

    def frames_fresh(self, max_age=1.0):
        """冻结检测用的宽松读数：快照 ≤max_age 秒到达 + 整帧数，
        不看 isRunning —— isRunning=False 恰是尺子给的「游戏已暂停」
        签名，此刻 frames_live 返回 None 会让冻结检测器变盲：
        真暂停被永远确认成「钟丢了」（实机踩过）。
        通道活性纪律不变：快照停止到达（通道死）照样 None。"""
        with self._lock:
            if self._snap is None or time.time() - self._snap_ts > max_age:
                return None
            f = self._snap.get("totalElapsedFrames")
            return f if isinstance(f, int) else None

    def snap_ts(self):
        """最近一份快照的到达时间戳（time.time 口径）；没收到过返回 0。"""
        with self._lock:
            return self._snap_ts

    def readable(self):
        """通道真可读吗（徽章用）：与 frames() 同一口径，
        避免『徽章亮着但引擎读不到』的两套标准。"""
        return self.frames() is not None

    def online(self, max_age=1.0):
        """尺子通道活着吗（徽章用）：最近 max_age 秒内有快照到达就算在线。
        不要求 isRunning：游戏不在关卡内时尺子照样推快照，
        那是游戏侧状态（当前帧显示 --、引擎日志提示），不是尺子坏了。
        通道真死（接口断/卡死不推）快照停到 >1s → 徽章灭（实机口径）。"""
        with self._lock:
            return self._snap is not None and \
                time.time() - self._snap_ts <= max_age

    def send(self, obj):
        """向尺子发控制命令（adjustTimer/setTimer 等，见 API.md）。
        仅 WS 通道可用；发成功返回 True。预留接口，当前链路只读。"""
        with self._ws_lock:
            if self._ws is None:
                return False
            try:
                self._ws.send(json.dumps(obj))
                return True
            except Exception:
                self._ws = None
                return False

    def close(self):
        self._stop = True


def wait_frozen(client, hold=0.12, timeout=3.0, frames_fn=None, ts_fn=None):
    """等帧数连续 hold 秒不增长（=确认暂停）。超时返回 False。
    frames_fn 可注入（自测用 mock），默认取 client.frames。
    ts_fn 给快照到达时间戳（RulerClient.snap_ts）：提供后冻结确认
    必须见到「值不变 + 新快照到达」——尺子活着并复核了冻结；
    通道断了（没有新快照）就确认不了，超时返回 False，
    避免把『通道卡死值不跳』误判成『游戏已暂停』（实机踩过）。"""
    fn = frames_fn or client.frames
    deadline = time.perf_counter() + timeout
    last = object()
    last_ts = object()
    stable_since = None
    while time.perf_counter() < deadline:
        f = fn()
        if f is None:
            return False            # 表不可读：确认不了暂停
        if f != last:
            last = f
            last_ts = ts_fn() if ts_fn else None   # 基线：此后 ts 变了才算新快照
            stable_since = time.perf_counter() if ts_fn is None else None
        elif ts_fn is None:
            if stable_since is not None and \
                    time.perf_counter() - stable_since >= hold:
                return True
        else:
            ts = ts_fn()
            if ts != last_ts:       # 值没变但到了新快照：活的冻结证据
                last_ts = ts
                if stable_since is None:
                    stable_since = time.perf_counter()
            if stable_since is not None and \
                    time.perf_counter() - stable_since >= hold:
                return True
        time.sleep(0.02)
    return False


# ---- 自动启动尺子 ----

def _load_cfg():
    try:
        with open(RULER_CFG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cfg(cfg):
    try:
        os.makedirs("calib", exist_ok=True)
        with open(RULER_CFG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _find_ruler_exe():
    """找尺子可执行文件：配置记住的路径 → 源码仓编译产物 → 官方发行包 → 同级/上级目录。
    找不到返回 None。"""
    cfg = _load_cfg()
    p = cfg.get("exe")
    if p and os.path.isfile(p):
        return p
    cands = []
    patterns = [
        os.path.join("reviver_for_barruler", "target", "release", "*.exe"),
        os.path.join("reviver_for_barruler", "target", "debug", "*.exe"),
        os.path.join("reviver_for_barruler", "dist", "**", "*.exe"),
        os.path.join("..", "reviver_for_barruler", "dist", "**", "*.exe"),
        os.path.join("..", "BarRuler", "**", "*.exe"),
        os.path.join("..", "ArknightsCostBarRuler", "**", "*.exe"),
        os.path.join("BarRuler", "**", "*.exe"),
        os.path.join("dist", "**", "*.exe"),
    ]
    for pat in patterns:
        cands.extend(glob.glob(pat, recursive=True))
    named = [p for p in cands if "ruler" in os.path.basename(p).lower()]
    hits = named or cands
    if hits:
        best = sorted(hits, key=os.path.getmtime, reverse=True)[0]
        cfg["exe"] = os.path.abspath(best)
        _save_cfg(cfg)
        return best
    return None


def _spawn_ruler(exe):
    """启动尺子：先普通起；报 WinError 740（要求管理员）就 UAC 提权重起。"""
    exe_dir = os.path.dirname(os.path.abspath(exe))
    try:
        subprocess.Popen([exe], cwd=exe_dir,
                         creationflags=(subprocess.DETACHED_PROCESS
                                        | subprocess.CREATE_NEW_PROCESS_GROUP))
        return True
    except OSError as e:
        if getattr(e, "winerror", None) != 740:
            print("  [!] 启动失败：%s" % e)
            return False
    # 需要管理员：ShellExecute runas 提权（会弹 UAC，点是即可；
    # 本程序自己已是管理员时不弹窗直接起）
    print("  尺子需要管理员权限，正在提权启动（弹 UAC 请点是）...")
    try:
        h = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, None, exe_dir, 1)
        if int(h) > 32:
            return True
        print("  [!] 提权启动失败（码 %d）：可能点了 UAC 的『否』。" % int(h))
    except Exception as e:
        print("  [!] 提权启动异常：%s" % e)
    return False


def _ruler_proc_alive():
    """查尺子进程在不在（只认名字带 ruler 的 exe）。
    用于防双开：进程在但端口不通时是卡死/启动中，
    再 spawn 一个只会两个实例抢窗口/端口，更乱。
    tasklist 不带 timeout：高负载下超时异常被吃掉会误判“不在”，
    防双开失效（实机踩过 tasklist 跑超 5 秒）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in out.stdout.decode("gbk", "ignore").splitlines():
            low = line.lower()
            if "ruler" in low and ".exe" in low:
                return True
    except Exception:
        pass
    return False


def ensure_ruler_running(wait_timeout=20.0, interactive=True):
    """保证尺子在跑：已在线直接 True；不在就启动 exe 并等接口就绪。
    找不到 exe 时 interactive=True 会问用户要路径并记住；仍失败返回 False。"""
    if probe(timeout=0.6) is not None:
        return True
    if _ruler_proc_alive():
        print("  [!] 尺子进程在运行但接口无响应："
              "请手动退出尺子（托盘/任务管理器）后重开，不重复启动。")
        return False
    exe = _find_ruler_exe()
    if exe is None and interactive:
        print("  没找到 BarRuler 可执行文件。")
        print("  （可从 GitHub Releases 下官方包解压到 reviver_for_barruler\\dist\\，")
        print("   或用源码仓的 build.ps1 编译）")
        p = input("  请输入尺子 exe 的完整路径（回车=放弃）> ").strip().strip('"')
        if p and os.path.isfile(p):
            exe = p
            cfg = _load_cfg()
            cfg["exe"] = exe
            _save_cfg(cfg)
    if exe is None:
        return False
    print("  正在启动 BarRuler：%s" % exe)
    if not _spawn_ruler(exe):
        return False
    t0 = time.time()
    while time.time() - t0 < wait_timeout:
        if probe(timeout=0.6) is not None:
            print("  BarRuler 接口已就绪。")
            return True
        time.sleep(0.5)
    print("  [!] 等了 %.0f 秒仍连不上尺子接口（127.0.0.1:2606）。"
          % wait_timeout)
    return False
