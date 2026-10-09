# -*- coding: utf-8 -*-
"""「恢复出厂：只留验收患者」执行脚本。

目标（用户 2026-10-09 确认）：
  保留：验收测试患者 P-ACCEPT01（patient_id=17）的 4 条挂号及其全链路数据。
  清除：① seed 脚本给现有患者 P001/P002/P003/P004/P006 补齐的数据
            （reg_no = ACCEPT-X01..X05 及其病历/处方/检验/收费/体征）；
        ② 字面上的「测试患者」patient_id=9（P20260922184015）全部数据
            （1 挂号 + 1 收费 + 1 预约）。

不触碰：P001~P008 原有的挂号与业务数据（非 ACCEPT- 前缀的都不动）。

用法：
    python reset_keep_acceptance.py --dry-run    # 只打印将要删除的内容，不提交
    python reset_keep_acceptance.py --apply      # 真正执行
"""
import argparse
import sys

import mysql.connector

DB = dict(host="127.0.0.1", port=3306, user="root", password="123456",
          database="hospital_outpatient", charset="utf8mb4")

ACCEPT_PATIENT_NO = "P-ACCEPT01"
KEEP_PATIENT_ID = 17          # 验收患者，保留
TEST_PATIENT_ID = 9           # 「测试患者」，删除
BACKFILL_REG_PREFIX = "ACCEPT-X%"   # 补给现有患者的挂号
ACCEPT_REG_PREFIX = "ACCEPT-%"      # seed 造的全部挂号

ALL_TABLES = ["patients", "registrations", "medical_records", "prescriptions",
              "prescription_items", "lab_tests", "lab_test_results", "payments",
              "vital_signs", "appointments", "doctor_schedules", "operation_logs"]


def snap(cur):
    """各表行数快照。"""
    out = {}
    for t in ALL_TABLES:
        cur.execute("SELECT COUNT(*) FROM %s" % t)
        out[t] = cur.fetchone()[0]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    args = ap.parse_args()
    dry = not args.apply

    c = mysql.connector.connect(**DB)
    cur = c.cursor()

    print("=== 执行前快照 ===")
    before = snap(cur)
    for k, v in before.items():
        print("  %-20s %d" % (k, v))

    # ---- 1) 确认验收患者存在且不动 ----
    cur.execute("SELECT patient_id FROM patients WHERE patient_no=%s", (ACCEPT_PATIENT_NO,))
    r = cur.fetchone()
    assert r and r[0] == KEEP_PATIENT_ID, "验收患者 P-ACCEPT01 校验失败：%r" % (r,)
    print("\n[保留] 验收患者 patient_id=%d (%s)" % (KEEP_PATIENT_ID, ACCEPT_PATIENT_NO))

    # ---- 2) 定位补给现有患者的挂号（ACCEPT-X..）与测试患者(9)的挂号 ----
    cur.execute("SELECT registration_id, reg_no, patient_id FROM registrations "
                "WHERE reg_no LIKE %s", (BACKFILL_REG_PREFIX,))
    backfill_regs = cur.fetchall()
    cur.execute("SELECT registration_id, reg_no FROM registrations WHERE patient_id=%s",
                (TEST_PATIENT_ID,))
    test_regs = cur.fetchall()
    print("\n[删除] 补给现有患者的挂号 %d 条：%s" % (
        len(backfill_regs), [(x[0], x[1], x[2]) for x in backfill_regs]))
    print("[删除] 测试患者(9)的挂号 %d 条：%s" % (len(test_regs), [(x[0], x[1]) for x in test_regs]))

    backfill_ids = [x[0] for x in backfill_regs]
    test_reg_ids = [x[0] for x in test_regs]

    # ---- 3) 定位这些挂号名下的处方 / 检验 ----
    def ids_for(table, id_col, reg_ids):
        if not reg_ids:
            return []
        ph = ", ".join(["%s"] * len(reg_ids))
        cur.execute("SELECT %s FROM %s WHERE registration_id IN (%s)" % (id_col, table, ph),
                    tuple(reg_ids))
        return [r[0] for r in cur.fetchall()]

    del_reg_ids = backfill_ids + test_reg_ids
    pres_ids = ids_for("prescriptions", "prescription_id", del_reg_ids)
    test_ids = ids_for("lab_tests", "test_id", del_reg_ids)
    print("\n[删除] 关联处方 %d 张：%s" % (len(pres_ids), pres_ids))
    print("[删除] 关联检验单 %d 张：%s" % (len(test_ids), test_ids))

    n = 0
    def run(sql, args=()):
        nonlocal n
        if dry:
            cur.execute(sql.replace("DELETE", "SELECT COUNT(*)").split(" WHERE ")[0]
                        if False else sql, args)
            # dry-run 下也执行，最后统一 rollback
            n += cur.rowcount
        else:
            cur.execute(sql, args)
            n += cur.rowcount

    # 子表 → 主表（dry-run 也执行但整体回滚）
    if test_ids:
        ph = ", ".join(["%s"] * len(test_ids))
        run("DELETE FROM lab_test_results WHERE test_id IN (%s)" % ph, tuple(test_ids))
        run("DELETE FROM payments WHERE lab_test_id IN (%s)" % ph, tuple(test_ids))
    if pres_ids:
        ph = ", ".join(["%s"] * len(pres_ids))
        run("DELETE FROM prescription_items WHERE prescription_id IN (%s)" % ph, tuple(pres_ids))
        run("DELETE FROM payments WHERE prescription_id IN (%s)" % ph, tuple(pres_ids))
    if del_reg_ids:
        ph = ", ".join(["%s"] * len(del_reg_ids))
        run("DELETE FROM payments WHERE registration_id IN (%s)" % ph, tuple(del_reg_ids))
        run("DELETE FROM medical_records WHERE registration_id IN (%s)" % ph, tuple(del_reg_ids))
        run("DELETE FROM vital_signs WHERE registration_id IN (%s)" % ph, tuple(del_reg_ids))
    if test_ids:
        ph = ", ".join(["%s"] * len(test_ids))
        run("DELETE FROM lab_tests WHERE test_id IN (%s)" % ph, tuple(test_ids))
    if pres_ids:
        ph = ", ".join(["%s"] * len(pres_ids))
        run("DELETE FROM prescriptions WHERE prescription_id IN (%s)" % ph, tuple(pres_ids))

    # 测试患者(9) 的收费 + 预约（非 seed 数据，单独处理）
    run("DELETE FROM payments WHERE patient_id=%s", (TEST_PATIENT_ID,))
    run("DELETE FROM appointments WHERE patient_id=%s", (TEST_PATIENT_ID,))
    run("DELETE FROM vital_signs WHERE patient_id=%s", (TEST_PATIENT_ID,))
    run("DELETE FROM medical_records WHERE patient_id=%s", (TEST_PATIENT_ID,))
    run("DELETE FROM lab_tests WHERE patient_id=%s", (TEST_PATIENT_ID,))
    run("DELETE FROM prescriptions WHERE patient_id=%s", (TEST_PATIENT_ID,))

    # 挂号：先还原 registered_count，再删
    for rid in del_reg_ids:
        run("""UPDATE doctor_schedules s
               JOIN registrations r ON s.doctor_id = r.doctor_id AND s.work_date = r.reg_date
               SET s.registered_count = GREATEST(s.registered_count - 1, 0)
               WHERE r.registration_id = %s""", (rid,))
    if del_reg_ids:
        ph = ", ".join(["%s"] * len(del_reg_ids))
        run("DELETE FROM registrations WHERE registration_id IN (%s)" % ph, tuple(del_reg_ids))

    # 最后删测试患者档案
    run("DELETE FROM patients WHERE patient_id=%s AND patient_no<>%s",
        (TEST_PATIENT_ID, ACCEPT_PATIENT_NO))

    print("\n共计影响 %d 行。" % n)

    print("\n=== 执行后快照（dry-run 下为事务内预览） ===")
    print("%-20s %8s %8s %8s" % ("table", "before", "after", "delta"))
    after = snap(cur)
    for k in ALL_TABLES:
        print("%-20s %8d %8d %8d" % (k, before[k], after[k], after[k] - before[k]))

    # 验收患者必须原样保留
    cur.execute("""SELECT (SELECT COUNT(*) FROM registrations WHERE patient_id=%s),
                          (SELECT COUNT(*) FROM medical_records WHERE patient_id=%s),
                          (SELECT COUNT(*) FROM prescriptions WHERE patient_id=%s),
                          (SELECT COUNT(*) FROM lab_tests WHERE patient_id=%s),
                          (SELECT COUNT(*) FROM payments WHERE patient_id=%s),
                          (SELECT COUNT(*) FROM vital_signs WHERE patient_id=%s)""",
                (KEEP_PATIENT_ID,) * 6)
    chk = cur.fetchone()
    print("\n验收患者(17) 保留：挂号%s 病历%s 处方%s 检验%s 收费%s 体征%s" % chk)
    assert chk == (4, 2, 2, 2, 3, 2), "验收患者数据被动到了！%r" % (chk,)
    print("✓ 验收患者数据完好")

    if dry:
        c.rollback()
        print("\n[dry-run] 已回滚，数据库未被修改。")
    else:
        c.commit()
        print("\n[apply] 已提交。")
    cur.close(); c.close()


if __name__ == "__main__":
    main()
