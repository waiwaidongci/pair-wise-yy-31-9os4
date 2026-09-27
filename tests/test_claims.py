import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, RecallService, Store
from ledger import ClaimLedger
from standards import StandardBook


class ClaimFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "c.db")
        self.svc = RecallService(self.store)
        self.standards = StandardBook(self.store.conn); self.standards.init_schema()
        self.ledger = ClaimLedger(self.store.conn, self.standards); self.ledger.init_schema()
        self.svc.claim_hook = self.ledger.create_claim_for_repair
        self.svc.evidence_hook = lambda rid, actor: self.ledger.reprice_for_repair(rid, actor, "维修单据变更，重新核价")
        self.dealer = self.svc.register_dealer("reg", "regulator", "D-CN", "中国中心", "CN")
        self.dealer_sg = self.svc.register_dealer("reg", "regulator", "D-SG", "新加坡中心", "SG")
        self.recall = self._publish_recall()
        self.ledger.set_standard("maker", "manufacturer", 1, "X", "CN", 10000, "CNY", "初始口径")

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def _publish_recall(self):
        r = self.svc.create_recall("maker", "manufacturer", "RC-1", "制动检查",
                                   {"models": ["X"], "model_years": [2018], "vin_prefixes": ["LX"], "countries": ["CN"]},
                                   {"version": 1, "description": "更换软管"})
        r = self.svc.submit_recall("maker", "manufacturer", r["id"], r["revision"])
        return self.svc.review_recall("reg", "regulator", r["id"], "publish", r["revision"], "同意发布")

    def _vehicle(self, vin):
        return self.svc.register_vehicle("maker", "manufacturer", vin, "X", 2018, "CN", "车主")

    def _confirm(self, vin, dealer, key):
        self.svc.add_parts("maker", "manufacturer", self.recall["id"], dealer["id"], 1, 1)
        permit = "BP-1" if dealer["country"] != "CN" else ""
        rep = self.svc.report_repair("dlr", "dealer", self.recall["id"], vin, dealer["id"], 1, "ev-1", True, permit, key)
        self.svc.review_repair("reg", "regulator", rep["id"], "confirm", "证据一致")
        return rep

    def test_claim_created_on_confirm_with_snapshot_and_idempotent(self):
        self._vehicle("LX00001")
        rep = self._confirm("LX00001", self.dealer, "k1")
        claim = self.ledger.list_claims()[0]
        self.assertEqual("pending", claim["state"])
        self.assertEqual((10000, 1, "CNY"), (claim["amount_cents"], claim["standard_version"], claim["currency"]))
        self.assertEqual(("X", "CN", 1), (claim["model"], claim["country"], claim["remedy_version"]))
        again = self.ledger.create_claim_for_repair(rep["id"], "reg")
        self.assertEqual(claim["id"], again["id"])
        self.assertEqual(1, len(self.ledger.list_claims()))

    def test_standard_change_reprices_until_approved(self):
        self._vehicle("LX00001"); self._confirm("LX00001", self.dealer, "k1")
        claim = self.ledger.list_claims()[0]
        out = self.ledger.set_standard("maker", "manufacturer", 1, "X", "CN", 15000, "CNY", "口径调整")
        self.assertEqual([claim["id"]], out["repriced_claim_ids"])
        claim = self.ledger.get_claim(claim["id"])
        self.assertEqual((15000, 2), (claim["amount_cents"], claim["standard_version"]))
        self.assertIn("repriced", [v["event"] for v in claim["versions"]])
        self.ledger.review_claim("reg", "regulator", claim["id"], "approve", "确认")
        self.ledger.set_standard("maker", "manufacturer", 1, "X", "CN", 20000, "CNY", "再次调整")
        claim = self.ledger.get_claim(claim["id"])
        self.assertEqual("approved", claim["state"])
        self.assertEqual(15000, claim["amount_cents"])  # 监管确认后金额锁定

    def test_evidence_change_reprices_and_blocked_after_approve(self):
        self._vehicle("LX00001")
        rep = self._confirm("LX00001", self.dealer, "k1")
        claim = self.ledger.list_claims()[0]
        with self.assertRaises(ApiError):
            self.svc.update_repair_evidence("other", "dealer", rep["id"], "ev-9", True)
        self.svc.update_repair_evidence("dlr", "dealer", rep["id"], "ev-2", True)
        claim = self.ledger.get_claim(claim["id"])
        repriced = [v for v in claim["versions"] if v["event"] == "repriced"]
        self.assertTrue(repriced)
        self.assertEqual("维修单据变更，重新核价", repriced[-1]["reason"])
        self.ledger.review_claim("reg", "regulator", claim["id"], "approve", "确认")
        with self.assertRaises(ApiError):
            self.svc.update_repair_evidence("dlr", "dealer", rep["id"], "ev-3", True)

    def test_reject_resubmit_keeps_history_and_pays_once(self):
        self._vehicle("LX00001"); self._confirm("LX00001", self.dealer, "k1")
        claim = self.ledger.list_claims()[0]
        rejected = self.ledger.review_claim("reg", "regulator", claim["id"], "reject", "单据不全")
        self.assertEqual("rejected", rejected["state"])
        with self.assertRaises(ApiError):
            self.ledger.review_claim("reg", "regulator", claim["id"], "approve", "重复审批")
        self.ledger.set_standard("maker", "manufacturer", 1, "X", "CN", 15000, "CNY", "口径调整")
        with self.assertRaises(ApiError):
            self.ledger.resubmit("other", "dealer", claim["id"], "越权重提")
        resub = self.ledger.resubmit("dlr", "dealer", claim["id"], "已补单据")
        self.assertEqual(("pending", 2, 15000), (resub["state"], resub["version"], resub["amount_cents"]))
        events = [v["event"] for v in resub["versions"]]
        self.assertEqual(["created", "rejected", "repriced", "resubmitted"], events)
        old = [v for v in resub["versions"] if v["event"] == "rejected"][0]
        self.assertEqual((10000, "单据不全"), (old["amount_cents"], old["reason"]))  # 旧金额和原因留着
        self.ledger.review_claim("reg", "regulator", claim["id"], "approve", "确认")
        paid = self.ledger.pay("maker", "manufacturer", claim["id"])
        self.assertEqual("paid", paid["state"])
        with self.assertRaises(ApiError):
            self.ledger.pay("maker", "manufacturer", claim["id"])  # 同一维修单不能拿两次钱
        self.assertEqual(1, len(self.ledger.list_claims()))

    def test_summary_buckets_and_cross_border_standard(self):
        for vin in ("LX00001", "LX00002", "LX00003"): self._vehicle(vin)
        self.ledger.set_standard("maker", "manufacturer", 1, "X", "SG", 20000, "SGD", "新加坡口径")
        self._confirm("LX00001", self.dealer, "k1")
        self._confirm("LX00002", self.dealer, "k2")
        self._confirm("LX00003", self.dealer_sg, "k3")  # 跨境：按维修网点所在国 SG 口径
        c1, c2, c3 = self.ledger.list_claims()
        self.assertEqual((20000, "SGD", "SG"), (c3["amount_cents"], c3["currency"], c3["country"]))
        self.ledger.review_claim("reg", "regulator", c2["id"], "approve", "确认")
        self.ledger.review_claim("reg", "regulator", c3["id"], "reject", "单据不全")
        summary = self.ledger.summary()
        cn = next(d for d in summary["dealers"] if d["code"] == "D-CN")
        sg = next(d for d in summary["dealers"] if d["code"] == "D-SG")
        self.assertEqual((10000, 10000, 0), (cn["pending_cents"], cn["payable_cents"], cn["disputed_cents"]))
        self.assertEqual((1, 1), (cn["pending_count"], cn["payable_count"]))
        self.assertEqual((20000, 1), (sg["disputed_cents"], sg["disputed_count"]))

    def test_approve_requires_priced_standard(self):
        self._vehicle("LX00001")
        self._confirm("LX00001", self.dealer_sg, "k1")  # SG 无口径
        claim = self.ledger.list_claims()[0]
        self.assertEqual((0, 0), (claim["standard_version"], claim["amount_cents"]))
        with self.assertRaises(ApiError):
            self.ledger.review_claim("reg", "regulator", claim["id"], "approve", "确认")
        out = self.ledger.set_standard("maker", "manufacturer", 1, "X", "SG", 20000, "SGD", "新加坡口径")
        self.assertEqual([claim["id"]], out["repriced_claim_ids"])
        ok = self.ledger.review_claim("reg", "regulator", claim["id"], "approve", "确认")
        self.assertEqual(("approved", 20000), (ok["state"], ok["amount_cents"]))

    def test_role_guards(self):
        with self.assertRaises(ApiError):
            self.ledger.set_standard("dlr", "dealer", 1, "X", "CN", 1)
        self._vehicle("LX00001"); self._confirm("LX00001", self.dealer, "k1")
        claim = self.ledger.list_claims()[0]
        with self.assertRaises(ApiError):
            self.ledger.review_claim("dlr", "dealer", claim["id"], "approve", "")
        with self.assertRaises(ApiError):
            self.ledger.pay("dlr", "dealer", claim["id"])


if __name__ == "__main__": unittest.main()
