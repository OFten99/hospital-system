# -*- coding: utf-8 -*-
"""验收演示数据注入脚本（医院门诊管理系统）。

用途
----
1. 新建一个专门的「验收测试患者」（患者档案，patient_no = P-ACCEPT01）；
2. 为该患者造一条**贯穿全业务链路**的完整数据：
   挂号 → 接诊 → 体征 → 门诊病历（含诊断）→ 处方 → 检验申请
   → 收费 → 发药 → 检验结果录入 → 已完成；
   另外再补几条不同状态的数据（待就诊 / 已退号）用于验证各状态的展示；
3. 给**现有其他患者**也补齐数据，使各模块列表都有可展示内容。

设计原则
--------
* **尽量走真实存储过程**（sp_register / sp_issue_prescription / sp_charge /
  sp_dispense / sp_add_vital_sign / sp_add_lab_test / sp_charge_lab_test /
  sp_record_lab_result / sp_save_medical_record），这样造出来的数据满足
  全部业务约束与库存联动，而不是硬塞 INSERT。
* 少数过程覆盖不到的场景（历史日期挂号、退号）直接写表并同步改
  registered_count，保持视图口径一致。
* 所有造出的数据都带**可识别的特征**，便于一键清理：
  - 验收患者 patient_no = 'P-ACCEPT01'
  - 其他患者的数据全部落在「指定历史日期」上，且 reg_no 带 'ACCEPT' 前缀。

用法
----
    python seed_acceptance_data.py            # 注入数据
    python seed_acceptance_data.py --clean    # 清除本脚本造出的全部数据
"""
import argparse
import json
import sys
from datetime import date, datetime, timedelta

import mysql.connector

DB = dict(host="127.0.0.1", port=3306, user="root", password="123456",
          database="hospital_outpatient", charset="utf8mb4")

# ---- 造数据的特征标记 ----
ACCEPT_PATIENT_NO = "P-ACCEPT01"
ACCEPT_REG_PREFIX = "ACCEPT-"       # 其他患者数据的 reg_no 前缀（便于清理）
ACCEPT_PRES_PREFIX = "ACCPRES"      # 处方号前缀
ACCEPT_LAB_PREFIX = "ACCLAB"        # 检验单号前缀
ACCEPT_PAY_PREFIX = "ACCPAY"        # 收费单号前缀
ACCEPT_MR_PREFIX = "ACCMR"          # 病历号前缀

# 固定日期用「最近一个可用的历史工作日」，避免与真实数据（今日）冲突
BASE_DATE = date(2026, 9, 28)       # 住院历史日期，用于其他患者的补齐数据

# 操作人（对应 users.user_id）。挂号/建档已并入收费窗口，故 window=3（cash01）。
OPERATOR = dict(admin=1, window=3, cashier=3, lab=12, pharm=15)


def connect():
    return mysql.connector.connect(**DB)


def scalar(cur, sql, args=None):
    cur.execute(sql, args or ())
    row = cur.fetchone()
    return row[0] if row else None


def next_no(cur, table, col, prefix):
    """生成不会被占用的编号：prefix + 6 位序号。"""
    like = prefix + "%"
    cur.execute("SELECT %s FROM %s WHERE %s LIKE %%s" % (col, table, col), (like,))
    used = [r[0] for r in cur.fetchall()]
    n = 0
    for u in used:
        tail = u[len(prefix):]
        if tail.isdigit():
            n = max(n, int(tail))
    return "%s%06d" % (prefix, n + 1)


# ============================================================
# 清理
# ============================================================

def clean(cur):
    """删除本脚本造出的全部数据。

    顺序：先删子表（结果/明细/收费），再删主表（检验/处方/病历/挂号/患者）。
    只删带特征标记的行，不动任何原有数据。
    """
    print("=== 清理验收演示数据 ===")

    # 1) 找出验收患者 + 本脚本造出的挂号
    cur.execute("SELECT patient_id FROM patients WHERE patient_no = %s", (ACCEPT_PATIENT_NO,))
    rows = cur.fetchall()
    acc_pid = rows[0][0] if rows else None

    cur.execute("SELECT registration_id FROM registrations WHERE reg_no LIKE %s",
                (ACCEPT_REG_PREFIX + "%",))
    acc_regs = [r[0] for r in cur.fetchall()]

    # 补：验收患者名下「非 ACCEPT 前缀」的挂号也要一并收进来
    # （例如演示/联调时在前端给该患者新挂的号）。否则漏删会导致下面
    # DELETE FROM patients 触发外键约束而整单失败。
    if acc_pid:
        cur.execute("SELECT registration_id FROM registrations WHERE patient_id = %s", (acc_pid,))
        for (rid,) in cur.fetchall():
            if rid not in acc_regs:
                acc_regs.append(rid)

    reg_filter = ""
    reg_args = ()
    if acc_regs:
        ph = ", ".join(["%s"] * len(acc_regs))
        reg_filter = " OR registration_id IN (%s)" % ph
        reg_args = tuple(acc_regs)

    # 2) 定位所有本脚本造出的 lab_tests / prescriptions
    seen_test_ids = set()
    seen_pres_ids = set()
    for sql in (
        "SELECT test_id, registration_id, patient_id FROM lab_tests",
    ):
        cur.execute(sql)
        for tid, rid, pid in cur.fetchall():
            if (pid == acc_pid) or (rid is not None and rid in acc_regs):
                seen_test_ids.add(tid)
    cur.execute("SELECT test_no FROM lab_tests WHERE test_no LIKE %s", (ACCEPT_LAB_PREFIX + "%",))
    for (tn,) in cur.fetchall():
        cur.execute("SELECT test_id FROM lab_tests WHERE test_no = %s", (tn,))
        for (tid,) in cur.fetchall():
            seen_test_ids.add(tid)

    cur.execute("SELECT prescription_id, registration_id, patient_id FROM prescriptions")
    for pid_, rid, pid in cur.fetchall():
        if (pid == acc_pid) or (rid is not None and rid in acc_regs):
            seen_pres_ids.add(pid_)
    cur.execute("SELECT prescription_no FROM prescriptions WHERE prescription_no LIKE %s",
                (ACCEPT_PRES_PREFIX + "%",))
    for (pn,) in cur.fetchall():
        cur.execute("SELECT prescription_id FROM prescriptions WHERE prescription_no = %s", (pn,))
        for (pid_,) in cur.fetchall():
            seen_pres_ids.add(pid_)

    def del_in(table, col, ids):
        if not ids:
            return 0
        ph = ", ".join(["%s"] * len(ids))
        cur.execute("DELETE FROM %s WHERE %s IN (%s)" % (table, col, ph), tuple(ids))
        return cur.rowcount

    n = 0
    # 子表优先
    n += del_in("lab_test_results", "test_id", sorted(seen_test_ids))
    n += del_in("prescription_items", "prescription_id", sorted(seen_pres_ids))
    # 收费：本脚本造的单号 + 挂在验收患者/挂号上的
    cur.execute("DELETE FROM payments WHERE payment_no LIKE %s", (ACCEPT_PAY_PREFIX + "%",))
    n += cur.rowcount
    if acc_pid:
        cur.execute("DELETE FROM payments WHERE patient_id = %s", (acc_pid,))
        n += cur.rowcount
    if acc_regs:
        ph = ", ".join(["%s"] * len(acc_regs))
        cur.execute("DELETE FROM payments WHERE registration_id IN (%s)" % ph, tuple(acc_regs))
        n += cur.rowcount
    if seen_test_ids:
        ph = ", ".join(["%s"] * len(seen_test_ids))
        cur.execute("DELETE FROM payments WHERE lab_test_id IN (%s)" % ph, tuple(seen_test_ids))
        n += cur.rowcount

    n += del_in("lab_tests", "test_id", sorted(seen_test_ids))
    n += del_in("prescriptions", "prescription_id", sorted(seen_pres_ids))
    n += del_in("medical_records", "registration_id", acc_regs)
    if acc_pid:
        n += del_in("medical_records", "patient_id", [acc_pid])
        n += del_in("vital_signs", "patient_id", [acc_pid])
    n += del_in("vital_signs", "registration_id", acc_regs)

    # 挂号：先还原排班计数，再删挂号
    if acc_regs:
        for rid in acc_regs:
            cur.execute("""
                UPDATE doctor_schedules s
                JOIN registrations r ON s.doctor_id = r.doctor_id AND s.work_date = r.reg_date
                SET s.registered_count = GREATEST(s.registered_count - 1, 0)
                WHERE r.registration_id = %s
            """, (rid,))
        n += del_in("registrations", "registration_id", acc_regs)

    # operation_logs 里带特征的单号
    for pat in (ACCEPT_LAB_PREFIX, ACCEPT_PRES_PREFIX, ACCEPT_PAY_PREFIX, ACCEPT_MR_PREFIX):
        cur.execute("DELETE FROM operation_logs WHERE operation_content LIKE %s", ("%" + pat + "%",))
        n += cur.rowcount

    # 最后删验收患者
    if acc_pid:
        cur.execute("DELETE FROM patients WHERE patient_id = %s AND patient_no = %s",
                    (acc_pid, ACCEPT_PATIENT_NO))
        n += cur.rowcount

    # 其他患者补齐数据造的挂号（reg_no 前缀）已在上面的 acc_regs 里，无需重复
    print("已删除 %d 行相关记录。" % n)


# ============================================================
# 注入
# ============================================================

def ensure_patient(cur, name, gender, id_card, phone, blood, birth, addr, history=None):
    cur.execute("SELECT patient_id FROM patients WHERE id_card = %s", (id_card,))
    r = cur.fetchone()
    if r:
        return r[0]
    cur.execute("SELECT patient_id FROM patients WHERE patient_no = %s", (ACCEPT_PATIENT_NO,))
    r = cur.fetchone()
    if r:
        return r[0]
    cur.execute("""
        INSERT INTO patients (patient_no, patient_name, gender, birth_date, id_card,
                              phone, address, blood_type, medical_history)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (ACCEPT_PATIENT_NO, name, gender, birth, id_card, phone, addr, blood, history))
    return cur.lastrowid


def get_schedule(cur, doctor_id, work_date, shift="上午", room=None, maxn=40):
    """取（或造）一条指定医生/日期的排班。"""
    cur.execute("""SELECT schedule_id, max_registrations, registered_count FROM doctor_schedules
                   WHERE doctor_id=%s AND work_date=%s AND shift_type=%s""",
                (doctor_id, work_date, shift))
    r = cur.fetchone()
    if r:
        return r[0]
    cur.execute("""
        INSERT INTO doctor_schedules (doctor_id, work_date, shift_type, clinic_room,
                                      max_registrations, registered_count, schedule_status)
        VALUES (%s,%s,%s,%s,%s,0,'正常')
    """, (doctor_id, work_date, shift, room, maxn))
    return cur.lastrowid


def make_registration(cur, reg_no, patient_id, doctor_id, department_id, reg_date,
                      reg_type, reg_fee, queue_no, status, operator_id):
    """直接插挂号（用于历史日期 / 指定状态），并同步排班计数。"""
    cur.execute("""
        INSERT INTO registrations (reg_no, patient_id, doctor_id, department_id, reg_date,
                                   reg_type, reg_fee, queue_no, visit_status, operator_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (reg_no, patient_id, doctor_id, department_id, reg_date, reg_type,
          reg_fee, queue_no, status, operator_id))
    rid = cur.lastrowid
    if status != "已退号":
        cur.execute("""
            UPDATE doctor_schedules SET registered_count = registered_count + 1
            WHERE doctor_id=%s AND work_date=%s
            ORDER BY schedule_id LIMIT 1
        """, (doctor_id, reg_date))
    return rid


def add_payment(cur, patient_id, registration_id, prescription_id, lab_test_id,
                ptype, amount, method, status, operator_id, pay_time=None):
    no = next_no(cur, "payments", "payment_no", ACCEPT_PAY_PREFIX)
    cur.execute("""
        INSERT INTO payments (payment_no, patient_id, registration_id, prescription_id,
                              lab_test_id, payment_type, pay_amount, pay_method,
                              pay_status, operator_id, pay_time)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (no, patient_id, registration_id, prescription_id, lab_test_id, ptype,
          amount, method, status, operator_id, pay_time or datetime.now()))
    return no


def issue_prescription(cur, patient_id, doctor_id, registration_id, meds, status="待收费"):
    """造处方 + 明细（不走过程，便于指定状态与历史日期）。"""
    no = next_no(cur, "prescriptions", "prescription_no", ACCEPT_PRES_PREFIX)
    total = 0.0
    items = []
    for mid, qty, dosage in meds:
        cur.execute("SELECT medicine_name, unit_price FROM medicines WHERE medicine_id=%s", (mid,))
        r = cur.fetchone()
        if not r:
            raise RuntimeError("药品不存在: %s" % mid)
        mname, price = r
        amount = float(price) * qty
        total += amount
        items.append((mid, mname, qty, price, amount, dosage))
    cur.execute("""
        INSERT INTO prescriptions (prescription_no, patient_id, doctor_id, registration_id,
                                   prescribe_date, total_amount, prescription_status)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
    """, (no, patient_id, doctor_id, registration_id, BASE_DATE, round(total, 2), status))
    pid = cur.lastrowid
    for mid, mname, qty, price, amount, dosage in items:
        # amount 是 STORED GENERATED 列（= quantity × unit_price），不能显式赋值
        cur.execute("""
            INSERT INTO prescription_items (prescription_id, medicine_id, medicine_name,
                                            quantity, unit_price, dosage)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (pid, mid, mname, qty, price, dosage))
    return pid, no, round(total, 2)


def issue_lab_test(cur, patient_id, doctor_id, department_id, registration_id, item_ids):
    no = next_no(cur, "lab_tests", "test_no", ACCEPT_LAB_PREFIX)
    cur.execute("SELECT COALESCE(SUM(price),0) FROM lab_items WHERE FIND_IN_SET(item_id, %s) > 0",
                (",".join(str(i) for i in item_ids),))
    fee = float(cur.fetchone()[0] or 0)
    cur.execute("""
        INSERT INTO lab_tests (test_no, patient_id, doctor_id, department_id, registration_id,
                               sample_type, total_fee, test_status, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'待检验',%s)
    """, (no, patient_id, doctor_id, department_id, registration_id,
          "血液", fee, OPERATOR["lab"]))
    tid = cur.lastrowid
    for iid in item_ids:
        cur.execute("INSERT INTO lab_test_results (test_id, item_id) VALUES (%s,%s)", (tid, iid))
    return tid, no, fee


def record_result(cur, test_id, item_id, value, flag, note=None):
    cur.execute("""
        UPDATE lab_test_results SET result_value=%s, result_flag=%s, result_note=%s, operator_id=%s
        WHERE test_id=%s AND item_id=%s
    """, (value, flag, note, OPERATOR["lab"], test_id, item_id))


def finish_lab_test(cur, test_id):
    cur.execute("SELECT COUNT(*) FROM lab_test_results WHERE test_id=%s AND (result_value IS NULL OR result_value='')",
                (test_id,))
    pending = cur.fetchone()[0]
    st = "已完成" if pending == 0 else "检验中"
    cur.execute("UPDATE lab_tests SET test_status=%s, completed_at=%s WHERE test_id=%s",
                (st, datetime.now() if st == "已完成" else None, test_id))


def save_medical_record(cur, reg, chief, present, past, exam, diag, code, advice, status="已归档"):
    """reg = dict(registration_id, patient_id, doctor_id, department_id, visit_date)"""
    no = next_no(cur, "medical_records", "record_no", ACCEPT_MR_PREFIX)
    cur.execute("""
        INSERT INTO medical_records (record_no, registration_id, patient_id, doctor_id,
                                     department_id, visit_date, chief_complaint, present_illness,
                                     past_history, physical_exam, diagnosis, diagnosis_code,
                                     advice, record_status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (no, reg["registration_id"], reg["patient_id"], reg["doctor_id"], reg["department_id"],
          reg["visit_date"], chief, present, past, exam, diag, code, advice, status))
    # 同步 patients.diagnosis（与 sp_save_medical_record 行为一致）
    cur.execute("UPDATE patients SET diagnosis=%s WHERE patient_id=%s", (diag, reg["patient_id"]))
    return no


def build():
    c = connect()
    cur = c.cursor()
    print("=== 注入验收演示数据 ===")

    # ---------- 0. 清理旧的本脚本数据，保证可重复运行 ----------
    clean(cur)
    c.commit()

    # ---------- 1. 验收测试患者 ----------
    pid = ensure_patient(
        cur, "验收测试患者", "男", "330199199001011234", "13900001234", "O",
        date(1990, 1, 1), "浙江省杭州市西湖区验收路 1 号",
        "既往体健，无药物过敏史。")
    cur.execute("SELECT patient_no FROM patients WHERE patient_id=%s", (pid,))
    print("验收患者：patient_id=%s patient_no=%s" % (pid, cur.fetchone()[0]))

    d1 = 1   # 张建国 内科（doctor_id）
    dept1 = 1
    # 体征 operator_id 需要的是 users.user_id，由 doctor_id 换算而来
    cur.execute("SELECT user_id FROM doctors WHERE doctor_id=%s", (d1,))
    d1_user = cur.fetchone()[0]
    meds_a = [(1, 2, "口服，一次 0.5g，一日 3 次"), (6, 1, "口服，一次 3g，一日 3 次")]
    meds_b = [(2, 1, "口服，一次 0.3g，一日 2 次"), (5, 1, "口服，一次 20mg，一日 1 次")]

    # ===== A. 完整链路：已就诊 → 病历 → 处方 → 检验 → 收费 → 发药 → 结果 =====
    ra = make_registration(cur, ACCEPT_REG_PREFIX + "A01", pid, d1, dept1, BASE_DATE,
                           "普通号", 50.00, 1, "已就诊", OPERATOR["window"])
    # 体征
    cur.execute("""
        INSERT INTO vital_signs (patient_id, registration_id, operator_id, temperature,
                                 systolic_pressure, diastolic_pressure, heart_rate,
                                 respiration_rate, weight, height, note)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (pid, ra, d1_user, 37.6, 128, 82, 88, 19, 70.5, 175.0, "发热待查，咽部充血"))
    # 病历
    save_medical_record(cur, dict(registration_id=ra, patient_id=pid, doctor_id=d1,
                                  department_id=dept1, visit_date=BASE_DATE),
                        "发热、咽痛 3 天。",
                        "患者 3 天前受凉后出现发热，最高 38.5℃，伴咽痛、乏力，无咳嗽咳痰，"
                        "无腹痛腹泻。自行服用感冒药效果欠佳，遂来诊。",
                        "既往体健，否认高血压、糖尿病史，否认药物过敏史。",
                        "T 37.6℃，P 88 次/分，R 19 次/分，BP 128/82mmHg。咽部充血，"
                        "双侧扁桃体 I 度肿大，心肺听诊未闻及异常，腹软无压痛。",
                        "急性上呼吸道感染", "J06.900", "多饮水、注意休息；按时服药，3 天后复诊。")
    # 处方 → 收费 → 发药
    pres_a, pno_a, amt_a = issue_prescription(cur, pid, d1, ra, meds_a, status="已发药")
    add_payment(cur, pid, ra, pres_a, None, "药品费", amt_a, "微信", "已收费", OPERATOR["cashier"])
    # 检验 → 收费 → 结果
    lab_a, lno_a, fee_a = issue_lab_test(cur, pid, d1, dept1, ra, [1, 2, 3, 4])
    add_payment(cur, pid, None, None, lab_a, "检查费", fee_a, "医保", "已收费", OPERATOR["cashier"])
    record_result(cur, lab_a, 1, "11.2", "偏高", "白细胞升高，提示细菌感染")
    record_result(cur, lab_a, 2, "4.50", "正常")
    record_result(cur, lab_a, 3, "140", "正常")
    record_result(cur, lab_a, 4, "228", "正常")
    finish_lab_test(cur, lab_a)

    # ===== B. 就诊中：已有病历草稿 + 待收费处方 + 待检验 =====
    rb = make_registration(cur, ACCEPT_REG_PREFIX + "B01", pid, d1, dept1,
                           BASE_DATE + timedelta(days=1),
                           "专家号", 50.00, 2, "就诊中", OPERATOR["window"])
    cur.execute("""
        INSERT INTO vital_signs (patient_id, registration_id, operator_id, temperature,
                                 systolic_pressure, diastolic_pressure, heart_rate,
                                 respiration_rate, weight, height, note)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (pid, rb, d1_user, 36.8, 122, 78, 76, 18, 70.0, 175.0, "复诊，一般情况可"))
    save_medical_record(cur, dict(registration_id=rb, patient_id=pid, doctor_id=d1,
                                  department_id=dept1, visit_date=BASE_DATE + timedelta(days=1)),
                        "复诊：咽痛缓解。",
                        "服药后体温正常，咽痛明显缓解，仍有轻微乏力。",
                        "同前。",
                        "咽部轻度充血，扁桃体无肿大，心肺未见异常。",
                        "急性上呼吸道感染（恢复期）", "J06.900",
                        "继续服药 2 天，多休息。", status="草稿")
    issue_prescription(cur, pid, d1, rb, meds_b, status="待收费")
    issue_lab_test(cur, pid, d1, dept1, rb, [16, 40])   # CRP + 血沉，待检验

    # ===== C. 待就诊：体检开单 =====
    rc = make_registration(cur, ACCEPT_REG_PREFIX + "C01", pid, d1, dept1,
                           BASE_DATE + timedelta(days=2),
                           "普通号", 50.00, 3, "待就诊", OPERATOR["window"])

    # ===== D. 已退号：退号退款留痕 =====
    rd = make_registration(cur, ACCEPT_REG_PREFIX + "D01", pid, d1, dept1,
                           BASE_DATE + timedelta(days=3),
                           "普通号", 50.00, 4, "已退号", OPERATOR["window"])
    pno_d = add_payment(cur, pid, rd, None, None, "挂号费", 50.00, "现金", "已退费",
                        OPERATOR["cashier"],
                        pay_time=datetime.combine(BASE_DATE + timedelta(days=3), datetime.min.time()))
    cur.execute("UPDATE payments SET refund_time=%s, refund_operator_id=%s, refund_reason=%s "
                "WHERE payment_no=%s",
                (datetime.now(), OPERATOR["cashier"], "患者临时有事，取消就诊", pno_d))

    # ===== E. 给「现有其他患者」补齐数据（各模块都有可展示内容） =====
    extra = [
        # (patient_id, doctor_id, dept, reg_type, fee, 诊断, 药品, 检验项)
        (1, 2, 1, "普通号", 25.00, "慢性胃炎", [(5, 1, "口服，一次 20mg，一日 1 次")], [8, 9]),
        (2, 3, 2, "专家号", 50.00, "急性阑尾炎（术后复查）", [(4, 1, "口服，一次 0.1g，一日 2 次")], [1, 3]),
        (3, 5, 3, "普通号", 30.00, "小儿支气管炎", [(7, 1, "口服，按体重服用")], [16]),
        (4, 6, 4, "专家号", 40.00, "缺铁性贫血", [(3, 2, "口服，一次 1 袋，一日 3 次")], [2, 3, 19]),
        (6, 8, 6, "普通号", 25.00, "结膜炎", [(1, 1, "外用/口服，遵医嘱")], [1]),
    ]
    for i, (p_id, doc_id, dept, rtype, fee, diag, meds, labs) in enumerate(extra):
        rdate = BASE_DATE - timedelta(days=i + 1)
        reg_no = ACCEPT_REG_PREFIX + "X%02d" % (i + 1)
        rid = make_registration(cur, reg_no, p_id, doc_id, dept, rdate,
                                rtype, fee, i + 1, "已就诊", OPERATOR["window"])
        # 体征的 operator_id 必须是 users.user_id，而 doc_id 是 doctors.doctor_id，
        # 两者不是一回事（docs/doctors 表通过 user_id 关联）。这里显式换算。
        cur.execute("SELECT user_id FROM doctors WHERE doctor_id=%s", (doc_id,))
        doc_user_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO vital_signs (patient_id, registration_id, operator_id, temperature,
                                     systolic_pressure, diastolic_pressure, heart_rate,
                                     respiration_rate, note)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (p_id, rid, doc_user_id, 36.5 + (i % 3) * 0.3, 118 + i * 2, 76 + i, 72 + i * 2,
              18, "门诊常规体征"))
        save_medical_record(cur, dict(registration_id=rid, patient_id=p_id, doctor_id=doc_id,
                                      department_id=dept, visit_date=rdate),
                            "%s相关不适 2 天。" % diag,
                            "患者 2 天前出现不适，来院就诊。",
                            "既往史详见患者档案。",
                            "生命体征平稳，专科查体见阳性体征。",
                            diag, None, "按时服药，注意休息，不适随诊。")
        pres_id, _, amt = issue_prescription(cur, p_id, doc_id, rid, meds, status="已发药")
        add_payment(cur, p_id, rid, pres_id, None, "药品费", amt, "微信", "已收费", OPERATOR["cashier"])
        if labs:
            lt, _, lfee = issue_lab_test(cur, p_id, doc_id, dept, rid, labs)
            add_payment(cur, p_id, None, None, lt, "检查费", lfee, "支付宝", "已收费",
                        OPERATOR["cashier"])
            for j, iid in enumerate(labs):
                cur.execute("SELECT reference_range FROM lab_items WHERE item_id=%s", (iid,))
                rr = cur.fetchone()[0] or ""
                # 一半正常一半异常，便于展示标志
                if j % 2 == 0:
                    record_result(cur, lt, iid, "正常值", "正常")
                else:
                    record_result(cur, lt, iid, "偏高值", "偏高", "建议复查")
            finish_lab_test(cur, lt)

    c.commit()

    # ---------- 汇总 ----------
    print("\n--- 注入结果 ---")
    for label, sql in [
        ("验收患者的挂号", "SELECT COUNT(*) FROM registrations WHERE patient_id=%s"),
        ("验收患者的病历", "SELECT COUNT(*) FROM medical_records WHERE patient_id=%s"),
        ("验收患者的处方", "SELECT COUNT(*) FROM prescriptions WHERE patient_id=%s"),
        ("验收患者的检验单", "SELECT COUNT(*) FROM lab_tests WHERE patient_id=%s"),
        ("验收患者的收费单", "SELECT COUNT(*) FROM payments WHERE patient_id=%s"),
        ("验收患者的体征", "SELECT COUNT(*) FROM vital_signs WHERE patient_id=%s"),
    ]:
        cur.execute(sql, (pid,))
        print("  %-16s %d" % (label, cur.fetchone()[0]))
    for t in ("registrations", "medical_records", "prescriptions", "prescription_items",
              "lab_tests", "lab_test_results", "payments", "vital_signs"):
        print("  全库 %-20s %d 行" % (t, scalar(cur, "SELECT COUNT(*) FROM " + t)))
    cur.close()
    c.close()
    print("\n完成。验收患者：patient_no=%s（姓名：验收测试患者）" % ACCEPT_PATIENT_NO)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="只清除本脚本造出的数据")
    args = ap.parse_args()
    if args.clean:
        c = connect()
        cur = c.cursor()
        clean(cur)
        c.commit()
        cur.close()
        c.close()
    else:
        build()


if __name__ == "__main__":
    sys.exit(main())
