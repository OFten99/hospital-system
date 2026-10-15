"""患者建档、挂号/退号、医生排班。

这些用例通过真实的 HTTP 表单（Flask 测试客户端）驱动收费人员与管理员账号，
验证「建档 → 挂号（自动收挂号费）→ 退号（恢复号源并退费）」的完整业务闭环，
以及排班维护的时段冲突、停诊与删除保护。
"""

import unittest
from datetime import datetime, timedelta

from tests.base import HisTestCase, requires_db, unique


@requires_db
class PatientArchiveTests(HisTestCase):

    def setUp(self):
        super().setUp()
        self.cash = self.client_as("cash01")

    def _add(self, client=None, **over):
        sfx = unique()
        data = {
            "action": "add",
            "patient_no": "TSTH%s" % sfx,
            "patient_name": "建档测试%s" % sfx,
            "gender": "女",
            "birth_date": "1988-06-01",
            "id_card": "TSTHID%s" % sfx,
            "phone": "137%08d" % (int(datetime.now().timestamp()) % 100_000_000),
            "blood_type": "O",
            "address": "建档地址",
            "medical_history": "无",
        }
        data.update(over)
        resp = (client or self.cash).post("/patients", data=data, follow_redirects=True)
        return resp, data

    def test_create_patient(self):
        resp, data = self._add()
        self.assertIn("患者档案创建成功", self.flash_text(resp))
        pid = self.track_patient(data["patient_no"])
        self.assertIsNotNone(pid)
        row = self.one("SELECT * FROM patients WHERE patient_id = %s", (pid,))
        self.assertEqual(row["patient_name"], data["patient_name"])
        self.assertEqual(row["gender"], "女")
        self.assertEqual(row["blood_type"], "O")

    def test_duplicate_patient_no_reports_field(self):
        resp, data = self._add()
        self.track_patient(data["patient_no"])
        dup = dict(data)
        dup["id_card"] = "TSTHID2%s" % unique()
        dup["phone"] = "136%08d" % (int(datetime.now().timestamp()) % 100_000_000)
        resp2 = self.cash.post("/patients", data=dup, follow_redirects=True)
        body = self.flash_text(resp2)
        self.assertIn("病历号已存在", body)

    def test_duplicate_id_card_reports_field(self):
        resp, data = self._add()
        self.track_patient(data["patient_no"])
        dup = dict(data)
        dup["patient_no"] = "TSTH2%s" % unique()
        dup["phone"] = "135%08d" % (int(datetime.now().timestamp()) % 100_000_000)
        resp2 = self.cash.post("/patients", data=dup, follow_redirects=True)
        self.assertIn("身份证号已存在", self.flash_text(resp2))

    def test_missing_required_field_is_handled(self):
        """缺必填字段走第三层参数异常，不该冒成 500。"""
        data = {
            "action": "add",
            "patient_name": "缺字段患者",
            "gender": "男",
            "birth_date": "1990-01-01",
            "id_card": "TSTMISS%s" % unique(),
            "phone": "134%08d" % (int(datetime.now().timestamp()) % 100_000_000),
            "blood_type": "A",
        }
        resp = self.cash.post("/patients", data=data, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("不能为空", self.flash_text(resp))

    def test_update_patient(self):
        resp, data = self._add()
        pid = self.track_patient(data["patient_no"])
        resp2 = self.cash.post("/patients", data={
            "action": "update",
            "patient_id": pid,
            "phone": "13012345678",
            "address": "新地址",
            "blood_type": "AB",
            "medical_history": "高血压",
        }, follow_redirects=True)
        self.assertIn("患者档案已更新", self.flash_text(resp2))
        row = self.one("SELECT * FROM patients WHERE patient_id = %s", (pid,))
        self.assertEqual(row["address"], "新地址")
        self.assertEqual(row["blood_type"], "AB")
        self.assertEqual(row["medical_history"], "高血压")

    def test_delete_patient_without_business(self):
        resp, data = self._add()
        pid = self.track_patient(data["patient_no"])
        resp2 = self.cash.post("/patients", data={"action": "delete", "patient_id": pid},
                               follow_redirects=True)
        self.assertIn("患者档案已删除", self.flash_text(resp2))
        self.assertIsNone(self.one("SELECT 1 AS x FROM patients WHERE patient_id = %s", (pid,)))

    def test_delete_patient_with_registration_blocked(self):
        """已挂号的档案被外键保护，删除应给出可读提示而不是 500。"""
        pid = self.mk_patient()
        did = self.doctor_for(0)
        self.mk_registration(pid, did)
        resp = self.cash.post("/patients", data={"action": "delete", "patient_id": pid},
                              follow_redirects=True)
        body = self.flash_text(resp)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("引用", body)
        self.assertIsNotNone(self.one("SELECT 1 AS x FROM patients WHERE patient_id = %s", (pid,)))

    def test_search_patient(self):
        resp, data = self._add(patient_name="检索目标患者AAA")
        self.track_patient(data["patient_no"])
        resp2 = self.cash.get("/patients?keyword=检索目标患者AAA")
        self.assertIn("检索目标患者AAA", self.flash_text(resp2))


@requires_db
class RegistrationTests(HisTestCase):

    def setUp(self):
        super().setUp()
        self.cash = self.client_as("cash01")
        # 使用专属测试医生，避免与基线排班（同医生/日期/班次唯一键）冲突
        self.doctor_id = self.mk_doctor()
        self.fee = float(self.one(
            "SELECT consultation_fee FROM doctors WHERE doctor_id = %s",
            (self.doctor_id,))["consultation_fee"])

    def _schedule(self, max_reg=20, shift="上午"):
        return self.mk_schedule(self.doctor_id, shift_type=shift,
                                max_registrations=max_reg)

    def _register(self, schedule_id, patient_id=None, reg_type="普通号",
                  pay_method="现金", client=None):
        patient_id = patient_id or self.mk_patient()
        resp = (client or self.cash).post("/registrations", data={
            "action": "register",
            "schedule_id": schedule_id,
            "patient_id": patient_id,
            "reg_type": reg_type,
            "pay_method": pay_method,
        }, follow_redirects=True)
        return resp, patient_id

    def test_register_creates_visit_and_charges_fee(self):
        sid = self._schedule()
        resp, pid = self._register(sid)
        self.assertIn("挂号成功", self.flash_text(resp))

        reg = self.one("SELECT * FROM registrations WHERE patient_id = %s "
                       "ORDER BY registration_id DESC LIMIT 1", (pid,))
        self.assertEqual(reg["visit_status"], "待就诊")
        self.assertEqual(reg["queue_no"], 1)
        self.assertEqual(float(reg["reg_fee"]), self.fee)
        self.assertEqual(reg["reg_date"].isoformat(), datetime.now().strftime("%Y-%m-%d"))

        pay = self.one("SELECT * FROM payments WHERE registration_id = %s "
                       "AND payment_type = '挂号费'", (reg["registration_id"],))
        self.assertEqual(pay["pay_status"], "已收费")
        self.assertEqual(float(pay["pay_amount"]), self.fee)

        sched = self.one("SELECT registered_count FROM doctor_schedules WHERE schedule_id = %s",
                         (sid,))
        self.assertEqual(sched["registered_count"], 1)

    def test_expert_registration_fee(self):
        sid = self._schedule()
        resp, pid = self._register(sid, reg_type="专家号")
        reg = self.one("SELECT reg_type, reg_fee FROM registrations WHERE patient_id = %s "
                       "ORDER BY registration_id DESC LIMIT 1", (pid,))
        self.assertEqual(reg["reg_type"], "专家号")
        self.assertEqual(float(reg["reg_fee"]), self.fee)

    def test_queue_number_increases(self):
        sid = self._schedule()
        _, p1 = self._register(sid)
        _, p2 = self._register(sid)
        r1 = self.one("SELECT queue_no FROM registrations WHERE patient_id = %s "
                      "ORDER BY registration_id DESC LIMIT 1", (p1,))
        r2 = self.one("SELECT queue_no FROM registrations WHERE patient_id = %s "
                      "ORDER BY registration_id DESC LIMIT 1", (p2,))
        self.assertEqual(r1["queue_no"], 1)
        self.assertEqual(r2["queue_no"], 2)

    def test_full_schedule_rejected(self):
        sid = self._schedule(max_reg=1)
        self._register(sid)
        resp, _ = self._register(sid)
        self.assertIn("号源已满", self.flash_text(resp))

    def test_suspended_schedule_rejected(self):
        sid = self._schedule()
        self.exec_sql("UPDATE doctor_schedules SET schedule_status = '停诊' WHERE schedule_id = %s",
                      (sid,))
        resp, _ = self._register(sid)
        self.assertIn("停诊", self.flash_text(resp))

    def test_cancel_registration_restores_seat_and_refunds(self):
        sid = self._schedule()
        _, pid = self._register(sid)
        reg = self.one("SELECT registration_id FROM registrations WHERE patient_id = %s "
                       "ORDER BY registration_id DESC LIMIT 1", (pid,))
        resp = self.cash.post("/registrations", data={
            "action": "cancel",
            "registration_id": reg["registration_id"],
            "reason": "患者临时有事",
        }, follow_redirects=True)
        self.assertIn("退号成功", self.flash_text(resp))

        after = self.one("SELECT visit_status FROM registrations WHERE registration_id = %s",
                         (reg["registration_id"],))
        self.assertEqual(after["visit_status"], "已退号")
        pay = self.one("SELECT pay_status, refund_reason FROM payments WHERE registration_id = %s",
                       (reg["registration_id"],))
        self.assertEqual(pay["pay_status"], "已退费")
        self.assertEqual(pay["refund_reason"], "患者临时有事")
        sched = self.one("SELECT registered_count FROM doctor_schedules WHERE schedule_id = %s",
                         (sid,))
        self.assertEqual(sched["registered_count"], 0)

    def test_cancel_twice_rejected(self):
        sid = self._schedule()
        _, pid = self._register(sid)
        reg = self.one("SELECT registration_id FROM registrations WHERE patient_id = %s "
                       "ORDER BY registration_id DESC LIMIT 1", (pid,))
        self.cash.post("/registrations", data={"action": "cancel",
                                               "registration_id": reg["registration_id"]},
                       follow_redirects=True)
        resp = self.cash.post("/registrations", data={"action": "cancel",
                                                      "registration_id": reg["registration_id"]},
                              follow_redirects=True)
        self.assertIn("仅待就诊", self.flash_text(resp))

    def test_unknown_schedule_rejected(self):
        resp = self.cash.post("/registrations", data={
            "action": "register",
            "schedule_id": 999999999,
            "patient_id": self.mk_patient(),
            "reg_type": "普通号",
            "pay_method": "现金",
        }, follow_redirects=True)
        self.assertIn("排班不存在", self.flash_text(resp))


@requires_db
class ScheduleTests(HisTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.client_as("admin01")
        self.doctor_id = self.mk_doctor()
        self.future = (datetime.now() + timedelta(days=45)).strftime("%Y-%m-%d")

    def test_cashier_cannot_edit_schedule(self):
        """收费人员可查看排班，但不能新增/停诊/删除。"""
        cash = self.client_as("cash01")
        resp = cash.post("/schedules", data={
            "action": "add", "doctor_id": self.doctor_id,
            "work_date": self.future, "shift_type": "上午",
            "max_registrations": 5,
        }, follow_redirects=True)
        self.assertIn("仅可查看排班", self.flash_text(resp))

    def test_admin_adds_schedule(self):
        resp = self.admin.post("/schedules", data={
            "action": "add", "doctor_id": self.doctor_id,
            "work_date": self.future, "shift_type": "上午",
            "clinic_room": "TST-诊室", "max_registrations": 12,
        }, follow_redirects=True)
        self.assertIn("排班创建成功", self.flash_text(resp))
        row = self.one("SELECT * FROM doctor_schedules WHERE doctor_id = %s AND work_date = %s "
                       "AND shift_type = '上午'", (self.doctor_id, self.future))
        self.assertIsNotNone(row)
        self.extra_cleanup.append(("DELETE FROM doctor_schedules WHERE schedule_id = %s",
                                   (row["schedule_id"],)))
        self.assertEqual(row["max_registrations"], 12)

    def test_overlapping_shift_conflict(self):
        """上午与全天时段重叠，同一天不能同时排。"""
        self.admin.post("/schedules", data={
            "action": "add", "doctor_id": self.doctor_id,
            "work_date": self.future, "shift_type": "上午", "max_registrations": 5,
        }, follow_redirects=True)
        row = self.one("SELECT schedule_id FROM doctor_schedules WHERE doctor_id = %s "
                       "AND work_date = %s AND shift_type = '上午'", (self.doctor_id, self.future))
        self.extra_cleanup.append(("DELETE FROM doctor_schedules WHERE schedule_id = %s",
                                   (row["schedule_id"],)))
        resp = self.admin.post("/schedules", data={
            "action": "add", "doctor_id": self.doctor_id,
            "work_date": self.future, "shift_type": "全天", "max_registrations": 5,
        }, follow_redirects=True)
        self.assertIn("时段冲突", self.flash_text(resp))

    def test_morning_and_afternoon_coexist(self):
        """上午 + 下午是两个时段，可以并存。"""
        for shift in ("上午", "下午"):
            self.admin.post("/schedules", data={
                "action": "add", "doctor_id": self.doctor_id,
                "work_date": self.future, "shift_type": shift, "max_registrations": 5,
            }, follow_redirects=True)
        rows = self.all("SELECT schedule_id FROM doctor_schedules WHERE doctor_id = %s "
                        "AND work_date = %s", (self.doctor_id, self.future))
        for r in rows:
            self.extra_cleanup.append(("DELETE FROM doctor_schedules WHERE schedule_id = %s",
                                       (r["schedule_id"],)))
        self.assertEqual(len(rows), 2)

    def test_delete_schedule_blocked_when_registered(self):
        """已被挂号引用的排班不能删除（按医生 + 日期反查）。"""
        sid = self.mk_schedule(self.doctor_id)          # 今天
        pid = self.mk_patient()
        self.mk_registration(pid, self.doctor_id, schedule_id=sid)
        resp = self.admin.post("/schedules", data={"action": "delete", "schedule_id": sid},
                               follow_redirects=True)
        self.assertIn("条挂号记录引用", self.flash_text(resp))
        self.assertIsNotNone(self.one(
            "SELECT 1 AS x FROM doctor_schedules WHERE schedule_id = %s", (sid,)))

    def test_mark_schedule_suspended(self):
        sid = self.mk_schedule(self.doctor_id, work_date=self.future)
        resp = self.admin.post("/schedules", data={
            "action": "status", "schedule_id": sid, "schedule_status": "停诊",
        }, follow_redirects=True)
        self.assertIn("排班状态已更新", self.flash_text(resp))
        row = self.one("SELECT schedule_status FROM doctor_schedules WHERE schedule_id = %s", (sid,))
        self.assertEqual(row["schedule_status"], "停诊")

    def test_doctor_cannot_touch_other_doctor_schedule(self):
        """医生只能操作自己的排班（doctor01 无法删除测试医生的排班）。"""
        other_sid = self.mk_schedule(self.mk_doctor(), work_date=self.future)
        doc = self.client_as("doctor01")
        resp = doc.post("/schedules", data={
            "action": "delete", "schedule_id": other_sid,
        }, follow_redirects=True)
        self.assertIn("只能操作自己的排班", self.flash_text(resp))


if __name__ == "__main__":
    unittest.main(verbosity=2)
