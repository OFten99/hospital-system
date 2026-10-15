"""处方开具 → 收费/退费 → 药房发药 的完整药品链路。

这条链路是任务书「七大功能」里最核心的部分，涉及库存与金额的多步一致性：
  * 开方只生成待收费处方，**不动库存**；
  * 收费才扣库存，并写一条「已收费」的收费记录；
  * 退费恢复库存，并把收费记录改成「已退费」（不再计入收入合计）；
  * 发药要求处方已收费，并通过库存复核，成功后状态置「已发药」。
"""

import unittest

from tests.base import HisTestCase, requires_db, unique


@requires_db
class PrescriptionPaymentPharmacyTests(HisTestCase):

    def setUp(self):
        super().setUp()
        self.doctor = self.client_as("doctor01")
        self.cashier = self.client_as("cash01")
        self.pharmacist = self.client_as("pharm01")
        self.doctor_id = self.doctor_id_of("doctor01")
        self.patient_id = self.mk_patient()
        self.reg_id = self.mk_registration(self.patient_id, self.doctor_id)

    # ---------------- 辅助 ----------------
    def _issue(self, med_id, qty=2, dosage="每次1片", client=None, **over):
        data = {
            "action": "issue",
            "registration_id": self.reg_id,
            "medicine_id[]": [str(med_id)],
            "quantity[]": [str(qty)],
            "dosage[]": [dosage],
        }
        data.update(over)
        return (client or self.doctor).post("/prescriptions", data=data, follow_redirects=True)

    def _prescription(self):
        return self.one("SELECT * FROM prescriptions WHERE registration_id = %s", (self.reg_id,))

    def _stock(self, med_id):
        return self.one("SELECT stock_quantity FROM medicines WHERE medicine_id = %s",
                        (med_id,))["stock_quantity"]

    def _charge(self, prescription_id, method="微信", client=None):
        return (client or self.cashier).post("/payments", data={
            "action": "charge", "prescription_id": prescription_id, "pay_method": method,
        }, follow_redirects=True)

    def _mk_lab_test(self, fee=25.00, status="待检验"):
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO lab_tests (test_no, patient_id, doctor_id, department_id,
                                   registration_id, sample_type, total_fee, test_status)
            VALUES (%s, %s, %s, %s, %s, '血液', %s, %s)
            """,
            ("TSTLAB%s" % unique(), self.patient_id, self.doctor_id,
             self.dept_of_doctor(self.doctor_id), self.reg_id, fee, status))
        tid = cur.lastrowid
        cur.close()
        return tid

    # ---------------- 开方 ----------------
    def test_issue_does_not_touch_stock(self):
        med = self.mk_medicine(price=12.50, stock=50)
        resp = self._issue(med, qty=3)
        self.assertIn("处方开具成功", self.flash_text(resp))
        pres = self._prescription()
        self.assertEqual(pres["prescription_status"], "待收费")
        self.assertEqual(float(pres["total_amount"]), 37.50)
        self.assertEqual(self._stock(med), 50, "开方阶段不应扣减库存")

    def test_issue_multiple_medicines(self):
        a = self.mk_medicine(price=10.00, stock=100)
        b = self.mk_medicine(price=20.00, stock=100)
        resp = self.doctor.post("/prescriptions", data={
            "action": "issue", "registration_id": self.reg_id,
            "medicine_id[]": [str(a), str(b)],
            "quantity[]": ["2", "1"],
            "dosage[]": ["早晚各1片", "睡前1片"],
        }, follow_redirects=True)
        self.assertIn("处方开具成功", self.flash_text(resp))
        pres = self._prescription()
        self.assertEqual(float(pres["total_amount"]), 40.00)
        items = self.all("SELECT * FROM prescription_items WHERE prescription_id = %s "
                         "ORDER BY item_id", (pres["prescription_id"],))
        self.assertEqual(len(items), 2)

    def test_issue_insufficient_stock_rejected(self):
        med = self.mk_medicine(price=10.00, stock=2)
        resp = self._issue(med, qty=5)
        self.assertIn("库存不足", self.flash_text(resp))
        self.assertIsNone(self.one(
            "SELECT 1 AS x FROM prescriptions WHERE registration_id = %s", (self.reg_id,)))

    def test_issue_requires_medicine(self):
        resp = self.doctor.post("/prescriptions", data={
            "action": "issue", "registration_id": self.reg_id,
        }, follow_redirects=True)
        self.assertIn("请至少添加一种药品", self.flash_text(resp))

    def test_issue_sets_registration_visited(self):
        med = self.mk_medicine()
        self._issue(med)
        row = self.one("SELECT visit_status FROM registrations WHERE registration_id = %s",
                       (self.reg_id,))
        self.assertEqual(row["visit_status"], "已就诊")

    # ---------------- 收费 ----------------
    def test_charge_deducts_stock_and_records_payment(self):
        med = self.mk_medicine(price=12.50, stock=50)
        self._issue(med, qty=4)
        pres = self._prescription()
        resp = self._charge(pres["prescription_id"], method="医保")
        self.assertIn("收费成功", self.flash_text(resp))

        after = self.one("SELECT prescription_status FROM prescriptions WHERE prescription_id = %s",
                         (pres["prescription_id"],))
        self.assertEqual(after["prescription_status"], "已收费")
        self.assertEqual(self._stock(med), 46, "收费应扣减库存")

        pay = self.one("SELECT * FROM payments WHERE prescription_id = %s AND payment_type = '药品费'",
                       (pres["prescription_id"],))
        self.assertEqual(pay["pay_status"], "已收费")
        self.assertEqual(pay["pay_method"], "医保")
        self.assertEqual(float(pay["pay_amount"]), 50.00)

    def test_charge_twice_rejected(self):
        med = self.mk_medicine()
        self._issue(med)
        pres = self._prescription()
        self._charge(pres["prescription_id"])
        resp = self._charge(pres["prescription_id"])
        self.assertIn("仅待收费状态的处方可以收费", self.flash_text(resp))

    def test_refund_restores_stock(self):
        med = self.mk_medicine(price=10.00, stock=50)
        self._issue(med, qty=5)
        pres = self._prescription()
        self._charge(pres["prescription_id"])
        self.assertEqual(self._stock(med), 45)

        resp = self.cashier.post("/payments", data={
            "action": "refund", "prescription_id": pres["prescription_id"],
            "reason": "患者要求退药",
        }, follow_redirects=True)
        self.assertIn("退费成功", self.flash_text(resp))

        after = self.one("SELECT prescription_status FROM prescriptions WHERE prescription_id = %s",
                         (pres["prescription_id"],))
        self.assertEqual(after["prescription_status"], "已退费")
        self.assertEqual(self._stock(med), 50, "退费应恢复库存")
        pay = self.one("SELECT * FROM payments WHERE prescription_id = %s",
                       (pres["prescription_id"],))
        self.assertEqual(pay["pay_status"], "已退费")
        self.assertEqual(pay["refund_reason"], "患者要求退药")

    def test_refund_unpaid_rejected(self):
        med = self.mk_medicine()
        self._issue(med)
        pres = self._prescription()
        resp = self.cashier.post("/payments", data={
            "action": "refund", "prescription_id": pres["prescription_id"],
        }, follow_redirects=True)
        self.assertIn("仅已收费状态的处方可以退费", self.flash_text(resp))

    # ---------------- 作废 ----------------
    def test_void_prescription(self):
        med = self.mk_medicine()
        self._issue(med)
        pres = self._prescription()
        resp = self.doctor.post("/prescriptions", data={
            "action": "void", "prescription_id": pres["prescription_id"],
        }, follow_redirects=True)
        self.assertIn("处方已作废", self.flash_text(resp))
        after = self.one("SELECT prescription_status FROM prescriptions WHERE prescription_id = %s",
                         (pres["prescription_id"],))
        self.assertEqual(after["prescription_status"], "已作废")

    def test_cannot_void_other_doctors_prescription(self):
        med = self.mk_medicine()
        other = self.mk_doctor()
        other_reg = self.mk_registration(self.mk_patient(), other)
        # 由管理员以 other 医生名义开方
        admin = self.client_as("admin01")
        admin.post("/prescriptions", data={
            "action": "issue", "registration_id": other_reg, "doctor_id": other,
            "medicine_id[]": [str(med)], "quantity[]": ["1"], "dosage[]": ["x"],
        }, follow_redirects=True)
        pres = self.one("SELECT prescription_id FROM prescriptions WHERE registration_id = %s",
                        (other_reg,))
        self.assertIsNotNone(pres)
        resp = self.doctor.post("/prescriptions", data={
            "action": "void", "prescription_id": pres["prescription_id"],
        }, follow_redirects=True)
        self.assertIn("只能操作自己开具的处方", self.flash_text(resp))

    # ---------------- 检验收费 ----------------
    def test_charge_lab_test(self):
        tid = self._mk_lab_test(fee=33.00)
        resp = self.cashier.post("/payments", data={
            "action": "charge_lab", "test_id": tid, "pay_method": "现金",
        }, follow_redirects=True)
        self.assertIn("检验收费成功", self.flash_text(resp))
        pay = self.one("SELECT * FROM payments WHERE lab_test_id = %s", (tid,))
        self.assertEqual(pay["payment_type"], "检查费")
        self.assertEqual(float(pay["pay_amount"]), 33.00)
        self.assertEqual(pay["pay_status"], "已收费")

    def test_charge_lab_twice_rejected(self):
        tid = self._mk_lab_test()
        self.cashier.post("/payments", data={"action": "charge_lab", "test_id": tid,
                                             "pay_method": "现金"}, follow_redirects=True)
        resp = self.cashier.post("/payments", data={"action": "charge_lab", "test_id": tid,
                                                    "pay_method": "现金"}, follow_redirects=True)
        self.assertIn("不能重复收费", self.flash_text(resp))

    # ---------------- 发药 ----------------
    def test_dispense_requires_payment(self):
        med = self.mk_medicine()
        self._issue(med)
        pres = self._prescription()
        resp = self.pharmacist.post("/pharmacy", data={
            "action": "dispense", "prescription_id": pres["prescription_id"],
        }, follow_redirects=True)
        self.assertIn("仅已收费处方可以发药", self.flash_text(resp))

    def test_dispense_after_charge(self):
        med = self.mk_medicine(price=10.00, stock=50)
        self._issue(med, qty=2)
        pres = self._prescription()
        self._charge(pres["prescription_id"])
        resp = self.pharmacist.post("/pharmacy", data={
            "action": "dispense", "prescription_id": pres["prescription_id"],
        }, follow_redirects=True)
        self.assertIn("发药成功", self.flash_text(resp))
        after = self.one("SELECT prescription_status FROM prescriptions WHERE prescription_id = %s",
                         (pres["prescription_id"],))
        self.assertEqual(after["prescription_status"], "已发药")

    def test_payments_page_renders_summary(self):
        resp = self.cashier.get("/payments")
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("收费", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
