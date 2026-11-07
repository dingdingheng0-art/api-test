"""用例依赖编排：让「先造数据、再验证」的用例按正确顺序跑。

**要解决什么问题**

真实业务里，用例之间是有先后的：

.. code-block:: yaml

    # 用例 A：先造一条订单
    testcase:
      name: 创建订单
      steps:
        - id: create
          path: /order/create
          method: POST

    # 用例 B：再查刚才那条订单
    testcase:
      name: 查询订单详情
      depends_on: [创建订单]
      steps:
        - id: detail
          path: /order/detail?orderNo={{创建订单.create.result.orderNo}}
          method: GET

问题是 pytest **不认识**「用例 B 依赖用例 A」这层关系。它只按文件顺序、
模块顺序跑用例。如果 A 和 B 在不同文件、甚至不同模块，B 很可能先跑 ——
那时候 A 造的数据还不存在，B 必然失败。

更糟的是：并行执行（``pytest -n 4``）时顺序完全不确定，B 什么时候跑全看运气。

**这个模块怎么解决**

1. **建图** —— 扫描所有用例，把 ``depends_on`` 收成一张有向图
2. **排序** —— 用拓扑排序算出「先跑谁后跑谁」
3. **查环** —— 有循环依赖直接报错（死等下去只会卡住 CI）
4. **传数据** —— 依赖用例跑完后，把它的步骤变量存下来，
   下游用例可以直接 ``{{依赖用例名.步骤名.字段}}`` 引用

**和并行执行的关系**

依赖是**硬约束** —— 有依赖关系的用例必须串行。
并行执行时，框架会自动把有依赖关系的用例分到同一个「组」里，
组内串行、组间并行。这样既保证顺序，又不牺牲并行度。

没有依赖关系的用例照旧并行跑。

**怎么写**

.. code-block:: yaml

    testcase:
      name: 查询订单详情
      depends_on:
        - 创建订单
      steps:
        - id: detail
          path: /order/detail
          method: GET
          params:
            orderNo: '{{创建订单.create.result.orderNo}}'

被依赖的用例名要**在同一个项目内唯一**。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from utils.logs.log_manager import log

# 跨进程共享的用例结果目录
DEPENDENCY_STATE_DIR_NAME = "dependencies"

# 结果文件后缀
RESULT_SUFFIX = ".result.json"

# 等待依赖完成的轮询间隔（秒）
POLL_INTERVAL = 0.3

# 等待依赖完成的超时（秒）
#
# 以前是 600 秒，而且**每个依赖各算一份** —— 一条用例依赖 3 个上游，
# 上游都没跑起来时就会干等 30 分钟，CI 看起来像卡死。
# 现在默认 120 秒，而且多个依赖共享同一个截止时间（见
# ``testcases/case_runner.py::_load_dependency_outputs``）。
# 真需要等更久就配 ``configs/config.yaml`` 的 ``dependency.wait_timeout``。
DEFAULT_WAIT_TIMEOUT = 120.0

# 依赖结果的有效期（秒）
#
# 结果文件里存的是**上一轮造出来的数据 id**。上一轮的 id 拿到这一轮用，
# 删/改的可能已经是别的记录了 —— 所以过期的结果一律当作"没有结果"。
# 默认 6 小时：同一个上午反复跑（--lf 局部重跑）仍然能复用。
DEFAULT_RESULT_MAX_AGE = 6 * 3600.0


class DependencyError(RuntimeError):
    """依赖关系有问题（循环依赖、依赖的用例不存在等）。"""


@dataclass
class CaseNode:
    """依赖图里的一个节点（一条用例）。

    Attributes:
        name: 用例名（依赖关系里用它来指代）
        project: 所属项目
        file: 用例文件路径
        depends_on: 依赖的用例名列表
        order: 原始执行顺序（拓扑排序时作为稳定排序的次要依据）
    """

    name: str
    project: str = ""
    file: str = ""
    depends_on: list[str] = field(default_factory=list)
    order: int = 0


class DependencyGraph:
    """用例依赖图。

    说人话：一张表，记着「谁要先跑」。
    """

    def __init__(self, nodes: list[CaseNode] | None = None):
        self.nodes: dict[str, CaseNode] = {}
        for node in nodes or []:
            self.nodes[node.name] = node

    # ---------------- 建图 ----------------

    def add(self, node: CaseNode) -> None:
        """加一个节点。"""
        self.nodes[node.name] = node

    def get(self, name: str) -> CaseNode | None:
        """按名字找节点。"""
        return self.nodes.get(name)

    def dependents_of(self, name: str) -> list[str]:
        """谁依赖了这条用例（反向边）。"""
        return [n.name for n in self.nodes.values() if name in n.depends_on]

    def __len__(self) -> int:
        return len(self.nodes)

    # ---------------- 校验 ----------------

    def validate(self, project: str = "") -> list[str]:
        """检查依赖关系有没有问题。

        Returns:
            问题清单（空 = 没问题）
        """
        problems: list[str] = []

        for node in self.nodes.values():
            if project and node.project and node.project != project:
                continue

            for dep in node.depends_on:
                target = self.nodes.get(dep)
                if target is None:
                    problems.append(
                        f"用例「{node.name}」依赖的「{dep}」不存在"
                        f"（依赖名要和用例 name 完全一致）"
                    )
                elif target.project and node.project and target.project != node.project:
                    problems.append(
                        f"用例「{node.name}」依赖的「{dep}」在另一个项目"
                        f"（{target.project}）里，不能跨项目依赖"
                    )

        problems.extend(self._find_cycles())
        return problems

    def _find_cycles(self) -> list[str]:
        """找循环依赖。

        用三色标记法（白=没访问、灰=在栈上、黑=已完成）——
        遇到灰色的节点就说明绕回来了。

        Returns:
            每条环一句人话，比如「用例依赖存在循环：A → B → A」
        """
        cycles: list[str] = []
        WHITE, GRAY, BLACK = 0, 1, 2
        colors: dict[str, int] = {name: WHITE for name in self.nodes}

        def visit(name: str, path: list[str]) -> None:
            colors[name] = GRAY
            path.append(name)

            node = self.nodes.get(name)
            for dep in (node.depends_on if node else []):
                if dep not in self.nodes:
                    continue
                state = colors.get(dep, WHITE)
                if state == GRAY:
                    # 找到环了
                    start = path.index(dep)
                    cycle = path[start:] + [dep]
                    cycles.append(f"用例依赖存在循环：{' → '.join(cycle)}")
                elif state == WHITE:
                    visit(dep, path)

            path.pop()
            colors[name] = BLACK

        for name in self.nodes:
            if colors.get(name, WHITE) == WHITE:
                visit(name, [])

        return cycles

    # ---------------- 排序 ----------------

    def topological_order(self, project: str = "") -> list[str]:
        """算出执行顺序。

        依赖少的先跑？不对 —— 是**被依赖的先跑**。

        同一层里按原始顺序排（保持用例 ID 稳定，报告好读）。
        """
        scope = {
            name: node
            for name, node in self.nodes.items()
            if not project or not node.project or node.project == project
        }

        # 入度 = 还有几个依赖没满足
        indegree: dict[str, int] = {}
        for name, node in scope.items():
            count = 0
            for dep in node.depends_on:
                if dep in scope and dep != name:
                    count += 1
            indegree[name] = count

        # 就绪队列：入度为 0 的（没有依赖的）先跑
        ready = sorted(
            (name for name, deg in indegree.items() if deg == 0),
            key=lambda n: scope[n].order,
        )
        result: list[str] = []

        while ready:
            name = ready.pop(0)
            result.append(name)

            # 谁依赖我？我可以解锁了
            for other, node in scope.items():
                if name in node.depends_on:
                    indegree[other] -= 1
                    if indegree[other] == 0:
                        ready.append(other)

            # 保持顺序稳定
            ready.sort(key=lambda n: scope[n].order)

        # 没排完的说明有环
        if len(result) != len(scope):
            remaining = [n for n in scope if n not in result]
            cycles = self._find_cycles()
            detail = "；".join(cycles) if cycles else "、".join(remaining)
            raise DependencyError(f"用例依赖存在循环，无法排序：{detail}")

        return result

    def parallel_groups(self, project: str = "") -> list[list[str]]:
        """把用例分组：**组内必须串行，组间可以并行**。

        算法：拓扑排序的每一层就是一个组。同一层的用例互相没有依赖，
        可以同时跑；不同层之间有先后。

        Returns:
            ``[[用例名, ...], [...], ...]`` —— 列表里的每个子列表是一组
        """
        scope = {
            name: node
            for name, node in self.nodes.items()
            if not project or not node.project or node.project == project
        }
        if not scope:
            return []

        # 每个节点的「层号」= 最长依赖链长度
        depth: dict[str, int] = {}

        def compute_depth(name: str, seen: frozenset[str] = frozenset()) -> int:
            if name in depth:
                return depth[name]
            if name in seen:
                raise DependencyError(f"用例依赖存在循环：{name}")
            node = scope.get(name)
            if not node:
                return 0
            deps = [d for d in node.depends_on if d in scope and d != name]
            value = 0 if not deps else 1 + max(
                compute_depth(d, seen | {name}) for d in deps
            )
            depth[name] = value
            return value

        for name in scope:
            compute_depth(name)

        groups: dict[int, list[str]] = {}
        for name, node in scope.items():
            groups.setdefault(depth[name], []).append(name)

        return [
            sorted(groups[level], key=lambda n: scope[n].order)
            for level in sorted(groups)
        ]

    def summary(self, project: str = "") -> str:
        """一句人话说明（启动时打日志用）。"""
        scope = [
            n for n in self.nodes.values()
            if not project or not n.project or n.project == project
        ]
        if not scope:
            return "没有用例依赖关系"

        linked = [n for n in scope if n.depends_on]
        if not linked:
            return f"{len(scope)} 条用例都没有依赖关系"

        groups = self.parallel_groups(project)
        return (
            f"{len(scope)} 条用例，其中 {len(linked)} 条有依赖，"
            f"分 {len(groups)} 组执行（组内串行，组间可并行）"
        )


# ---------------------------------------------------------------------------
# 跨进程结果共享
# ---------------------------------------------------------------------------


class DependencyStore:
    """存放用例执行结果，供下游用例读取。

    为什么落盘而不是放内存：

    - pytest-xdist 下每个 worker 是**独立进程**，内存不共享
    - 用例重跑时，结果要从上一次留着（框架支持 ``--lf``）

    所以用 JSON 文件存，文件名按用例名哈希（用例名可能有中文和特殊字符）。
    """

    def __init__(
        self,
        base_dir: Path | None = None,
        max_age: float | None = None,
    ):
        if base_dir is None:
            from utils.paths import CACHE_DIR

            base_dir = CACHE_DIR / DEPENDENCY_STATE_DIR_NAME
        self.base_dir = base_dir
        # 超过这个时间的旧结果当作不存在（里面存的是上一轮的数据 id）
        self.max_age = get_result_max_age() if max_age is None else max_age
        self._lock = threading.Lock()

    def _ensure_dir(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, case_name: str) -> Path:
        """结果文件路径（用例名做哈希，避免非法字符）。"""
        import hashlib

        digest = hashlib.sha1(case_name.encode("utf-8")).hexdigest()[:16]
        # 文件名里保留用例名的一部分，方便人工排查
        safe = "".join(ch for ch in case_name if ch.isalnum() or ch in "-_")[:40]
        return self.base_dir / f"{safe}_{digest}{RESULT_SUFFIX}"

    def save(self, case_name: str, payload: dict[str, Any]) -> None:
        """存一条用例的执行结果。"""
        with self._lock:
            self._ensure_dir()
            path = self._path_for(case_name)
            data = {
                "case": case_name,
                "saved_at": time.time(),
                **payload,
            }
            try:
                path.write_text(
                    json.dumps(data, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
            except OSError as exc:
                log.warning("存依赖结果失败 %s: %s", case_name, exc)

    def load(self, case_name: str) -> dict[str, Any] | None:
        """读一条用例的执行结果（没有就返回 None）。

        超过 ``max_age`` 的旧结果当作没有 —— 里面存的是上一轮造的数据 id，
        拿到这一轮去删/改，目标可能已经是别的记录了。
        """
        path = self._path_for(case_name)
        if not path.exists():
            return None
        try:
            if self.max_age and self.max_age > 0:
                age = time.time() - path.stat().st_mtime
                if age > self.max_age:
                    log.info(
                        "依赖结果「%s」已过期（%.0f 秒前），忽略",
                        case_name,
                        age,
                    )
                    return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def has(self, case_name: str) -> bool:
        """这条用例跑过没有。"""
        return self._path_for(case_name).exists()

    def wait_for(
        self,
        case_name: str,
        timeout: float | None = None,
        interval: float = POLL_INTERVAL,
        min_saved_at: float = 0.0,
    ) -> dict[str, Any] | None:
        """等一条依赖用例跑完。

        并行执行时，下游用例可能比上游先开始 —— 这时就在这儿等。

        Args:
            case_name: 依赖的用例名
            timeout: 最长等多久（秒）。不传就用配置里的 ``dependency.wait_timeout``
            interval: 多久轮询一次
            min_saved_at: 只认这个时间点之后写入的结果。
                **必须传本轮开始时间** —— 否则磁盘上还留着上一轮的结果，
                第一次轮询就会"立刻等到"，等到的却是过期数据。

        Returns:
            依赖用例的结果；超时返回 None
        """
        if timeout is None:
            timeout = get_wait_timeout()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.load(case_name)
            if result is not None and self._saved_after(result, min_saved_at):
                return result
            time.sleep(interval)
        # 最后一次检查：避免刚好卡在 deadline 边界上
        result = self.load(case_name)
        if result is not None and self._saved_after(result, min_saved_at):
            return result
        return None

    @staticmethod
    def _saved_after(result: dict[str, Any], min_saved_at: float) -> bool:
        """这份结果是不是 ``min_saved_at`` 之后写的（旧结果一律不认）。"""
        if not min_saved_at:
            return True
        try:
            saved_at = float(result.get("saved_at") or 0)
        except (TypeError, ValueError):
            return False
        return saved_at >= min_saved_at

    def clear(self) -> None:
        """清空所有结果（新一轮执行前调）。"""
        with self._lock:
            if not self.base_dir.exists():
                return
            for path in self.base_dir.glob(f"*{RESULT_SUFFIX}"):
                try:
                    path.unlink()
                except OSError:
                    pass

    def prune(self, max_age: float | None = None) -> int:
        """删掉过期 / 损坏的结果文件，返回删了几个。

        为什么要在每轮开始时做：结果目录从来不清理的话，跑上几个月就是
        几千个小文件（实测 182 个文件 3MB，其中单个文件 388KB）。
        更麻烦的是**旧结果会被当成本轮结果用掉** —— 上一轮那条 area 是
        999000000001，这一轮读到它去 DELETE，删的就是别的数据了。
        """
        age = self.max_age if max_age is None else max_age
        if age is None or age <= 0:
            # 配成 0 / 负数 = 不过期，那就别删
            return 0

        removed = 0
        with self._lock:
            if not self.base_dir.exists():
                return 0
            now = time.time()
            for path in self.base_dir.glob(f"*{RESULT_SUFFIX}"):
                try:
                    expired = (now - path.stat().st_mtime) > age
                except OSError:
                    continue
                if not expired:
                    continue
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed


# 本轮实际会跑的用例名（收集阶段填），以及本轮开始时间。
#
# 用途：区分「依赖用例还没轮到」和「依赖用例根本没被选中」。
# 前者必须等新的结果，否则会读到上一轮的旧结果（数据 id 早已失效）。
_SESSION_CASES: set[str] = set()
_SESSION_STARTED_AT: float = 0.0


def begin_session(case_names: list[str] | None = None, prune: bool = True) -> int:
    """开始新一轮：记下本轮要跑的用例，清掉过期结果。

    必须在**收集完成之后**调用（那时才知道选了哪些用例）。
    并行时 worker 传 ``prune=False`` —— 删文件这一步只由主进程做，
    worker 一删就会把别的 worker 刚写的结果抹掉。

    Returns:
        清理掉的过期结果文件数（``prune=False`` 时恒为 0）
    """
    global _SESSION_STARTED_AT
    _SESSION_CASES.clear()
    _SESSION_CASES.update(str(n) for n in (case_names or []) if str(n).strip())
    # 时间戳取"当前时间"，但比文件系统精度略早一点 ——
    # 同一个时间片里刚写完的结果不该被误判成"上一轮的"。
    _SESSION_STARTED_AT = time.time() - 1.0
    return get_store().prune() if prune else 0


def has_session_plan() -> bool:
    """本轮的执行计划登记过没有（``begin_session`` 调过且名单非空）。"""
    return bool(_SESSION_CASES)


def is_active_case(case_name: str) -> bool:
    """这条用例在本轮的执行计划里吗（收集阶段有没有它的名字）。

    没登记过计划时（比如直接在脚本里调 ``run_case``）一律返回 True ——
    没计划就无从判断"它被过滤掉了"，不该因此报错。
    """
    if not _SESSION_CASES:
        return True
    return case_name in _SESSION_CASES


def _dependency_config() -> dict[str, Any]:
    """``configs/config.yaml`` 里的 ``dependency`` 段（没有就是空字典）。"""
    try:
        from utils.config.config_manager import ConfigManager

        cfg = (ConfigManager.load_config() or {}).get("dependency") or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:  # noqa: BLE001 - 配置读不到就用默认值
        return {}


def _positive_float(raw: Any, fallback: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def get_result_max_age() -> float:
    """依赖结果的有效期（秒）。

    配置 ``dependency.result_max_age``；配 0 或负数表示不过期。
    """
    cfg = _dependency_config()
    if cfg.get("result_max_age") is not None:
        try:
            return float(cfg["result_max_age"])
        except (TypeError, ValueError):
            pass
    return DEFAULT_RESULT_MAX_AGE


def get_wait_timeout() -> float:
    """等依赖用例的超时（秒）。

    优先级：``configs/config.yaml`` 的 ``dependency.wait_timeout``
    > 环境变量 ``APITEST_DEPENDENCY_TIMEOUT`` > ``DEFAULT_WAIT_TIMEOUT``。
    """
    raw = os.environ.get("APITEST_DEPENDENCY_TIMEOUT", "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass

    cfg = _dependency_config()
    if cfg.get("wait_timeout") is not None:
        return _positive_float(cfg["wait_timeout"], DEFAULT_WAIT_TIMEOUT)
    return DEFAULT_WAIT_TIMEOUT


def session_started_at() -> float:
    """本轮开始的时间戳（没调过 ``begin_session`` 就是 0）。"""
    return _SESSION_STARTED_AT


def get_store() -> DependencyStore:
    """拿全局的结果存储。"""
    return DependencyStore()


# ---------------------------------------------------------------------------
# 从用例数据建图
# ---------------------------------------------------------------------------


def build_graph_from_cases(
    cases: list[tuple[str, Path, dict[str, Any]]],
) -> DependencyGraph:
    """从用例数据建依赖图。

    Args:
        cases: ``[(项目名, 文件路径, 用例数据), ...]``
    """
    graph = DependencyGraph()

    for order, (project, path, case_data) in enumerate(cases):
        case = (case_data or {}).get("testcase") or {}
        name = str(case.get("name") or "").strip()
        if not name:
            continue

        deps = case.get("depends_on") or []
        if isinstance(deps, str):
            deps = [deps]
        dep_names = [str(d).strip() for d in deps if str(d).strip()]

        graph.add(
            CaseNode(
                name=name,
                project=project,
                file=str(path),
                depends_on=dep_names,
                order=order,
            )
        )

    return graph


def extract_dependencies(case: dict[str, Any]) -> list[str]:
    """取一条用例声明的依赖。"""
    deps = case.get("depends_on") or []
    if isinstance(deps, str):
        deps = [deps]
    return [str(d).strip() for d in deps if str(d).strip()]


def collect_case_outputs(
    case_name: str,
    case_data: dict[str, Any],
    step_vars: dict[str, Any],
) -> dict[str, Any]:
    """收集一条用例跑完后要留给下游的数据。

    下游用例通过 ``{{依赖用例名.步骤名.字段}}`` 引用。
    """
    case = (case_data or {}).get("testcase") or {}

    outputs: dict[str, Any] = {
        "name": case_name,
        "outputs": dict(step_vars or {}),
    }

    # 用例级 variables 也带过去（有些用例把 id 写在 variables 里）
    variables = case.get("variables")
    if isinstance(variables, dict) and variables:
        outputs["variables"] = variables

    return outputs


def merge_dependencies_into_globals(
    dependency_outputs: dict[str, Any],
    global_vars: dict[str, Any],
) -> None:
    """把依赖用例的输出合并进全局变量。

    这样下游用例里写 ``{{创建订单.create.result.orderNo}}`` 就能取到。
    """
    for name, payload in (dependency_outputs or {}).items():
        if not isinstance(payload, dict):
            continue
        outputs = payload.get("outputs")
        if not isinstance(outputs, dict):
            continue
        global_vars[name] = outputs


__all__ = [
    "CaseNode",
    "DependencyError",
    "DependencyGraph",
    "DependencyStore",
    "begin_session",
    "build_graph_from_cases",
    "collect_case_outputs",
    "extract_dependencies",
    "get_result_max_age",
    "get_store",
    "get_wait_timeout",
    "has_session_plan",
    "is_active_case",
    "merge_dependencies_into_globals",
    "session_started_at",
]
