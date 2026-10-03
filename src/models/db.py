import sqlite3
from typing import Any

class Database:
    """Async wrapper for Cloudflare D1 with fallback to local sqlite3 for unit tests."""

    def __init__(self, d1_binding: Any = None, sqlite_conn: sqlite3.Connection | None = None):
        self.d1 = d1_binding
        self.sqlite = sqlite_conn
        if self.sqlite:
            self.sqlite.row_factory = sqlite3.Row

    async def query_all(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        if self.d1 is not None:
            stmt = self.d1.prepare(sql)
            if params:
                stmt = stmt.bind(*params)
            res = await stmt.all()
            # D1 results may be Pyodide JsProxy or dict
            raw_results = getattr(res, "results", res)
            if hasattr(raw_results, "to_py"):
                raw_results = raw_results.to_py()
            return [dict(row) for row in raw_results]
        elif self.sqlite is not None:
            cursor = self.sqlite.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        raise RuntimeError("No database connection available (neither D1 nor sqlite3)")

    async def query_first(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None

    async def execute(self, sql: str, params: tuple | list = ()) -> None:
        if self.d1 is not None:
            stmt = self.d1.prepare(sql)
            if params:
                stmt = stmt.bind(*params)
            await stmt.run()
        elif self.sqlite is not None:
            cursor = self.sqlite.cursor()
            cursor.execute(sql, params)
            self.sqlite.commit()
        else:
            raise RuntimeError("No database connection available")

    async def execute_batch(self, statements: list[tuple[str, tuple | list]]) -> None:
        """Executes a list of (sql, params) queries."""
        if self.d1 is not None:
            prepared = []
            for sql, params in statements:
                stmt = self.d1.prepare(sql)
                if params:
                    stmt = stmt.bind(*params)
                prepared.append(stmt)
            await self.d1.batch(prepared)
        elif self.sqlite is not None:
            cursor = self.sqlite.cursor()
            for sql, params in statements:
                cursor.execute(sql, params)
            self.sqlite.commit()
        else:
            raise RuntimeError("No database connection available")
