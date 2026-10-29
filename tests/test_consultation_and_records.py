"""医生接诊工作台与门诊病历。

接诊工作台（/consultation）是「队列 → 开始接诊 → 写病历 → 按需检验 → 开药 → 完成」
的引导式闭环；门诊病历（/medical_records）按「一次就诊一份」书写，并支持打印。

重点验证：
  * 状态机（待就诊 → 就诊中 → 已就诊）与「完成就诊前必须有病历」的强制约束；
  * 归属校验（医生只能操作自己名下的挂号与病历）；
  * 体征写入必须绑定接诊上下文，不采信表单里的 patient_id（防越权写他人病历）；
  * 病历与检验、处方联动，且病历打印页不泄露内部对象。
"""

import unittest

from tests.base import HisTestCase, requires_db


@requires_db
class ConsultationWorkflowTests(HisTestCase):

    def setUp(self):
        super().setUp()
        self.doctor = self.client_as("doctor01")
        self.doctor_id = self.doctor_id_of("doctor01")
        self.assertIsNotNone(self.doctor_id)
        self.patient_id = self.mk_patient()
        self.reg_id = self.mk_registration(self.patient_id, self.doctor_id)

    def _post(self, action, **data):
        payload = {"action": action, "registration_id": self.reg_id}
        payload.update(data)
        return self.doctor.post("/consultation", data=payload, follow_redirects=True)

    def _status(self, reg_id=None):
        row = self.one("SELECT visit_status FROM registrations WHERE registration_id = %s",
                       (reg_id or self.reg_id,))
        return row["visit_status"]

    # ---------------- 状态机 ----------------
    def test_start_moves_to_in_progress(self):
        resp = self._post("start")
        self.assertIn("已开始接诊", self.flash_text(resp))
        self.assertEqual(self._status(), "就诊中")

    def test_start_twice_rejected(self):
        self._post("start")
        resp = self._post("start")
        self.assertIn("无法开始接诊", self.flash_text(resp))

    def test_finish_requires_medical_record(self):
        self._post("start")
        resp = self._post("finish")
        self.assertIn("请先书写并保存病历", self.flash_text(resp))
        self.assertEqual(self._status(), "就诊中")

    def test_finish_after_record(self):
        self._post("start")
        self._post("save_record", chief_complaint="发热 2 天", diagnosis="上呼吸道感染",
                   record_status="已归档")
        resp = self._post("finish")
        self.assertIn("已完成就诊", self.flash_text(resp))
        self.assertEqual(self._status(), "已就诊")

    def test_workspace_page_renders_after_start(self):
        self._post("start")
        resp = self.doctor.get("/consultation/%d" % self.reg_id)
        self.assertEqual(resp.status_code, 200)

    # ---------------- 归属校验 ----------------
    def test_cannot_start_other_doctors_registration(self):
        other_doctor = self.mk_doctor()
        other_reg = self.mk_registration(self.mk_patient(), other_doctor)
        resp = self.doctor.post("/consultation", data={
            "action": "start", "registration_id": other_reg,
        }, follow_redirects=True)
        self.assertIn("只能接诊自己名下的挂号记录", self.flash_text(resp))
        self.assertEqual(self._status(other_reg), "待就诊")

    def test_cannot_write_record_for_other_doctors_registration(self):
        other_doctor = self.mk_doctor()
        other_reg = self.mk_registration(self.mk_patient(), other_doctor)
        resp = self.doctor.post("/consultation", data={
            "action": "save_record", "registration_id": other_reg,
            "chief_complaint": "伪造",
        }, follow_redirects=True)
        self.assertIn("只能操作自己名下的挂号记录", self.flash_text(resp))
        self.assertIsNone(self.one(
            "SELECT 1 AS x FROM medical_records WHERE registration_id = %s", (other_reg,)))

    def test_cannot_amend_finished_registration(self):
        """已就诊的挂号不能再补写病历或补开单。"""
        self.exec_sql("UPDATE registrations SET visit_status = '已就诊' "
                      "WHERE registration_id = %s", (self.reg_id,))
        resp = self._post("save_record", chief_complaint="事后补写")
        self.assertIn("不能再书写病历", self.flash_text(resp))

    # ---------------- 病历 ----------------
    def test_save_record_persists_fields(self):
        self._post("start")
        self._post("save_record",
                   chief_complaint="主诉A", present_illness="现病史B",
                   past_history="既往史C", physical_exam="体格检查D",
                   diagnosis="诊断E", advice="医嘱F", record_status="草稿")
        rec = self.one("SELECT * FROM medical_records WHERE registration_id = %s", (self.reg_id,))
        self.assertIsNotNone(rec)
        self.assertEqual(rec["chief_complaint"], "主诉A")
        self.assertEqual(rec["present_illness"], "现病史B")
        self.assertEqual(rec["diagnosis"], "诊断E")
        self.assertEqual(rec["record_status"], "草稿")
        self.assertEqual(rec["patient_id"], self.patient_id)
        self.assertEqual(rec["doctor_id"], self.doctor_id)
        # 病历号形如 MR + 日期 + 4 位序号
        self.assertTrue(rec["record_no"].startswith("MR"))

    def test_duplicate_record_rejected(self):
        self._post("start")
        self._post("save_record", diagnosis="首次诊断")
        resp = self._post("save_record", diagnosis="再次诊断")
        self.assertIn("已有病历", self.flash_text(resp))
        n = self.one("SELECT COUNT(*) AS n FROM medical_records WHERE registration_id = %s",
                     (self.reg_id,))["n"]
        self.assertEqual(n, 1)

    # ---------------- 检验开单 ----------------
    def test_order_lab_creates_linked_test(self):
        self._post("start")
        resp = self.doctor.post("/consultation", data={
            "action": "order_lab", "registration_id": self.reg_id,
            "item_ids": ["1", "2"],          # 白细胞(15) + 红细胞(10)
        }, follow_redirects=True)
        self.assertIn("已开具", self.flash_text(resp))
        test = self.one("SELECT * FROM lab_tests WHERE registration_id = %s", (self.reg_id,))
        self.assertIsNotNone(test)
        self.assertEqual(test["patient_id"], self.patient_id)
        self.assertEqual(test["doctor_id"], self.doctor_id)
        self.assertEqual(test["sample_type"], "血液")
        self.assertEqual(float(test["total_fee"]), 25.00)
        self.assertEqual(test["test_status"], "待检验")
        n = self.one("SELECT COUNT(*) AS n FROM lab_test_results WHERE test_id = %s",
                     (test["test_id"],))["n"]
        self.assertEqual(n, 2)

    def test_order_lab_urine_sample_type(self):
        self._post("start")
        # 尿常规的项目 ID 不写死：项目字典随灌库脚本演进而整体漂移
        # （早期尿常规是 5 号，现在 5 号是血常规的「中性粒细胞百分比」）。
        urine_id = self.one(
            "SELECT item_id FROM lab_items WHERE item_category = '尿常规' "
            "ORDER BY item_id LIMIT 1")["item_id"]
        self.doctor.post("/consultation", data={
            "action": "order_lab", "registration_id": self.reg_id,
            "item_ids": [str(urine_id)],
        }, follow_redirects=True)
        test = self.one("SELECT sample_type FROM lab_tests WHERE registration_id = %s",
                        (self.reg_id,))
        self.assertEqual(test["sample_type"], "尿液")

    def test_order_lab_requires_items(self):
        self._post("start")
        resp = self.doctor.post("/consultation", data={
            "action": "order_lab", "registration_id": self.reg_id,
        }, follow_redirects=True)
        self.assertIn("请至少勾选一项检验项目", self.flash_text(resp))

    # ---------------- 开药 ----------------
    def test_issue_drug_creates_prescription(self):
        self._post("start")
        med = self.mk_medicine(price=12.50)
        resp = self.doctor.post("/consultation", data={
            "action": "issue_drug", "registration_id": self.reg_id,
            "medicine_id[]": [str(med)], "quantity[]": ["2"], "dosage[]": ["每次1片"],
        }, follow_redirects=True)
        self.assertIn("处方开具成功", self.flash_text(resp))
        pres = self.one("SELECT * FROM prescriptions WHERE registration_id = %s", (self.reg_id,))
        self.assertIsNotNone(pres)
        self.assertEqual(pres["prescription_status"], "待收费")
        self.assertEqual(float(pres["total_amount"]), 25.00)
        items = self.all("SELECT * FROM prescription_items WHERE prescription_id = %s",
                         (pres["prescription_id"],))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["quantity"], 2)
        self.assertEqual(items[0]["dosage"], "每次1片")

    def test_issue_drug_requires_medicine(self):
        self._post("start")
        resp = self.doctor.post("/consultation", data={
            "action": "issue_drug", "registration_id": self.reg_id,
        }, follow_redirects=True)
        self.assertIn("请至少添加一种药品", self.flash_text(resp))

    # ---------------- 现场体征 ----------------
    def test_save_vitals_binds_session_patient(self):
        """体征必须写到接诊患者身上，表单里的 patient_id 不可信。"""
        self._post("start")
        forged = self.mk_patient(name="被伪造患者")
        resp = self.doctor.post("/consultation", data={
            "action": "save_vitals", "registration_id": self.reg_id,
            "patient_id": forged,            # 伪造：指向另一位患者
            "temperature": "38.5", "heart_rate": "88",
        }, follow_redirects=True)
        self.assertIn("体征已记录", self.flash_text(resp))
        row = self.one("SELECT * FROM vital_signs WHERE registration_id = %s", (self.reg_id,))
        self.assertIsNotNone(row)
        self.assertEqual(row["patient_id"], self.patient_id)
        self.assertNotEqual(row["patient_id"], forged)
        self.assertEqual(float(row["temperature"]), 38.5)

    def test_save_vitals_all_empty_is_skipped(self):
        self._post("start")
        resp = self._post("save_vitals")
        self.assertIn("未记录体征", self.flash_text(resp))
        self.assertIsNone(self.one(
            "SELECT 1 AS x FROM vital_signs WHERE registration_id = %s", (self.reg_id,)))


@requires_db
class MedicalRecordPageTests(HisTestCase):

    def setUp(self):
        super().setUp()
        self.doctor = self.client_as("doctor01")
        self.doctor_id = self.doctor_id_of("doctor01")
        self.patient_id = self.mk_patient()
        self.reg_id = self.mk_registration(self.patient_id, self.doctor_id)

    def _save_record(self, client=None, **over):
        data = {
            "action": "save",
            "registration_id": self.reg_id,
            "chief_complaint": "咽痛 3 天",
            "diagnosis": "急性咽炎",
            "advice": "多饮水",
            "record_status": "已归档",
        }
        data.update(over)
        return (client or self.doctor).post("/medical_records", data=data, follow_redirects=True)

    def test_save_record_and_print(self):
        resp = self._save_record()
        self.assertIn("病历已保存", self.flash_text(resp))
        rec = self.one("SELECT * FROM medical_records WHERE registration_id = %s", (self.reg_id,))
        page = self.doctor.get("/medical_records/%d/print" % rec["record_id"])
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn("急性咽炎", body)
        self.assertIn("门诊病历", body)
        self.assertNotIn("<built-in method", body)

    def test_save_record_with_lab_items(self):
        resp = self._save_record(item_ids=["1", "2"])
        self.assertIn("已开具 2 项检验申请", self.flash_text(resp))
        n = self.one("SELECT COUNT(*) AS n FROM lab_tests WHERE registration_id = %s",
                     (self.reg_id,))["n"]
        self.assertEqual(n, 1)

    def test_doctor_cannot_write_for_other_doctor(self):
        other = self.mk_doctor()
        other_reg = self.mk_registration(self.mk_patient(), other)
        resp = self.doctor.post("/medical_records", data={
            "action": "save", "registration_id": other_reg, "diagnosis": "越权",
        }, follow_redirects=True)
        self.assertIn("只能为自己接诊的患者书写病历", self.flash_text(resp))

    def test_edit_other_doctors_record_is_blocked_on_get(self):
        """已知病历 id 直接 GET 编辑页也会被拦截（防内容泄露）。"""
        other = self.mk_doctor()
        other_reg = self.mk_registration(self.mk_patient(), other)
        # 直接用管理员写一份属于 other 医生的病历
        admin = self.client_as("admin01")
        admin.post("/medical_records", data={
            "action": "save", "registration_id": other_reg, "diagnosis": "他人病历",
        }, follow_redirects=True)
        rec = self.one("SELECT record_id FROM medical_records WHERE registration_id = %s",
                       (other_reg,))
        self.assertIsNotNone(rec)
        resp = self.doctor.get("/medical_records?record_id=%d" % rec["record_id"],
                               follow_redirects=True)
        self.assertIn("只能查看与编辑自己书写的病历", self.flash_text(resp))

    def test_delete_record(self):
        self._save_record()
        rec = self.one("SELECT record_id FROM medical_records WHERE registration_id = %s",
                       (self.reg_id,))
        resp = self.doctor.post("/medical_records", data={
            "action": "delete", "record_id": rec["record_id"],
        }, follow_redirects=True)
        self.assertIn("病历已删除", self.flash_text(resp))
        self.assertIsNone(self.one("SELECT 1 AS x FROM medical_records WHERE record_id = %s",
                                   (rec["record_id"],)))

    def test_print_highlights_abnormal_lab_result(self):
        """打印页要对异常检验项加粗标红并给出异常计数。"""
        self._save_record()
        # 造一张已收费 + 已出结果的检验单（直接写库，聚焦打印渲染）
        rec = self.one("SELECT * FROM medical_records WHERE registration_id = %s", (self.reg_id,))
        dept = self.dept_of_doctor(self.doctor_id)
        test_id = self._insert_paid_lab_test(dept)
        # 白细胞参考区间 3.5-9.5，给一个偏高值
        self.exec_sql(
            "UPDATE lab_test_results r JOIN lab_items li ON r.item_id = li.item_id "
            "SET r.result_value = '15.0', r.result_flag = '偏高' "
            "WHERE r.test_id = %s AND li.item_code = 'WBC'", (test_id,))
        page = self.doctor.get("/medical_records/%d/print" % rec["record_id"])
        body = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn("异常", body)
        self.assertIn("15.0", body)

    def _insert_paid_lab_test(self, department_id):
        from tests.base import unique
        test_no = "TSTLAB%s" % unique()
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO lab_tests (test_no, patient_id, doctor_id, department_id,
                                   registration_id, sample_type, total_fee, test_status)
            VALUES (%s, %s, %s, %s, %s, '血液', 15.00, '已完成')
            """,
            (test_no, self.patient_id, self.doctor_id, department_id, self.reg_id))
        test_id = cur.lastrowid
        cur.execute(
            "INSERT INTO lab_test_results (test_id, item_id, result_value, result_flag) "
            "SELECT %s, item_id, NULL, '' FROM lab_items WHERE item_code = 'WBC'",
            (test_id,))
        cur.close()
        return test_id


if __name__ == "__main__":
    unittest.main(verbosity=2)
