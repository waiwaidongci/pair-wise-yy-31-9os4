"""Compensation pricing policy: maintained independently of the ledger and UI.

A rate is keyed by (remedy_version, model, country) so a claim is always priced
against the plan the repair was performed under, the vehicle model and the
country captured at repair completion. When a rate changes, pending claims are
re-priced through `quote` again; claims already approved by the regulator keep
their locked amount.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from errors import ApiError


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def require_actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
    if not actor:
        raise ApiError(401, "缺少身份")
    if role not in allowed:
        raise ApiError(403, "角色无权执行此操作")
    return actor


class CompensationPolicy:
    def __init__(self, conn: sqlite3.Connection, audit):
        self.conn = conn
        self.audit = audit

    def init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS compensation_standards (
          remedy_version INTEGER NOT NULL, model TEXT NOT NULL, country TEXT NOT NULL,
          amount INTEGER NOT NULL CHECK(amount>=0), currency TEXT NOT NULL,
          updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(remedy_version,model,country)
        );
        """)
        self.conn.commit()

    def set_standard(self, actor: str | None, role: str | None, remedy_version: int, model: str,
                     country: str, amount: int, currency: str = "CNY") -> dict:
        actor = require_actor(actor, role, {"manufacturer", "regulator"})
        model, country, currency = model.strip(), country.strip().upper(), currency.strip().upper()
        if not model or not country:
            raise ApiError(400, "车型和国家不能为空")
        if int(amount) < 0:
            raise ApiError(400, "赔付金额不能为负")
        stamp = now()
        with self.conn:
            self.conn.execute("""INSERT INTO compensation_standards(remedy_version,model,country,amount,currency,updated_by,updated_at)
                                 VALUES(?,?,?,?,?,?,?)
                                 ON CONFLICT(remedy_version,model,country) DO UPDATE
                                 SET amount=excluded.amount,currency=excluded.currency,
                                     updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                              (int(remedy_version), model, country, int(amount), currency, actor, stamp))
            self.audit(actor, "standard.set", "compensation_standard",
                       f"{remedy_version}:{model}:{country}",
                       {"remedy_version": int(remedy_version), "model": model, "country": country,
                        "amount": int(amount), "currency": currency})
        return self.quote(remedy_version, model, country)

    def quote(self, remedy_version: int, model: str, country: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM compensation_standards WHERE remedy_version=? AND model=? AND country=?",
            (int(remedy_version), model.strip(), country.strip().upper())).fetchone()
        if not row:
            raise ApiError(409, f"缺少赔付口径：方案v{remedy_version}/车型{model}/{country.strip().upper()} 尚未配置标准")
        return {"remedy_version": int(row["remedy_version"]), "model": row["model"],
                "country": row["country"], "amount": int(row["amount"]),
                "currency": row["currency"], "updated_at": row["updated_at"]}

    def list_standards(self) -> list[dict]:
        return [{"remedy_version": int(r["remedy_version"]), "model": r["model"], "country": r["country"],
                 "amount": int(r["amount"]), "currency": r["currency"],
                 "updated_by": r["updated_by"], "updated_at": r["updated_at"]}
                for r in self.conn.execute(
                    "SELECT * FROM compensation_standards ORDER BY remedy_version,model,country")]
