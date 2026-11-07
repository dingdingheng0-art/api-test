"""起一个本地静态服务器来打开 allure 报告。

**为什么需要这个？**

allure 2.29 的前端是通过 JS 的 ``fetch()`` 去读 ``data/*.json`` 的。
用 ``file://`` 协议直接双击打开时，浏览器会按跨域策略拒绝这些请求
（控制台能看到 CORS 报错），页面就一直停在加载动画上 —— 看着像"空白"。

起一个 ``http://127.0.0.1:端口`` 的本地服务器就没有这个问题。

用法::

    python scripts/serve_report.py            # 打开最新那份报告
    python scripts/serve_report.py --port 8888
    python scripts/serve_report.py reports/allure_20261030_082454
"""
from __future__ import annotations

import argparse
import functools
import http.server
import socket
import socketserver
import sys
import threading
import webbrowser
from pathlib import Path

# 本文件在 scripts/ 下，先让项目根进 sys.path 才能 import utils/framework
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils.paths import REPORTS_ROOT  # noqa: E402


def find_latest_report() -> Path | None:
    """找最新生成的 allure 报告目录。"""
    if not REPORTS_ROOT.exists():
        return None

    candidates = [
        d for d in REPORTS_ROOT.glob("allure_*")
        if d.is_dir() and (d / "index.html").exists()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.name)


def pick_free_port(preferred: int = 8765) -> int:
    """找一个能用的端口（默认 8765，被占了就往后找）。"""
    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    # 全占了就让它随机挑
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """别刷屏的 HTTP handler。"""

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass


def serve(directory: Path, port: int | None = None, open_browser: bool = True) -> int:
    """在后台起服务器并打开浏览器。

    Args:
        directory: 要 serve 的目录（allure 报告目录）
        port: 端口，None 表示自动找
        open_browser: 是否自动打开浏览器

    Returns:
        实际使用的端口
    """
    if not (directory / "index.html").exists():
        print(f"错误:  {directory} 里没有 index.html")
        return 1

    port = port or pick_free_port()
    handler = functools.partial(_QuietHandler, directory=str(directory))

    # 允许复用端口，不然重启会报 "Address already in use"
    class _Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    try:
        httpd = _Server(("127.0.0.1", port), handler)
    except OSError as exc:
        print(f"错误:  起服务器失败: {exc}")
        return 1

    url = f"http://127.0.0.1:{port}/"

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    # 注意：不要用 emoji —— Windows 终端默认 GBK 编码，输出 emoji 会抛
    # UnicodeEncodeError 直接把服务搞挂
    print(f"allure 报告已启动: {url}")
    print(f"   目录: {directory}")
    print("   按 Ctrl+C 停止")

    if open_browser:
        webbrowser.open(url)

    try:
        thread.join()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.shutdown()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="打开 allure 报告（本地 HTTP 服务）")
    parser.add_argument("path", nargs="?", default=None, help="报告目录，默认用最新那份")
    parser.add_argument("--port", type=int, default=None, help="端口，默认自动找")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    if args.path:
        directory = Path(args.path).resolve()
        if not directory.exists():
            print(f"错误:  目录不存在: {directory}")
            return 1
    else:
        directory = find_latest_report()
        if directory is None:
            print("错误:  没找到 allure 报告，先跑一次：python scripts/run.py --report allure")
            return 1

    return serve(directory, args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
