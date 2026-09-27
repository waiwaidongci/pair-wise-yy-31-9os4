"""Compensation ledger: maintained independently of pricing policy and UI.

One confirmed repair maps to exactly one compensation claim (UNIQUE(repair_id),
no second payout). The claim snapshots the remedy version, model and vehicle
country at repair completion; re-pricing never moves that basis, only the
amount is refreshed. Every repricing, rejection, correction and payment is
appended to claim_revisions, so old amounts and rejection reasons survive
resubmission.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from errors import ApiError
from pricing import require_actor


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"


class CompensationLedger:
    def __init__(self, conn: sqlite3.Connection, audit, policy):
        self.conn = conn
        self.audit = audit
        self.policy = policy

    def init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS compensation_claims (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          repair_id INTEGER NOT NULL UNIQUE REFERENCES repairs(id),
          recall_id INTEGER NOT NULL, vehicle_id INTEGER NOT NULL, dealer_id INTEGER NOT NULL,
          vin TEXT NOT NULL,
          remedy_version INTEGER NOT NULL, model TEXT NOT NULL, country TEXT NOT NULL,
          amount INTEGER NOT NULL, currency TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
          evidence_hash TEXT NOT NULL,
          decided_by TEXT, decided_at TEXT, reject_reason TEXT,
          submitted_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          settled_by TEXT, settled_at TEXT
        );
        CREATE TABLE IF NOT EXISTS claim_revisions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          claim_id INTEGER NOT NULL REFERENCES compensation_claims(id),
          revision INTEGER NOT NULL,
          event TEXT NOT NULL,
          amount_before INTEGER, amount_after INTEGER NOT NULL,
          actor TEXT NOT NULL, reason TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(claim_id,revision)
        );
        """)
        self.conn.commit()

    # ---- creation: triggered when a repair is confirmed --------------------

    def on_repair_confirmed(self, actor: str, repair: sqlite3.Row) -> dict:
        """Create the single compensation claim for a confirmed repair.

        Idempotent: a confirmed repair already carrying a claim is returned as-is.
        """
        existing = self.conn.execute("SELECT * FROM compensation_claims WHERE repair_id=?",
                                     (repair["id"],)).fetchone()
        if existing:
            return self.claim_detail(existing["id"])
        if repair["status"] != "confirmed":
            raise ApiError(409, "只有已确认维修单才能生成赔付申请")
        vehicle = self._row("vehicles", repair["vehicle_id"])
        quote = self.policy.quote(repair["remedy_version"], vehicle["model"], vehicle["country"])
        stamp = now()
        with self.conn:
            cur = self.conn.execute("""INSERT INTO compensation_claims(
                repair_id,recall_id,vehicle_id,dealer_id,vin,remedy_version,model,country,
                amount,currency,status,evidence_hash,submitted_by,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?, 'pending',?,?,?,?)""",
                (repair["id"], repair["recall_id"], vehicle["id"], repair["dealer_id"], vehicle["vin"],
                 quote["remedy_version"], quote["model"], quote["country"], quote["amount"],
                 quote["currency"], repair["evidence_hash"], actor, stamp, stamp))
            self._append(cur.lastrowid, 1, "created", None, quote["amount"], actor,
                         "维修确认时按完成时方案/车型/国家核价", stamp)
            self.audit(actor, "claim.create", "compensation_claim", cur.lastrowid,
                       {"repair_id": repair["id"], "dealer_id": repair["dealer_id"],
                        "amount": quote["amount"], "currency": quote["currency"]})
        return self.claim_detail(cur.lastrowid)

    # ---- re-pricing while the regulator has not decided --------------------

    def reprice_pending(self, actor: str, reason: str, repair_id: int | None = None) -> int:
        """Re-quote pending claims under the current standards.

        Triggered when the pricing policy changes (all pending claims) or when a
        repair document changes (single claim). Approved claims keep their locked
        rate; rejected claims keep the disputed amount until the dealer resubmits
        (resubmit_claim re-prices at that point).
        """
        query = "SELECT * FROM compensation_claims WHERE status='pending'"
        params: tuple = ()
        if repair_id is not None:
            query += " AND repair_id=?"
            params = (repair_id,)
        count = 0
        for claim in self.conn.execute(query, params).fetchall():
            self._reprice_one(claim, actor, reason)
            count += 1
        return count

    def _reprice_one(self, claim: sqlite3.Row, actor: str, reason: str) -> dict:
        quote = self.policy.quote(claim["remedy_version"], claim["model"], claim["country"])
        old_amount, new_amount = int(claim["amount"]), quote["amount"]
        stamp = now()
        with self.conn:
            if old_amount != new_amount:
                self.conn.execute("UPDATE compensation_claims SET amount=?,currency=?,updated_at=? WHERE id=?",
                                  (new_amount, quote["currency"], stamp, claim["id"]))
            self._append(claim["id"], self._next_revision(claim["id"]),
                         "repriced", old_amount, new_amount, actor,
                         reason if old_amount != new_amount else f"{reason}（金额未变）", stamp)
            self.audit(actor, "claim.reprice", "compensation_claim", claim["id"],
                       {"old_amount": old_amount, "new_amount": new_amount, "reason": reason})
        return self.claim_detail(claim["id"])

    def amend_repair_documents(self, actor: str | None, role: str | None, repair_id: int,
                               evidence_hash: str) -> dict:
        """Dealer corrects the document behind a repair; pending claim re-prices."""
        actor = require_actor(actor, role, {"dealer"})
        if not evidence_hash.strip():
            raise ApiError(400, "单据哈希不能为空")
        repair = self._row("repairs", repair_id)
        if repair["status"] != "confirmed":
            raise ApiError(409, "只有已确认维修单可以更正单据")
        claim = self.conn.execute("SELECT * FROM compensation_claims WHERE repair_id=?",
                                  (repair_id,)).fetchone()
        if not claim:
            raise ApiError(404, "该维修单没有赔付申请")
        if claim["status"] == "approved":
            raise ApiError(409, "监管已确认，不能再更改单据")
        with self.conn:
            self.conn.execute("UPDATE repairs SET evidence_hash=? WHERE id=?",
                              (evidence_hash.strip(), repair_id))
            self.conn.execute("UPDATE compensation_claims SET evidence_hash=?,updated_at=? WHERE id=?",
                              (evidence_hash.strip(), now(), claim["id"]))
            self.audit(actor, "repair.amend_documents", "repair", repair_id,
                       {"old_hash": repair["evidence_hash"], "new_hash": evidence_hash.strip()})
        self.reprice_pending(actor, "维修单据变化，重新核价", repair_id=repair_id)
        return self.claim_detail(claim["id"])

    # ---- regulator decision and dealer resubmission ------------------------

    def review_claim(self, actor: str | None, role: str | None, claim_id: int,
                     decision: str, note: str = "") -> dict:
        actor = require_actor(actor, role, {"regulator"})
        if decision not in {"approve", "reject"}:
            raise ApiError(400, "决定只能是 approve 或 reject")
        claim = self._row("compensation_claims", claim_id)
        if claim["status"] != PENDING:
            raise ApiError(409, "只有待核申请可以监管确认")
        if decision == "reject" and not note.strip():
            raise ApiError(400, "驳回必须填写原因")
        stamp = now()
        with self.conn:
            self.conn.execute("""UPDATE compensation_claims
                                 SET status=?,decided_by=?,decided_at=?,reject_reason=?,updated_at=?
                                 WHERE id=?""",
                              (APPROVED if decision == "approve" else REJECTED, actor, stamp,
                               note.strip() if decision == "reject" else None, stamp, claim_id))
            event = "approved" if decision == "approve" else "rejected"
            self._append(claim_id, self._next_revision(claim_id), event,
                         int(claim["amount"]), int(claim["amount"]), actor,
                         note.strip() or "监管确认通过", stamp)
            self.audit(actor, f"claim.{event}", "compensation_claim", claim_id,
                       {"amount": int(claim["amount"]), "note": note.strip()})
        return self.claim_detail(claim_id)

    def resubmit_claim(self, actor: str | None, role: str | None, claim_id: int,
                       note: str = "", evidence_hash: str = "") -> dict:
        """A rejected claim can be corrected and resubmitted by the dealer.

        Re-priced under the current standards; prior amount and rejection reason
        stay in claim_revisions.
        """
        actor = require_actor(actor, role, {"dealer"})
        claim = self._row("compensation_claims", claim_id)
        if claim["status"] != REJECTED:
            raise ApiError(409, "只有被驳回的申请可以更正重提")
        stamp = now()
        with self.conn:
            self.conn.execute("""UPDATE compensation_claims
                                 SET status='pending',decided_by=NULL,decided_at=NULL,
                                     reject_reason=NULL,evidence_hash=COALESCE(NULLIF(?,''),evidence_hash),
                                     updated_at=? WHERE id=?""",
                              (evidence_hash.strip(), stamp, claim_id))
            self._append(claim_id, self._next_revision(claim_id), "resubmitted",
                         int(claim["amount"]), int(claim["amount"]), actor,
                         note.strip() or f"网点更正重提（上次驳回原因留档）", stamp)
            self.audit(actor, "claim.resubmit", "compensation_claim", claim_id,
                       {"evidence_hash": evidence_hash.strip() or claim["evidence_hash"], "note": note.strip()})
        return self._reprice_one(self._row("compensation_claims", claim_id), actor,
                                 "驳回重提，按当前口径重新核价")

    def settle_claim(self, actor: str | None, role: str | None, claim_id: int) -> dict:
        """Pay an approved claim exactly once (duplicate payout guard)."""
        actor = require_actor(actor, role, {"manufacturer"})
        claim = self._row("compensation_claims", claim_id)
        if claim["status"] != APPROVED:
            raise ApiError(409, "只有监管确认的申请可以付款")
        if claim["settled_at"]:
            raise ApiError(409, "该维修单已经付过款，不能重复赔付")
        stamp = now()
        with self.conn:
            cur = self.conn.execute(
                "UPDATE compensation_claims SET settled_by=?,settled_at=? WHERE id=? AND settled_at IS NULL",
                (actor, stamp, claim_id))
            if cur.rowcount != 1:
                raise ApiError(409, "该维修单已经付过款，不能重复赔付")
            self._append(claim_id, self._next_revision(claim_id), "settled",
                         int(claim["amount"]), int(claim["amount"]), actor, "厂家付款", stamp)
            self.audit(actor, "claim.settle", "compensation_claim", claim_id,
                       {"repair_id": claim["repair_id"], "amount": int(claim["amount"])})
        return self.claim_detail(claim_id)

    # ---- views --------------------------------------------------------------

    def claim_detail(self, claim_id: int) -> dict:
        claim = self._row("compensation_claims", claim_id)
        d = self._claim_dict(claim)
        d["revisions"] = [self._revision_dict(r)
                          for r in self.conn.execute("SELECT * FROM claim_revisions WHERE claim_id=? ORDER BY id",
                                                     (claim_id,))]
        return d

    def dealer_summary(self, actor: str | None, role: str | None, dealer_id: int | None = None) -> dict:
        require_actor(actor, role, {"dealer", "manufacturer", "regulator"})
        where, params = "", []
        if dealer_id is not None:
            where, params = "WHERE c.dealer_id=?", [dealer_id]
        rows = self.conn.execute(
            f"SELECT c.* FROM compensation_claims c {where} ORDER BY c.dealer_id,c.id", params).fetchall()
        dealers: dict[int, dict] = {}
        for row in rows:
            bucket = dealers.setdefault(int(row["dealer_id"]), self._empty_bucket(int(row["dealer_id"])))
            bucket["claims"].append(self._claim_dict(row))
            amount = int(row["amount"])
            if row["status"] == APPROVED:
                bucket["approved_total"] += amount
                bucket["currency"] = row["currency"]
                if row["settled_at"]:
                    bucket["paid_total"] += amount
            elif row["status"] == PENDING:
                bucket["pending_total"] += amount
            else:
                bucket["disputed_total"] += amount
        result = sorted(dealers.values(), key=lambda b: b["dealer_id"])
        for bucket in result:
            bucket["net_payable"] = bucket["approved_total"] - bucket["paid_total"]
        return {"generated_at": now(), "dealers": result}

    # ---- helpers ------------------------------------------------------------

    def _empty_bucket(self, dealer_id: int) -> dict:
        dealer = self._row("dealers", dealer_id)
        return {"dealer_id": dealer_id, "dealer_code": dealer["code"], "dealer_name": dealer["name"],
                "country": dealer["country"], "approved_total": 0, "pending_total": 0,
                "disputed_total": 0, "paid_total": 0, "net_payable": 0, "currency": "", "claims": []}

    def _claim_dict(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "repair_id": row["repair_id"], "recall_id": row["recall_id"],
                "vin": row["vin"], "dealer_id": row["dealer_id"],
                "remedy_version": row["remedy_version"], "model": row["model"], "country": row["country"],
                "amount": int(row["amount"]), "currency": row["currency"], "status": row["status"],
                "evidence_hash": row["evidence_hash"], "decided_by": row["decided_by"],
                "decided_at": row["decided_at"], "reject_reason": row["reject_reason"],
                "submitted_by": row["submitted_by"], "created_at": row["created_at"],
                "updated_at": row["updated_at"], "settled_by": row["settled_by"],
                "settled_at": row["settled_at"],
                "paid": bool(row["settled_at"])}

    def _revision_dict(self, row: sqlite3.Row) -> dict:
        return {"revision": int(row["revision"]), "event": row["event"],
                "amount_before": None if row["amount_before"] is None else int(row["amount_before"]),
                "amount_after": int(row["amount_after"]), "actor": row["actor"],
                "reason": row["reason"], "created_at": row["created_at"]}

    def _append(self, claim_id: int, revision: int, event: str, amount_before: int | None,
                amount_after: int, actor: str, reason: str, stamp: str) -> None:
        self.conn.execute("""INSERT INTO claim_revisions(claim_id,revision,event,amount_before,amount_after,actor,reason,created_at)
                             VALUES(?,?,?,?,?,?,?,?)""",
                          (claim_id, revision, event, amount_before, amount_after, actor, reason, stamp))

    def _next_revision(self, claim_id: int) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(revision),0)+1 AS n FROM claim_revisions WHERE claim_id=?",
                                (claim_id,)).fetchone()
        return int(row["n"])

    def _row(self, table: str, identity: object, column: str = "id") -> sqlite3.Row:
        row = self.conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (identity,)).fetchone()
        if not row:
            raise ApiError(404, "对象不存在")
        return row
