"""测试报告生成。

支持三种：

- **allure**：需要装了 ``allure-pytest`` + 本机 allure 命令行
- **html**：``pytest-html`` 生成自包含 HTML
- **json**：总是生成一份 ``result.json`` 给 CI 采集

环境变量 ``APITEST_ALLURE_BIN`` 可指定 allure 命令路径。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from utils.logs.log_manager import log

from utils.paths import ALLURE_RESULTS, REPORTS_ROOT


# 已知安装位置（按优先级从高到低）
# 2.20+ 才认得 allure-pytest 2.13+ 写的 titlePath 字段，老版本会生成空报告
_ALLURE_KNOWN_PATHS = (
    r"D:\ALLURECOMMANDLINE\allure-2.29.0\bin\allure.bat",
    r"D:\ALLURECOMMANDLINE\allure-2.29.0\bin\allure",
    r"C:\Program Files\allure\bin\allure.bat",
    r"C:\tools\allure\bin\allure.bat",
    r"D:\allure\bin\allure.bat",
)


def _find_java_home() -> str | None:
    """找 JDK 安装目录。

    allure 命令本身是个 Java 程序，``allure generate`` 必须能找到 java。
    但 java 不一定在 PATH 里：常见情况是 JAVA_HOME 只在"用户环境变量"
    里配了，而 IDE / CI / 计划任务启动的进程没继承到 —— 表现出来就是
    allure 命令直接失败（Windows 上是 9009），报告生成不出来。

    这里按可靠程度依次找：环境变量 → 注册表（Windows）→ 常见安装目录
    → PATH 里的 java。
    """
    suffix = "java.exe" if sys.platform == "win32" else "java"

    def _valid(home: str | None) -> str | None:
        if not home:
            return None
        candidate = Path(home) / "bin" / suffix
        return str(Path(home)) if candidate.exists() else None

    env_home = _valid(os.environ.get("JAVA_HOME"))
    if env_home:
        return env_home

    # Windows: JAVA_HOME 常配在用户环境变量里，进程却没继承
    if sys.platform == "win32":
        try:
            import winreg

            locations = (
                (winreg.HKEY_CURRENT_USER, r"Environment"),
                (
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
                ),
            )
            for hive, key in locations:
                try:
                    with winreg.OpenKey(hive, key) as handle:
                        value, _ = winreg.QueryValueEx(handle, "JAVA_HOME")
                except OSError:
                    continue
                found = _valid(value)
                if found:
                    return found
        except ImportError:  # pragma: no cover - 非 Windows
            pass

    # 常见安装根目录下扫 jdk-*
    for root in (
        r"C:\tools",
        r"C:\Program Files\Java",
        r"C:\Program Files\Eclipse Adoptium",
        r"C:\Program Files\Microsoft",
        r"D:\tools",
        r"D:\Program Files\Java",
        "/usr/lib/jvm",
        "/opt/java",
    ):
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for child in sorted(root_path.glob("jdk*"), reverse=True):
            found = _valid(str(child))
            if found:
                return found

    found = shutil.which("java")
    if found:
        # <home>/bin/java
        return str(Path(found).resolve().parent.parent)

    return None


def _allure_env() -> dict[str, str]:
    """给 allure 子进程准备环境变量（补上 JAVA_HOME / PATH）。"""
    env = os.environ.copy()
    java_home = _find_java_home()
    if java_home:
        env["JAVA_HOME"] = java_home
        java_bin = str(Path(java_home) / "bin")
        path_parts = env.get("PATH", "").split(os.pathsep)
        if java_bin not in path_parts:
            env["PATH"] = java_bin + os.pathsep + env.get("PATH", "")
    return env


def _allure_bin() -> str | None:
    """找 allure 命令。

    优先级：
    1. 环境变量 ``APITEST_ALLURE_BIN``（最可控）
    2. 已知安装位置里的新版本（避免 PATH 指向老版本）
    3. PATH 里的 allure
    """
    env_bin = os.environ.get("APITEST_ALLURE_BIN")
    if env_bin and Path(env_bin).exists():
        return env_bin

    # 已知位置的新版本优先 —— PATH 里可能还留着老版本
    for candidate in _ALLURE_KNOWN_PATHS:
        if Path(candidate).exists():
            return candidate

    found = shutil.which("allure")
    if found:
        return found

    # 兜底：扫一下常见安装根目录
    for root in (r"D:\ALLURECOMMANDLINE", r"C:\allure", r"C:\tools\allure"):
        root_path = Path(root)
        if not root_path.exists():
            continue
        for child in sorted(root_path.glob("allure-*/bin/allure.bat"), reverse=True):
            return str(child)

    return None


def write_allure_meta(env: str) -> Path:
    """写 Allure 的环境信息文件。"""
    ALLURE_RESULTS.mkdir(parents=True, exist_ok=True)

    (ALLURE_RESULTS / "environment.properties").write_text(
        f"Environment={env}\n"
        f"PythonVersion={platform.python_version()}\n"
        f"Platform={platform.system()}\n",
        encoding="utf-8",
    )
    (ALLURE_RESULTS / "executor.json").write_text(
        json.dumps(
            {"name": "local", "type": "local", "reportName": "接口自动化测试报告"},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    # 分类：除了按状态分，再按"失败原因"分
    #
    # messageRegex 匹配的是失败信息里的关键词 —— 让报告里能一眼看出
    # "这条是因为数据校验挂的"还是"接口挂了"。
    #
    # ⚠️ 顺序很重要：**归因类要排在前面**。
    #    Allure 按第一个匹配的规则归类，泛化的"接口断言失败"放前面
    #    会把所有失败都吃掉，归因就白算了。
    (ALLURE_RESULTS / "categories.json").write_text(
        json.dumps(
            [
                {
                    "name": "服务问题（后端修）",
                    "matchedStatuses": ["failed", "broken"],
                    "messageRegex": ".*【服务问题】.*",
                },
                {
                    "name": "用例数据问题（用例要改）",
                    "matchedStatuses": ["failed", "broken"],
                    "messageRegex": ".*【用例数据问题】.*",
                },
                {
                    "name": "脚本问题（自动化要改）",
                    "matchedStatuses": ["failed", "broken"],
                    "messageRegex": ".*【脚本问题】.*",
                },
                {
                    "name": "环境问题（网络/服务不通）",
                    "matchedStatuses": ["failed", "broken"],
                    "messageRegex": ".*【环境问题】.*",
                },
                {
                    "name": "数据校验失败（数据库/缓存/MQ）",
                    "matchedStatuses": ["failed", "broken"],
                    "messageRegex": ".*(SQL |\\[redis |\\[MQ |校验失败).*",
                },
                {
                    "name": "前置/后置处理失败",
                    "matchedStatuses": ["failed", "broken"],
                    "messageRegex": ".*(setup |teardown |工厂 ).*",
                },
                {
                    "name": "接口断言失败",
                    "matchedStatuses": ["failed"],
                },
                {"name": "执行报错", "matchedStatuses": ["broken"]},
                {"name": "跳过", "matchedStatuses": ["skipped"]},
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return ALLURE_RESULTS

def clear_allure_results() -> None:
    """清空 allure 结果目录。

    ⚠️ 必须在 pytest 跑之前调用 —— pytest 跑完结果就写在这里，
    跑完再清就等于把数据全删了（报告会变成空的）。
    """
    if not ALLURE_RESULTS.exists():
        return

    for old in ALLURE_RESULTS.glob("*"):
        if old.name in ("environment.properties", "executor.json", "categories.json"):
            continue
        try:
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
            else:
                old.unlink()
        except OSError:
            pass
    log.info("已清空 allure 结果目录: %s", ALLURE_RESULTS)


def merge_worker_results() -> int:
    """把各 worker 的 allure 结果合并到主目录。

    **只在并行跑完之后调用**（主进程收尾时）。

    xdist 的每个 worker 写自己的结果目录（``worker-gw0``、``worker-gw1`` …），
    不合并的话 allure 只看得到一个 worker 的结果，报告会缺一大半。

    Returns:
        合并了多少个结果文件
    """
    if not ALLURE_RESULTS.exists():
        return 0

    worker_dirs = [
        d for d in ALLURE_RESULTS.glob("worker-*")
        if d.is_dir()
    ]

    if not worker_dirs:
        return 0

    merged = 0
    for worker_dir in worker_dirs:
        for item in worker_dir.iterdir():
            if item.name.endswith(".json") and item.is_file():
                target = ALLURE_RESULTS / item.name
                try:
                    # 同名文件加后缀，避免互相覆盖
                    if target.exists():
                        stem, _, suffix = item.name.rpartition(".")
                        target = ALLURE_RESULTS / f"{stem}-{worker_dir.name}.{suffix}"
                    shutil.copy2(item, target)
                    merged += 1
                except OSError as exc:
                    log.warning("合并 allure 结果失败 %s: %s", item.name, exc)

        # 合并完删掉 worker 子目录（避免下次误当结果）
        try:
            shutil.rmtree(worker_dir, ignore_errors=True)
        except OSError:
            pass

    if merged:
        log.info("已合并 %s 个 worker 的 allure 结果（%s）", merged, len(worker_dirs))
    return merged


def _allure_cli_version(allure_bin: str) -> tuple[int, ...]:
    """拿 allure 命令行的版本号（解析不出来就返回空元组）。"""
    try:
        proc = subprocess.run(
            [allure_bin, "--version"],
            check=True,
            capture_output=True,
            timeout=30,
            env=_allure_env(),
        )
        text = proc.stdout.decode("utf-8", "replace").strip()
        # 形如 "2.13.5"
        parts = text.split(".")
        return tuple(int(p) for p in parts if p.isdigit())
    except Exception:  # noqa: BLE001
        return ()


# 老版本命令行不认识的字段（新版 allure-pytest 写的）
# 2.13.5 的 allure 会因为 "Unrecognized field titlePath" 直接跳过所有用例，
# 结果就是"报告生成成功但里面是空的"。这里在生成前把这些字段剔掉。
_FIELDS_REJECTED_BY_OLD_CLI = ("titlePath", "rerunOf")


def strip_new_allure_fields(results_dir: Path) -> int:
    """把老版本 allure 命令行不认识的字段从结果文件里去掉。

    只在命令行版本较老时才会动手；新版本留着这些字段（能显示更完整的信息）。

    Returns:
        处理了多少个文件
    """
    changed = 0
    for path in results_dir.glob("*-result.json"):
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            continue

        if not isinstance(data, dict):
            continue

        removed = False
        for field in _FIELDS_REJECTED_BY_OLD_CLI:
            if field in data:
                data.pop(field, None)
                removed = True

        if removed:
            try:
                path.write_text(
                    json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                changed += 1
            except OSError:
                pass
    return changed


def generate_allure_report(env: str = "online", open_browser: bool = True) -> Path | None:
    """生成 allure HTML 报告。

    ⚠️ 这个函数在 pytest **跑完之后**调用，只负责把已有的结果目录转成 HTML。
    清目录的事交给 ``clear_allure_results()``（要在跑之前做）。

    Args:
        env: 环境名（写进报告的环境信息里）
        open_browser: 生成后是否自动用浏览器打开

    Returns:
        报告首页路径；失败返回 None
    """
    allure_bin = _allure_bin()
    if not allure_bin:
        log.warning(
            "没找到 allure 命令，跳过 allure 报告。"
            "装了 allure-pytest 的话可以用 APITEST_ALLURE_BIN 指定命令行路径。"
        )
        return None

    results_dir = write_allure_meta(env)

    # 检查一下有没有真的跑出结果
    result_files = list(results_dir.glob("*-result.json"))
    if not result_files:
        log.warning(
            "allure 结果目录里没有结果文件（%s），"
            "报告会是空的。确认 pytest 带了 --alluredir 参数。",
            results_dir,
        )
        return None

    # 老版本 allure 命令行会跳过不认识的字段（报告变成空的），
    # 这里先探测版本，需要的话把字段剔掉
    cli_version = _allure_cli_version(allure_bin)
    if cli_version and cli_version < (2, 20):
        changed = strip_new_allure_fields(results_dir)
        if changed:
            log.info(
                "allure 命令行版本 %s 较老，已剔除 %s 个结果文件里的新字段",
                ".".join(str(p) for p in cli_version),
                changed,
            )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = REPORTS_ROOT / f"allure_{stamp}"

    java_home = _find_java_home()
    if not java_home:
        log.warning(
            "没找到 Java/JDK。allure 是 Java 程序，没有 java 会生成失败；"
            "请安装 JDK 8+ 或设置 JAVA_HOME。"
        )

    log.info(
        "生成 allure 报告（%s 条结果）-> %s（JAVA_HOME=%s）",
        len(result_files),
        output_dir,
        java_home or "未找到",
    )
    try:
        subprocess.run(
            [allure_bin, "generate", str(results_dir), "-o", str(output_dir), "--clean"],
            check=True,
            capture_output=True,
            env=_allure_env(),
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", "replace") if exc.stderr else str(exc)
        log.error("allure 报告生成失败: %s", stderr[:800])
        return None
    except OSError as exc:
        log.error("allure 命令执行失败: %s", exc)
        return None

    index = output_dir / "index.html"
    if not index.exists():
        log.error("allure 报告生成失败：%s 不存在", index)
        return None

    log.info("allure 报告已生成: %s", index)

    # 固定一个"最新报告"的入口，方便直接打开
    # 注意：这里存的是相对路径，用 file:// 打开会空白，
    # 所以真正看报告要走 HTTP（见下面 _open_via_http）
    latest = REPORTS_ROOT / "allure_latest.html"
    try:
        latest.write_text(
            f'<!DOCTYPE html><meta charset="utf-8">'
            f'<title>接口自动化测试报告</title>'
            f'<p>最新报告目录：<code>{output_dir.name}</code></p>'
            f'<p>⚠️ allure 报告需要通过 HTTP 打开（直接双击会是空白页）：</p>'
            f'<p>运行 <code>python scripts\\serve_report.py</code> 自动起服务并打开。</p>',
            encoding="utf-8",
        )
    except OSError:
        pass

    if open_browser:
        # ⚠️ 必须用 HTTP 打开，不能用 file://
        # allure 2.29 的前端用 fetch() 读 data/*.json，
        # file:// 协议下浏览器按跨域策略拒绝 → 页面永远空白
        _open_via_http(output_dir)

    return index


def _open_via_http(report_dir: Path) -> bool:
    """起个本地 HTTP 服务器并打开报告（解决 file:// 空白问题）。

    服务器跑在**独立进程**里 —— 独立于 pytest 主进程，
    这样 pytest 退出后报告依然能看。
    """
    # 独立进程起服务器，主进程不受影响
    serve_script = Path(__file__).resolve().parent.parent / "scripts" / "serve_report.py"
    if not serve_script.exists():
        return open_in_browser(report_dir / "index.html")

    try:
        proc = subprocess.Popen(
            [sys.executable, str(serve_script), str(report_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        log.warning("起本地服务器失败（%s），退回到直接打开文件", exc)
        return open_in_browser(report_dir / "index.html")

    # 给它一点时间起服务并打开浏览器
    import time

    time.sleep(1.5)
    log.info("报告已在本地服务器上打开（后台进程 PID=%s）", proc.pid)
    return True


def open_in_browser(path: Path) -> bool:
    """用系统默认浏览器打开文件。"""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("自动打开浏览器失败: %s", exc)
        return False


def generate_html_report(env: str = "online") -> Path | None:
    """用 pytest-html 生成 HTML 报告（由 pytest --html 调用，这里只算路径）。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
    output = REPORTS_ROOT / f"report_{stamp}.html"
    log.info("HTML 报告: %s", output)
    return output


def write_result_json(results: dict) -> Path:
    """把执行结果写成 JSON（给 CI 采集用）。"""
    REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
    out = REPORTS_ROOT / "result.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def generate_report(report_type: str = "allure", env: str = "online") -> Path | None:
    """按类型生成报告。"""
    if report_type == "allure":
        return generate_allure_report(env)
    if report_type == "html":
        return generate_html_report(env)
    return None
