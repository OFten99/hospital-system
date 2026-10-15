# -*- coding: utf-8 -*-
"""演示模式：一键走完一条完整就诊闭环。

背景
----
课程设计答辩/验收时，现场演示有两个固定痛点：

1. **反复登录**。一条主线要横跨收费 / 医生 / 检验 / 药房四个角色，
   每次切换都要「先退出、再输账号、再输密码」，一轮演示要重复 4~5 次。
2. **手工录入**。病历正文、检验结果这些文本只能现场敲键盘，既慢又容易敲错。

本模块把这两件事压缩成「点一下」：免密切换角色 + 自动走完 12 步主线。

设计原则
--------
1. **不是伪造数据**。每一步调用的都是与页面完全相同的存储过程
   （``sp_register`` / ``sp_save_medical_record`` / ``sp_issue_prescription`` …），
   因此演示产生的数据、单号、状态流转与手工操作**没有任何差别**；
   演示完可以照常去各页面查看、打印、导出。
2. **单步可执行**。既能一键连跑，也能一步步走并在中间讲解；
   每步返回业务单号与跳转链接，失败时能精确定位到是哪一步。
3. **默认只在本机生效**。免密登录是危险能力，必须同时满足
   「``HIS_DEMO_MODE=1``」和「请求来自本机」两个条件才会开启；
   确需远程演示时显式设置 ``HIS_DEMO_ALLOW_REMOTE=1``。

依赖注入
--------
本模块不 ``import app``（避免循环导入），而是由 ``app.py`` 把三件事传进来：

* ``make_no``             —— 生成业务单号（与页面上完全同一个函数）
* ``derive_sample_type``  —— 按检验项目推导标本类型
* ``auto_flag``           —— 按参考范围判定检验结果标志
* ``describe_error``      —— 把数据库异常翻译成中文提示

统一放在 ``helpers`` 字典里传递，保证「演示模式」与「页面操作」
走的是同一套逻辑，不会出现两套行为分叉。
"""

import json
import os
import random
import time
from datetime import datetime
from pathlib import Path

from db import call_proc, call_proc_scalar, execute, fetch_all, fetch_one

# ============================================================
# 开关与访问控制
# ============================================================

_TRUTHY = {"1", "true", "yes", "on", "y"}

STATE_PATH = Path(__file__).resolve().parent / "_demo_state.json"

# 演示主线固定的主角：医生 张建国（内科 · 主任医师）。
# 之所以写死一位医生，是为了让「挂号 → 接诊」两步落在同一个医生名下——
# 接诊工作台只允许医生操作自己的挂号。
LEAD_DOCTOR_ACCOUNT = "doctor01"

# 各步骤的操作员（决定审计日志里的 operator_id，也对应演示脚本里的角色顺序）
CASHIER_ACCOUNT = "cash01"
LAB_ACCOUNT = "lab01"
PHARM_ACCOUNT = "pharm01"


def demo_enabled():
    """演示模式是否开启：环境变量 ``HIS_DEMO_MODE`` 为真值时开启。"""
    return os.getenv("HIS_DEMO_MODE", "").strip().lower() in _TRUTHY


def remote_allowed(remote_addr):
    """判断该来源 IP 是否允许使用演示模式。

    默认只放行本机。这是刻意收紧的：``/demo/login`` 能免密登录任意账号，
    一旦在公网部署时被打开，等于把系统完全敞开。
    确实需要从别的机器演示时，显式设置 ``HIS_DEMO_ALLOW_REMOTE=1``。
    """
    if os.getenv("HIS_DEMO_ALLOW_REMOTE", "").strip().lower() in _TRUTHY:
        return True
    addr = (remote_addr or "").strip()
    if addr in ("127.0.0.1", "::1", "localhost"):
        return True
    # IPv4-mapped IPv6，例如 ::ffff:127.0.0.1
    if addr.startswith("::ffff:127."):
        return True
    return False


def demo_active(remote_addr):
    """两个条件同时满足，演示能力才生效。"""
    return demo_enabled() and remote_allowed(remote_addr)


# ============================================================
# 账号清单（登录页快捷入口 + 控制台角色切换共用）
# ============================================================

DEMO_ACCOUNTS = [
    {
        "username": "admin01", "role_code": "ADMIN",
        "label": "系统超级用户", "desc": "全部菜单：看板 / 报表 / 用户 / 科室 / 公告",
    },
    {
        "username": "cash01", "role_code": "CASHIER",
        "label": "收费人员", "desc": "兼挂号窗口：建档 / 挂号退号 / 收费退费",
    },
    {
        "username": "doctor01", "role_code": "DOCTOR",
        "label": "医生 · 张建国", "desc": "内科 主任医师：接诊 / 病历 / 处方 / 检验",
    },
    {
        "username": "lab01", "role_code": "LAB_TECH",
        "label": "检验科人员", "desc": "检验管理：录入检验结果",
    },
    {
        "username": "pharm01", "role_code": "PHARMACIST",
        "label": "药房人员", "desc": "药品目录 / 发药管理",
    },
]

DEMO_PASSWORD = "123456"


# ============================================================
# 主线步骤定义
# ============================================================
# actor 用于界面分组，sp 标注该步落到的存储过程（讲解时可直接指着说）。

STEPS = [
    {
        "key": "patient", "actor": "cash01", "title": "患者建档",
        "desc": "病历号自动生成、身份证号唯一约束", "sp": "",
        "page": "/patients", "page_label": "患者档案",
    },
    {
        "key": "register", "actor": "cash01", "title": "挂号",
        "desc": "一个事务里同时写挂号记录 + 挂号费，号源自动扣减", "sp": "sp_register",
        "page": "/registrations", "page_label": "挂号管理",
    },
    {
        "key": "start_visit", "actor": "doctor01", "title": "开始接诊",
        "desc": "挂号状态：待就诊 → 就诊中", "sp": "",
        "page": "/consultation", "page_label": "门诊接诊",
    },
    {
        "key": "vitals", "actor": "doctor01", "title": "体征记录",
        "desc": "体温 / 血压 / 心率 / 呼吸 / 身高体重，形成体征历史", "sp": "sp_add_vital_sign",
        "page": "/vitals", "page_label": "体征记录",
    },
    {
        "key": "record", "actor": "doctor01", "title": "书写病历",
        "desc": "主诉 / 现病史 / 既往史 / 体格检查 / 诊断 / 医嘱", "sp": "sp_save_medical_record",
        "page": "/medical_records", "page_label": "门诊病历",
    },
    {
        "key": "lab_order", "actor": "doctor01", "title": "开具检验",
        "desc": "尿常规三项，检验费按项目单价自动合计", "sp": "sp_add_lab_test",
        "page": "/lab_tests", "page_label": "检验科",
    },
    {
        "key": "prescription", "actor": "doctor01", "title": "开具处方",
        "desc": "库存校验 + 金额自动计算，状态置为待收费", "sp": "sp_issue_prescription",
        "page": "/prescriptions", "page_label": "处方开具",
    },
    {
        "key": "finish", "actor": "doctor01", "title": "完成就诊",
        "desc": "挂号状态：就诊中 → 已就诊（前置条件：已书写病历）", "sp": "",
        "page": "/consultation", "page_label": "门诊接诊",
    },
    {
        "key": "lab_charge", "actor": "cash01", "title": "检验收费",
        "desc": "未收费的检验单不允许录入结果（存储过程校验）", "sp": "sp_charge_lab_test",
        "page": "/payments", "page_label": "收费退费",
    },
    {
        "key": "drug_charge", "actor": "cash01", "title": "药品收费",
        "desc": "收费同时扣减药品库存", "sp": "sp_charge",
        "page": "/payments", "page_label": "收费退费",
    },
    {
        "key": "lab_result", "actor": "lab01", "title": "录入检验结果",
        "desc": "按参考范围自动判定标志，异常值自动打「异常 / 偏高 / 偏低」", "sp": "sp_record_lab_result",
        "page": "/lab_tests", "page_label": "检验管理",
    },
    {
        "key": "dispense", "actor": "pharm01", "title": "发药",
        "desc": "处方状态：已收费 → 已发药，发药前再复核一次库存", "sp": "sp_dispense",
        "page": "/pharmacy", "page_label": "发药管理",
    },
]

STEP_BY_KEY = {s["key"]: s for s in STEPS}


# ============================================================
# 状态存取
# ============================================================


def new_state():
    return {
        "batch": None,
        "doctor_id": None,
        "patient_id": None, "patient_no": None, "patient_name": None,
        "registration_id": None, "reg_no": None,
        "test_id": None, "test_no": None, "test_fee": None,
        "prescription_id": None, "prescription_no": None, "prescription_amount": None,
        "done": [],
        "log": [],
    }


def load_state():
    try:
        with STATE_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            base = new_state()
            base.update(data)
            return base
    except Exception:
        pass
    return new_state()


def save_state(state):
    try:
        with STATE_PATH.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
    except Exception:
        # 状态文件写不进去不该影响演示本身，内存里的 state 仍然是对的
        pass


def reset_state():
    state = new_state()
    save_state(state)
    return state


# ============================================================
# 小工具
# ============================================================


def _uid(username):
    """按用户名取 user_id（审计字段 / 操作员 ID 都用它）。"""
    row = fetch_one("SELECT user_id FROM users WHERE username = %s", (username,))
    return int(row["user_id"]) if row else None


def _doctor_id_of(username):
    """按用户名取 doctor_id。"""
    row = fetch_one(
        "SELECT d.doctor_id FROM doctors d JOIN users u ON d.user_id = u.user_id "
        "WHERE u.username = %s",
        (username,),
    )
    return int(row["doctor_id"]) if row else None


def _resolved_department_id(registration_id):
    """解析本次就诊的科室：优先挂号上指定的科室，其次医生所属科室。

    ⚠️ 注意 ``doctors`` 表本身**没有** department_id 列 —— 医生的科室挂在
    ``users.department_id`` 上。口径与接诊工作台（app.py 里的
    ``resolved_department_id``）完全一致，否则开检验时会传错科室。
    """
    row = fetch_one(
        """
        SELECT COALESCE(r.department_id, u.department_id) AS dept_id
        FROM registrations r
        JOIN doctors d ON r.doctor_id = d.doctor_id
        JOIN users u ON d.user_id = u.user_id
        WHERE r.registration_id = %s
        """,
        (registration_id,),
    )
    return int(row["dept_id"]) if row and row["dept_id"] is not None else None


def _require(state, field, hint):
    value = state.get(field)
    if value is None:
        raise RuntimeError("缺少前置数据（%s），请先执行：%s" % (field, hint))
    return value


# ============================================================
# 各步骤实现
# ============================================================


def _step_patient(state, H):
    """建一位新患者：演示病历号自动生成 + 唯一约束。"""
    # 精确到秒：同一分钟内连跑两轮也不会出现两位同名「演示患者」，
    # 现场反复演示时在列表里一眼就能区分是哪一轮。
    stamp = datetime.now().strftime("%m%d%H%M%S")
    name = "演示患者%s" % stamp

    row = fetch_one(
        "SELECT MAX(CAST(SUBSTRING(patient_no, 2) AS UNSIGNED)) AS m FROM patients "
        "WHERE patient_no REGEXP '^P[0-9]+$'"
    )
    next_no = "P%03d" % int((row["m"] or 0) + 1)

    # 身份证号 / 手机号都有唯一约束，这里随机生成并重试，
    # 顺便把「撞唯一键会报什么」也留给现场讲解用。
    last_error = None
    for _ in range(8):
        id_card = "33010219900101%04d" % random.randint(0, 9999)
        phone = "139%08d" % random.randint(0, 99999999)
        try:
            execute(
                """
                INSERT INTO patients (patient_no, patient_name, gender, birth_date,
                                      id_card, phone, address, blood_type, medical_history)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (next_no, name, "男", "1990-01-01", id_card, phone,
                 "演示地址：门诊楼自助机", "A", "演示数据：既往体健，无药物过敏史"),
            )
            break
        except Exception as error:  # 唯一键冲突就换一组号重试
            last_error = error
            continue
    else:
        raise RuntimeError("患者建档失败：%s" % (last_error or "未知原因"))

    patient = fetch_one(
        "SELECT patient_id, patient_no, patient_name FROM patients "
        "WHERE patient_no = %s ORDER BY patient_id DESC LIMIT 1",
        (next_no,),
    )
    state["patient_id"] = int(patient["patient_id"])
    state["patient_no"] = patient["patient_no"]
    state["patient_name"] = patient["patient_name"]
    detail = "已建档 %s（病历号 %s，身份证号 %s）" % (
        patient["patient_name"], patient["patient_no"], id_card)
    return detail, [
        {"label": "去患者档案看", "href": "/patients?keyword=%s" % patient["patient_no"]},
    ]


def _step_register(state, H):
    """挂号：sp_register 一个事务里写挂号记录 + 挂号费，并扣减号源。"""
    patient_id = _require(state, "patient_id", "患者建档")
    doctor_id = state.get("doctor_id") or _doctor_id_of(LEAD_DOCTOR_ACCOUNT)
    state["doctor_id"] = doctor_id

    schedule = fetch_one(
        """
        SELECT schedule_id, max_registrations, registered_count
        FROM doctor_schedules
        WHERE work_date = CURDATE() AND doctor_id = %s AND schedule_status = '正常'
        ORDER BY schedule_id LIMIT 1
        """,
        (doctor_id,),
    )
    if not schedule:
        # 挂号页的排班下拉只查当天。库里排班日期常常停在几天前，
        # 这里顺手补一条当天排班，演示就不再依赖先跑 demo_prep.py。
        execute(
            """
            INSERT INTO doctor_schedules
                (doctor_id, work_date, shift_type, clinic_room,
                 max_registrations, registered_count, schedule_status)
            VALUES (%s, CURDATE(), '全天', %s, 30, 0, '正常')
            """,
            (doctor_id, "%d01" % doctor_id),
        )
        schedule = fetch_one(
            "SELECT schedule_id, max_registrations, registered_count FROM doctor_schedules "
            "WHERE work_date = CURDATE() AND doctor_id = %s ORDER BY schedule_id LIMIT 1",
            (doctor_id,),
        )

    before = int(schedule["registered_count"])
    call_proc("sp_register", [
        H["make_no"]("REG"),
        int(patient_id),
        int(doctor_id),
        int(schedule["schedule_id"]),
        "普通号",
        _uid(CASHIER_ACCOUNT),
        "微信",
        None,
    ])

    reg = fetch_one(
        """
        SELECT registration_id, reg_no, queue_no, reg_fee, visit_status
        FROM registrations WHERE patient_id = %s ORDER BY registration_id DESC LIMIT 1
        """,
        (patient_id,),
    )
    state["registration_id"] = int(reg["registration_id"])
    state["reg_no"] = reg["reg_no"]

    after = fetch_one(
        "SELECT registered_count FROM doctor_schedules WHERE schedule_id = %s",
        (schedule["schedule_id"],),
    )
    detail = "挂号成功 %s，号序 %s，挂号费 %s 元；号源 %d/%d → %d/%d" % (
        reg["reg_no"], reg["queue_no"], reg["reg_fee"],
        before, schedule["max_registrations"],
        int(after["registered_count"]), schedule["max_registrations"],
    )
    return detail, [
        {"label": "去挂号管理看", "href": "/registrations"},
        {"label": "去收费记录看", "href": "/payments"},
    ]


def _step_start_visit(state, H):
    """开始接诊：待就诊 → 就诊中。"""
    rid = _require(state, "registration_id", "挂号")
    execute(
        "UPDATE registrations SET visit_status = '就诊中' "
        "WHERE registration_id = %s AND visit_status = '待就诊'",
        (rid,),
    )
    row = fetch_one("SELECT visit_status FROM registrations WHERE registration_id = %s", (rid,))
    status = row["visit_status"] if row else "未知"
    return "挂号状态已更新为「%s」" % status, [
        {"label": "去接诊工作台", "href": "/consultation/%d" % rid},
    ]


def _step_vitals(state, H):
    """体征记录：sp_add_vital_sign。"""
    patient_id = _require(state, "patient_id", "患者建档")
    rid = _require(state, "registration_id", "挂号")
    call_proc("sp_add_vital_sign", [
        int(patient_id), int(rid), _uid(LEAD_DOCTOR_ACCOUNT),
        37.4, 128, 82, 88, 19, 65.5, 172.0,
        "演示模式自动录入：咽部充血，余未见异常",
    ])
    detail = "体征已记录：体温 37.4℃ / 血压 128-82 / 心率 88 / 呼吸 19 / 65.5kg / 172cm"
    return detail, [{"label": "去体征记录看", "href": "/vitals"}]


def _step_record(state, H):
    """书写病历：sp_save_medical_record（10 个 IN + 第 11 个是 OUT）。"""
    rid = _require(state, "registration_id", "挂号")
    record_id = call_proc_scalar("sp_save_medical_record", [
        0,
        int(rid),
        "咽痛、咳嗽 3 天，伴低热",
        "3 天前受凉后出现咽部干痛，继而咳嗽、咳少量白痰，自测体温 37.4℃；"
        "无胸闷气促，无腹痛腹泻，精神饮食可。",
        "既往体健，否认高血压、糖尿病等慢性病史，否认药物过敏史。",
        "体温 37.4℃，咽部充血，双侧扁桃体 I 度肿大，双肺呼吸音清，未闻及干湿啰音，"
        "心律齐，腹软无压痛。",
        "急性上呼吸道感染",
        None,
        "1. 多饮水、清淡饮食、注意休息；2. 抗感染及对症治疗（见处方）；"
        "3. 若持续发热超过 3 天或出现气促，及时复诊。",
        "已归档",
    ], out_index=11)
    detail = "病历已保存并归档（record_id=%s）" % record_id
    return detail, [{"label": "去门诊病历看", "href": "/medical_records"}]


def _step_lab_order(state, H):
    """开具检验：sp_add_lab_test，取尿常规三项。"""
    patient_id = _require(state, "patient_id", "患者建档")
    rid = _require(state, "registration_id", "挂号")
    doctor_id = state.get("doctor_id") or _doctor_id_of(LEAD_DOCTOR_ACCOUNT)
    dept_id = _resolved_department_id(rid)
    if dept_id is None:
        raise RuntimeError("未能确定本次就诊的科室，无法开具检验申请。")

    # 尿常规三项（lab_items.item_id：5 尿蛋白 / 6 尿糖 / 7 尿潜血）
    item_ids = ["5", "6", "7"]
    call_proc("sp_add_lab_test", [
        H["make_no"]("LAB"),
        int(patient_id),
        int(doctor_id),
        int(dept_id),
        int(rid),
        H["derive_sample_type"](item_ids),
        ",".join(item_ids),
        _uid(LEAD_DOCTOR_ACCOUNT),
    ])
    test = fetch_one(
        "SELECT test_id, test_no, total_fee, sample_type FROM lab_tests "
        "WHERE registration_id = %s ORDER BY test_id DESC LIMIT 1",
        (rid,),
    )
    state["test_id"] = int(test["test_id"])
    state["test_no"] = test["test_no"]
    state["test_fee"] = str(test["total_fee"])
    detail = "检验单 %s（标本：%s，%d 项），检验费合计 %s 元，当前未收费" % (
        test["test_no"], test["sample_type"], len(item_ids), test["total_fee"])
    return detail, [{"label": "去检验科看", "href": "/lab_tests"}]


def _step_prescription(state, H):
    """开具处方：sp_issue_prescription（库存校验 + 金额自动计算）。"""
    patient_id = _require(state, "patient_id", "患者建档")
    rid = _require(state, "registration_id", "挂号")
    doctor_id = state.get("doctor_id") or _doctor_id_of(LEAD_DOCTOR_ACCOUNT)

    items = [
        {"medicine_id": 1, "quantity": 2, "dosage": "口服，一次 1 粒，一日 3 次"},
        {"medicine_id": 3, "quantity": 1, "dosage": "开水冲服，一次 1 袋，一日 3 次"},
    ]
    call_proc("sp_issue_prescription", [
        H["make_no"]("PRES"),
        int(patient_id),
        int(doctor_id),
        int(rid),
        json.dumps(items, ensure_ascii=False),
    ])
    pres = fetch_one(
        "SELECT prescription_id, prescription_no, total_amount, prescription_status "
        "FROM prescriptions WHERE registration_id = %s ORDER BY prescription_id DESC LIMIT 1",
        (rid,),
    )
    state["prescription_id"] = int(pres["prescription_id"])
    state["prescription_no"] = pres["prescription_no"]
    state["prescription_amount"] = str(pres["total_amount"])
    detail = "处方 %s 共 %d 种药品，金额 %s 元，状态「%s」" % (
        pres["prescription_no"], len(items), pres["total_amount"], pres["prescription_status"])
    return detail, [{"label": "去处方开具看", "href": "/prescriptions"}]


def _step_finish(state, H):
    """完成就诊：就诊中 → 已就诊。"""
    rid = _require(state, "registration_id", "挂号")
    execute(
        "UPDATE registrations SET visit_status = '已就诊' "
        "WHERE registration_id = %s AND visit_status = '就诊中'",
        (rid,),
    )
    row = fetch_one("SELECT visit_status FROM registrations WHERE registration_id = %s", (rid,))
    return "挂号状态已更新为「%s」，医生侧流程结束" % (row["visit_status"] if row else "未知"), []


def _step_lab_charge(state, H):
    """检验收费：sp_charge_lab_test。"""
    test_id = _require(state, "test_id", "开具检验")
    call_proc("sp_charge_lab_test", [
        H["make_no"]("PAY"),
        int(test_id),
        "微信",
        _uid(CASHIER_ACCOUNT),
    ])
    row = fetch_one("SELECT test_no, total_fee FROM lab_tests WHERE test_id = %s", (test_id,))
    detail = "检验单 %s 已收费 %s 元，现在检验科可以录入结果了" % (
        row["test_no"], row["total_fee"])
    return detail, [{"label": "去收费退费看", "href": "/payments"}]


def _step_drug_charge(state, H):
    """药品收费：sp_charge（同时扣减药品库存）。"""
    pres_id = _require(state, "prescription_id", "开具处方")
    call_proc("sp_charge", [
        int(pres_id),
        _uid(CASHIER_ACCOUNT),
        H["make_no"]("PAY"),
        "微信",
    ])
    row = fetch_one(
        "SELECT prescription_no, total_amount, prescription_status FROM prescriptions "
        "WHERE prescription_id = %s", (pres_id,))
    detail = "处方 %s 已收费 %s 元，状态「%s」；药品库存同步扣减" % (
        row["prescription_no"], row["total_amount"], row["prescription_status"])
    return detail, [{"label": "去收费退费看", "href": "/payments"}]


def _step_lab_result(state, H):
    """录入检验结果：sp_record_lab_result；标志按参考范围自动判定。"""
    test_id = _require(state, "test_id", "开具检验")
    rows = fetch_all(
        """
        SELECT r.result_id, li.item_name, li.reference_range
        FROM lab_test_results r
        JOIN lab_items li ON r.item_id = li.item_id
        WHERE r.test_id = %s
        ORDER BY r.result_id
        """,
        (test_id,),
    )
    if not rows:
        raise RuntimeError("该检验单没有待录结果的项目。")

    # 第一项刻意给一个异常值（尿蛋白阳性），用来演示「异常标志自动判定」；
    # 其余给正常值，形成对比。
    recorded = []
    for index, item in enumerate(rows):
        value = "阳性" if index == 0 else "阴性"
        flag = H["auto_flag"](item["reference_range"], value) or "正常"
        call_proc("sp_record_lab_result", [
            int(test_id),
            int(item["result_id"]),
            value,
            flag,
            None,
            _uid(LAB_ACCOUNT),
        ])
        recorded.append("%s=%s(%s)" % (item["item_name"], value, flag))

    row = fetch_one("SELECT test_no, test_status FROM lab_tests WHERE test_id = %s", (test_id,))
    detail = "检验单 %s 已录完结果，状态「%s」：%s" % (
        row["test_no"], row["test_status"], "，".join(recorded))
    return detail, [{"label": "去看检验报告", "href": "/lab_tests/%d/results" % test_id}]


def _step_dispense(state, H):
    """发药：sp_dispense（已收费 → 已发药）。"""
    pres_id = _require(state, "prescription_id", "开具处方")
    call_proc("sp_dispense", [int(pres_id), _uid(PHARM_ACCOUNT)])
    row = fetch_one(
        "SELECT prescription_no, prescription_status FROM prescriptions "
        "WHERE prescription_id = %s", (pres_id,))
    detail = "处方 %s 已发药，状态「%s」，整条就诊闭环完成" % (
        row["prescription_no"], row["prescription_status"])
    return detail, [{"label": "去发药管理看", "href": "/pharmacy"}]


_RUNNERS = {
    "patient": _step_patient,
    "register": _step_register,
    "start_visit": _step_start_visit,
    "vitals": _step_vitals,
    "record": _step_record,
    "lab_order": _step_lab_order,
    "prescription": _step_prescription,
    "finish": _step_finish,
    "lab_charge": _step_lab_charge,
    "drug_charge": _step_drug_charge,
    "lab_result": _step_lab_result,
    "dispense": _step_dispense,
}


# ============================================================
# 对外入口
# ============================================================


def run_step(key, helpers):
    """执行一步，返回可 JSON 化的结果。

    返回结构::

        {"ok": True,  "key": "patient", "title": "患者建档", "actor": "cash01",
         "detail": "已建档 演示患者1015-2130（病历号 P010）",
         "links": [{"label": "去患者档案看", "href": "/patients?keyword=P010"}],
         "elapsed_ms": 42}

    失败时 ``ok=False``，``error`` 里是中文提示，不会往前端抛异常栈。
    """
    spec = STEP_BY_KEY.get(key)
    if not spec:
        return {"ok": False, "key": key, "error": "未知的演示步骤：%s" % key}

    state = load_state()
    if not state.get("batch"):
        state["batch"] = datetime.now().strftime("%Y%m%d-%H%M%S")

    started = time.time()
    try:
        detail, links = _RUNNERS[key](state, helpers)
    except Exception as error:  # noqa: BLE001 - 演示场景需要把任何失败都转成可读提示
        state["log"].append({
            "key": key, "title": spec["title"], "ok": False,
            "detail": str(error), "at": datetime.now().strftime("%H:%M:%S"),
        })
        save_state(state)
        message = helpers["describe_error"](error)
        return {
            "ok": False, "key": key, "title": spec["title"], "actor": spec["actor"],
            "error": message,
            "hint": "该步未成功，后续依赖它的步骤会一并失败；修好后重新执行本步即可。",
            "state": _public_state(state),
        }

    elapsed = int((time.time() - started) * 1000)
    if key not in state["done"]:
        state["done"].append(key)
    state["log"].append({
        "key": key, "title": spec["title"], "ok": True,
        "detail": detail, "at": datetime.now().strftime("%H:%M:%S"),
    })
    save_state(state)

    return {
        "ok": True, "key": key, "title": spec["title"], "actor": spec["actor"],
        "detail": detail, "links": links, "elapsed_ms": elapsed,
        "state": _public_state(state),
    }


def _public_state(state):
    """给前端用的状态摘要（不暴露内部日志全量）。"""
    return {
        "batch": state.get("batch"),
        "patient_name": state.get("patient_name"),
        "patient_no": state.get("patient_no"),
        "reg_no": state.get("reg_no"),
        "test_no": state.get("test_no"),
        "prescription_no": state.get("prescription_no"),
        "registration_id": state.get("registration_id"),
        "test_id": state.get("test_id"),
        "prescription_id": state.get("prescription_id"),
        "done": list(state.get("done") or []),
    }


def overview():
    """控制台顶部的「当前库里有什么」概览，全部为实时查询。"""
    def count(sql, params=None):
        row = fetch_one(sql, params)
        return int(list(row.values())[0]) if row else 0

    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "schedules": count(
            "SELECT COUNT(*) AS v FROM doctor_schedules "
            "WHERE work_date = CURDATE() AND schedule_status = '正常'"),
        "patients": count("SELECT COUNT(*) AS v FROM patients"),
        "registrations_today": count(
            "SELECT COUNT(*) AS v FROM registrations WHERE reg_date = %s "
            "AND visit_status <> '已退号'", (today,)),
        "waiting_visit": count(
            "SELECT COUNT(*) AS v FROM registrations "
            "WHERE visit_status IN ('待就诊', '就诊中')"),
        "prescriptions_pending": count(
            "SELECT COUNT(*) AS v FROM prescriptions WHERE prescription_status = '待收费'"),
        "prescriptions_charged": count(
            "SELECT COUNT(*) AS v FROM prescriptions WHERE prescription_status = '已收费'"),
        "lab_unpaid": count(
            "SELECT COUNT(*) AS v FROM v_lab_test_list "
            "WHERE test_status <> '已作废' AND pay_status = '未收费'"),
        "medical_records": count("SELECT COUNT(*) AS v FROM medical_records"),
    }
