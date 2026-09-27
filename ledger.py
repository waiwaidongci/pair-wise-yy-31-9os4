"""赔付账本：维修确认后生成赔付申请，监管确认前可重新核价，确认后金额锁定。

- 一张维修单只对应一笔赔付申请（claims.repair_id 唯一），驳回重提沿用同一笔；
- 每次生成/核价/审批/支付都追加 claim_versions，旧金额与驳回原因保留；
- 支付仅允许 approved -> paid 的原子迁移，同一维修单不会拿两次钱。

赔付口径在 standards.py、页面在 static/claims.html，与本模块分开维护。
"""
from __future__ import annotations

import sqlite3

from core import ApiError, j, now
from standards import StandardBook

PENDING, APPROVED, REJECTED, PAID = "pending", "approved", "rejected", "paid"
REPRICEABLE = (PENDING, REJECTED)  # 监管确认前都可以重新核价


class ClaimLedger:
    def __init__(self, conn: sqlite3.Connection, standards: StandardBook):
        self.conn, self.standards = conn, standards

    def init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
          entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS claims (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          repair_id INTEGER NOT NULL UNIQUE REFERENCES repairs(id),
          recall_id INTEGER NOT NULL REFERENCES recalls(id),
          dealer_id INTEGER NOT NULL REFERENCES dealers(id),
          vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
          state TEXT NOT NULL CHECK(state IN ('pending','approved','rejected','paid')),
          remedy_version INTEGER NOT NULL,
          model TEXT NOT NULL,
          country TEXT NOT NULL,
          standard_version INTEGER NOT NULL,
          amount_cents INTEGER NOT NULL CHECK(amount_cents>=0),
          currency TEXT NOT NULL DEFAULT '',
          version INTEGER NOT NULL DEFAULT 1,
          revision INTEGER NOT NULL DEFAULT 1,
          submitted_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          decided_by TEXT, decided_at TEXT, decision_note TEXT, paid_at TEXT
        );
        CREATE TABLE IF NOT EXISTS claim_versions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          claim_id INTEGER NOT NULL REFERENCES claims(id),
          claim_version INTEGER NOT NULL,
          event TEXT NOT NULL,
          state TEXT NOT NULL,
          amount_cents INTEGER NOT NULL,
          currency TEXT NOT NULL DEFAULT '',
          standard_version INTEGER NOT NULL,
          reason TEXT NOT NULL DEFAULT '',
          actor TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        self.conn.commit()

    @staticmethod
    def _actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
        if not actor: raise ApiError(401, "缺少身份")
        if role not in allowed: raise ApiError(403, "角色无权执行此操作")
        return actor

    def _row(self, table: str, identity: object, column: str = "id") -> sqlite3.Row:
        row = self.conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (identity,)).fetchone()
        if not row: raise ApiError(404, "对象不存在")
        return row

    def _audit(self, actor: str, action: str, entity_type: str, entity_id: object, details: dict) -> None:
        self.conn.execute("INSERT INTO audit_log(at,actor,action,entity_type,entity_id,details_json) VALUES(?,?,?,?,?,?)",
                          (now(), actor, action, entity_type, str(entity_id), j(details)))

    def _record(self, claim_id: int, event: str, state: str, amount_cents: int, currency: str,
                standard_version: int, reason: str, actor: str) -> None:
        claim_version = self.conn.execute("SELECT version FROM claims WHERE id=?", (claim_id,)).fetchone()["version"]
        self.conn.execute("""INSERT INTO claim_versions(claim_id,claim_version,event,state,amount_cents,currency,standard_version,reason,actor,created_at)
                             VALUES(?,?,?,?,?,?,?,?,?,?)""",
                          (claim_id, claim_version, event, state, amount_cents, currency, standard_version, reason, actor, now()))

    def _price(self, remedy_version: int, model: str, country: str) -> tuple[int, int, str]:
        std = self.standards.current(remedy_version, model, country)
        return (std["amount_cents"], std["version"], std["currency"]) if std else (0, 0, "")

    # ---- 生成与核价 ----

    def create_claim_for_repair(self, repair_id: int, actor: str) -> dict:
        """维修确认后生成赔付申请；同一维修单幂等，不会生成第二笔。"""
        repair = self._row("repairs", repair_id)
        if repair["status"] != "confirmed": raise ApiError(409, "只有已确认维修可以生成赔付申请")
        existing = self.conn.execute("SELECT id FROM claims WHERE repair_id=?", (repair_id,)).fetchone()
        if existing: return self.get_claim(existing["id"])
        vehicle = self._row("vehicles", repair["vehicle_id"])
        dealer = self._row("dealers", repair["dealer_id"])
        # 按完成时的方案版本、车型和维修网点所在国家留存口径
        amount, std_ver, currency = self._price(repair["remedy_version"], vehicle["model"], dealer["country"])
        stamp = now()
        try:
            with self.conn:
                cur = self.conn.execute("""INSERT INTO claims(repair_id,recall_id,dealer_id,vehicle_id,state,remedy_version,model,country,
                                           standard_version,amount_cents,currency,version,revision,submitted_by,created_at,updated_at)
                                           VALUES(?,?,?,?,?,?,?,?,?,?,?,1,1,?,?,?)""",
                                        (repair_id, repair["recall_id"], repair["dealer_id"], repair["vehicle_id"], PENDING,
                                         repair["remedy_version"], vehicle["model"], dealer["country"], std_ver, amount, currency,
                                         repair["reported_by"], stamp, stamp))
                claim_id = cur.lastrowid
                self._record(claim_id, "created", PENDING, amount, currency, std_ver,
                             "" if std_ver else "缺少赔付标准，待定价", actor)
                self._audit(actor, "claim.create", "claim", claim_id,
                            {"repair_id": repair_id, "amount_cents": amount, "standard_version": std_ver})
        except sqlite3.IntegrityError:
            row = self.conn.execute("SELECT id FROM claims WHERE repair_id=?", (repair_id,)).fetchone()
            if row: return self.get_claim(row["id"])
            raise
        return self.get_claim(claim_id)

    def _apply_reprice(self, claim: dict, actor: str, reason: str, force: bool = False) -> bool:
        amount, std_ver, currency = self._price(claim["remedy_version"], claim["model"], claim["country"])
        if not force and amount == claim["amount_cents"] and std_ver == claim["standard_version"]:
            return False
        self.conn.execute("UPDATE claims SET amount_cents=?,standard_version=?,currency=?,revision=revision+1,updated_at=? WHERE id=?",
                          (amount, std_ver, currency, now(), claim["id"]))
        self._record(claim["id"], "repriced", claim["state"], amount, currency, std_ver, reason, actor)
        self._audit(actor, "claim.reprice", "claim", claim["id"],
                    {"old_amount_cents": claim["amount_cents"], "new_amount_cents": amount, "reason": reason})
        return True

    def reprice_for_standard(self, remedy_version: int, model: str, country: str, actor: str) -> list[int]:
        """口径变更：重核所有尚未监管确认的匹配申请。"""
        rows = self.conn.execute("SELECT * FROM claims WHERE remedy_version=? AND model=? AND country=? AND state IN (?,?)",
                                 (remedy_version, model, country, PENDING, REJECTED)).fetchall()
        changed = []
        with self.conn:
            for row in rows:
                if self._apply_reprice(dict(row), actor, "赔付标准变更，重新核价"):
                    changed.append(row["id"])
        return changed

    def reprice_for_repair(self, repair_id: int, actor: str, reason: str) -> int | None:
        """维修单据变更：重核对应申请；已确认/已支付则不允许改单据。"""
        claim = self.conn.execute("SELECT * FROM claims WHERE repair_id=?", (repair_id,)).fetchone()
        if not claim: return None
        if claim["state"] not in REPRICEABLE:
            raise ApiError(409, "赔付已确认，维修单据不可更改")
        with self.conn:
            self._apply_reprice(dict(claim), actor, reason, force=True)
        return claim["id"]

    # ---- 口径维护（联动重核） ----

    def set_standard(self, actor: str | None, role: str | None, remedy_version: int, model: str, country: str,
                     amount_cents: int, currency: str = "CNY", note: str = "") -> dict:
        self._actor(actor, role, {"manufacturer"})
        std = self.standards.set_standard(actor, remedy_version, model, country, amount_cents, currency, note)
        changed = self.reprice_for_standard(std["remedy_version"], std["model"], std["country"], actor)
        with self.conn:
            self._audit(actor, "standard.set", "standard", std["id"],
                        {"key": [std["remedy_version"], std["model"], std["country"]],
                         "version": std["version"], "amount_cents": std["amount_cents"], "repriced": changed})
        return {"standard": std, "repriced_claim_ids": changed}

    def list_standards(self) -> list[dict]:
        return self.standards.list_all()

    # ---- 审批、重提、支付 ----

    def review_claim(self, actor: str | None, role: str | None, claim_id: int, decision: str, note: str = "") -> dict:
        self._actor(actor, role, {"regulator"})
        if decision not in {"approve", "reject"}: raise ApiError(400, "决定只能是 approve 或 reject")
        claim = self._row("claims", claim_id)
        if claim["state"] != PENDING: raise ApiError(409, "只有待核申请可以审批")
        if decision == "reject" and not note.strip(): raise ApiError(400, "驳回必须填写原因")
        if decision == "approve" and int(claim["standard_version"]) == 0:
            raise ApiError(409, "缺少赔付标准，不能确认")
        state = APPROVED if decision == "approve" else REJECTED
        stamp = now()
        with self.conn:
            cur = self.conn.execute("""UPDATE claims SET state=?,decided_by=?,decided_at=?,decision_note=?,revision=revision+1,updated_at=?
                                       WHERE id=? AND state=?""", (state, actor, stamp, note, stamp, claim_id, PENDING))
            if cur.rowcount != 1: raise ApiError(409, "并发更新冲突")
            self._record(claim_id, state, state, claim["amount_cents"], claim["currency"], claim["standard_version"], note, actor)
            self._audit(actor, f"claim.{state}", "claim", claim_id, {"note": note, "amount_cents": claim["amount_cents"]})
        return self.get_claim(claim_id)

    def resubmit(self, actor: str | None, role: str | None, claim_id: int, note: str = "") -> dict:
        """驳回后更正重提：沿用同一笔申请，按当前口径重新核价，旧金额与原因留在历史里。"""
        self._actor(actor, role, {"dealer"})
        claim = self._row("claims", claim_id)
        if claim["state"] != REJECTED: raise ApiError(409, "只有被驳回的申请可以更正重提")
        if claim["submitted_by"] != actor: raise ApiError(403, "只能重提本网点提交的申请")
        amount, std_ver, currency = self._price(claim["remedy_version"], claim["model"], claim["country"])
        stamp = now()
        with self.conn:
            cur = self.conn.execute("""UPDATE claims SET state=?,amount_cents=?,standard_version=?,currency=?,version=version+1,revision=revision+1,
                                       decided_by=NULL,decided_at=NULL,decision_note=NULL,updated_at=? WHERE id=? AND state=?""",
                                    (PENDING, amount, std_ver, currency, stamp, claim_id, REJECTED))
            if cur.rowcount != 1: raise ApiError(409, "并发更新冲突")
            self._record(claim_id, "resubmitted", PENDING, amount, currency, std_ver, note or "更正后重新提交", actor)
            self._audit(actor, "claim.resubmit", "claim", claim_id, {"note": note, "amount_cents": amount})
        return self.get_claim(claim_id)

    def pay(self, actor: str | None, role: str | None, claim_id: int) -> dict:
        self._actor(actor, role, {"manufacturer"})
        claim = self._row("claims", claim_id)
        stamp = now()
        with self.conn:
            cur = self.conn.execute("UPDATE claims SET state=?,paid_at=?,revision=revision+1,updated_at=? WHERE id=? AND state=?",
                                    (PAID, stamp, stamp, claim_id, APPROVED))
            if cur.rowcount != 1: raise ApiError(409, "只有已确认未支付的申请可以支付")
            self._record(claim_id, "paid", PAID, claim["amount_cents"], claim["currency"], claim["standard_version"], "赔付完成", actor)
            self._audit(actor, "claim.pay", "claim", claim_id,
                        {"repair_id": claim["repair_id"], "amount_cents": claim["amount_cents"]})
        return self.get_claim(claim_id)

    # ---- 查询 ----

    def _brief(self, claim: dict) -> dict:
        vehicle = self._row("vehicles", claim["vehicle_id"])
        dealer = self._row("dealers", claim["dealer_id"])
        recall = self._row("recalls", claim["recall_id"])
        return {**claim, "vin": vehicle["vin"], "dealer_code": dealer["code"],
                "dealer_name": dealer["name"], "campaign_code": recall["campaign_code"]}

    def get_claim(self, claim_id: int) -> dict:
        claim = self._brief(dict(self._row("claims", claim_id)))
        claim["versions"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM claim_versions WHERE claim_id=? ORDER BY id", (claim_id,))]
        return claim

    def list_claims(self, dealer_id: int | None = None, state: str = "") -> list[dict]:
        sql, conds, args = "SELECT * FROM claims", [], []
        if dealer_id is not None: conds.append("dealer_id=?"); args.append(dealer_id)
        if state:
            if state not in (PENDING, APPROVED, REJECTED, PAID): raise ApiError(400, "状态无效")
            conds.append("state=?"); args.append(state)
        if conds: sql += " WHERE " + " AND ".join(conds)
        return [self._brief(dict(r)) for r in self.conn.execute(sql + " ORDER BY id", args)]

    def summary(self, dealer_id: int | None = None) -> dict:
        """按网点汇总：可赔（已确认）、待核、争议（被驳回）、已付。"""
        dealers = [self._row("dealers", dealer_id)] if dealer_id is not None else \
            list(self.conn.execute("SELECT * FROM dealers ORDER BY id"))
        claims = self.list_claims()
        buckets = []
        for d in dealers:
            mine = [c for c in claims if c["dealer_id"] == d["id"]]
            def total(state: str) -> int: return sum(c["amount_cents"] for c in mine if c["state"] == state)
            def count(state: str) -> int: return sum(1 for c in mine if c["state"] == state)
            buckets.append({"dealer_id": d["id"], "code": d["code"], "name": d["name"], "country": d["country"],
                            "payable_cents": total(APPROVED), "pending_cents": total(PENDING),
                            "disputed_cents": total(REJECTED), "paid_cents": total(PAID),
                            "payable_count": count(APPROVED), "pending_count": count(PENDING),
                            "disputed_count": count(REJECTED), "paid_count": count(PAID),
                            "claims": mine})
        totals = {k: sum(b[k] for b in buckets)
                  for k in ("payable_cents", "pending_cents", "disputed_cents", "paid_cents")}
        return {"dealers": buckets, "totals": totals}
