"""敏感信息扫描：找出配置里硬编码的密码、token、凭证。

**为什么需要**

配置文件要进 git。里面的密码、token 一旦泄漏，git 历史里就洗不掉了。
靠人肉评审不靠谱 —— 评审的人不一定懂哪行是敏感信息。

这个脚本做两件事：

1. **扫描** —— 找出配置里直接写死的敏感信息
2. **检查** —— 确认 ``.env.example`` 里该有的键都有

**怎么用**

.. code-block:: bash

    python tools/maint/scan_secrets.py           # 扫描（CI 里可以当门禁）
    python tools/maint/scan_secrets.py --strict  # 发现问题就返回非 0 退出码

**退出码**

- ``0`` —— 干净
- ``1`` —— 发现硬编码的敏感信息
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.config.config_util import load_raw_config  # noqa: E402
from utils.config.secrets import (  # noqa: E402
    ENV_EXAMPLE,
    check_production_default,
    scan_config,
)


def _read_env_example_keys() -> set[str]:
    """读示例文件里的键名。"""
    if not ENV_EXAMPLE.exists():
        return set()

    keys: set[str] = set()
    try:
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.partition("=")[0].strip()
            if key:
                keys.add(key)
    except OSError:
        return set()
    return keys


def _check_placeholders(config: dict, keys: set[str]) -> list[str]:
    """检查 ``.env.example`` 里列的变量，配置里是不是都用上了。

    反过来查：配置里引用了某个 ``${变量}``，示例文件里却没这个键 ——
    说明开发者不知道要配什么。
    """
    import re

    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")

    used: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            for match in pattern.finditer(node):
                used.add(match.group(1))

    walk(config)

    missing = sorted(used - keys)
    return [f"配置里用了 ${{{name}}}，但 {ENV_EXAMPLE.name} 里没有这个键的说明" for name in missing]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描配置里的硬编码敏感信息")
    parser.add_argument(
        "--strict", action="store_true",
        help="发现问题就返回非 0 退出码（CI 门禁用）",
    )
    parser.add_argument(
        "--config", default=None,
        help="要扫的配置文件（默认 configs/config.yaml）",
    )
    args = parser.parse_args(argv)

    if args.config:
        import yaml

        target = Path(args.config)
        config = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    else:
        # 用**未展开**的原始配置 —— 扫的是"文件里写了什么"，
        # 而不是运行时替换后的值。否则 .env 里的真实密码会被误报。
        config = load_raw_config()

    print("=" * 60)
    print("敏感信息扫描")
    print("=" * 60)

    problems = 0

    # ---- 1) 硬编码的敏感信息 ----
    findings = scan_config(config)
    if findings:
        print(f"\n发现 {len(findings)} 处硬编码的敏感信息：\n")
        for item in findings:
            print(f"  [X] {item['路径']}: {item['值']}")
            print(f"    {item['原因']}")
            print("    改法：写成 ${环境变量名}，值放到 .env 文件里")
        problems += len(findings)
    else:
        print("\n[OK] 没有发现硬编码的敏感信息")

    # ---- 2) 生产环境兜底 ----
    prod_warning = check_production_default(config)
    if prod_warning:
        print(f"\n[!] {prod_warning}")
        problems += 1

    # ---- 3) 示例文件完整性 ----
    env_keys = _read_env_example_keys()
    if env_keys:
        missing = _check_placeholders(config, env_keys)
        if missing:
            print(f"\n发现 {len(missing)} 处示例文件缺漏：\n")
            for item in missing:
                print(f"  [!] {item}")
            problems += len(missing)
        else:
            print("\n[OK] .env.example 覆盖了配置里用到的所有变量")
    else:
        print(f"\n[!] 找不到 {ENV_EXAMPLE}，无法检查示例文件完整性")

    print()
    print("=" * 60)
    if problems:
        print(f"扫描完成：发现 {problems} 个问题")
    else:
        print("扫描完成：一切正常")

    if problems and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
