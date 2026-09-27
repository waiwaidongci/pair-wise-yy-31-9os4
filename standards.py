"""赔付口径：按（方案版本, 车型, 国家）维护赔付标准。

口径调整只追加新版本，旧版本保留可审计；核价永远读取该口径的当前版本。
账本在 ledger.py、页面在 static/claims.html，与本模块分开维护。
"""
from __future__ import annotations

import sqlite3

from core import ApiError, now


class StandardBook:
    """赔付标准的存取与版本管理。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS comp_standards (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          remedy_version INTEGER NOT NULL,
          model TEXT NOT NULL,
          country TEXT NOT NULL,
          version INTEGER NOT NULL,
          amount_cents INTEGER NOT NULL CHECK(amount_cents>=0),
          currency TEXT NOT NULL,
          note TEXT NOT NULL DEFAULT '',
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(remedy_version,model,country,version)
        );
        """)
        self.conn.commit()

    def set_standard(self, actor: str, remedy_version: int, model: str, country: str,
                     amount_cents: int, currency: str = "CNY", note: str = "") -> dict:
        model, country = model.strip(), country.strip().upper()
        currency = (currency or "CNY").strip().upper()
        if not model or not country: raise ApiError(400, "车型和国家不能为空")
        amount_cents = int(amount_cents)
        if amount_cents < 0: raise ApiError(400, "赔付金额不能为负")
        with self.conn:
            row = self.conn.execute("SELECT MAX(version) AS v FROM comp_standards WHERE remedy_version=? AND model=? AND country=?",
                                    (int(remedy_version), model, country)).fetchone()
            version = (row["v"] or 0) + 1
            cur = self.conn.execute("""INSERT INTO comp_standards(remedy_version,model,country,version,amount_cents,currency,note,created_by,created_at)
                                       VALUES(?,?,?,?,?,?,?,?,?)""",
                                    (int(remedy_version), model, country, version, amount_cents, currency, note, actor, now()))
        return self._dict(self.conn.execute("SELECT * FROM comp_standards WHERE id=?", (cur.lastrowid,)).fetchone())

    def current(self, remedy_version: int, model: str, country: str) -> dict | None:
        row = self.conn.execute("""SELECT * FROM comp_standards WHERE remedy_version=? AND model=? AND country=?
                                   ORDER BY version DESC LIMIT 1""", (remedy_version, model, country)).fetchone()
        return self._dict(row) if row else None

    def list_all(self) -> list[dict]:
        return [self._dict(r) for r in self.conn.execute(
            "SELECT * FROM comp_standards ORDER BY remedy_version,model,country,version DESC")]

    @staticmethod
    def _dict(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "remedy_version": row["remedy_version"], "model": row["model"],
                "country": row["country"], "version": row["version"], "amount_cents": row["amount_cents"],
                "currency": row["currency"], "note": row["note"], "created_by": row["created_by"],
                "created_at": row["created_at"]}
