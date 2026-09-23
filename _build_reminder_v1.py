# -*- coding: utf-8 -*-
"""纯净原版记轴本发布打包脚本 (_build_reminder_v1.py)。

仅打包记轴本核心组件：
- reminder_core.py, reminder_server.py, ruler_client.py, rawkeys.py
- reminder_web 前端网页
- calib 配置文件
- reviver_for_barruler 尺子
- 独立精简 Python 运行时环境
"""
import os
import shutil
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "releases", "记轴本_v1.0")
ZIP_PATH = os.path.join(ROOT, "releases", "记轴本_v1.0.zip")


def build():
    print("=== 开始 记轴本_v1.0 独立打包 ===")
    if os.path.exists(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.makedirs(OUT_DIR, exist_ok=True)

    # 1. 拷贝核心文件
    core_files = [
        "reminder_core.py",
        "reminder_server.py",
        "ruler_client.py",
        "rawkeys.py",
    ]
    for f in core_files:
        src = os.path.join(ROOT, f)
        dst = os.path.join(OUT_DIR, f)
        if os.path.exists(src):
            print("复制核心文件:", f)
            shutil.copy2(src, dst)

    # 2. 拷贝网页资源
    web_src = os.path.join(ROOT, "reminder_web")
    web_dst = os.path.join(OUT_DIR, "reminder_web")
    print("复制网页前端:", web_src, "->", web_dst)
    shutil.copytree(web_src, web_dst)

    # 3. 配置文件与标定目录
    calib_dst = os.path.join(OUT_DIR, "calib")
    os.makedirs(calib_dst, exist_ok=True)
    cam_dst = os.path.join(calib_dst, "camera")
    os.makedirs(cam_dst, exist_ok=True)

    for cfg_f in ["reminder.json", "reminders.json", "keys.json"]:
        src = os.path.join(ROOT, "calib", cfg_f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(calib_dst, cfg_f))

    # 4. 拷贝 BarRuler 尺子进程
    ruler_src = os.path.join(ROOT, "reviver_for_barruler", "dist")
    if os.path.exists(ruler_src):
        ruler_dst = os.path.join(OUT_DIR, "reviver_for_barruler", "dist")
        print("复制 BarRuler 尺子进程:", ruler_src, "->", ruler_dst)
        shutil.copytree(ruler_src, ruler_dst)

    # 5. 拷贝独立 Python 运行时环境
    py_src = os.path.join(ROOT, "python312")
    if os.path.exists(py_src):
        py_dst = os.path.join(OUT_DIR, "python312")
        print("复制便携 Python 环境 (python312)...")
        shutil.copytree(py_src, py_dst)

    # 6. 生成一键启动脚本
    run_bat = os.path.join(OUT_DIR, "启动记轴本.bat")
    with open(run_bat, "w", encoding="gbk") as f:
        f.write("@echo off\r\n")
        f.write("cd /d %~dp0\r\n")
        f.write("if exist python312\\python.exe (\r\n")
        f.write("    python312\\python.exe reminder_server.py --open-browser\r\n")
        f.write(") else (\r\n")
        f.write("    python reminder_server.py --open-browser\r\n")
        f.write(")\r\n")

    # 7. 打包成 ZIP
    print("生成压缩包:", ZIP_PATH)
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for root_p, _, files in os.walk(OUT_DIR):
            for file in files:
                abs_p = os.path.join(root_p, file)
                rel_p = os.path.relpath(abs_p, OUT_DIR)
                zf.write(abs_p, rel_p)

    sz_mb = os.path.getsize(ZIP_PATH) / (1024 * 1024)
    print("=== 原版记轴本打包完成！===")
    print("发布包目录:", OUT_DIR)
    print("发布压缩包:", ZIP_PATH, f"({sz_mb:.2f} MB)")


if __name__ == "__main__":
    build()