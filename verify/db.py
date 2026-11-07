"""数据库校验。

支持两种驱动：

- **mysql**（默认）—— 用 ``mysql-connector-python``。线上库用这个。
- **sqlite** —— Python 自带，不用装任何东西。本地自测、单元测试用这个。

除了查数据，这里还提供"按条件删数据"的能力（给测试数据工厂的清理用）。

⚠️ **SQL 注入提醒**：这里的 ``query()`` 直接把 SQL 发给数据库，
不负责参数化。用例里的 SQL 是自己写的，没问题；
但**不要把用户输入直接拼进 query 字符串** —— 想传值就用 ``params``。

    db.query("SELECT status FROM orders WHERE id = %s", ("123",))
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Iterable, Sequence

from utils.logs.log_manager import log
from verify.errors import DependencyMissing, VerifyConfigError, VerifyError


def _sqlite_placeholders(sql: str) -> str:
    """把 ``%s`` 占位符换成 sqlite 的 ``?``。

    只换**引号外**的 ``%s`` —— ``LIKE '%s%'`` 里的是数据，不能动。
    ``%%``（转义成字面量百分号）也原样保留。
    """
    out: list[str] = []
    quote = ""
    i = 0
    length = len(sql)
    while i < length:
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            elif ch == "\\" and i + 1 < length:
                i += 1
                out.append(sql[i])
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "%" and i + 1 < length and sql[i + 1] == "s":
            out.append("?")
            i += 1
        else:
            out.append(ch)
        i += 1
    return "".join(out)


class DatabaseClient:
    """数据库客户端（懒连接，连接复用）。

    Args:
        config: 连接配置。至少要有 ``type``（``mysql`` / ``sqlite``）；
            mysql 还需要 ``host`` / ``user`` / ``database``；
            sqlite 只需要 ``database``（也可以不写，默认内存库）。
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = dict(config or {})
        self._connection: Any = None
        self._driver = str(self.config.get("type") or "mysql").lower()

    # ---------------- 连接管理 ----------------

    def _connect(self) -> Any:
        """建立连接（连一次就行，之后复用）。"""
        if self._connection is not None:
            return self._connection

        if self._driver == "sqlite":
            self._connection = sqlite3.connect(
                self.config.get("database") or ":memory:",
                detect_types=sqlite3.PARSE_DECLTYPES,
            )
            self._connection.row_factory = sqlite3.Row
        elif self._driver in ("mysql", "mariadb"):
            try:
                import mysql.connector
            except ImportError as exc:
                raise DependencyMissing(
                    "数据库校验需要 mysql-connector-python 包。"
                    "装一下：uv pip install mysql-connector-python"
                ) from exc

            params = {
                k: v
                for k, v in self.config.items()
                if k not in ("type", "params")
                and v is not None
            }
            # 允许把连接参数收在 config["params"] 里（和 yaml 结构对齐）
            params.update(self.config.get("params") or {})
            # ${PORT:3306} 这种占位符展开后是字符串，驱动要 int
            if "port" in params and params["port"] not in (None, ""):
                try:
                    params["port"] = int(params["port"])
                except (TypeError, ValueError):
                    raise VerifyConfigError(
                        f"数据库端口不是数字: {params['port']!r}"
                    ) from None
            params.setdefault("connection_timeout", 10)

            # ⚠️ 必须开 autocommit。
            #
            # mysql-connector 默认 autocommit=False，而 MySQL 默认隔离级别是
            # REPEATABLE-READ：连接不提交就一直停在同一个事务里，**读视图在
            # 第一次 SELECT 时就钉死了**。之后被测系统新写进去的数据（也就是
            # 用例刚造出来的那些）这条连接永远看不见。
            #
            # 实测过：A 连接先读一次 COUNT(*) 拿到 112，B 连接插入并提交，
            # A 再读还是 112，新开连接读到 113。查库断言会因此假失败 ——
            # 明明落库了，"验证落库"却说没有。
            #
            # 配置里显式写了 autocommit 就按配置来（个别场景要事务语义）。
            params.setdefault("autocommit", True)

            # 没配齐就别硬连：报错要说清楚缺什么、去哪个文件补，
            # 否则会变成一句 "Access denied" 或者连到 127.0.0.1 上超时。
            missing = [
                key
                for key in ("host", "user", "database")
                if not str(params.get(key) or "").strip()
            ]
            if missing:
                raise VerifyConfigError(
                    f"数据库配置不完整，缺少 {', '.join(missing)}。"
                    "在 .env 里补上 MALL4J_DB_HOST / MALL4J_DB_USER / "
                    "MALL4J_DB_PASSWORD / MALL4J_DB_NAME（参考 configs/.env.example）"
                )

            self._connection = mysql.connector.connect(**params)
        else:
            raise VerifyConfigError(
                f"不支持的数据库类型 {self._driver!r}（支持 mysql / sqlite）"
            )

        return self._connection

    def close(self) -> None:
        """关掉连接。"""
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # noqa: BLE001 - 关连接失败不用管
                pass
            self._connection = None

    def __enter__(self) -> "DatabaseClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ---------------- 查询 ----------------

    def query(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """执行一条查询，返回字典列表（每行一个 dict）。

        Args:
            sql: SQL 语句
            params: 位置参数（用 ``%s`` 占位）—— 有值一定要用这个，别拼字符串
        """
        connection = self._connect()
        cursor = connection.cursor()
        started = time.perf_counter()
        statement = self._adapt_sql(sql)
        try:
            if params:
                cursor.execute(statement, tuple(params))
            else:
                cursor.execute(statement)

            rows = cursor.fetchall()
            description = cursor.description or []
            columns = [d[0] for d in description]
        except Exception as exc:
            raise VerifyError(f"SQL 执行失败: {sql} -> {exc}") from exc
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001
                pass

        elapsed = int((time.perf_counter() - started) * 1000)
        result = [self._row_to_dict(row, columns) for row in rows]
        log.info("SQL %s -> %s 行 / %sms", sql, len(result), elapsed)
        return result

    def query_one(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> dict[str, Any] | None:
        """执行查询，只取第一行（没有就返回 None）。"""
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        column: Any = None,
    ) -> Any:
        """执行查询，取一个标量值。

        Args:
            column: 取第几列（0 开头）或列名；不给就取第一列
        """
        row = self.query_one(sql, params)
        if row is None:
            return None
        if column is None:
            return next(iter(row.values()), None)
        if isinstance(column, int) or (isinstance(column, str) and column.isdigit()):
            values = list(row.values())
            index = int(column)
            return values[index] if index < len(values) else None
        return row.get(str(column))

    # ---------------- 写入 ----------------

    def execute(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        commit: bool = True,
    ) -> int:
        """执行一条写语句（增删改），返回影响行数。"""
        connection = self._connect()
        cursor = connection.cursor()
        statement = self._adapt_sql(sql)
        try:
            if params:
                cursor.execute(statement, tuple(params))
            else:
                cursor.execute(statement)
            affected = cursor.rowcount
            if commit:
                connection.commit()
        except Exception as exc:
            try:
                connection.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise VerifyError(f"SQL 执行失败: {sql} -> {exc}") from exc
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001
                pass

        log.info("SQL %s -> 影响 %s 行", sql, affected)
        return int(affected if affected is not None else 0)

    def cleanup(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> int:
        """清理数据（删记录）。返回删掉的行数。

        清理失败**不抛异常** —— 清理是收尾动作，
        就算一条数据删不掉，也不该把已经跑完的用例判成失败。
        失败只记日志。
        """
        try:
            return self.execute(sql, params)
        except VerifyError as exc:
            log.warning("清理数据失败（已忽略）: %s", exc)
            return 0

    # ---------------- 内部 ----------------

    def _adapt_sql(self, sql: str) -> str:
        """按驱动把占位符统一成该驱动认识的写法。

        用例里一律写 ``%s``（mysql 的写法），sqlite 需要 ``?``。
        以前没做这层转换 —— 配了 sqlite 一执行就 ``near "%": syntax error``，
        只能怪到"数据库断言不好用"上。
        """
        if self._driver == "sqlite":
            return _sqlite_placeholders(sql)
        return sql

    @staticmethod
    def _row_to_dict(row: Any, columns: list[str]) -> dict[str, Any]:
        """把一行结果转成 dict（sqlite 的 Row 和 mysql 的 tuple 都能处理）。"""
        if isinstance(row, dict):
            return dict(row)
        try:
            return dict(row)
        except (TypeError, ValueError):
            return {columns[i]: row[i] for i in range(min(len(columns), len(row)))}


def build_clients(configs: Iterable[tuple[str, dict[str, Any]]]) -> dict[str, DatabaseClient]:
    """按 ``(别名, 配置)`` 造一批客户端。

    用例里可以用 ``alias`` 指定用哪个库（比如 ``test_db`` 和 ``prod_db``）。
    """
    return {alias: DatabaseClient(cfg) for alias, cfg in configs}
