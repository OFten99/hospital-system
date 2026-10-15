# -*- coding: utf-8 -*-
"""演示前准备（演示/答辩当天开演前跑一次）。

解决两个现场最容易翻车的点
--------------------------
1. **今日没有排班** —— `/registrations` 的排班下拉只查 `work_date = 今天`，
   库里最新排班往往停在几天前，一开演「挂号」下拉就是空的。本脚本按需补今天的排班。
2. **记不住该点哪条记录** —— 脚本最后打印一张「演示清单」，直接告诉你每一步
   该用哪个患者 / 哪张单号，不用现场翻列表找。

安全性
------
* 幂等：只补「今天 + 该医生 + 该班次」尚不存在的排班，重复跑不会产生重复行
  （排班表上有唯一键 `uk_schedule_doctor_date`）。
* 只新增排班，**不改动任何业务数据**（不碰患者、挂号、处方、收费）。
* `--date` 可指定别的日期；`--dry-run` 只看不写。

用法
----
    python demo_prep.py                # 补今日排班 + 打印演示清单
    python demo_prep.py --dry-run      # 只看要补几条，不写库
    python demo_prep.py --date 2026-10-16
"""
import argparse
import sys
from datetime import date

import mysql.connector

DB = dict(host="127.0.0.1", port=3306, user="root", password="123456",
          database="hospital_outpatient", charset="utf8mb4")

LINE = "=" * 66


def hr(title=""):
    print(LINE)
    if title:
        print("  " + title)
        print(LINE)


def connect():
    try:
        return mysql.connector.connect(**DB)
    except mysql.connector.Error as e:
        print("[错误] 连不上数据库：%s" % e)
        print("       先双击 start_mysql.bat 启动 MySQL，再重跑本脚本。")
        sys.exit(1)


# ----------------------------------------------------------------------
# 1. 补今日排班
# ----------------------------------------------------------------------
def ensure_schedules(conn, work_date, dry_run=False):
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT d.doctor_id, u.user_name, dep.department_name, d.consultation_fee
        FROM doctors d
        JOIN users u ON d.user_id = u.user_id
        JOIN departments dep ON u.department_id = dep.department_id
        ORDER BY d.doctor_id
        """
    )
    doctors = cur.fetchall()

    cur.execute(
        "SELECT doctor_id, shift_type FROM doctor_schedules WHERE work_date = %s",
        (work_date,),
    )
    existing = {(r["doctor_id"], r["shift_type"]) for r in cur.fetchall()}

    added, skipped = [], []
    for doc in doctors:
        shift = "全天"
        if (doc["doctor_id"], shift) in existing:
            skipped.append(doc["user_name"])
            continue
        added.append(doc)

    hr("1/2  补齐 %s 的排班" % work_date)
    if not doctors:
        print("  [警告] doctors 表是空的，无法补排班。")
        return
    if not added:
        print("  今日排班已齐（%d 位医生），无需新增。" % len(doctors))
        return

    for doc in added:
        print("  + %-6s %-6s 诊室%s01  号源 30  挂号费 %.2f 元"
              % (doc["user_name"], doc["department_name"],
                 doc["doctor_id"], float(doc["consultation_fee"])))

    if dry_run:
        print("\n  [dry-run] 未写库。去掉 --dry-run 即执行。")
        return

    for doc in added:
        cur.execute(
            """
            INSERT INTO doctor_schedules
                (doctor_id, work_date, shift_type, clinic_room,
                 max_registrations, registered_count, schedule_status)
            VALUES (%s, %s, %s, %s, 30, 0, '正常')
            """,
            (doc["doctor_id"], work_date, "全天", "%d01" % doc["doctor_id"]),
        )
    conn.commit()
    print("\n  已新增 %d 条排班（%s）。" % (len(added), work_date))


# ----------------------------------------------------------------------
# 2. 打印演示清单
# ----------------------------------------------------------------------
def show_checklist(conn, work_date):
    cur = conn.cursor(dictionary=True)

    hr("2/2  演示清单（今天的库里实际有什么）")

    # 今日排班
    cur.execute(
        """
        SELECT u.user_name, dep.department_name, s.shift_type, s.clinic_room,
               s.registered_count, s.max_registrations, s.schedule_status
        FROM doctor_schedules s
        JOIN doctors d ON s.doctor_id = d.doctor_id
        JOIN users u ON d.user_id = u.user_id
        JOIN departments dep ON u.department_id = dep.department_id
        WHERE s.work_date = %s
        ORDER BY s.schedule_id
        """,
        (work_date,),
    )
    rows = cur.fetchall()
    print("\n【今日排班】%d 条" % len(rows))
    for r in rows:
        print("   %-6s %-6s %-4s 诊室%-5s 号源 %d/%d  %s"
              % (r["user_name"], r["department_name"], r["shift_type"],
                 r["clinic_room"], r["registered_count"], r["max_registrations"],
                 r["schedule_status"]))
    if not rows:
        print("   [警告] 仍然为空，挂号演示会卡住！")

    # 可接诊的挂号
    cur.execute(
        """
        SELECT r.registration_id, r.reg_no, p.patient_name, r.reg_date,
               r.visit_status, u.user_name AS doctor
        FROM registrations r
        JOIN patients p ON r.patient_id = p.patient_id
        JOIN doctors d ON r.doctor_id = d.doctor_id
        JOIN users u ON d.user_id = u.user_id
        WHERE r.visit_status IN ('待就诊', '就诊中')
        ORDER BY r.registration_id
        """
    )
    print("\n【可接诊的挂号】待就诊 / 就诊中")
    for r in cur.fetchall():
        print("   #%-4d %-18s %-8s %s  医生=%s  [%s]"
              % (r["registration_id"], r["reg_no"], r["patient_name"],
                 r["reg_date"], r["doctor"], r["visit_status"]))

    # 处方锚点
    cur.execute(
        """
        SELECT prescription_id, prescription_no, prescription_status,
               total_amount, patient_id
        FROM prescriptions
        WHERE prescription_status IN ('待收费', '已收费')
        ORDER BY prescription_id
        """
    )
    rows = cur.fetchall()
    print("\n【处方的钱路】待收费 = 可演示收费；已收费 = 可演示发药")
    for r in rows:
        print("   #%-4d %-18s %-6s %8.2f 元  [%s]"
              % (r["prescription_id"], r["prescription_no"],
                 r["prescription_status"], float(r["total_amount"]),
                 r["prescription_status"]))
    if not rows:
        print("   （无待收费/已收费处方 —— 发药演示需现场先走一遍开方→收费）")

    # 检验锚点
    cur.execute(
        """
        SELECT t.test_id, t.test_no, t.test_status, t.total_fee,
               p.patient_name,
               (SELECT COUNT(*) FROM payments pay
                 WHERE pay.lab_test_id = t.test_id AND pay.pay_status = '已收费') AS paid
        FROM lab_tests t JOIN patients p ON t.patient_id = p.patient_id
        WHERE t.test_status <> '已完成'
        ORDER BY t.test_id
        """
    )
    rows = cur.fetchall()
    print("\n【可录结果的检验单】待检验 / 检验中（须已收费）")
    for r in rows:
        print("   #%-4d %-20s %-6s %6.2f 元  已收费=%s  %s"
              % (r["test_id"], r["test_no"], r["test_status"],
                 float(r["total_fee"]), "是" if r["paid"] else "否",
                 r["patient_name"]))
    if not rows:
        print("   （无待检验单 —— 检验演示需医生现场新开一张，再走收费）")

    # 病历
    cur.execute(
        """
        SELECT m.record_id, m.record_no, p.patient_name, m.visit_date, u.user_name
        FROM medical_records m
        JOIN patients p ON m.patient_id = p.patient_id
        JOIN doctors d ON m.doctor_id = d.doctor_id
        JOIN users u ON d.user_id = u.user_id
        ORDER BY m.record_id
        """
    )
    print("\n【已归档门诊病历】可直接展示 + A4 打印")
    for r in cur.fetchall():
        print("   #%-4d %-14s %-8s %s  医生=%s"
              % (r["record_id"], r["record_no"], r["patient_name"],
                 r["visit_date"], r["user_name"]))

    # 账号
    print("\n【账号】密码统一 123456")
    print("   admin01  系统超级用户（全部菜单）")
    print("   cash01   收费人员（= 现行的「挂号窗口」，患者建档 / 挂号退号 / 收费退费）")
    print("   doctor01 医生「张建国」· 内科 · 主任医师（接诊 / 病历 / 处方 / 检验 / 体征 / 诊断）")
    print("   lab01    检验科人员（检验管理）")
    print("   pharm01  药房人员（药品目录 / 发药管理）")

    cur.close()


def main():
    ap = argparse.ArgumentParser(description="演示前准备：补今日排班 + 打印演示清单")
    ap.add_argument("--date", default=None, help="目标日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--dry-run", action="store_true", help="只看不写")
    args = ap.parse_args()

    work_date = (date.fromisoformat(args.date) if args.date else date.today())

    hr("医院门诊管理系统 · 演示前准备")
    print("  目标日期：%s" % work_date)
    print("  数据库  ：%s@%s:%d/%s" % (DB["user"], DB["host"], DB["port"], DB["database"]))

    conn = connect()
    try:
        ensure_schedules(conn, work_date, dry_run=args.dry_run)
        if not args.dry_run:
            show_checklist(conn, work_date)
    finally:
        conn.close()

    print("\n" + LINE)
    print("  好，可以开演了。下一步：")
    print("    1) 双击 run_web.bat        → 浏览器开 http://127.0.0.1:5000")
    print("    2) 双击 run_tests.bat      → 现场跑 172 条自动化测试")
    print("    3) 双击 ai_test_demo/index.html → 打开「AI + 测试」演示台")
    print(LINE)


if __name__ == "__main__":
    main()
