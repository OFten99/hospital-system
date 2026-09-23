"""
医院门诊管理系统（对标小型 HIS 门诊业务）Web 版。
启动方式：python app.py
访问地址：http://127.0.0.1:5000

模块：患者档案、挂号、医生排班、处方开具、收费退费、药品目录、报表查询。
角色：ADMIN 管理员 / DOCTOR 医生 / REGISTRAR 挂号员 / CASHIER 收费员
"""

import csv
import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

import mysql.connector
from flask import Flask, Response, flash, redirect, render_template, request, session, url_for

from db import call_proc, close_request_connections, execute, fetch_all, fetch_one, get_connection


# 模板与静态资源目录按「本文件所在目录」定位，
# 这样本地运行和云端（云函数）运行解析结果完全一致。
# 云端（EdgeOne 云函数）打包时文件挂载位置可能与源码目录不同，
# 因此按多个候选路径依次探测，哪个目录里有模板就优先用哪个。
APP_DIR = Path(__file__).resolve().parent
_CWD = Path(os.getcwd())

_TEMPLATE_CANDIDATES = [
    APP_DIR / "templates",            # 本地 / 常规部署
    _CWD / "templates",               # 云函数工作目录
    _CWD / "cloud-functions" / "templates",
    APP_DIR.parent / "templates",
]
_STATIC_CANDIDATES = [
    APP_DIR / "static",
    _CWD / "static",
    _CWD / "cloud-functions" / "static",
    APP_DIR.parent / "static",
]

TEMPLATE_DIR = next((c for c in _TEMPLATE_CANDIDATES if (c / "login.html").exists()),
                    APP_DIR / "templates")
STATIC_DIR = next((c for c in _STATIC_CANDIDATES if c.exists()),
                  APP_DIR / "static")

import logging as _logging
_logging.getLogger(__name__).warning(
    "HIS 路径诊断：APP_DIR=%s cwd=%s TEMPLATE_DIR=%s(%s) STATIC_DIR=%s(%s)",
    APP_DIR, _CWD, TEMPLATE_DIR, (TEMPLATE_DIR / "login.html").exists(),
    STATIC_DIR, (STATIC_DIR / "style.css").exists(),
)

# 兜底：EdgeOne 云函数打包只含 .py 文件，磁盘没有模板/静态资源时，
# 从同目录 templates_data.py（自动生成）解包到临时目录供 Flask 使用。
if not (TEMPLATE_DIR / "login.html").exists():
    try:
        import tempfile
        import templates_data as _td
        _tmp_root = Path(tempfile.gettempdir()) / "his_templates_data"
        _tmp_root.mkdir(parents=True, exist_ok=True)
        for _name, _content in _td.TEMPLATES.items():
            _f = _tmp_root / _name
            if not _f.exists():
                _f.parent.mkdir(parents=True, exist_ok=True)
                _f.write_text(_content, encoding="utf-8")
        _t_tpl = _tmp_root / "templates"
        _t_static = _tmp_root / "static"
        if (_t_tpl / "login.html").exists():
            TEMPLATE_DIR = _t_tpl
            STATIC_DIR = _t_static if _t_static.exists() else STATIC_DIR
            _logging.getLogger(__name__).warning(
                "HIS 已从 templates_data 解包模板到 %s（%d 项）", _tmp_root, len(_td.TEMPLATES))
    except ImportError:
        _logging.getLogger(__name__).warning(
            "HIS 磁盘无模板且无 templates_data.py，模板不可用（APP_DIR=%s cwd=%s）",
            APP_DIR, _CWD)


_USE_MEM_STATIC = not (STATIC_DIR / "style.css").exists()

app = Flask(
    __name__,
    template_folder=str(TEMPLATE_DIR),
    static_folder=None if _USE_MEM_STATIC else str(STATIC_DIR),
)


# 云端静态资源兜底：EdgeOne 云函数内 Flask send_file 服务 /static 可能异常，
# 此时显式注册 static 路由，直接从内存字典（templates_data.py）返回样式文件，
# 保证云端界面与本地完全一致（本地磁盘 static 存在时不会启用）。
if _USE_MEM_STATIC:
    def _serve_static_from_memory(filename):
        try:
            import templates_data as _td
        except ImportError:
            return Response("not found", status=404, mimetype="text/plain")
        key = "static/" + filename
        if key in _td.TEMPLATES:
            mime = ("text/css" if filename.endswith(".css")
                    else "application/octet-stream")
            return Response(_td.TEMPLATES[key], mimetype=mime)
        return Response("not found", status=404, mimetype="text/plain")

    app.add_url_rule("/static/<path:filename>", endpoint="static",
                     view_func=_serve_static_from_memory, methods=["GET"])
# 会话密钥：本地开发用固定默认值，线上建议通过环境变量 HIS_SECRET_KEY 覆盖
app.secret_key = os.getenv("HIS_SECRET_KEY", "hospital-outpatient-his-course-design")
# 全局注入内嵌 CSS：云端 /static/ 请求被 EdgeOne 静态资源层拦截（产物中无该文件
# 时直接 500 且不进云函数），因此把样式随 HTML 一起渲染，保证云端界面与本地一致。
def _load_style_css():
    try:
        _disk = STATIC_DIR / "style.css"
        if _disk.exists():
            return _disk.read_text(encoding="utf-8")
    except Exception:
        pass
    try:
        import templates_data as _td
        return _td.TEMPLATES.get("static/style.css", "")
    except ImportError:
        return ""


_STYLE_CSS = _load_style_css()


@app.context_processor
def _inject_inline_style():
    return {"style_css": _STYLE_CSS}


@app.teardown_appcontext
def release_db_connections(exc=None):
    """请求结束时统一关闭该请求占用的数据库连接（云端必备，防止连接数耗尽）"""
    close_request_connections(exc)


@app.after_request
def add_no_cache(response):
    """禁用页面缓存，确保用户刷新后始终看到最新界面"""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    """
    未捕获异常统一处理。

    默认的 500 页面只有一句「Internal Server Error」，云端排查时毫无信息量。
    这里把完整堆栈写进日志（对应 EdgeOne 的函数日志），同时给浏览器返回一段
    可读提示 + 自检入口，便于定位是环境变量、数据库还是代码问题。
    """
    from werkzeug.exceptions import HTTPException

    # 404 / 405 这类是正常业务响应，交回 Flask 默认处理
    if isinstance(error, HTTPException):
        return error

    app.logger.error("未处理异常：%s", error, exc_info=True)
    return Response(
        "服务端内部错误（500）。\n\n"
        f"异常信息：{type(error).__name__}: {error}\n\n"
        "排查建议：\n"
        "  1. 浏览器打开 /healthz，查看环境变量是否生效、数据库能否连上；\n"
        "  2. 常见原因：HIS_DB_* 环境变量未配置、漏设 HIS_DB_SSL=1、\n"
        "     或改完环境变量后没有重新部署（必须重新部署才生效）。\n",
        status=500,
        mimetype="text/plain; charset=utf-8",
    )


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def current_user():
    return session.get("user")


def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapper


PERMISSIONS = {
    "REGISTRAR": {"dashboard", "patients", "registrations", "schedules", "appointments"},
    "REGMACHINE": {"kiosk"},
    "DOCTOR": {"dashboard", "schedules", "prescriptions"},
    "CASHIER": {"dashboard", "payments"},
    "LAB_TECH": {"dashboard", "lab"},
    "PHARMACIST": {"dashboard", "dispense", "medicines"},
    "ADMIN": {"dashboard", "patients", "registrations", "schedules", "prescriptions",
              "payments", "medicines", "reports", "export", "codes", "vitals", "lab",
              "diagnosis", "dispense", "users"},
}


def role_allowed(menu_key):
    user = current_user()
    if not user:
        return False
    if user["role_code"] == "ADMIN":
        return True
    return menu_key in PERMISSIONS.get(user["role_code"], set())


def permission_required(menu_key):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(*args, **kwargs):
            if not role_allowed(menu_key):
                flash("当前角色无权访问该功能。", "error")
                return redirect(url_for("dashboard"))
            return view_func(*args, **kwargs)

        return wrapper

    return decorator


@app.context_processor
def inject_common_data():
    return {
        "current_user": current_user(),
        "role_allowed": role_allowed,
        "now": datetime.now(),
    }


def handle_db_error(error):
    message = str(error)
    # 存储过程 SIGNAL 抛出的业务错误（1644 为 SIGNAL 的错误码）
    if "1644" in message:
        parts = message.split(": ", 1)
        if len(parts) == 2 and parts[1].strip():
            return parts[1].strip()
        return "业务规则校验未通过。"
    if "Duplicate entry" in message:
        return "数据重复：病历号、身份证号、手机号、登录名等唯一字段不能重复。"
    if "Cannot delete or update a parent row" in message:
        return "该数据已被挂号、处方或收费记录引用，不能直接删除。"
    if "Incorrect datetime value" in message or "Incorrect date value" in message:
        return "日期格式错误：日期用 2026-09-14，日期时间用 2026-09-14 08:00:00。"
    return f"数据库操作失败：{message}"


def make_no(prefix):
    return prefix + datetime.now().strftime("%Y%m%d%H%M%S")


# ============================================================
# 登录 / 退出
# ============================================================

@app.route("/", methods=["GET"])
def index():
    if current_user():
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


# ============================================================
# 部署自检（无需登录）：浏览器打开 /healthz 即可看到环境变量与数据库连通性
# 云端遇到 500 时，这里是排查的第一站
# ============================================================

# 本地默认值：如果云端读到的还是这些值，说明环境变量根本没生效
_LOCAL_DEFAULTS = {
    "HIS_DB_HOST": "127.0.0.1",
    "HIS_DB_PORT": "3306",
    "HIS_DB_USER": "root",
    "HIS_DB_PASSWORD": "123456",
    "HIS_DB_NAME": "hospital_outpatient",
}

_ENV_ADVICE = {
    "HIS_DB_HOST": "云端应填托管数据库主机，默认值 127.0.0.1 在云端必然连不上",
    "HIS_DB_PORT": "Aiven 等托管库的端口不是 3306，要照抄控制台给出的值",
    "HIS_DB_USER": "云端用户名一般是 avnadmin 之类，不是 root",
    "HIS_DB_PASSWORD": "应填托管库的真实密码",
    "HIS_DB_SSL": "托管数据库强制 TLS，云端必须设为 1，漏了必然连不上",
    "HIS_SECRET_KEY": "未设置会使用代码里的默认密钥，线上建议改成随机串",
}


@app.route("/healthz", methods=["GET"])
def healthz():
    import platform

    lines = ["医院门诊管理系统 —— 部署自检", "=" * 44]
    lines.append(f"Python 版本：{platform.python_version()}")
    try:
        from importlib.metadata import version

        lines.append(f"Flask：{version('flask')}")
        lines.append(f"mysql-connector-python：{version('mysql-connector-python')}")
    except Exception as error:  # 依赖缺失时也要能返回，方便定位
        lines.append(f"依赖版本读取失败：{error}")

    lines.append("")
    lines.append("【环境变量】")
    for key in ("HIS_DB_HOST", "HIS_DB_PORT", "HIS_DB_USER", "HIS_DB_PASSWORD",
                "HIS_DB_NAME", "HIS_DB_SSL", "HIS_SECRET_KEY"):
        raw = os.getenv(key)
        if raw is None:
            shown = "（未设置）"
        elif key == "HIS_DB_PASSWORD":
            shown = "*" * len(raw)
        else:
            shown = raw
        note = ""
        if raw is None and key in _ENV_ADVICE:
            note = "  <- " + _ENV_ADVICE[key]
        elif key in _LOCAL_DEFAULTS and raw == _LOCAL_DEFAULTS[key]:
            note = "  <- 仍是本地默认值！" + _ENV_ADVICE.get(key, "")
        lines.append(f"  {key} = {shown}{note}")

    lines.append("")
    lines.append("【数据库连通性】")
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT VERSION(), DATABASE(), CURRENT_USER()")
        row = cursor.fetchone()
        lines.append(f"  连接成功：server={row[0]}  database={row[1]}  user={row[2]}")
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE()"
        )
        lines.append(f"  当前库对象数（表+视图）：{cursor.fetchone()[0]}")
        cursor.execute("SELECT COUNT(*) FROM users")
        lines.append(f"  users 表行数：{cursor.fetchone()[0]}")
        cursor.close()
    except Exception as error:
        lines.append(f"  连接失败：{type(error).__name__}: {error}")
        lines.append("  排查顺序：HIS_DB_SSL 是否为 1 → Host/Port 是否照抄 → 改完是否重新部署 → 实例是否 Running")

    return Response("\n".join(lines) + "\n", mimetype="text/plain; charset=utf-8")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        user = fetch_one(
            """
            SELECT
              u.user_id, u.user_name, u.username, u.account_status,
              r.role_code, r.role_name, d.department_name
            FROM users u
            JOIN roles r ON u.role_id = r.role_id
            LEFT JOIN departments d ON u.department_id = d.department_id
            WHERE u.username = %s AND u.password_hash = %s
            """,
            (username, sha256_text(password)),
        )
        if not user:
            flash("用户名或密码错误。", "error")
            return render_template("login.html")
        if user["account_status"] != "启用":
            flash("账号已被锁定，无法登录。", "error")
            return render_template("login.html")
        session["user"] = user
        execute("UPDATE users SET last_login_at = NOW() WHERE user_id = %s", (user["user_id"],))
        flash(f"登录成功：{user['user_name']}（{user['role_name']}）", "success")
        if user["role_code"] == "REGMACHINE":
            return redirect(url_for("kiosk"))
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("已退出登录", "success")
    return redirect(url_for("login"))


# ============================================================
# 首页看板
# ============================================================

@app.route("/dashboard")
@login_required
def dashboard():
    # 自助挂号机终端登录后直达自助挂号页，不展示内部运营看板
    if current_user()["role_code"] == "REGMACHINE":
        return redirect(url_for("kiosk"))
    today = datetime.now().strftime("%Y-%m-%d")
    stats = {
        "today_reg": fetch_one(
            "SELECT COUNT(*) AS v FROM registrations WHERE reg_date = %s AND visit_status <> '已退号'", (today,)
        )["v"],
        "today_visited": fetch_one(
            "SELECT COUNT(*) AS v FROM registrations WHERE reg_date = %s AND visit_status = '已就诊'", (today,)
        )["v"],
        "waiting_pres": fetch_one(
            "SELECT COUNT(*) AS v FROM prescriptions WHERE prescription_status = '待收费'"
        )["v"],
        "today_income": fetch_one(
            """
            SELECT COALESCE(SUM(pay_amount), 0) AS v FROM payments
            WHERE DATE(pay_time) = %s AND pay_status = '已收费'
            """,
            (today,),
        )["v"],
    }
    reg_by_dept = fetch_all("SELECT * FROM v_today_registrations ORDER BY registration_count DESC")
    notices = fetch_all(
        """
        SELECT a.title, a.content, a.created_at
        FROM announcements a
        WHERE a.is_published = 1
        ORDER BY a.created_at DESC, a.announcement_id DESC
        LIMIT 3
        """
    )
    today_occupied = fetch_all(
        """
        SELECT r.reg_no, pat.patient_name, dept.department_name, u.user_name AS doctor_name,
               r.reg_type, r.queue_no, r.visit_status, r.reg_fee
        FROM registrations r
        JOIN patients pat ON r.patient_id = pat.patient_id
        JOIN departments dept ON r.department_id = dept.department_id
        JOIN doctors doc ON r.doctor_id = doc.doctor_id
        JOIN users u ON doc.user_id = u.user_id
        WHERE r.reg_date = %s
        ORDER BY CASE r.visit_status WHEN '待就诊' THEN 0 WHEN '就诊中' THEN 1 ELSE 2 END, r.queue_no
        """,
        (today,),
    )
    return render_template("dashboard.html", stats=stats, reg_by_dept=reg_by_dept,
                           today_occupied=today_occupied, notices=notices)


# ============================================================
# 患者档案管理
# ============================================================

@app.route("/patients", methods=["GET", "POST"])
@login_required
@permission_required("patients")
def patients():
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "add":
                execute(
                    """
                    INSERT INTO patients (patient_no, patient_name, gender, birth_date,
                                          id_card, phone, address, blood_type, medical_history)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        request.form["patient_no"].strip(),
                        request.form["patient_name"].strip(),
                        request.form["gender"],
                        request.form["birth_date"] or None,
                        request.form["id_card"].strip(),
                        request.form["phone"].strip(),
                        request.form.get("address") or None,
                        request.form["blood_type"],
                        request.form.get("medical_history") or None,
                    ),
                )
                flash("患者档案创建成功。", "success")
            elif action == "update":
                execute(
                    """
                    UPDATE patients
                    SET phone = %s, address = %s, blood_type = %s, medical_history = %s
                    WHERE patient_id = %s
                    """,
                    (
                        request.form["phone"].strip(),
                        request.form.get("address") or None,
                        request.form["blood_type"],
                        request.form.get("medical_history") or None,
                        request.form["patient_id"],
                    ),
                )
                flash("患者档案已更新。", "success")
            elif action == "delete":
                execute("DELETE FROM patients WHERE patient_id = %s", (request.form["patient_id"],))
                flash("患者档案已删除。", "success")
        except mysql.connector.Error as error:
            flash(handle_db_error(error), "error")
        return redirect(url_for("patients"))

    keyword = request.args.get("keyword", "").strip()
    if keyword:
        like = f"%{keyword}%"
        rows = fetch_all(
            """
            SELECT * FROM patients
            WHERE patient_name LIKE %s OR id_card LIKE %s OR phone LIKE %s OR patient_no LIKE %s
            ORDER BY patient_id
            """,
            (like, like, like, like),
        )
    else:
        rows = fetch_all("SELECT * FROM patients ORDER BY patient_id")
    return render_template("patients.html", rows=rows, keyword=keyword)


# ============================================================
# 挂号管理
# ============================================================

@app.route("/registrations", methods=["GET", "POST"])
@login_required
@permission_required("registrations")
def registrations():
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "register":
                schedule = fetch_one(
                    "SELECT doctor_id FROM doctor_schedules WHERE schedule_id = %s",
                    (request.form["schedule_id"],),
                )
                if not schedule:
                    flash("排班不存在，请刷新后重试。", "error")
                    return redirect(url_for("registrations"))
                call_proc(
                    "sp_register",
                    [
                        make_no("REG"),
                        int(request.form["patient_id"]),
                        schedule["doctor_id"],
                        int(request.form["schedule_id"]),
                        request.form["reg_type"],
                        int(current_user()["user_id"]),
                        request.form["pay_method"],
                        None,
                    ],
                )
                flash("挂号成功，挂号费已收取。", "success")
            elif action == "cancel":
                call_proc(
                    "sp_cancel_registration",
                    [
                        int(request.form["registration_id"]),
                        int(current_user()["user_id"]),
                        request.form.get("reason") or "患者申请退号",
                    ],
                )
                flash("退号成功，号源已恢复，挂号费已退回。", "success")
        except mysql.connector.Error as error:
            flash(handle_db_error(error), "error")
        return redirect(url_for("registrations"))

    reg_date = request.args.get("reg_date") or datetime.now().strftime("%Y-%m-%d")
    rows = fetch_all(
        """
        SELECT r.registration_id, r.reg_no, pat.patient_name, dept.department_name,
               u.user_name AS doctor_name, r.reg_type, r.reg_fee, r.queue_no,
               r.visit_status, r.created_at AS reg_time
        FROM registrations r
        JOIN patients pat ON r.patient_id = pat.patient_id
        JOIN departments dept ON r.department_id = dept.department_id
        JOIN doctors doc ON r.doctor_id = doc.doctor_id
        JOIN users u ON doc.user_id = u.user_id
        WHERE r.reg_date = %s
        ORDER BY r.queue_no
        """,
        (reg_date,),
    )
    patients_rows = fetch_all("SELECT patient_id, patient_no, patient_name, phone FROM patients ORDER BY patient_id")
    schedules = fetch_all(
        """
        SELECT s.schedule_id, s.work_date, s.shift_type, s.clinic_room,
               s.max_registrations, s.registered_count,
               doc.doctor_id, doc.doctor_name, doc.title,
               dept.department_name, doc.consultation_fee,
               GREATEST(s.max_registrations - s.registered_count -
                 (SELECT COUNT(*) FROM appointments a
                   WHERE a.schedule_id = s.schedule_id AND a.status = '已预约' AND a.expire_time > NOW()), 0) AS remain
        FROM doctor_schedules s
        JOIN (
          SELECT d.doctor_id, u.user_name AS doctor_name, d.title, d.consultation_fee, u.department_id
          FROM doctors d JOIN users u ON d.user_id = u.user_id
        ) doc ON s.doctor_id = doc.doctor_id
        JOIN departments dept ON doc.department_id = dept.department_id
        WHERE s.work_date = %s AND s.schedule_status = '正常'
        ORDER BY s.work_date, dept.department_name
        """,
        (reg_date,),
    )
    return render_template("registrations.html", rows=rows, patients=patients_rows,
                           schedules=schedules, reg_date=reg_date)


# ============================================================
# 预约挂号（提前预约 -> 预约流水号与挂号时限 -> 取号转正式挂号）
# ============================================================

@app.route("/appointments", methods=["GET", "POST"])
@login_required
@permission_required("appointments")
def appointments():
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "find":
                keyword = request.form.get("keyword", "").strip()
                if not keyword:
                    flash("请输入身份证号或手机号。", "error")
                    return redirect(url_for("appointments"))
                return redirect(url_for("appointments", keyword=keyword))
            if action == "create":
                appointment_no = make_no("APT")
                call_proc(
                    "sp_create_appointment",
                    [
                        appointment_no,
                        int(request.form["patient_id"]),
                        int(request.form["schedule_id"]),
                        request.form["reg_type"],
                        int(current_user()["user_id"]),
                    ],
                )
                appt = fetch_one(
                    "SELECT appointment_no, appointment_date, expire_time FROM appointments WHERE appointment_no = %s",
                    (appointment_no,),
                )
                if appt:
                    flash(
                        f"预约成功：流水号 {appt['appointment_no']}，就诊日期 {appt['appointment_date']}，"
                        f"请于 {appt['expire_time'].strftime('%m-%d %H:%M')} 前取号。",
                        "success",
                    )
                else:
                    flash("预约成功。", "success")
            elif action == "cancel":
                call_proc(
                    "sp_cancel_appointment",
                    [
                        int(request.form["appointment_id"]),
                        int(current_user()["user_id"]),
                        request.form.get("reason") or "患者申请取消预约",
                    ],
                )
                flash("预约已取消，号源已释放。", "success")
        except mysql.connector.Error as error:
            flash(handle_db_error(error), "error")
        except ValueError:
            flash("提交参数格式错误，请检查后重试。", "error")
        return redirect(url_for("appointments"))

    # 患者识别（同自助终端：身份证号或手机号）
    keyword = request.args.get("keyword", "").strip()
    patient = None
    if keyword:
        patient = fetch_one(
            """
            SELECT patient_id, patient_no, patient_name, gender, birth_date, id_card, phone
            FROM patients WHERE id_card = %s OR phone = %s
            """,
            (keyword, keyword),
        )

    # 默认展示次日的排班（当日挂号请走挂号管理/自助终端）
    appt_date = request.args.get("appt_date") or (
        datetime.now() + timedelta(days=1)
    ).strftime("%Y-%m-%d")
    schedules = fetch_all(
        """
        SELECT s.schedule_id, s.work_date, s.shift_type, s.clinic_room,
               s.max_registrations, s.registered_count,
               doc.doctor_id, doc.doctor_name, doc.title,
               dept.department_name, doc.consultation_fee,
               GREATEST(s.max_registrations - s.registered_count -
                 (SELECT COUNT(*) FROM appointments a
                   WHERE a.schedule_id = s.schedule_id AND a.status = '已预约' AND a.expire_time > NOW()), 0) AS remain
        FROM doctor_schedules s
        JOIN (
          SELECT d.doctor_id, u.user_name AS doctor_name, d.title, d.consultation_fee, u.department_id
          FROM doctors d JOIN users u ON d.user_id = u.user_id
        ) doc ON s.doctor_id = doc.doctor_id
        JOIN departments dept ON doc.department_id = dept.department_id
        WHERE s.work_date = %s AND s.schedule_status = '正常'
        ORDER BY s.work_date, dept.department_name, s.shift_type
        """,
        (appt_date,),
    )

    # 预约列表（已预约且超过挂号时限展示为"已过期"）
    status_filter = request.args.get("status", "").strip()
    where = "WHERE 1=1"
    params = []
    if status_filter:
        where += " AND CASE WHEN a.status = '已预约' AND a.expire_time < NOW() THEN '已过期' ELSE a.status END = %s"
        params.append(status_filter)
    if keyword and patient:
        where += " AND a.patient_id = %s"
        params.append(patient["patient_id"])
    rows = fetch_all(
        f"""
        SELECT a.appointment_id, a.appointment_no, a.appointment_date, a.reg_type, a.expire_time,
               CASE WHEN a.status = '已预约' AND a.expire_time < NOW() THEN '已过期' ELSE a.status END AS status,
               pat.patient_name, dept.department_name, u.user_name AS doctor_name,
               s.shift_type, s.clinic_room
        FROM appointments a
        JOIN patients pat ON a.patient_id = pat.patient_id
        JOIN doctor_schedules s ON a.schedule_id = s.schedule_id
        JOIN doctors doc ON s.doctor_id = doc.doctor_id
        JOIN users u ON doc.user_id = u.user_id
        JOIN departments dept ON u.department_id = dept.department_id
        {where}
        ORDER BY a.created_at DESC
        """,
        params,
    )
    return render_template(
        "appointments.html", patient=patient, keyword=keyword,
        appt_date=appt_date, schedules=schedules, rows=rows, status=status_filter,
    )


@app.route("/appointments/<int:appointment_id>/checkin", methods=["GET", "POST"])
@login_required
@permission_required("appointments")
def appointment_checkin(appointment_id):
    appt = fetch_one(
        """
        SELECT a.appointment_id, a.appointment_no, a.appointment_date, a.reg_type,
               a.expire_time, a.status,
               pat.patient_id, pat.patient_name, pat.id_card,
               dept.department_name, u.user_name AS doctor_name, d.consultation_fee,
               s.shift_type, s.clinic_room
        FROM appointments a
        JOIN patients pat ON a.patient_id = pat.patient_id
        JOIN doctor_schedules s ON a.schedule_id = s.schedule_id
        JOIN doctors d ON s.doctor_id = d.doctor_id
        JOIN users u ON d.user_id = u.user_id
        JOIN departments dept ON u.department_id = dept.department_id
        WHERE a.appointment_id = %s
        """,
        (appointment_id,),
    )
    if not appt:
        flash("预约记录不存在。", "error")
        return redirect(url_for("appointments"))
    if request.method == "POST":
        pay_method = request.form.get("pay_method", "微信")
        try:
            call_proc(
                "sp_checkin_appointment",
                [
                    appointment_id,
                    make_no("REG"),
                    int(current_user()["user_id"]),
                    pay_method,
                ],
            )
            flash("取号成功，已转正式挂号并收取挂号费。", "success")
        except mysql.connector.Error as error:
            flash(handle_db_error(error), "error")
            return redirect(url_for("appointments"))
        return redirect(url_for("appointments"))

    # 仅"已预约"、未过挂号时限且为就诊当日可办理取号
    can_checkin = (
        appt["status"] == "已预约"
        and appt["expire_time"] > datetime.now()
        and appt["appointment_date"] == datetime.now().date()
    )
    return render_template("appointment_checkin.html", appt=appt, can_checkin=can_checkin)


# ============================================================
# 自助挂号机（终端专用：患者识别 -> 选排班 -> 挂号 -> 排队号）
# ============================================================

@app.route("/kiosk", methods=["GET", "POST"])
@login_required
@permission_required("kiosk")
def kiosk():
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "find":
                keyword = request.form.get("id_card", "").strip()
                if not keyword:
                    flash("请输入身份证号或手机号。", "error")
                    return redirect(url_for("kiosk"))
                return redirect(url_for("kiosk", id_card=keyword))
            if action == "quick_register":
                keyword = request.form.get("id_card", "").strip()
                name = request.form.get("patient_name", "").strip()
                gender = request.form.get("gender", "男")
                phone = request.form.get("phone", "").strip()
                birth_date = request.form.get("birth_date") or None
                if not keyword or not name or not phone:
                    flash("身份证号、姓名、手机号必填。", "error")
                    return redirect(url_for("kiosk", id_card=keyword))
                execute(
                    """
                    INSERT INTO patients (patient_no, patient_name, gender, birth_date,
                                          id_card, phone, blood_type)
                    VALUES (%s, %s, %s, %s, %s, %s, '未知')
                    """,
                    (make_no("P"), name, gender, birth_date, keyword, phone),
                )
                flash("建档成功，请确认信息后选择排班挂号。", "success")
                return redirect(url_for("kiosk", id_card=keyword))
            if action == "register":
                patient_id = int(request.form["patient_id"])
                schedule_id = int(request.form["schedule_id"])
                reg_type = request.form["reg_type"]
                pay_method = request.form["pay_method"]
                if reg_type not in ("普通号", "专家号") or pay_method not in ("微信", "支付宝", "医保"):
                    flash("号别或支付方式无效（自助机支持微信/支付宝/医保）。", "error")
                    return redirect(url_for("kiosk"))
                patient = fetch_one(
                    "SELECT patient_id, patient_name FROM patients WHERE patient_id = %s", (patient_id,)
                )
                schedule = fetch_one(
                    "SELECT doctor_id FROM doctor_schedules WHERE schedule_id = %s", (schedule_id,)
                )
                if not patient:
                    flash("患者不存在，请重新识别。", "error")
                    return redirect(url_for("kiosk"))
                if not schedule:
                    flash("排班不存在，请刷新后重试。", "error")
                    return redirect(url_for("kiosk"))
                reg_no = make_no("REG")
                call_proc(
                    "sp_register",
                    [
                        reg_no,
                        patient_id,
                        schedule["doctor_id"],
                        schedule_id,
                        reg_type,
                        int(current_user()["user_id"]),
                        pay_method,
                        None,
                    ],
                )
                reg = fetch_one(
                    """
                    SELECT r.reg_no, r.queue_no, r.reg_fee, r.reg_type,
                           dept.department_name, u.user_name AS doctor_name, d.title
                    FROM registrations r
                    JOIN departments dept ON r.department_id = dept.department_id
                    JOIN doctors d ON r.doctor_id = d.doctor_id
                    JOIN users u ON d.user_id = u.user_id
                    WHERE r.reg_no = %s
                    """,
                    (reg_no,),
                )
                sched = fetch_one(
                    "SELECT clinic_room FROM doctor_schedules WHERE schedule_id = %s", (schedule_id,)
                )
                success = {
                    "patient_name": patient["patient_name"],
                    "clinic_room": sched["clinic_room"] if sched else None,
                }
                success.update(reg or {})
                return render_template(
                    "kiosk.html", id_card="", patient=None, today_regs=[],
                    schedules=[], success=success,
                )
        except mysql.connector.Error as error:
            flash(handle_db_error(error), "error")
            return redirect(url_for("kiosk", id_card=request.form.get("id_card", "").strip()))
        except ValueError:
            flash("提交参数格式错误，请检查后重试。", "error")
            return redirect(url_for("kiosk"))

    id_card = request.args.get("id_card", "").strip()
    patient = None
    today_regs = []
    reg_history = []
    pay_history = []
    reports = []
    if id_card:
        patient = fetch_one(
            """
            SELECT patient_id, patient_no, patient_name, gender, birth_date, id_card, phone
            FROM patients WHERE id_card = %s OR phone = %s
            """,
            (id_card, id_card),
        )
        if patient:
            today_regs = fetch_all(
                """
                SELECT r.reg_no, r.reg_type, r.reg_fee, r.queue_no, r.visit_status,
                       dept.department_name, u.user_name AS doctor_name
                FROM registrations r
                JOIN departments dept ON r.department_id = dept.department_id
                JOIN doctors doc ON r.doctor_id = doc.doctor_id
                JOIN users u ON doc.user_id = u.user_id
                WHERE r.patient_id = %s AND r.reg_date = CURDATE()
                ORDER BY r.queue_no
                """,
                (patient["patient_id"],),
            )
            # 患者端：历史就诊记录
            reg_history = fetch_all(
                """
                SELECT r.reg_no, r.reg_date, r.reg_type, r.reg_fee, r.queue_no, r.visit_status,
                       dept.department_name, u.user_name AS doctor_name
                FROM registrations r
                JOIN departments dept ON r.department_id = dept.department_id
                JOIN doctors doc ON r.doctor_id = doc.doctor_id
                JOIN users u ON doc.user_id = u.user_id
                WHERE r.patient_id = %s
                ORDER BY r.reg_date DESC, r.registration_id DESC
                """,
                (patient["patient_id"],),
            )
            # 患者端：缴费记录
            pay_history = fetch_all(
                """
                SELECT p.payment_no, p.payment_type, p.pay_amount, p.pay_method, p.pay_status, p.pay_time
                FROM payments p
                WHERE p.patient_id = %s
                ORDER BY p.payment_id DESC
                """,
                (patient["patient_id"],),
            )
            # 患者端：检验报告单（含结果明细）
            reports = fetch_all(
                """
                SELECT t.test_id, t.test_no, t.total_fee, t.test_status, t.completed_at,
                       dept.department_name,
                       GROUP_CONCAT(
                         CONCAT(li.item_name, '：', IFNULL(r.result_value, '未出'),
                           IF(r.result_flag IS NOT NULL AND r.result_flag <> '',
                              CONCAT('（', r.result_flag, '）'), ''))
                         ORDER BY r.result_id SEPARATOR '；'
                       ) AS items_text
                FROM lab_tests t
                JOIN departments dept ON t.department_id = dept.department_id
                LEFT JOIN lab_test_results r ON r.test_id = t.test_id
                LEFT JOIN lab_items li ON li.item_id = r.item_id
                WHERE t.patient_id = %s AND t.test_status <> '已作废'
                GROUP BY t.test_id, t.test_no, t.total_fee, t.test_status, t.completed_at, dept.department_name
                ORDER BY t.test_id DESC
                """,
                (patient["patient_id"],),
            )
    schedules = fetch_all(
        """
        SELECT s.schedule_id, s.work_date, s.shift_type, s.clinic_room,
               s.max_registrations, s.registered_count,
               doc.doctor_id, doc.doctor_name, doc.title,
               dept.department_name, doc.consultation_fee,
               GREATEST(s.max_registrations - s.registered_count -
                 (SELECT COUNT(*) FROM appointments a
                   WHERE a.schedule_id = s.schedule_id AND a.status = '已预约' AND a.expire_time > NOW()), 0) AS remain
        FROM doctor_schedules s
        JOIN (
          SELECT d.doctor_id, u.user_name AS doctor_name, d.title, d.consultation_fee, u.department_id
          FROM doctors d JOIN users u ON d.user_id = u.user_id
        ) doc ON s.doctor_id = doc.doctor_id
        JOIN departments dept ON doc.department_id = dept.department_id
        WHERE s.work_date = CURDATE() AND s.schedule_status = '正常'
        ORDER BY dept.department_name, s.shift_type
        """
    )
    return render_template("kiosk.html", id_card=id_card, patient=patient,
                           today_regs=today_regs, schedules=schedules, success=None,
                           reg_history=reg_history, pay_history=pay_history, reports=reports)


# ============================================================
# 医生排班管理
# ============================================================

@app.route("/schedules", methods=["GET", "POST"])
@login_required
@permission_required("schedules")
def schedules():
    user = current_user()
    # 医生角色只能维护自己的排班：取当前登录用户对应的医生档案
    is_doctor = user["role_code"] == "DOCTOR"
    # 只有管理员和医生可操作排班；挂号员等其他角色仅可查看
    can_edit = user["role_code"] in ("ADMIN", "DOCTOR")
    my_doctor_id = None
    my_doctor_name = None
    if is_doctor:
        my_row = fetch_one(
            """
            SELECT d.doctor_id, d.doctor_no, u.user_name AS doctor_name, dept.department_name, d.title
            FROM doctors d
            JOIN users u ON d.user_id = u.user_id
            JOIN departments dept ON u.department_id = dept.department_id
            WHERE d.user_id = %s
            """,
            (user["user_id"],),
        )
        my_doctor_id = my_row["doctor_id"] if my_row else None
        my_doctor_no = my_row["doctor_no"] if my_row else None
        my_doctor_name = my_row["doctor_name"] if my_row else None
    else:
        my_doctor_no = None

    def _schedule_belongs_to_me(schedule_id):
        """医生操作排班前校验归属：只能操作自己的排班"""
        if not is_doctor:
            return True
        owner = fetch_one(
            "SELECT doctor_id FROM doctor_schedules WHERE schedule_id = %s", (schedule_id,)
        )
        return bool(owner and my_doctor_id and owner["doctor_id"] == my_doctor_id)

    if request.method == "POST":
        action = request.form.get("action")
        if not can_edit:
            flash("当前角色仅可查看排班，不能进行操作。", "error")
            return redirect(url_for("schedules"))
        try:
            if action == "add":
                # 医生只能给自己排班：doctor_id 强制取当前账号档案，忽略表单提交值
                if is_doctor:
                    if not my_doctor_id:
                        flash("当前账号未关联医生档案，无法排班。", "error")
                        return redirect(url_for("schedules"))
                    doctor_id = my_doctor_id
                else:
                    doctor_id = int(request.form["doctor_id"])
                execute(
                    """
                    INSERT INTO doctor_schedules (doctor_id, work_date, shift_type, clinic_room, max_registrations)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        doctor_id,
                        request.form["work_date"],
                        request.form["shift_type"],
                        request.form.get("clinic_room") or None,
                        int(request.form["max_registrations"]),
                    ),
                )
                flash("排班创建成功。", "success")
            elif action == "status":
                schedule_id = int(request.form["schedule_id"])
                if not _schedule_belongs_to_me(schedule_id):
                    flash("只能操作自己的排班。", "error")
                    return redirect(url_for("schedules"))
                execute(
                    "UPDATE doctor_schedules SET schedule_status = %s WHERE schedule_id = %s",
                    (request.form["schedule_status"], schedule_id),
                )
                flash("排班状态已更新。", "success")
            elif action == "delete":
                schedule_id = int(request.form["schedule_id"])
                if not _schedule_belongs_to_me(schedule_id):
                    flash("只能操作自己的排班。", "error")
                    return redirect(url_for("schedules"))
                execute("DELETE FROM doctor_schedules WHERE schedule_id = %s", (schedule_id,))
                flash("排班已删除。", "success")
        except mysql.connector.Error as error:
            flash(handle_db_error(error), "error")
        except ValueError:
            flash("提交参数格式错误，请检查后重试。", "error")
        return redirect(url_for("schedules"))

    work_date = request.args.get("work_date") or datetime.now().strftime("%Y-%m-%d")
    if is_doctor:
        # 医生只能看到自己的排班（视图无 doctor_id，用医生工号过滤）
        rows = fetch_all(
            """
            SELECT * FROM v_doctor_schedule
            WHERE work_date = %s AND doctor_no = %s
            ORDER BY shift_type
            """,
            (work_date, my_doctor_no),
        )
    else:
        rows = fetch_all(
            """
            SELECT * FROM v_doctor_schedule
            WHERE work_date = %s
            ORDER BY department_name, shift_type
            """,
            (work_date,),
        )
    doctors = fetch_all(
        """
        SELECT d.doctor_id, u.user_name AS doctor_name, dept.department_name, d.title, d.consultation_fee
        FROM doctors d
        JOIN users u ON d.user_id = u.user_id
        JOIN departments dept ON u.department_id = dept.department_id
        ORDER BY dept.department_id, d.doctor_id
        """
    )
    return render_template("schedules.html", rows=rows, doctors=doctors, work_date=work_date,
                           is_doctor=is_doctor, can_edit=can_edit, my_doctor_name=my_doctor_name)


# ============================================================
# 处方开具
# ============================================================

@app.route("/prescriptions", methods=["GET", "POST"])
@login_required
@permission_required("prescriptions")
def prescriptions():
    user = current_user()
    # 医生角色只能以本人名义开方：取当前登录用户对应的医生档案
    is_doctor = user["role_code"] == "DOCTOR"
    my_doctor_id = None
    my_doctor_name = None
    if is_doctor:
        my_row = fetch_one(
            "SELECT doctor_id FROM doctors WHERE user_id = %s", (user["user_id"],)
        )
        my_doctor_id = my_row["doctor_id"] if my_row else None
        my_doctor_name = user["user_name"]

    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "issue":
                medicine_ids = request.form.getlist("medicine_id[]")
                quantities = request.form.getlist("quantity[]")
                dosages = request.form.getlist("dosage[]")
                items = []
                for mid, qty, dosage in zip(medicine_ids, quantities, dosages):
                    if not mid.strip():
                        continue
                    items.append({
                        "medicine_id": int(mid),
                        "quantity": int(qty or 1),
                        "dosage": dosage.strip() or "",
                    })
                if not items:
                    flash("请至少添加一种药品。", "error")
                    return redirect(url_for("prescriptions"))
                registration_id = int(request.form["registration_id"]) if request.form.get("registration_id") else None
                reg_row = fetch_one(
                    "SELECT patient_id FROM registrations WHERE registration_id = %s", (registration_id,)
                ) if registration_id else None
                patient_id = reg_row["patient_id"] if reg_row else int(request.form["patient_id"])
                # 医生只能以本人名义开方：doctor_id 强制取当前账号档案，忽略表单提交值
                if is_doctor:
                    if not my_doctor_id:
                        flash("当前账号未关联医生档案，无法开方。", "error")
                        return redirect(url_for("prescriptions"))
                    doctor_id = my_doctor_id
                else:
                    doctor_id = int(request.form["doctor_id"])
                call_proc(
                    "sp_issue_prescription",
                    [
                        make_no("PRES"),
                        patient_id,
                        doctor_id,
                        registration_id,
                        json.dumps(items, ensure_ascii=False),
                    ],
                )
                flash("处方开具成功，状态为待收费。", "success")
            elif action == "void":
                prescription_id = int(request.form["prescription_id"])
                # 医生只能作废自己开具的处方
                if is_doctor:
                    owner = fetch_one(
                        "SELECT doctor_id FROM prescriptions WHERE prescription_id = %s", (prescription_id,)
                    )
                    if not owner or owner["doctor_id"] != my_doctor_id:
                        flash("只能操作自己开具的处方。", "error")
                        return redirect(url_for("prescriptions"))
                execute(
                    "UPDATE prescriptions SET prescription_status = '已作废' WHERE prescription_id = %s",
                    (prescription_id,),
                )
                flash("处方已作废。", "success")
            elif action in ("start_visit", "finish_visit"):
                registration_id = int(request.form["registration_id"])
                # 医生只能接诊/完成本人名下的挂号
                if is_doctor:
                    row = fetch_one(
                        "SELECT doctor_id, visit_status FROM registrations WHERE registration_id = %s",
                        (registration_id,),
                    )
                    if not row or row["doctor_id"] != my_doctor_id:
                        flash("只能操作自己名下的挂号记录。", "error")
                        return redirect(url_for("prescriptions"))
                target = "就诊中" if action == "start_visit" else "已就诊"
                src = "待就诊" if action == "start_visit" else "就诊中"
                execute(
                    "UPDATE registrations SET visit_status = %s WHERE registration_id = %s AND visit_status = %s",
                    (target, registration_id, src),
                )
                flash("已开始接诊，患者状态更新为就诊中。" if action == "start_visit"
                      else "已完成就诊，患者状态更新为已就诊。", "success")
        except mysql.connector.Error as error:
            flash(handle_db_error(error), "error")
        except ValueError:
            flash("提交参数格式错误，请检查后重试。", "error")
        return redirect(url_for("prescriptions"))

    status = request.args.get("status") or ""
    base_sql = """
        SELECT p.prescription_id, p.prescription_no, p.prescribe_date, p.total_amount,
               p.prescription_status, pat.patient_name, u.user_name AS doctor_name,
               dept.department_name
        FROM prescriptions p
        JOIN patients pat ON p.patient_id = pat.patient_id
        JOIN doctors doc ON p.doctor_id = doc.doctor_id
        JOIN users u ON doc.user_id = u.user_id
        JOIN departments dept ON u.department_id = dept.department_id
    """
    if is_doctor:
        # 医生只能看到自己开具的处方
        if status:
            rows = fetch_all(
                base_sql + " WHERE p.prescription_status = %s AND p.doctor_id = %s ORDER BY p.prescription_id DESC",
                (status, my_doctor_id),
            )
        else:
            rows = fetch_all(
                base_sql + " WHERE p.doctor_id = %s ORDER BY p.prescription_id DESC", (my_doctor_id,)
            )
    else:
        if status:
            rows = fetch_all(base_sql + " WHERE p.prescription_status = %s ORDER BY p.prescription_id DESC", (status,))
        else:
            rows = fetch_all(base_sql + " ORDER BY p.prescription_id DESC")

    # 开方所需的患者（今日已挂号待就诊/就诊中；医生只能看到挂给本人的患者）
    registerable_sql = """
        SELECT r.registration_id, r.reg_no, pat.patient_id, pat.patient_name, dept.department_name,
               u.user_name AS doctor_name, r.visit_status
        FROM registrations r
        JOIN patients pat ON r.patient_id = pat.patient_id
        JOIN departments dept ON r.department_id = dept.department_id
        JOIN doctors doc ON r.doctor_id = doc.doctor_id
        JOIN users u ON doc.user_id = u.user_id
        WHERE r.reg_date = CURDATE() AND r.visit_status IN ('待就诊','就诊中')
    """
    if is_doctor:
        registerable = fetch_all(
            registerable_sql + " AND r.doctor_id = %s ORDER BY r.queue_no", (my_doctor_id,)
        )
    else:
        registerable = fetch_all(registerable_sql + " ORDER BY r.queue_no")
    # 今日就诊队列（医生端：接诊 / 完成就诊 状态流转）
    queue_sql = """
        SELECT r.registration_id, r.reg_no, pat.patient_name, dept.department_name,
               u.user_name AS doctor_name, r.reg_type, r.queue_no, r.visit_status
        FROM registrations r
        JOIN patients pat ON r.patient_id = pat.patient_id
        JOIN departments dept ON r.department_id = dept.department_id
        JOIN doctors doc ON r.doctor_id = doc.doctor_id
        JOIN users u ON doc.user_id = u.user_id
        WHERE r.reg_date = CURDATE() AND r.visit_status IN ('待就诊', '就诊中')
    """
    if is_doctor:
        visit_queue = fetch_all(
            queue_sql + " AND r.doctor_id = %s ORDER BY r.queue_no", (my_doctor_id,)
        )
    else:
        visit_queue = fetch_all(queue_sql + " ORDER BY r.queue_no")
    medicines = fetch_all(
        "SELECT medicine_id, medicine_code, medicine_name, specification, unit, unit_price, stock_quantity FROM medicines WHERE status = '启用' ORDER BY medicine_name"
    )
    # 医生选择（DOCTOR 角色强制本人，页面不显示下拉）
    doctors = fetch_all(
        """
        SELECT d.doctor_id, u.user_name AS doctor_name, dept.department_name
        FROM doctors d
        JOIN users u ON d.user_id = u.user_id
        JOIN departments dept ON u.department_id = dept.department_id
        ORDER BY dept.department_id
        """
    )
    return render_template("prescriptions.html", rows=rows, registerable=registerable,
                           medicines=medicines, doctors=doctors, status=status,
                           is_doctor=is_doctor, my_doctor_name=my_doctor_name,
                           visit_queue=visit_queue)


# ============================================================
# 收费退费
# ============================================================

@app.route("/payments", methods=["GET", "POST"])
@login_required
@permission_required("payments")
def payments():
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "charge":
                call_proc(
                    "sp_charge",
                    [
                        int(request.form["prescription_id"]),
                        int(current_user()["user_id"]),
                        make_no("PAY"),
                        request.form["pay_method"],
                    ],
                )
                flash("收费成功，药品库存已扣减。", "success")
            elif action == "refund":
                call_proc(
                    "sp_refund",
                    [
                        int(request.form["prescription_id"]),
                        int(current_user()["user_id"]),
                        request.form.get("reason") or "患者申请退费",
                    ],
                )
                flash("退费成功，药品库存已恢复。", "success")
            elif action == "charge_lab":
                call_proc(
                    "sp_charge_lab_test",
                    [
                        make_no("PAY"),
                        int(request.form["test_id"]),
                        request.form["pay_method"],
                        int(current_user()["user_id"]),
                    ],
                )
                flash("检验收费成功。", "success")
        except mysql.connector.Error as error:
            flash(handle_db_error(error), "error")
        return redirect(url_for("payments"))

    waiting = fetch_all(
        """
        SELECT p.prescription_id, p.prescription_no, pat.patient_name, u.user_name AS doctor_name,
               p.total_amount, p.prescribe_date
        FROM prescriptions p
        JOIN patients pat ON p.patient_id = pat.patient_id
        JOIN doctors doc ON p.doctor_id = doc.doctor_id
        JOIN users u ON doc.user_id = u.user_id
        WHERE p.prescription_status = '待收费'
        ORDER BY p.prescription_id
        """
    )
    charged = fetch_all(
        """
        SELECT p.prescription_id, p.prescription_no, pat.patient_name, u.user_name AS doctor_name,
               p.total_amount, p.prescription_status,
               pm.payment_no, pm.pay_method, pm.pay_time, pm.refund_time, pm.refund_reason
        FROM prescriptions p
        JOIN patients pat ON p.patient_id = pat.patient_id
        JOIN doctors doc ON p.doctor_id = doc.doctor_id
        JOIN users u ON doc.user_id = u.user_id
        LEFT JOIN payments pm ON p.prescription_id = pm.prescription_id AND pm.payment_type = '药品费'
        WHERE p.prescription_status IN ('已收费','已退费')
        ORDER BY p.prescription_id DESC
        """
    )
    lab_waiting = fetch_all(
        """
        SELECT test_id, test_no, patient_name, doctor_name, total_fee, ordered_at
        FROM v_lab_test_list
        WHERE test_status <> '已作废' AND pay_status = '未收费'
        ORDER BY test_id DESC LIMIT 50
        """
    )
    lab_charged = fetch_all(
        """
        SELECT pm.payment_no, pm.pay_method, pm.pay_time, pm.pay_amount,
               t.test_no, pat.patient_name
        FROM payments pm
        JOIN lab_tests t ON pm.lab_test_id = t.test_id
        JOIN patients pat ON pm.patient_id = pat.patient_id
        WHERE pm.payment_type = '检查费'
        ORDER BY pm.payment_id DESC LIMIT 50
        """
    )
    return render_template(
        "payments.html", waiting=waiting, charged=charged,
        lab_waiting=lab_waiting, lab_charged=lab_charged,
    )


# ============================================================
# 药品目录管理
# ============================================================

@app.route("/medicines", methods=["GET", "POST"])
@login_required
@permission_required("medicines")
def medicines():
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "add":
                execute(
                    """
                    INSERT INTO medicines (medicine_code, medicine_name, specification, unit,
                                           manufacturer, unit_price, stock_quantity, medicine_type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        request.form["medicine_code"].strip(),
                        request.form["medicine_name"].strip(),
                        request.form.get("specification") or None,
                        request.form["unit"],
                        request.form.get("manufacturer") or None,
                        float(request.form["unit_price"]),
                        int(request.form["stock_quantity"]),
                        request.form["medicine_type"],
                    ),
                )
                flash("药品新增成功。", "success")
            elif action == "update":
                execute(
                    """
                    UPDATE medicines
                    SET unit_price = %s, stock_quantity = %s, status = %s
                    WHERE medicine_id = %s
                    """,
                    (
                        float(request.form["unit_price"]),
                        int(request.form["stock_quantity"]),
                        request.form["status"],
                        request.form["medicine_id"],
                    ),
                )
                flash("药品信息已更新。", "success")
        except mysql.connector.Error as error:
            flash(handle_db_error(error), "error")
        return redirect(url_for("medicines"))

    keyword = request.args.get("keyword", "").strip()
    if keyword:
        like = f"%{keyword}%"
        rows = fetch_all(
            "SELECT * FROM medicines WHERE medicine_name LIKE %s OR medicine_code LIKE %s ORDER BY medicine_id",
            (like, like),
        )
    else:
        rows = fetch_all("SELECT * FROM medicines ORDER BY medicine_id")
    return render_template("medicines.html", rows=rows, keyword=keyword)


# ============================================================
# 报表查询
# ============================================================

@app.route("/reports")
@login_required
@permission_required("reports")
def reports():
    data = {
        "reg": fetch_all("SELECT * FROM v_today_registrations ORDER BY registration_count DESC"),
        "schedule": fetch_all("SELECT * FROM v_doctor_schedule WHERE work_date = CURDATE() ORDER BY department_name"),
        "payment": fetch_all("SELECT * FROM v_daily_payment ORDER BY pay_date DESC"),
        "prescription": fetch_all("SELECT * FROM v_prescription_detail ORDER BY prescription_id DESC"),
        "workload": fetch_all("SELECT * FROM v_doctor_workload ORDER BY total_amount DESC"),
    }
    return render_template("reports.html", data=data, tab=request.args.get("tab", "reg"))


# ============================================================
# 编号参考
# ============================================================

@app.route("/codes")
@login_required
@permission_required("codes")
def codes():
    data = {
        "departments": fetch_all("SELECT department_id, department_code, department_name FROM departments ORDER BY department_id"),
        "roles": fetch_all("SELECT role_id, role_code, role_name FROM roles ORDER BY role_id"),
        "doctors": fetch_all(
            """
            SELECT d.doctor_id, d.doctor_no, u.user_name AS doctor_name, dept.department_name, d.title, d.consultation_fee
            FROM doctors d
            JOIN users u ON d.user_id = u.user_id
            JOIN departments dept ON u.department_id = dept.department_id
            ORDER BY d.doctor_id
            """
        ),
        "patients": fetch_all("SELECT patient_id, patient_no, patient_name, phone FROM patients ORDER BY patient_id"),
        "medicines": fetch_all("SELECT medicine_id, medicine_code, medicine_name, unit_price, stock_quantity FROM medicines ORDER BY medicine_id"),
        "users": fetch_all(
            """
            SELECT u.user_id, u.user_no, u.user_name, r.role_name, u.account_status
            FROM users u JOIN roles r ON u.role_id = r.role_id ORDER BY u.user_id
            """
        ),
    }
    return render_template("codes.html", data=data)


# ============================================================
# 数据导出（CSV）
# ============================================================

@app.route("/export/<name>")
@login_required
@permission_required("export")
def export_csv(name):
    queries = {
        "patients": ("患者档案", "SELECT * FROM patients"),
        "registrations": ("今日挂号", "SELECT * FROM registrations WHERE reg_date = CURDATE()"),
        "prescriptions": ("处方数据", "SELECT * FROM v_prescription_detail"),
        "payments": ("收费记录", "SELECT * FROM payments"),
        "medicines": ("药品目录", "SELECT * FROM medicines"),
    }
    if name not in queries:
        flash("导出类型不存在。", "error")
        return redirect(url_for("reports"))

    title, sql = queries[name]
    rows = fetch_all(sql)
    output = []
    if rows:
        headers = list(rows[0].keys())
        output.append(",".join(headers))
        for row in rows:
            output.append(",".join(str(row.get(header, "")) for header in headers))
    else:
        output.append("暂无数据")
    csv_text = "\ufeff" + "\n".join(output)
    filename = f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        csv_text,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )



# ============================================================
# 体征记录（医生录入患者各项体征）
# ============================================================

def _form_num(field, cast=int):
    value = request.form.get(field, "").strip()
    if not value:
        return None
    return cast(value)


@app.route("/vitals", methods=["GET", "POST"])
@login_required
@permission_required("vitals")
def vitals():
    user = current_user()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            try:
                call_proc(
                    "sp_add_vital_sign",
                    [
                        int(request.form["patient_id"]),
                        int(request.form["registration_id"]) if request.form.get("registration_id") else None,
                        int(user["user_id"]),
                        _form_num("temperature", float),
                        _form_num("systolic_pressure"),
                        _form_num("diastolic_pressure"),
                        _form_num("heart_rate"),
                        _form_num("respiration_rate"),
                        _form_num("weight", float),
                        _form_num("height", float),
                        request.form.get("note") or None,
                    ],
                )
                flash("体征记录已保存。", "success")
            except mysql.connector.Error as error:
                flash(handle_db_error(error), "error")
            except ValueError:
                flash("体征数值格式错误，请检查后重试。", "error")
        return redirect(url_for("vitals"))

    patients = fetch_all(
        "SELECT patient_id, patient_no, patient_name, phone FROM patients ORDER BY patient_id"
    )
    regs = fetch_all(
        """
        SELECT r.registration_id, r.reg_no, r.patient_id, p.patient_name, r.visit_status
        FROM registrations r
        JOIN patients p ON r.patient_id = p.patient_id
        WHERE r.visit_status IN ('待就诊', '就诊中')
        ORDER BY r.registration_id
        """
    )
    if user["role_code"] == "ADMIN":
        rows = fetch_all("SELECT * FROM v_vital_history ORDER BY record_time DESC, vital_id DESC LIMIT 200")
    else:
        rows = fetch_all(
            "SELECT * FROM v_vital_history WHERE operator_name = %s ORDER BY record_time DESC, vital_id DESC LIMIT 200",
            (user["user_name"],),
        )
    return render_template("vitals.html", patients=patients, regs=regs, rows=rows)


# ============================================================
# 检验科管理（申请 / 结果录入 / 汇总）
# ============================================================

@app.route("/lab_tests", methods=["GET", "POST"])
@login_required
@permission_required("lab")
def lab_tests():
    user = current_user()
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "add_test":
                if user["role_code"] not in ("DOCTOR", "ADMIN"):
                    flash("仅医生或管理员可以开具检验申请。", "error")
                    return redirect(url_for("lab_tests"))
                call_proc(
                    "sp_add_lab_test",
                    [
                        make_no("LAB"),
                        int(request.form["patient_id"]),
                        int(request.form["doctor_id"]),
                        int(request.form["department_id"]),
                        int(request.form["registration_id"]) if request.form.get("registration_id") else None,
                        request.form.get("sample_type") or "血液",
                        ",".join(request.form.getlist("item_ids")),
                        int(user["user_id"]),
                    ],
                )
                flash("检验申请开具成功，状态：待检验。", "success")
            elif action == "cancel_test":
                call_proc("sp_cancel_lab_test", [int(request.form["test_id"]), int(user["user_id"])])
                flash("检验单已作废。", "success")
        except mysql.connector.Error as error:
            flash(handle_db_error(error), "error")
        except ValueError:
            flash("提交参数格式错误，请检查后重试。", "error")
        return redirect(url_for("lab_tests"))

    status = request.args.get("status", "").strip()
    summary = {
        "today_total": fetch_one("SELECT COUNT(*) AS v FROM lab_tests WHERE DATE(ordered_at) = CURDATE()")["v"],
        "doing": fetch_one("SELECT COUNT(*) AS v FROM lab_tests WHERE test_status IN ('待检验', '检验中')")["v"],
        "done": fetch_one("SELECT COUNT(*) AS v FROM lab_tests WHERE test_status = '已完成'")["v"],
        "abnormal": fetch_one(
            "SELECT COUNT(*) AS v FROM lab_test_results WHERE result_flag IN ('偏高', '偏低', '异常')"
        )["v"],
    }
    patient_summary = fetch_all(
        "SELECT * FROM v_lab_patient_summary ORDER BY test_count DESC, patient_id LIMIT 30"
    )
    if status:
        rows = fetch_all("SELECT * FROM v_lab_test_list WHERE test_status = %s ORDER BY test_id DESC", (status,))
    else:
        rows = fetch_all("SELECT * FROM v_lab_test_list ORDER BY test_id DESC LIMIT 100")
    patients = fetch_all("SELECT patient_id, patient_no, patient_name FROM patients ORDER BY patient_id")
    doctors = fetch_all(
        """
        SELECT doc.doctor_id, u.user_name, u.department_id, d.department_name
        FROM doctors doc
        JOIN users u ON doc.user_id = u.user_id
        JOIN departments d ON u.department_id = d.department_id
        ORDER BY doc.doctor_id
        """
    )
    items = fetch_all("SELECT * FROM lab_items WHERE status = '启用' ORDER BY item_category, item_id")
    regs = fetch_all(
        """
        SELECT r.registration_id, r.reg_no, r.patient_id, p.patient_name
        FROM registrations r
        JOIN patients p ON r.patient_id = p.patient_id
        WHERE r.visit_status IN ('待就诊', '就诊中')
        ORDER BY r.registration_id
        """
    )
    return render_template(
        "lab_tests.html",
        summary=summary,
        patient_summary=patient_summary,
        rows=rows,
        patients=patients,
        doctors=doctors,
        items=items,
        regs=regs,
        status=status,
    )


def auto_flag(reference_range, result_value):
    """根据参考范围自动判定检验结果标志。
    数值区间（如 3.5-9.5）：低于下限=偏低，高于上限=偏高，区间内=正常；
    定性值（如 阴性）：与参考值一致=正常，否则=异常；
    无法判定返回 None（由人工选择）。"""
    if not reference_range or result_value is None:
        return None
    text = str(reference_range).strip()
    value = str(result_value).strip()
    if not value:
        return None
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*[-~]\s*(-?\d+(?:\.\d+)?)\s*$", text)
    if m:
        try:
            v = float(value)
        except ValueError:
            return None
        lo, hi = float(m.group(1)), float(m.group(2))
        if v < lo:
            return "偏低"
        if v > hi:
            return "偏高"
        return "正常"
    return "正常" if value == text else "异常"


@app.route("/lab_tests/<int:test_id>/results", methods=["GET", "POST"])
@login_required
@permission_required("lab")
def lab_results(test_id):
    user = current_user()
    test = fetch_one("SELECT * FROM v_lab_test_list WHERE test_id = %s", (test_id,))
    if not test:
        flash("检验单不存在。", "error")
        return redirect(url_for("lab_tests"))
    items = fetch_all(
        """
        SELECT r.result_id, li.item_name, li.item_category, li.unit, li.reference_range,
               r.result_value, r.result_flag, r.result_note
        FROM lab_test_results r
        JOIN lab_items li ON r.item_id = li.item_id
        WHERE r.test_id = %s
        ORDER BY li.item_category, li.item_id
        """,
        (test_id,),
    )
    if request.method == "POST":
        action = request.form.get("action")
        if action == "record":
            if user["role_code"] not in ("LAB_TECH", "ADMIN"):
                flash("仅检验技师或管理员可以录入检验结果。", "error")
                return redirect(url_for("lab_results", test_id=test_id))
            try:
                for item in items:
                    result_value = request.form.get(f"result_{item['result_id']}", "").strip()
                    auto = auto_flag(item["reference_range"], result_value)
                    if auto is not None:
                        result_flag = auto
                    else:
                        result_flag = request.form.get(f"flag_{item['result_id']}", "正常")
                    result_note = request.form.get(f"note_{item['result_id']}", "").strip()
                    call_proc(
                        "sp_record_lab_result",
                        [
                            test_id,
                            item["result_id"],
                            result_value or None,
                            result_flag,
                            result_note or None,
                            int(user["user_id"]),
                        ],
                    )
                flash("检验结果已保存，状态已自动流转。", "success")
            except mysql.connector.Error as error:
                flash(handle_db_error(error), "error")
        return redirect(url_for("lab_results", test_id=test_id))
    can_edit = test["test_status"] in ("待检验", "检验中") and user["role_code"] in ("LAB_TECH", "ADMIN")
    return render_template("lab_results.html", test=test, items=items, can_edit=can_edit)



# ============================================================
# 诊断管理（医生添加/修改患者诊断结果）
# ============================================================

@app.route("/diagnosis", methods=["GET", "POST"])
@login_required
@permission_required("diagnosis")
def diagnosis():
    user = current_user()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "save":
            try:
                execute(
                    "UPDATE patients SET diagnosis = %s WHERE patient_id = %s",
                    (request.form.get("diagnosis") or None, int(request.form["patient_id"])),
                )
                flash("诊断结果已保存。", "success")
            except mysql.connector.Error as error:
                flash(handle_db_error(error), "error")
        return redirect(url_for("diagnosis"))
    patients = fetch_all(
        "SELECT patient_id, patient_no, patient_name, diagnosis FROM patients ORDER BY patient_id"
    )
    return render_template("diagnosis.html", patients=patients)


# ============================================================
# 药房发药管理
# ============================================================

@app.route("/pharmacy", methods=["GET", "POST"])
@login_required
@permission_required("dispense")
def pharmacy():
    user = current_user()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "dispense":
            try:
                call_proc("sp_dispense", [int(request.form["prescription_id"]), int(user["user_id"])])
                flash("发药成功。", "success")
            except mysql.connector.Error as error:
                flash(handle_db_error(error), "error")
        return redirect(url_for("pharmacy"))

    pending = fetch_all(
        """
        SELECT p.prescription_id, p.prescription_no, pat.patient_name, u.user_name AS doctor_name,
               p.total_amount, p.prescribe_date
        FROM prescriptions p
        JOIN patients pat ON p.patient_id = pat.patient_id
        JOIN doctors doc ON p.doctor_id = doc.doctor_id
        JOIN users u ON doc.user_id = u.user_id
        WHERE p.prescription_status = '已收费'
        ORDER BY p.prescription_id
        """
    )
    dispensed = fetch_all(
        """
        SELECT p.prescription_id, p.prescription_no, pat.patient_name, u.user_name AS doctor_name,
               p.total_amount, p.prescribe_date
        FROM prescriptions p
        JOIN patients pat ON p.patient_id = pat.patient_id
        JOIN doctors doc ON p.doctor_id = doc.doctor_id
        JOIN users u ON doc.user_id = u.user_id
        WHERE p.prescription_status = '已发药'
        ORDER BY p.prescription_id DESC
        """
    )
    return render_template("pharmacy.html", pending=pending, dispensed=dispensed)


# ============================================================
# 用户管理（超级用户为医院工作人员注册账号）
# ============================================================

@app.route("/users", methods=["GET", "POST"])
@login_required
@permission_required("users")
def users():
    user = current_user()
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "add_user":
                role_id = int(request.form["role_id"])
                role = fetch_one("SELECT role_code FROM roles WHERE role_id = %s", (role_id,))
                if not role:
                    flash("角色不存在。", "error")
                    return redirect(url_for("users"))
                username = request.form["username"].strip()
                password = request.form["password"].strip()
                if not username or not password:
                    flash("用户名与密码不能为空。", "error")
                    return redirect(url_for("users"))
                if role["role_code"] == "DOCTOR":
                    dept_id = int(request.form["department_id"])
                    call_proc(
                        "sp_add_doctor",
                        [
                            request.form["user_no"].strip() or f"D{datetime.now().strftime('%H%M%S')}",
                            request.form["user_name"].strip(),
                            request.form["gender"],
                            dept_id,
                            request.form.get("phone") or None,
                            username,
                            sha256_text(password),
                            request.form.get("title") or "主治医师",
                            request.form.get("specialty") or None,
                            float(request.form.get("consultation_fee") or 0),
                            int(request.form.get("max_daily_registrations") or 30),
                        ],
                    )
                else:
                    call_proc(
                        "sp_add_user",
                        [
                            request.form["user_no"].strip() or f"U{datetime.now().strftime('%H%M%S')}",
                            request.form["user_name"].strip(),
                            request.form["gender"],
                            int(request.form["department_id"]) if request.form.get("department_id") else None,
                            role_id,
                            request.form.get("phone") or None,
                            username,
                            sha256_text(password),
                        ],
                    )
                flash("新账号注册成功。", "success")
            elif action == "reset_pwd":
                execute(
                    "UPDATE users SET password_hash = %s WHERE user_id = %s",
                    (sha256_text(request.form["new_password"]), int(request.form["user_id"])),
                )
                flash("密码已重置。", "success")
            elif action == "toggle_status":
                execute(
                    "UPDATE users SET account_status = IF(account_status = '启用', '停用', '启用') WHERE user_id = %s",
                    (int(request.form["user_id"]),),
                )
                flash("账号状态已更新。", "success")
            elif action == "delete_user":
                target_id = int(request.form["user_id"])
                if target_id == int(user["user_id"]):
                    flash("不能删除当前登录账号。", "error")
                else:
                    # 先清理医生附属档案（新建医生账号会自动生成档案，若不清理会触发外键拦截）
                    execute("DELETE FROM doctors WHERE user_id = %s", (target_id,))
                    # 清理该账号产生的操作日志（登录/注册留痕，非业务单据）
                    execute("DELETE FROM operation_logs WHERE user_id = %s", (target_id,))
                    execute("DELETE FROM users WHERE user_id = %s", (target_id,))
                    flash("账号已删除。", "success")
        except mysql.connector.Error as error:
            flash(handle_db_error(error), "error")
        except ValueError:
            flash("提交参数格式错误，请检查后重试。", "error")
        return redirect(url_for("users"))

    rows = fetch_all(
        """
        SELECT u.user_id, u.user_no, u.user_name, u.username, u.gender, u.phone,
               u.account_status, u.last_login_at,
               r.role_code, r.role_name, d.department_name
        FROM users u
        JOIN roles r ON u.role_id = r.role_id
        LEFT JOIN departments d ON u.department_id = d.department_id
        ORDER BY u.user_id
        """
    )
    roles = fetch_all("SELECT role_id, role_code, role_name FROM roles ORDER BY role_id")
    depts = fetch_all("SELECT department_id, department_name FROM departments ORDER BY department_id")
    return render_template("users.html", rows=rows, roles=roles, depts=depts)


# ============================================================
# 公告管理（管理员发布，全员可见）
# ============================================================

@app.route("/announcements", methods=["GET", "POST"])
@login_required
def announcements():
    if request.method == "POST":
        user = current_user()
        if user["role_code"] != "ADMIN":
            flash("仅管理员可发布或删除公告。", "error")
            return redirect(url_for("announcements"))
        action = request.form.get("action")
        try:
            if action == "publish":
                title = request.form.get("title", "").strip()
                content = request.form.get("content", "").strip()
                if not title or not content:
                    flash("标题与内容不能为空。", "error")
                    return redirect(url_for("announcements"))
                execute(
                    "INSERT INTO announcements (title, content, publisher_id) VALUES (%s, %s, %s)",
                    (title, content, user["user_id"]),
                )
                flash("公告发布成功。", "success")
            elif action == "delete":
                execute(
                    "DELETE FROM announcements WHERE announcement_id = %s",
                    (int(request.form["announcement_id"]),),
                )
                flash("公告已删除。", "success")
        except mysql.connector.Error as error:
            flash(handle_db_error(error), "error")
        except ValueError:
            flash("提交参数格式错误，请检查后重试。", "error")
        return redirect(url_for("announcements"))

    rows = fetch_all(
        """
        SELECT a.announcement_id, a.title, a.content, a.created_at,
               u.user_name AS publisher_name
        FROM announcements a
        JOIN users u ON a.publisher_id = u.user_id
        WHERE a.is_published = 1
        ORDER BY a.created_at DESC, a.announcement_id DESC
        """
    )
    return render_template("announcements.html", rows=rows,
                           is_admin=current_user()["role_code"] == "ADMIN")


# ============================================================
# 科室管理（管理员维护科室字典）
# ============================================================

@app.route("/departments", methods=["GET", "POST"])
@login_required
@permission_required("departments")
def departments():
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "add":
                code = request.form.get("department_code", "").strip()
                name = request.form.get("department_name", "").strip()
                desc = request.form.get("description", "").strip()
                if not code or not name:
                    flash("科室编码与名称不能为空。", "error")
                    return redirect(url_for("departments"))
                execute(
                    "INSERT INTO departments (department_code, department_name, description) VALUES (%s, %s, %s)",
                    (code, name, desc or None),
                )
                flash("科室新增成功。", "success")
            elif action == "edit":
                execute(
                    "UPDATE departments SET department_code = %s, department_name = %s, description = %s WHERE department_id = %s",
                    (
                        request.form.get("department_code", "").strip(),
                        request.form.get("department_name", "").strip(),
                        request.form.get("description", "").strip() or None,
                        int(request.form["department_id"]),
                    ),
                )
                flash("科室信息已更新。", "success")
            elif action == "delete":
                execute(
                    "DELETE FROM departments WHERE department_id = %s",
                    (int(request.form["department_id"]),),
                )
                flash("科室已删除。", "success")
        except mysql.connector.Error as error:
            flash(handle_db_error(error), "error")
        except ValueError:
            flash("提交参数格式错误，请检查后重试。", "error")
        return redirect(url_for("departments"))

    rows = fetch_all(
        """
        SELECT d.department_id, d.department_code, d.department_name, d.description,
               (SELECT COUNT(*) FROM users u WHERE u.department_id = d.department_id) AS user_count,
               (SELECT COUNT(*) FROM doctors doc
                  JOIN users u2 ON doc.user_id = u2.user_id
                 WHERE u2.department_id = d.department_id) AS doctor_count
        FROM departments d
        ORDER BY d.department_id
        """
    )
    return render_template("departments.html", rows=rows)


# ============================================================
# 可选的 URL 前缀支持（默认关闭，本地开发和常规部署都不需要）
#
# 用途：如果云平台把函数挂在子路径下（例如函数目录为
# cloud-functions/his/[[default]].py，对外路径就变成 /his/...），
# 平台剥离前缀后才交给 Flask，这时 url_for / 页面跳转生成的链接会
# 丢掉前缀。设置环境变量 HIS_URL_PREFIX=/his 即可自动补上。
# ============================================================

URL_PREFIX = os.getenv("HIS_URL_PREFIX", "").strip().rstrip("/")


class _PrefixMiddleware:
    """把 URL 前缀写入 WSGI 的 SCRIPT_NAME，让 Flask 生成的链接带上前缀。"""

    def __init__(self, wsgi_app, prefix):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        environ["SCRIPT_NAME"] = self.prefix
        return self.wsgi_app(environ, start_response)


if URL_PREFIX:
    app.wsgi_app = _PrefixMiddleware(app.wsgi_app, URL_PREFIX)


if __name__ == "__main__":
    # 本地开发：python web_app/app.py
    # 云端部署时由平台接管启动，这两个环境变量用不上
    app.run(
        host=os.getenv("HIS_WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("HIS_WEB_PORT", "5000")),
        debug=os.getenv("HIS_WEB_DEBUG", "1") == "1",
    )
