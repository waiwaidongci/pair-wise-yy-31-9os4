import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, RecallService, Store


class CompensationFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = RecallService(Store(Path(self.tmp.name) / "c.db"))
        self.dealer_cn = self.s.register_dealer("reg", "regulator", "D-CN", "中国中心", "CN")
        self.dealer_sg = self.s.register_dealer("reg", "regulator", "D-SG", "新加坡中心", "SG")
        self.recall = self._publish_recall()
        # rate at completion time: plan v1 / model X / CN
        self.s.set_compensation_standard("maker", "manufacturer", 1, "X", "CN", 1000, "CNY")

    def tearDown(self):
        self.s.store.close(); self.tmp.cleanup()

    def _publish_recall(self, code="RC-1"):
        r = self.s.create_recall("maker", "manufacturer", code, "制动检查",
                                 {"models": ["X"], "model_years": [2018], "vin_prefixes": ["LX"], "countries": ["CN", "SG"]},
                                 {"version": 1, "description": "更换软管"})
        r = self.s.submit_recall("maker", "manufacturer", r["id"], r["revision"])
        return self.s.review_recall("reg", "regulator", r["id"], "publish", r["revision"], "同意发布")

    def _confirmed_repair(self, vin="LX00001", dealer_id=None, country="CN"):
        dealer_id = dealer_id or self.dealer_cn["id"]
        v = self.s.register_vehicle("maker", "manufacturer", vin, "X", 2018, country, "车主")
        self.s.add_parts("maker", "manufacturer", self.recall["id"], dealer_id, 1, 2)
        reported = self.s.report_repair("dealer", "dealer", self.recall["id"], v["vin"], dealer_id,
                                        1, "h1", True, idempotency_key=f"k-{vin}")
        confirmed = self.s.review_repair("reg", "regulator", reported["id"], "confirm", "证据一致")
        return v, confirmed, confirmed["compensation_claim"]

    def test_claim_auto_created_at_completion_with_snapshot_basis(self):
        v, _, claim = self._confirmed_repair()
        self.assertEqual("pending", claim["status"])
        self.assertEqual(1000, claim["amount"])
        self.assertEqual("CNY", claim["currency"])
        # 留存完成时的方案、车型、车辆所在国家
        self.assertEqual(1, claim["remedy_version"])
        self.assertEqual("X", claim["model"])
        self.assertEqual("CN", claim["country"])
        self.assertEqual(v["vin"], claim["vin"])
        self.assertEqual("created", claim["revisions"][0]["event"])

    def test_confirm_without_standard_fails(self):
        r2 = self.s.create_recall("maker", "manufacturer", "RC-2", "新方案检查",
                                  {"models": ["X"], "model_years": [2018], "vin_prefixes": ["LX"], "countries": ["CN"]},
                                  {"version": 2, "description": "v2 软管"})
        r2 = self.s.submit_recall("maker", "manufacturer", r2["id"], r2["revision"])
        r2 = self.s.review_recall("reg", "regulator", r2["id"], "publish", r2["revision"], "发布")
        v = self.s.register_vehicle("maker", "manufacturer", "LX90009", "X", 2018, "CN", "钱七")
        self.s.add_parts("maker", "manufacturer", r2["id"], self.dealer_cn["id"], 2, 1)
        rep = self.s.report_repair("dealer", "dealer", r2["id"], v["vin"], self.dealer_cn["id"],
                                   2, "h", True, idempotency_key="ky")
        with self.assertRaises(ApiError) as ctx:
            self.s.review_repair("reg", "regulator", rep["id"], "confirm")
        self.assertIn("赔付口径", ctx.exception.message)
        repair = self.s._row("repairs", rep["id"])
        self.assertEqual("reported", repair["status"])  # 未确认，未挂账
        self.assertFalse(self.s.conn.execute(
            "SELECT 1 FROM compensation_claims WHERE repair_id=?", (rep["id"],)).fetchone())

    def test_one_repair_one_claim_even_if_confirmation_replayed(self):
        v, _, claim = self._confirmed_repair()
        # replaying confirmation path must not create a second claim
        again = self.s.ledger.on_repair_confirmed("reg", self.s._row("repairs", claim["repair_id"]))
        self.assertEqual(claim["id"], again["id"])
        rows = self.s.conn.execute("SELECT COUNT(*) c FROM compensation_claims WHERE repair_id=?",
                                   (claim["repair_id"],)).fetchone()
        self.assertEqual(1, rows["c"])

    def test_standard_change_reprices_pending_and_approved_keeps_old_rate(self):
        _, _, pending = self._confirmed_repair("LX00002")
        _, _, to_approve = self._confirmed_repair("LX00003")
        approved = self.s.review_claim("reg", "regulator", to_approve["id"], "approve", "通过")
        self.assertEqual(1000, approved["amount"])
        # 赔付口径换版（旧标准 1000 -> 1300）
        result = self.s.set_compensation_standard("maker", "manufacturer", 1, "X", "CN", 1300, "CNY")
        self.assertGreaterEqual(result["repriced_claims"], 1)
        rep_pending = self.s.claim_detail(pending["id"])
        self.assertEqual(1300, rep_pending["amount"])
        self.assertEqual(1000, rep_pending["revisions"][-1]["amount_before"])  # 旧金额留档
        rep_approved = self.s.claim_detail(approved["id"])
        self.assertEqual(1000, rep_approved["amount"])  # 监管确认后锁定，不按新标准

    def test_document_change_reprices_before_regulator_decision(self):
        _, _, claim = self._confirmed_repair("LX00004")
        self.s.set_compensation_standard("maker", "manufacturer", 1, "X", "CN", 900, "CNY")
        fixed = self.s.amend_repair_documents("dealer", "dealer", claim["repair_id"], "h1-corrected")
        self.assertEqual(900, fixed["amount"])
        self.assertEqual("h1-corrected", fixed["evidence_hash"])
        events = [r["event"] for r in fixed["revisions"]]
        self.assertIn("repriced", events)
        approved = self.s.review_claim("reg", "regulator", claim["id"], "approve")
        # approved claim rejects further document changes
        with self.assertRaises(ApiError):
            self.s.amend_repair_documents("dealer", "dealer", claim["repair_id"], "h2")

    def test_reject_keeps_old_amount_and_reason_then_dealer_resubmits(self):
        _, _, claim = self._confirmed_repair("LX00005")
        with self.assertRaises(ApiError):
            self.s.review_claim("reg", "regulator", claim["id"], "reject", "")  # 驳回必须有原因
        rejected = self.s.review_claim("reg", "regulator", claim["id"], "reject", "单据图片不清")
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual(1000, rejected["amount"])
        # 口径已更换
        self.s.set_compensation_standard("maker", "manufacturer", 1, "X", "CN", 1250, "CNY")
        # 网点更正重提：按当前口径重新核价，历史不丢
        resub = self.s.resubmit_claim("dealer", "dealer", claim["id"], "已补清晰照片", "h-new")
        self.assertEqual("pending", resub["status"])
        self.assertEqual(1250, resub["amount"])
        self.assertEqual("h-new", resub["evidence_hash"])
        self.assertIsNone(resub["reject_reason"])  # 当前字段清空
        history = [(r["event"], r["amount_before"], r["amount_after"], r["reason"]) for r in resub["revisions"]]
        self.assertIn(("created", None, 1000, "维修确认时按完成时方案/车型/国家核价"), history)
        self.assertIn(("rejected", 1000, 1000, "单据图片不清"), history)  # 旧原因留着
        self.assertIn(("resubmitted", 1000, 1000, "已补清晰照片"), history)  # 旧金额留着
        self.assertIn(("repriced", 1000, 1250, "驳回重提，按当前口径重新核价"), history)

    def test_settlement_guard_against_double_payment(self):
        _, _, claim = self._confirmed_repair("LX00006")
        approved = self.s.review_claim("reg", "regulator", claim["id"], "approve")
        paid = self.s.settle_claim("maker", "manufacturer", approved["id"])
        self.assertTrue(paid["paid"])
        with self.assertRaises(ApiError) as ctx:
            self.s.settle_claim("maker", "manufacturer", approved["id"])
        self.assertIn("重复", ctx.exception.message)
        pending = self.s.claim_detail(claim["id"])
        self.assertEqual(1, len([r for r in pending["revisions"] if r["event"] == "settled"]))

    def test_cross_border_uses_vehicle_country_at_completion(self):
        v = self.s.register_vehicle("maker", "manufacturer", "LX00007", "X", 2018, "SG", "Wang")
        self.s.set_compensation_standard("maker", "manufacturer", 1, "X", "SG", 300, "SGD")
        self.s.add_parts("maker", "manufacturer", self.recall["id"], self.dealer_sg["id"], 1, 1)
        rep = self.s.report_repair("dealer", "dealer", self.recall["id"], v["vin"],
                                   self.dealer_sg["id"], 1, "h", True, "BP-1", "k-sg")
        confirmed = self.s.review_repair("reg", "regulator", rep["id"], "confirm")
        claim = confirmed["compensation_claim"]
        self.assertEqual("SG", claim["country"])
        self.assertEqual(300, claim["amount"])
        self.assertEqual("SGD", claim["currency"])
        # vehicle moves country after completion: pricing basis must stay frozen at SG
        self.s.transfer_vehicle("dealer", "dealer", v["vin"], "CN", "Wang")
        same = self.s.claim_detail(claim["id"])
        self.assertEqual("SG", same["country"])
        self.assertEqual(300, same["amount"])

    def test_dealer_page_amounts_and_details(self):
        _, _, c1 = self._confirmed_repair("LX00008")
        _, _, c2 = self._confirmed_repair("LX00009")
        _, _, c3 = self._confirmed_repair("LX00010")
        self.s.review_claim("reg", "regulator", c1["id"], "approve")
        self.s.review_claim("reg", "regulator", c3["id"], "reject", "争议：金额存疑")
        summary = self.s.dealer_ledger("reg", "regulator")
        d = next(x for x in summary["dealers"] if x["dealer_id"] == self.dealer_cn["id"])
        self.assertEqual(1000, d["approved_total"])   # 可赔
        self.assertEqual(1000, d["pending_total"])    # 待核
        self.assertEqual(1000, d["disputed_total"])   # 争议
        self.assertEqual(3, len(d["claims"]))         # 明细
        # filter works too
        only = self.s.dealer_ledger("reg", "regulator", self.dealer_sg["id"])
        self.assertEqual([], only["dealers"])
        with self.assertRaises(ApiError):
            self.s.dealer_ledger(None, None)  # 页面需要身份


if __name__ == "__main__":
    unittest.main()
