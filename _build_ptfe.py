# -*- coding: utf-8 -*-
"""PTFE 发布打包 (_build_ptfe.py)。

打包记轴本源码、网页和本机已有的 Python / 尺子。
不打包 MaaCore 绑定、asst，也不打包 resource 里的地图和模板。
"""
import os
import shutil
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "releases", "PTFE_v1.0")
ZIP_PATH = os.path.join(ROOT, "releases", "PTFE_v1.0.zip")


def build():
    print("=== 开始 PTFE_v1.0 (赛博塑料战术操作体) 打包 ===")
    if os.path.exists(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.makedirs(OUT_DIR, exist_ok=True)

    # 1. 拷贝全部核心代码
    core_files = [
        "reminder_core.py",
        "reminder_server.py",
        "ruler_client.py",
        "rawkeys.py",
        "replayer_types.py",
        "replayer_axis.py",
        "replayer_vision.py",
        "replayer_actuator.py",
        "replayer_mapper.py",
        "replayer_engine.py",
        "replayer_record.py",
    ]
    for f in core_files:
        src = os.path.join(ROOT, f)
        dst = os.path.join(OUT_DIR, f)
        if os.path.exists(src):
            print("复制核心代码:", f)
            shutil.copy2(src, dst)

    # 2. 拷贝网页资源
    web_src = os.path.join(ROOT, "reminder_web")
    web_dst = os.path.join(OUT_DIR, "reminder_web")
    print("复制网页前端:", web_src, "->", web_dst)
    shutil.copytree(web_src, web_dst)

    # 3. 配置文件与标定目录
    calib_dst = os.path.join(OUT_DIR, "calib")
    os.makedirs(calib_dst, exist_ok=True)
    for cfg_f in ["reminder.json", "reminders.json", "keys.json"]:
        src = os.path.join(ROOT, "calib", cfg_f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(calib_dst, cfg_f))

    # 6. 拷贝 BarRuler 尺子进程
    ruler_src = os.path.join(ROOT, "reviver_for_barruler", "dist")
    if os.path.exists(ruler_src):
        ruler_dst = os.path.join(OUT_DIR, "reviver_for_barruler", "dist")
        print("复制 BarRuler 尺子进程:", ruler_src, "->", ruler_dst)
        shutil.copytree(ruler_src, ruler_dst)

    # 7. 拷贝独立 Python 运行时环境
    py_src = os.path.join(ROOT, "python312")
    if os.path.exists(py_src):
        py_dst = os.path.join(OUT_DIR, "python312")
        print("复制便携 Python 环境 (python312)...")
        shutil.copytree(py_src, py_dst)

    # 8. 生成一键启动脚本
    run_bat = os.path.join(OUT_DIR, "启动PTFE战术复现器.bat")
    with open(run_bat, "w", encoding="gbk") as f:
        f.write("@echo off\r\n")
        f.write("title PTFE - Precision Tactical Frame Emulator\r\n")
        f.write("cd /d %~dp0\r\n")
        f.write("if exist python312\\python.exe (\r\n")
        f.write("    python312\\python.exe reminder_server.py --open-browser\r\n")
        f.write(") else (\r\n")
        f.write("    python reminder_server.py --open-browser\r\n")
        f.write(")\r\n")

    # 9. 打包成 ZIP
    print("生成压缩包:", ZIP_PATH)
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for root_p, _, files in os.walk(OUT_DIR):
            for file in files:
                abs_p = os.path.join(root_p, file)
                rel_p = os.path.relpath(abs_p, OUT_DIR)
                zf.write(abs_p, rel_p)

    sz_mb = os.path.getsize(ZIP_PATH) / (1024 * 1024)
    print("=== PTFE_v1.0 打包完成！===")
    print("发布包目录:", OUT_DIR)
    print("发布压缩包:", ZIP_PATH, f"({sz_mb:.2f} MB)")


if __name__ == "__main__":
    build()
