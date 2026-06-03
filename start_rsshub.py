"""
RSSHub 本地启动脚本（开发模式，无需构建）
用途：采集财经日报前启动，采集完成后关闭

用法:
    python start_rsshub.py        # 启动 RSSHub
    python start_rsshub.py stop   # 停止 RSSHub
"""

import subprocess
import sys
import time
import requests
import os

RSSUB_DIR = os.environ.get("RSSHUB_DIR", r"D:\RSSHub")
PORT = int(os.environ.get("RSSHUB_PORT", "1200"))


def is_running():
    """检查 RSSHub 是否已运行"""
    try:
        r = requests.get(f"http://localhost:{PORT}/", timeout=3)
        return r.status_code == 200
    except:
        return False


def start():
    """启动 RSSHub"""
    if is_running():
        print("RSSHub 已在运行")
        return True

    print("=" * 50)
    print("启动本地 RSSHub (port 1200, dev mode)")
    print("=" * 50)

    # 切换目录并启动（后台进程）
    os.chdir(RSSUB_DIR)
    cmd = f'node --import tsx lib/index.ts --port {PORT}'

    # Windows 后台启动
    subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=RSSUB_DIR
    )

    # 等待就绪
    print("等待 RSSHub 就绪...")
    for i in range(30):  # 最多等30秒
        time.sleep(1)
        if is_running():
            print(f"RSSHub 已就绪: http://localhost:{PORT}")
            print("可以运行: python src/finance_main.py")
            return True
        print(f"." if i < 25 else f" 等待中...({i}s)", end="", flush=True)

    print("\n启动超时，请检查日志")
    return False


def stop():
    """停止 RSSHub"""
    print("停止 RSSHub...")

    # Windows: 通过 wmic 查找并终止 node 进程
    try:
        result = subprocess.run(
            ['wmic', 'process', 'where', f'"CommandLine like \"%rsshub%\" or CommandLine like \"%index.ts%\""', 'get', 'ProcessId'],
            capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.strip().split('\n')[1:]  # 跳过标题行
        killed = 0
        for line in lines:
            pid = line.strip()
            if pid and pid.isdigit():
                try:
                    subprocess.run(['taskkill', '/F', '/PID', pid], capture_output=True)
                    killed += 1
                except:
                    pass

        if killed > 0:
            print(f"已停止 {killed} 个 RSSHub 进程")
        else:
            print("未找到 RSSHub 进程")

    except Exception as e:
        print(f"停止失败: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stop":
        stop()
    else:
        success = start()
        sys.exit(0 if success else 1)
