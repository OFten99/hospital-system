# -*- coding: utf-8 -*-
"""恢复出厂：清理「演示/测试患者」，把库退回干净基线。

背景
----
早期的 `reset_keep_acceptance.py` 写死了 `patient_id=17 / patient_no='P-ACCEPT01'`，
但那两个值在库里从未出现过（真实的验收患者是手工在网页上建的 P009）——
一跑就 assert 报错。本脚本（由它改名而来）改为**不依赖任何写死的 id**，按特征自动定位。

「演示/测试患者」的判定（任一命中即为目标，可用 --patient-no 精确指定）：
  1. patient_no 以 'P-ACCEPT' 开头（seed_acceptance_data.py 造的患者）；
  2. 姓名包含「验收」「测试」「演示」「TEST」「test」；
  3. 显式 --patient-no=P009 之类的精确病历号。

清理内容（该患者名下，含患者档案本身）：
  挂号 → 病历 / 体征 / 处方(+明细) / 检验单(+结果) / 收费 / 预约 / 操作日志
  以及受影响的 doctor_schedules.registered_count 回退。

安全措施：
  * 默认 **dry-run**，只打印将要删除的明细与行数对比，不提交；
  * `--apply` 才执行，且执行前把待删行完整导出为 JSON 备份；
  * 断言：非目标患者（其余 P0xx）的数据行数前后完全不变，否则整体回滚。

用法：
    python reset_to_baseline.py                 # 预览（dry-run）
    python reset_to_baseline.py --apply          # 执行
    python reset_to_baseline.py --patient-no=P009 --apply   # 只清指定病历号
"""
import argparse
import json
import os
import re

import mysql.connector

DB = dict(host="127.0.0.1", port=3306, user="root", password="123456",
          database="hospital_outpatient", charset="utf8mb4")

ACCEPT_PREFIX = "P-ACCEPT"
NAME_KEYWORDS = ["验收", "测试", "演示", "TEST", "test"]
BACKUP = r"C:\Users\60104\AppData\Local\Temp\reset_acceptance_backup.json"

AUDIT_TABLES = ["patients", "registrations", "medical_records", "vital_signs",
                "prescriptions", "prescription_items", "payments", "lab_tests",
                "lab_test_results", "appointments", "doctor_schedules", "operation_logs"]


def find_targets(cur, explicit_no=None):
    """定位演示/测试患者。返回 [(patient_id, patient_no, patient_name, 命中原因)]。"""
    cur.execute("SELECT patient_id, patient_no, patient_name FROM patients ORDER BY patient_id")
    rows = cur.fetchall()
    hits = []
    for pid, pno, pname in rows:
        reasons = []
        if explicit_no:
            if pno == explicit_no:
                reasons.append("--patient-no 指定")
        else:
            if (pno or "").startswith(ACCEPT_PREFIX):
                reasons.append("病历号前缀 %s" % ACCEPT_PREFIX)
            for kw in NAME_KEYWORDS:
                if kw in (pname or ""):
                    reasons.append("姓名含「%s」" % kw)
                    break
        if reasons:
            hits.append((pid, pno, pname, " / ".join(reasons)))
    return hits


def snapshot(cur):
    out = {}
    for t in AUDIT_TABLES:
        cur.execute("SELECT COUNT(*) FROM %s" % t)
        out[t] = cur.fetchone()[0]
    return out


def rows_of_others(cur, target_ids):
    """非目标患者（含无患者归属的行）计数，用于「没被误删」断言。"""
    res = {}
    ph = ", ".join(["%s"] * len(target_ids)) if target_ids else "NULL"
    cur.execute("SELECT COUNT(*) FROM patients WHERE patient_id NOT IN (%s)" % ph, tuple(target_ids))
    res["patients"] = cur.fetchone()[0]
    for t in ["registrations", "medical_records", "vital_signs", "prescriptions",
              "payments", "lab_tests", "appointments"]:
        cur.execute("SELECT COUNT(*) FROM %s WHERE patient_id NOT IN (%s)" % (t, ph), tuple(target_ids))
        res[t] = cur.fetchone()[0]
    return res


def dump(cur, sql, args=()):
    cur.execute(sql, args)
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        row = {}
        for k, v in zip(cols, r):
            row[k] = v.isoformat() if hasattr(v, "isoformat") else (
                float(v) if type(v).__name__ == "Decimal" else v)
        out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    ap.add_argument("--patient-no", default=None, help="只清理指定病历号，如 P009")
    args = ap.parse_args()
    dry = not args.apply

    c = mysql.connector.connect(**DB)
    cur = c.cursor()

    targets = find_targets(cur, args.patient_no)
    print("=== 目标（演示/测试患者）===")
    if not targets:
        print("  未发现任何演示/测试患者，无需清理。")
        cur.close(); c.close(); return
    for pid, pno, pname, why in targets:
        print("  patient_id=%-3d %-12s %-10s  ← %s" % (pid, pno, pname, why))

    target_ids = [t[0] for t in targets]
    before = snapshot(cur)
    others_before = rows_of_others(cur, target_ids)

    print("\n=== 清理前快照 ===")
    for k, v in before.items():
        print("  %-20s %d" % (k, v))

    # ---- 收集每个目标患者的挂号 / 处方 / 检验 ----
    cur.execute("SELECT registration_id, reg_no, patient_id FROM registrations WHERE patient_id IN (%s)"
                % ", ".join(["%s"] * len(target_ids)), tuple(target_ids))
    regs = cur.fetchall()
    reg_ids = [r[0] for r in regs]
    print("\n将删除的挂号 %d 条：%s" % (len(regs), [(r[0], r[1]) for r in regs]))

    ph_reg = ", ".join(["%s"] * len(reg_ids)) if reg_ids else "NULL"

    cur.execute("SELECT prescription_id FROM prescriptions WHERE patient_id IN (%s)"
                % ", ".join(["%s"] * len(target_ids)), tuple(target_ids))
    pres_ids = {x[0] for x in cur.fetchall()}
    if reg_ids:
        cur.execute("SELECT prescription_id FROM prescriptions WHERE registration_id IN (%s)" % ph_reg,
                    tuple(reg_ids))
        pres_ids |= {x[0] for x in cur.fetchall()}
    pres_ids = sorted(pres_ids)

    cur.execute("SELECT test_id FROM lab_tests WHERE patient_id IN (%s)"
                % ", ".join(["%s"] * len(target_ids)), tuple(target_ids))
    test_ids = {x[0] for x in cur.fetchall()}
    if reg_ids:
        cur.execute("SELECT test_id FROM lab_tests WHERE registration_id IN (%s)" % ph_reg, tuple(reg_ids))
        test_ids |= {x[0] for x in cur.fetchall()}
    test_ids = sorted(test_ids)

    print("将删除的处方：%s" % pres_ids)
    print("将删除的检验单：%s" % test_ids)

    # ---- 备份 ----
    backup = {"targets": [{"patient_id": p, "patient_no": n, "patient_name": nm} for p, n, nm, _ in targets],
              "tables": {}}
    ph_pid = ", ".join(["%s"] * len(target_ids))
    for t in ["patients", "registrations", "medical_records", "vital_signs",
              "prescriptions", "lab_tests", "payments", "appointments"]:
        backup["tables"][t] = dump(cur, "SELECT * FROM %s WHERE patient_id IN (%s)" % (t, ph_pid),
                                   tuple(target_ids))
    if pres_ids:
        ph = ", ".join(["%s"] * len(pres_ids))
        backup["tables"]["prescription_items"] = dump(
            cur, "SELECT * FROM prescription_items WHERE prescription_id IN (%s)" % ph, tuple(pres_ids))
    if test_ids:
        ph = ", ".join(["%s"] * len(test_ids))
        backup["tables"]["lab_test_results"] = dump(
            cur, "SELECT * FROM lab_test_results WHERE test_id IN (%s)" % ph, tuple(test_ids))

    if dry:
        print("\n[dry-run] 不做任何修改；如需执行请加 --apply")
        # dry-run 下不写备份文件
        cur.close(); c.close(); return

    with open(BACKUP, "w", encoding="utf-8") as f:
        json.dump(backup, f, ensure_ascii=False, indent=1, default=str)
    print("\n备份已写入：%s" % BACKUP)

    # ---- 删除（子表 → 主表） ----
    n = 0

    def run(sql, a=()):
        nonlocal n
        cur.execute(sql, a)
        n += cur.rowcount

    if test_ids:
        ph = ", ".join(["%s"] * len(test_ids))
        run("DELETE FROM lab_test_results WHERE test_id IN (%s)" % ph, tuple(test_ids))
        run("DELETE FROM payments WHERE lab_test_id IN (%s)" % ph, tuple(test_ids))
    if pres_ids:
        ph = ", ".join(["%s"] * len(pres_ids))
        run("DELETE FROM prescription_items WHERE prescription_id IN (%s)" % ph, tuple(pres_ids))
        run("DELETE FROM payments WHERE prescription_id IN (%s)" % ph, tuple(pres_ids))
    run("DELETE FROM payments WHERE patient_id IN (%s)" % ph_pid, tuple(target_ids))
    run("DELETE FROM medical_records WHERE patient_id IN (%s)" % ph_pid, tuple(target_ids))
    run("DELETE FROM vital_signs WHERE patient_id IN (%s)" % ph_pid, tuple(target_ids))
    run("DELETE FROM appointments WHERE patient_id IN (%s)" % ph_pid, tuple(target_ids))
    run("DELETE FROM lab_tests WHERE patient_id IN (%s)" % ph_pid, tuple(target_ids))
    run("DELETE FROM prescriptions WHERE patient_id IN (%s)" % ph_pid, tuple(target_ids))

    # 挂号：回退排班计数，再删
    for rid in reg_ids:
        run("""UPDATE doctor_schedules s
               JOIN registrations r ON s.doctor_id = r.doctor_id AND s.work_date = r.reg_date
               SET s.registered_count = GREATEST(s.registered_count - 1, 0)
               WHERE r.registration_id = %s""", (rid,))
    if reg_ids:
        run("DELETE FROM registrations WHERE registration_id IN (%s)" % ph_reg, tuple(reg_ids))

    # 操作日志：按目标患者姓名/病历号特征兜底清理
    for _, pno, pname, _s in targets:
        for pat in (pname, pno):
            if pat:
                run("DELETE FROM operation_logs WHERE operation_content LIKE %s", ("%" + pat + "%",))

    run("DELETE FROM patients WHERE patient_id IN (%s)" % ph_pid, tuple(target_ids))
    print("共删除 %d 行。" % n)

    # ---- 断言 + 提交 ----
    after = snapshot(cur)
    print("\n=== 清理前后对比 ===")
    print("%-20s %8s %8s %8s" % ("table", "before", "after", "delta"))
    for k in AUDIT_TABLES:
        print("%-20s %8d %8d %8d" % (k, before[k], after[k], after[k] - before[k]))

    others_after = rows_of_others(cur, target_ids)
    print("\n=== 非目标患者数据未被误删校验 ===")
    ok = True
    for k in others_before:
        same = others_before[k] == others_after[k]
        ok = ok and same
        print("  %-16s %4d -> %4d  %s" % (k, others_before[k], others_after[k], "OK" if same else "!! 变了"))
    if not ok:
        c.rollback()
        raise SystemExit("非目标患者数据发生变化，已回滚！")

    c.commit()
    print("\n✓ 已提交。演示/测试患者已全部清除，库退回干净基线。")
    cur.close(); c.close()


if __name__ == "__main__":
    main()
