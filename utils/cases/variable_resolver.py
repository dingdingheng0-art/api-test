"""变量解析：把 YAML 里的 ``{{ ... }}`` 表达式替换成真实值。

支持三种写法：

1. **取变量** —— ``{{mall4j.token}}``、``{{step1.data.id}}``、``{{step1.data.items[0].id}}``
   - 先在"本用例的步骤变量"里找，找不到再在"全局变量"里找
   - 想强制从全局取，用 ``{{Global.mall4j.token}}``
2. **调函数** —— ``{{tools.gen_order_no()}}``、``{{tools.add(a=1, b=2)}}``
   - 函数必须放在 ``utils/tools.py`` 里（也可写全 ``framework.tools.xxx``）
3. **纯文本拼接** —— ``"订单号={{step1.data.orderNo}} 已支付"``

参数解析用 ``ast`` 解析，不用 ``eval``，避免用例文件里出现任意代码执行。
"""
from __future__ import annotations

import ast
import importlib
import re
from typing import Any

from utils.logs.log_manager import log

_EXPR_RE = re.compile(r"\{\{(.*?)\}\}")
# 允许函数调用后面再跟切片：``tools.pluck(x, "date")[-7:]``
_FUNC_RE = re.compile(r"^([A-Za-z_][\w.]*)\((.*)\)$", re.DOTALL)
_FUNC_WITH_SLICE_RE = re.compile(
    r"^([A-Za-z_][\w.]*)\((.*)\)(\[[^\[\]]*\])$", re.DOTALL
)

# 内部哨兵：表示"这个路径在所有作用域里都没取到"（和 None 的含义不同）
_NOT_FOUND = object()


class VariableResolver:
    """解析 ``{{ }}`` 表达式。

    变量按这个顺序找：**参数变量 → 步骤变量 → 全局变量**。

    Args:
        session_vars: 本条用例执行过程中产生的变量（按 step id 存）。
        global_vars: 全局变量（配置、token 等）。
        param_vars: 当前参数组��变量（参数化时才有）。
    """

    def __init__(
        self,
        session_vars: dict[str, Any],
        global_vars: dict[str, Any],
        param_vars: dict[str, Any] | None = None,
    ):
        self.session_vars = session_vars
        self.global_vars = global_vars
        self.param_vars = param_vars or {}

    # ---------------- 对外入口 ----------------

    def process_data(self, data: Any) -> Any:
        """递归处理 dict / list / str 里的 ``{{ }}``。"""
        if isinstance(data, str):
            return self._process_string(data)
        if isinstance(data, dict):
            return {k: self.process_data(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self.process_data(v) for v in data]
        return data

    # ---------------- 内部实现 ----------------

    def _process_string(self, value: str) -> Any:
        matches = list(_EXPR_RE.finditer(value))
        if not matches:
            return value

        # 整串就是一个表达式时，保留原始类型（数字仍是数字）
        if len(matches) == 1 and matches[0].group(0) == value.strip():
            return self.resolve_expression(matches[0].group(1).strip())

        # 拼接场景：一次扫描替换完。
        #
        # 以前是「对每个匹配项做 value.replace(...)」—— 两个问题：
        #   1. ``replace`` 是**全局**替换，值一样的两处表达式会被替换两次
        #   2. 前一次替换的结果如果恰好又像 ``{{...}}``，后面会被二次求值
        # 用 re.sub + 回调，每个表达式严格求值一次。
        def _replace(match: re.Match[str]) -> str:
            return str(self.resolve_expression(match.group(1).strip()))

        return _EXPR_RE.sub(_replace, value)

    def resolve_expression(self, expression: str) -> Any:
        """解析单个表达式（不含 ``{{ }}``）。"""
        expression = expression.strip()
        if not expression:
            raise ValueError("变量表达式为空")

        func_match = _FUNC_RE.match(expression)
        if func_match:
            return self._call_function(func_match.group(1), func_match.group(2))

        # 函数调用 + 尾部切片：``tools.pluck(x, "date")[-7:]``
        slice_match = _FUNC_WITH_SLICE_RE.match(expression)
        if slice_match:
            value = self._call_function(slice_match.group(1), slice_match.group(2))
            return self._apply_slice(value, slice_match.group(3))

        if "." in expression or "[" in expression:
            return self._resolve_path(expression)
        # 单个变量名（参数变量常见写法：{{pageSize}}）
        if expression in self.param_vars:
            return self.param_vars[expression]
        if expression in self.session_vars:
            return self.session_vars[expression]
        if expression in self.global_vars:
            return self.global_vars[expression]
        raise ValueError(f"不支持的变量表达式: {expression}")

    def _call_function(self, dotted_name: str, args_str: str) -> Any:
        """调用 tools.py 里的函数。"""
        if "." not in dotted_name:
            raise ValueError(f"函数调用必须写成 模块名.函数名()，当前: {dotted_name}")

        module_name, func_name = dotted_name.rsplit(".", 1)
        # 去掉可能的包前缀，写全的也行（不写默认就是 utils.*）
        for prefix in ("framework.", "utils.", "apitest.framework.", "apitest."):
            if module_name.startswith(prefix):
                module_name = module_name[len(prefix) :]
                break
        module_name = f"utils.{module_name}"

        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ImportError(
                f"找不到模块 {module_name}"
                f"（自定义函数请写在 utils/tools.py）: {exc}"
            ) from exc

        func = getattr(module, func_name, None)
        if not callable(func):
            raise AttributeError(f"{module_name} 里没有可调用的 {func_name}")

        kwargs = self._parse_call_args(args_str, func)
        log.info("调用自定义函数 %s(%s)", func_name, kwargs)
        return func(**kwargs)

    def _parse_call_args(self, args_str: str, func: Any = None) -> dict[str, Any]:
        """把 ``a=1, b='x'`` 解析成 kwargs（用 ast，不用 eval）。

        参数值可以是：
        - 字面量：``1``、``'text'``、``True``
        - **变量名**：``override_field`` —— 取当前作用域（参数组 / 步骤变量 / 全局变量）的值
        """
        args_str = args_str.strip()
        if not args_str:
            return {}
        args_str = self._normalize_index_syntax(args_str)
        try:
            tree = ast.parse(f"f({args_str})", mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"函数参数写法不合法: {args_str!r} -> {exc}") from exc

        call = tree.body
        if not isinstance(call, ast.Call):
            raise ValueError(f"函数参数写法不合法: {args_str!r}")

        kwargs: dict[str, Any] = {}

        # 位置参数 -> 按函数签名顺序转成关键字参数
        if call.args:
            import inspect

            try:
                sig = inspect.signature(func)
                names = [
                    p.name
                    for p in sig.parameters.values()
                    if p.kind
                    in (inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD)
                ]
            except (TypeError, ValueError):
                names = []

            if len(call.args) > len(names):
                raise ValueError(
                    f"位置参数太多：给了 {len(call.args)} 个，函数只接受 {len(names)} 个"
                )

            for name, node in zip(names, call.args):
                kwargs[name] = self._eval_arg(node)

        for kw in call.keywords:
            if kw.arg is None:
                raise ValueError("不支持 **kwargs 形式的函数参数")
            kwargs[kw.arg] = self._eval_arg(kw.value)
        return kwargs

    @staticmethod
    def _normalize_index_syntax(args_str: str) -> str:
        """把 ``a.0.b`` 改写成 ``a[0].b``，让 ast 能解析。

        业务流用例里很自然地会写 ``goods_stats.result.0.goodsId``
        （第 0 条商品），但这不是合法 Python 语法，会让 ``ast.parse`` 直接报错。

        **只改写"属性位置上的数字"**，靠 tokenize 判断，不能用正则扫全文：

        - ``tools.find(records, "v1.0")`` —— 字符串里的 ``.0`` 不能动
        - ``tools.add(1.0, x.2)`` —— 浮点常量 ``1.0`` 不能动
        - ``tools.add(.0)`` —— 单独一个 ``.0`` 是浮点字面量，也不能动

        以前用的是 ``re.sub(r"\\.(\\d+)...")``，把字符串里的 ``v1.0`` 也改成了
        ``v1[0]``，函数参数直接变成另一个值 —— 而且报错信息完全看不出原因。
        """
        import io
        import tokenize

        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(args_str).readline))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            # 切不了就原样返回，让 ast.parse 去报一个更准确的语法错误
            return args_str

        lines = args_str.splitlines(keepends=True)
        # 记下要替换的位置（从后往前替换，前面的偏移量才不会被带偏）
        spans: list[tuple[int, int, int, int]] = []  # start_row, start_col, end_row, end_col

        prev: tokenize.TokenInfo | None = None
        for token in tokens:
            if (
                token.type == tokenize.NUMBER
                and re.fullmatch(r"\.\d+", token.string)
                and prev is not None
                # 前面必须是「取值」的位置：名字 / 右括号 / 右方括号
                and (
                    prev.type == tokenize.NAME
                    or prev.string in (")", "]")
                )
            ):
                spans.append((token.start[0], token.start[1], token.end[0], token.end[1]))
            if token.type not in (
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.COMMENT,
            ):
                prev = token

        if not spans:
            return args_str

        for start_row, start_col, end_row, end_col in reversed(spans):
            if start_row != end_row:  # pragma: no cover - tokenize 不会这样切
                continue
            line = lines[start_row - 1]
            index = int(lines[start_row - 1][start_col:end_col].lstrip("."))
            lines[start_row - 1] = (
                line[:start_col] + f"[{index}]" + line[end_col:]
            )
        return "".join(lines)

    def _eval_arg(self, node: Any) -> Any:
        """求一个函数参数节点的值。

        裸变量名（``ast.Name``）当成"取当前作用域的变量"，
        而不是当成未定义的常量。

        也支持嵌套调用：``md5(tools.login_field('mall4j'))``。
        """
        if isinstance(node, ast.Name):
            name = node.id
            for source in (self.param_vars, self.session_vars, self.global_vars):
                if name in source:
                    return source[name]
            # 找不到就当字符串（有些场景会传个标识符）
            return name

        # 形如 tools.login_field 的属性访问
        if isinstance(node, ast.Attribute):
            return self._eval_attr(node)

        # 嵌套的函数调用
        if isinstance(node, ast.Call):
            return self._eval_nested_call(node)

        # 切片：列表[:7] / 列表[-7:] —— 业务流里用来取"最近 N 天"
        if isinstance(node, ast.Subscript):
            return self._eval_subscript(node)

        if isinstance(node, ast.Constant):
            return node.value

        try:
            return ast.literal_eval(node)
        except (ValueError, SyntaxError):
            return node

    def _eval_attr(self, node: ast.Attribute) -> Any:
        """取 ``tools.xxx`` 这样的属性（指向 functions 模块里的函数）。

        也支持**步骤变量的多级路径**（``view_list.result.uvNum``）——
        这在业务流用例里很常用：把上一步的响应传给工具函数。
        """
        # 先把整条路径拆成"名字 + 下标"的序列，
        # 这样 ``a.b[0].c`` 和 ``a.b.0.c`` 都能处理
        segments: list[tuple[str, Any]] = []
        current: Any = node
        while True:
            if isinstance(current, ast.Attribute):
                segments.append(("attr", current.attr))
                current = current.value
            elif isinstance(current, ast.Subscript):
                segments.append(("index", current.slice))
                current = current.value
            elif isinstance(current, ast.Name):
                segments.append(("name", current.id))
                break
            else:
                return node

        segments.reverse()

        root = segments[0][1] if segments and segments[0][0] == "name" else None
        if root is None:
            return node

        # 不是 tools.xxx / framework.xxx / utils.xxx 开头，
        # 大概率是"步骤变量.字段"这种写法 —— 按变量路径解析
        if root not in ("tools", "framework", "utils", "apitest"):
            resolved = self._try_resolve_scope_path(segments)
            if resolved is not _NOT_FOUND:
                return resolved
            return node

        parts = [str(value) for kind, value in segments if kind in ("name", "attr")]

        # tools.xxx -> utils.tools.xxx
        if parts[0] == "tools":
            parts = ["utils"] + parts
        elif parts[0] == "framework":
            parts = ["framework"] + parts
        elif parts[0] == "utils":
            pass
        elif parts[0] == "apitest":
            # 兼容老写法：apitest.framework.tools -> utils.tools
            parts = parts[1:]
            if parts and parts[0] == "framework":
                parts = parts[1:]
                if parts and parts[0] == "tools":
                    parts = ["utils"] + parts

        try:
            # 最后一段是属性名，前面的是模块路径
            obj: Any = importlib.import_module(".".join(parts[:-1]))
            for attr in parts[-1:]:
                obj = getattr(obj, attr)
            return obj
        except (ImportError, AttributeError):
            return node

    def _try_resolve_scope_path(self, segments: list[tuple[str, Any]]) -> Any:
        """按 ``步骤变量.字段.子字段`` 解析；取不到返回 ``_NOT_FOUND``。

        变量来源顺序和 ``{{a.b}}`` 一致：参数变量 → 步骤变量 → 全局变量。

        Args:
            segments: ``[("name", "view_list"), ("attr", "result"), ("index", 0), ...]``
        """
        if not segments or segments[0][0] != "name":
            return _NOT_FOUND

        root = str(segments[0][1])
        for source in (self.param_vars, self.session_vars, self.global_vars):
            if root not in source:
                continue
            try:
                value: Any = source[root]
                for kind, key in segments[1:]:
                    if kind == "attr":
                        value = self._descend(value, str(key))
                    else:  # index
                        index = self._eval_arg(key) if not isinstance(key, int) else key
                        value = self._descend(value, str(index))
                return value
            except ValueError:
                continue
        return _NOT_FOUND

    def _eval_nested_call(self, node: ast.Call) -> Any:
        """求一个嵌套的函数调用（比如 ``md5(tools.xxx())``）。"""
        # 取出被调用的函数
        func = self._eval_arg(node.func)
        if not callable(func):
            raise ValueError(f"嵌套调用里的目标不可调用: {node.func!r}")

        # 解析参数
        kwargs: dict[str, Any] = {}
        args: list[Any] = []

        for arg_node in node.args:
            args.append(self._eval_arg(arg_node))
        for kw in node.keywords:
            if kw.arg is None:
                raise ValueError("不支持 **kwargs 形式的函数参数")
            kwargs[kw.arg] = self._eval_arg(kw.value)

        # 有位置参数就按签名对齐
        if args:
            import inspect

            try:
                sig = inspect.signature(func)
                names = [
                    p.name
                    for p in sig.parameters.values()
                    if p.kind
                    in (inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD)
                ]
                for name, value in zip(names, args):
                    kwargs[name] = value
            except (TypeError, ValueError):
                pass

        return func(**kwargs)

    def _apply_slice(self, value: Any, slice_text: str) -> Any:
        """对函数调用的结果做切片，比如 ``[-7:]``。

        用 ``ast`` 解析切片表达式（安全，不用 eval）。
        """
        if not isinstance(value, (list, tuple, str)):
            raise ValueError(f"不能对 {type(value).__name__} 做切片")

        try:
            node = ast.parse(f"x{slice_text}", mode="eval").body
        except SyntaxError as exc:
            raise ValueError(f"切片写法不合法: {slice_text!r} -> {exc}") from exc

        if not isinstance(node, ast.Subscript):
            raise ValueError(f"切片写法不合法: {slice_text!r}")

        index = node.slice
        if type(index).__name__ == "Index":  # pragma: no cover - 老版本 Python
            index = index.value  # type: ignore[attr-defined]

        if isinstance(index, ast.Slice):
            lower = self._eval_arg(index.lower) if index.lower else None
            upper = self._eval_arg(index.upper) if index.upper else None
            step = self._eval_arg(index.step) if index.step else None
            return list(value)[lower:upper:step]

        key = self._eval_arg(index)
        if isinstance(key, bool) or not isinstance(key, int):
            raise ValueError(f"下标必须是整数，收到 {key!r}")
        try:
            return value[key]
        except IndexError as exc:
            raise ValueError(f"下标越界: {key}，长度 {len(value)}") from exc

    def _eval_subscript(self, node: ast.Subscript) -> Any:
        """求切片表达式，比如 ``view_thirty.result["date"][-7:]``。

        业务流里常用它取"最近 N 天"这种片段：

        .. code-block:: yaml

            value: '{{tools.compare(tools.pluck(view_seven.result, "date"), tools.pluck(view_thirty.result, "date")[-7:])}}'
        """
        container = self._eval_arg(node.value)
        if not isinstance(container, (list, tuple, str)):
            raise ValueError(f"不能对 {type(container).__name__} 做切片")

        index = node.slice
        # Python 3.9+ 用 ast.Index 包装，更老版本才是直接表达式
        if type(index).__name__ == "Index":  # pragma: no cover - 老版本 Python
            index = index.value  # type: ignore[attr-defined]

        if isinstance(index, ast.Slice):
            lower = self._eval_arg(index.lower) if index.lower else None
            upper = self._eval_arg(index.upper) if index.upper else None
            step = self._eval_arg(index.step) if index.step else None
            return list(container)[lower:upper:step]

        # 单个下标
        key = self._eval_arg(index)
        if isinstance(key, bool) or not isinstance(key, int):
            raise ValueError(f"下标必须是整数，收到 {key!r}")
        try:
            return container[key]
        except IndexError as exc:
            raise ValueError(f"下标越界: {key}，长度 {len(container)}") from exc

    def _resolve_path(self, expression: str) -> Any:
        """按 ``a.b[0].c`` 取值。"""
        keys = [k for k in re.split(r"[.\[\]]", expression) if k]
        if not keys:
            raise ValueError(f"变量表达式无法解析: {expression}")

        root = keys[0]
        use_global = root in ("Global", "GLOBAL")
        use_params = root in ("params", "Params", "PARAM")
        start = 0

        if use_global:
            root = keys[1]
            start = 1
            value = self._lookup(root, self.global_vars, "全局变量")
        elif use_params:
            # {{params.xxx}} —— 强制从当前参数组取
            root = keys[1]
            start = 1
            value = self._lookup(root, self.param_vars, "参数变量")
        elif root in self.param_vars:
            value = self.param_vars[root]
        elif root in self.session_vars:
            value = self.session_vars[root]
        elif root in self.global_vars:
            value = self.global_vars[root]
        else:
            raise ValueError(
                f"变量 {root!r} 在参数变量、步骤变量和全局变量里都找不到"
            )

        for key in keys[start + 1 :]:
            value = self._descend(value, key)
        return value

    @staticmethod
    def _lookup(root: str, source: dict[str, Any], where: str) -> Any:
        if root not in source:
            raise ValueError(f"变量 {root!r} 在{where}里找不到")
        return source[root]

    @staticmethod
    def _descend(value: Any, key: str) -> Any:
        """在已取到的值上继续往下取一层。"""
        if isinstance(value, dict):
            if key not in value:
                raise ValueError(f"字段 {key!r} 不存在，现有字段: {list(value.keys())}")
            return value[key]
        if isinstance(value, (list, tuple)):
            if not key.isdigit():
                raise ValueError(f"列表只能用数字下标访问，收到 {key!r}")
            idx = int(key)
            if idx >= len(value):
                raise ValueError(f"列表下标越界: {idx}，长度 {len(value)}")
            return value[idx]
        raise ValueError(f"无法在 {type(value).__name__} 上取字段 {key!r}")
