"""测试公共基础设施。

设计要点
--------
1. **零额外依赖**：只使用标准库 ``unittest``，不要求安装 pytest。
2. **离线用例与数据库用例分离**：
   - 纯静态 / 纯函数用例（路由清单、异常分层、密码哈希、结果标志判定……）
     不碰数据库，克隆仓库后即可运行；
   - 业务集成用例通过 Flask 测试客户端走真实 HTTP 处理链路，
     需要本地 MySQL（``127.0.0.1:3306``，库 ``hospital_outpatient``）。
     数据库不可用时，这些用例会 **优雅跳过** 而不是报错。
3. **数据自清理**：所有由测试创建的挂号、处方、检验、收费等业务数据，
   一律挂在测试自建的「测试患者」名下；``tearDown`` 时按外键依赖顺序
   从测试患者反查并删除，保证不污染既有业务数据，且可重复运行。

⚠️ 集成用例假定运行期间没有其他人在同时操作同一套本地库
   （本地课程设计场景的标准前提）。测试会在结束时清理本次运行产生的
   业务数据与操作日志。
"""

import os
import random
import string
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------
# 让 tests/ 下的用例可以 import web_app 中的 app / db
# ---------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_APP_DIR = REPO_ROOT / "web_app"
if str(WEB_APP_DIR) not in sys.path:
    sys.path.insert(0, str(WEB_APP_DIR))

import mysql.connector  # noqa: E402  （必须在 sys.path 调整之后）


# ---------------------------------------------------------------
# 数据库连接参数（与 web_app/db.py 的本地默认值一致，可用环境变量覆盖）
# ---------------------------------------------------------------
DB_CONFIG = {
    "host": os.getenv("HIS_DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("HIS_DB_PORT", "3306")),
    "user": os.getenv("HIS_DB_USER", "root"),
    "password": os.getenv("HIS_DB_PASSWORD", "123456"),
    "database": os.getenv("HIS_DB_NAME", "hospital_outpatient"),
    "autocommit": True,
    "charset": "utf8mb4",
    "use_unicode": True,
    "connection_timeout": 5,
}

# 测试自建数据的统一前缀，便于识别与清理
MARK = "TST"


def _db_available():
    """探测本地 MySQL 是否可用（不可用时集成用例跳过）。"""
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        conn.close()
        return True
    except Exception:
        return False


DB_AVAILABLE = _db_available()

requires_db = unittest.skipUnless(
    DB_AVAILABLE,
    "本地 MySQL 不可用（%s:%s/%s），跳过需要数据库的集成用例。"
    "请先运行 start_mysql.bat 并执行 init_database.py。" % (
        DB_CONFIG["host"], DB_CONFIG["port"], DB_CONFIG["database"]),
)


def unique(tag=""):
    """生成一次性的唯一后缀，避免唯一约束在重复运行时冲突。"""
    return "%s%s%s" % (tag, int(time.time() * 1000) % 10_000_000,
                       "".join(random.choices(string.ascii_uppercase, k=3)))


def connect():
    """新建一条测试专用连接（autocommit）。"""
    return mysql.connector.connect(**DB_CONFIG)


class HisTestCase(unittest.TestCase):
    """所有集成用例的基类：提供连接、登录辅助与数据自清理。"""

    # 测试里会用到的基础数据快照（setUpClass 填充）
    roles = {}
    departments = {}
    doctors = {}      # doctor_id -> {user_id, user_name, department_id, title}
    users = {}        # username -> user_id

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import app as app_module  # noqa: E402  （导入需要 web_app 在 sys.path）

        cls.app_module = app_module
        # TESTING 让未处理异常直接抛出，失败时能看到完整堆栈
        app_module.app.config["TESTING"] = True

        cls.conn = connect()
        cls.cur = cls.conn.cursor(dictionary=True)
        cls._snapshot_baseline()

    @classmethod
    def _snapshot_baseline(cls):
        cls.cur.execute("SELECT role_id, role_code, role_name FROM roles")
        cls.roles = {r["role_code"]: r for r in cls.cur.fetchall()}

        cls.cur.execute("SELECT department_id, department_code, department_name FROM departments")
        cls.departments = {r["department_name"]: r for r in cls.cur.fetchall()}

        cls.cur.execute(
            """
            SELECT d.doctor_id, d.user_id, d.title, d.doctor_no,
                   u.user_name, u.department_id
            FROM doctors d JOIN users u ON d.user_id = u.user_id
            ORDER BY d.doctor_id
            """
        )
        cls.doctors = {r["doctor_id"]: r for r in cls.cur.fetchall()}

        cls.cur.execute("SELECT username, user_id FROM users")
        cls.users = {r["username"]: r["user_id"] for r in cls.cur.fetchall()}

    @classmethod
    def tearDownClass(cls):
        try:
            cls.cur.close()
            cls.conn.close()
        except Exception:
            pass
        super().tearDownClass()

    # -----------------------------------------------------------
    # 每个用例：新建测试客户端 + 记录清理目标
    # -----------------------------------------------------------
    def setUp(self):
        self.app = self.app_module.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.patient_ids = []      # 测试自建患者（连带清理其全部业务数据）
        self.extra_cleanup = []    # [(sql, params), ...] 逆序执行
        self.run_started = datetime.now().replace(microsecond=0)

    def tearDown(self):
        try:
            self._purge()
        finally:
            self.client = None

    # -----------------------------------------------------------
    # 数据清理
    # -----------------------------------------------------------
    def _purge(self):
        """清理本用例创建的数据。

        业务数据统一挂在测试患者名下 → 从 patients 出发按外键依赖顺序反查删除；
        字典类数据（用户/科室/药品/公告/排班）在用例中显式登记，逆序删除。
        """
        cur = self.conn.cursor()
        try:
            if self.patient_ids:
                ph = ",".join(["%s"] * len(self.patient_ids))
                args = tuple(self.patient_ids)
                # 1) 收费（引用处方 / 检验 / 挂号 / 患者）
                cur.execute("DELETE FROM payments WHERE patient_id IN (%s)" % ph, args)
                # 2) 检验结果 -> 检验单
                cur.execute(
                    "DELETE r FROM lab_test_results r JOIN lab_tests t "
                    "ON r.test_id = t.test_id WHERE t.patient_id IN (%s)" % ph, args)
                cur.execute("DELETE FROM lab_tests WHERE patient_id IN (%s)" % ph, args)
                # 3) 处方明细 -> 处方
                cur.execute(
                    "DELETE pi FROM prescription_items pi JOIN prescriptions p "
                    "ON pi.prescription_id = p.prescription_id WHERE p.patient_id IN (%s)" % ph, args)
                cur.execute("DELETE FROM prescriptions WHERE patient_id IN (%s)" % ph, args)
                # 4) 体征 / 病历 / 挂号 / 患者
                cur.execute("DELETE FROM vital_signs WHERE patient_id IN (%s)" % ph, args)
                cur.execute("DELETE FROM medical_records WHERE patient_id IN (%s)" % ph, args)
                cur.execute("DELETE FROM registrations WHERE patient_id IN (%s)" % ph, args)
                cur.execute("DELETE FROM patients WHERE patient_id IN (%s)" % ph, args)
            for sql, params in reversed(self.extra_cleanup):
                try:
                    cur.execute(sql, params or ())
                except Exception:
                    pass
            # 清理本次运行新增的操作日志（触发器写入的审计留痕）
            cur.execute("DELETE FROM operation_logs WHERE created_at >= %s", (self.run_started,))
        finally:
            cur.close()

    # -----------------------------------------------------------
    # HTTP 辅助
    # -----------------------------------------------------------
    def login(self, username, password="123456", client=None):
        target = client or self.client
        return target.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=True,
        )

    def client_as(self, username, password="123456"):
        """返回一个已登录为指定账号的独立测试客户端。"""
        c = self.app.test_client()
        resp = self.login(username, password, client=c)
        self.assertEqual(resp.status_code, 200, "登录 %s 失败" % username)
        return c

    @staticmethod
    def flash_text(resp):
        """把响应体转成文本，便于断言 flash 提示。"""
        return resp.get_data(as_text=True)

    # -----------------------------------------------------------
    # 直接查库辅助
    # -----------------------------------------------------------
    def one(self, sql, params=None):
        cur = self.conn.cursor(dictionary=True)
        try:
            cur.execute(sql, params or ())
            return cur.fetchone()
        finally:
            cur.close()

    def all(self, sql, params=None):
        cur = self.conn.cursor(dictionary=True)
        try:
            cur.execute(sql, params or ())
            return cur.fetchall()
        finally:
            cur.close()

    def exec_sql(self, sql, params=None):
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params or ())
            return cur.rowcount
        finally:
            cur.close()

    # -----------------------------------------------------------
    # 夹具构造
    # -----------------------------------------------------------
    def mk_patient(self, name=None, suffix=None):
        """创建一个测试患者，返回 patient_id（自动登记清理）。"""
        sfx = suffix or unique()
        pno = "TSTP%s" % sfx
        rec = {
            "patient_no": pno,
            "patient_name": name or ("测试患者%s" % sfx),
            "gender": "男",
            "birth_date": "1990-01-01",
            "id_card": "TSTID%s" % sfx,
            "phone": "139%08d" % (int(time.time() * 1000) % 100_000_000),
            "address": "自动化测试地址",
            "blood_type": "A",
            "medical_history": None,
        }
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO patients (patient_no, patient_name, gender, birth_date,
                                  id_card, phone, address, blood_type, medical_history)
            VALUES (%(patient_no)s, %(patient_name)s, %(gender)s, %(birth_date)s,
                    %(id_card)s, %(phone)s, %(address)s, %(blood_type)s, %(medical_history)s)
            """, rec)
        pid = cur.lastrowid
        cur.close()
        self.patient_ids.append(pid)
        return pid

    def mk_schedule(self, doctor_id, work_date=None, shift_type="上午",
                    max_registrations=20, status="正常"):
        """为指定医生插入一条排班（日期默认今天），返回 schedule_id。"""
        work_date = work_date or datetime.now().strftime("%Y-%m-%d")
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO doctor_schedules
              (doctor_id, work_date, shift_type, clinic_room, max_registrations,
               registered_count, schedule_status)
            VALUES (%s, %s, %s, %s, %s, 0, %s)
            """,
            (doctor_id, work_date, shift_type, "AUTO-TST", max_registrations, status))
        sid = cur.lastrowid
        cur.close()
        self.extra_cleanup.append(
            ("DELETE FROM doctor_schedules WHERE schedule_id = %s", (sid,)))
        return sid

    def doctor_for(self, index=0):
        """取一个基线医生（按 doctor_id 排序）。"""
        return sorted(self.doctors.keys())[index]

    def track_patient(self, patient_no):
        """把通过 HTTP 建档的患者纳入清理范围，返回 patient_id。"""
        row = self.one("SELECT patient_id FROM patients WHERE patient_no = %s", (patient_no,))
        if row:
            self.patient_ids.append(row["patient_id"])
            return row["patient_id"]
        return None

    def doctor_id_of(self, username):
        """按登录名取医生档案 id（用于基线医生账号，如 doctor01）。"""
        row = self.one(
            "SELECT d.doctor_id FROM doctors d JOIN users u ON d.user_id = u.user_id "
            "WHERE u.username = %s", (username,))
        return row["doctor_id"] if row else None

    def dept_of_doctor(self, doctor_id):
        """取医生所属科室 id（测试自建医生不在基线快照里，回库查询）。"""
        if doctor_id in self.doctors and self.doctors[doctor_id]["department_id"]:
            return int(self.doctors[doctor_id]["department_id"])
        row = self.one(
            "SELECT u.department_id FROM doctors d JOIN users u ON d.user_id = u.user_id "
            "WHERE d.doctor_id = %s", (doctor_id,))
        return int(row["department_id"]) if row and row["department_id"] else None

    def mk_medicine(self, name=None, price=9.90, stock=100, status="启用",
                    mtype="西药"):
        """新增一条测试药品，返回 medicine_id。"""
        code = "TSTM%s" % unique()
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO medicines (medicine_code, medicine_name, specification, unit,
                                   manufacturer, unit_price, stock_quantity, medicine_type, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (code, name or ("测试药%s" % code), "10mg*10片", "盒", "自动化测试",
             price, stock, mtype, status))
        mid = cur.lastrowid
        cur.close()
        self.extra_cleanup.append(("DELETE FROM medicines WHERE medicine_id = %s", (mid,)))
        return mid

    def mk_user(self, username=None, role_code="CASHIER", name=None,
                department_id=None, status="启用"):
        """直接写入一个测试账号（不走存储过程），返回 user_id。

        清理语句按「逆序执行」的需要 **倒着登记**：
        extra_cleanup 是 LIFO，登记 users → doctors → logs 才能保证
        真正执行时是 logs → doctors → users（满足外键依赖）。
        """
        import app as app_module
        sfx = unique()
        uname = username or ("tst_%s" % sfx.lower())
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO users (user_no, user_name, gender, department_id, role_id,
                               username, password_hash, account_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            ("TSTU%s" % sfx, name or ("测试账号%s" % sfx), "女", department_id,
             self.roles[role_code]["role_id"], uname,
             app_module.hash_password("123456"), status))
        uid = cur.lastrowid
        cur.close()
        self.extra_cleanup.append(("DELETE FROM users WHERE user_id = %s", (uid,)))
        self.extra_cleanup.append(("DELETE FROM doctors WHERE user_id = %s", (uid,)))
        self.extra_cleanup.append(("DELETE FROM operation_logs WHERE user_id = %s", (uid,)))
        return uid

    def mk_doctor(self, department_name="内科", fee=10.00, title="主治医师",
                  max_daily=30):
        """创建一个独立的测试医生（users + doctors），返回 doctor_id。

        集成用例使用专属医生，避免与基线排班（同医生同日期同班次的唯一键）
        以及在跑的既有业务数据相互干扰。
        """
        dept = self.departments.get(department_name)
        dept_id = dept["department_id"] if dept else None
        uid = self.mk_user(role_code="DOCTOR", department_id=dept_id)
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO doctors (user_id, doctor_no, title, specialty,
                                 consultation_fee, max_daily_registrations)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (uid, "TSTD%s" % unique(), title, "自动化测试", fee, max_daily))
        did = cur.lastrowid
        cur.close()
        # 逆序执行时需要先删 doctors 再删 users；users 的删除已在 mk_user 登记，
        # 这里后登记 doctors 删除即可保证顺序正确。
        self.extra_cleanup.append(("DELETE FROM doctors WHERE doctor_id = %s", (did,)))
        return did

    def mk_registration(self, patient_id, doctor_id, department_id=None,
                        schedule_id=None, reg_date=None, reg_type="普通号",
                        visit_status="待就诊"):
        """直接插入一条挂号（不走存储过程），返回 registration_id。"""
        reg_date = reg_date or datetime.now().strftime("%Y-%m-%d")
        department_id = department_id or self.dept_of_doctor(doctor_id)
        reg_no = "TSTREG%s" % unique()
        fee = 10.00 if reg_type == "普通号" else 30.00
        nxt = self.one(
            "SELECT COALESCE(MAX(queue_no), 0) + 1 AS n FROM registrations "
            "WHERE doctor_id = %s AND reg_date = %s", (doctor_id, reg_date))["n"]
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO registrations (reg_no, patient_id, doctor_id, department_id,
                                       reg_date, reg_type, reg_fee, queue_no, visit_status, operator_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (reg_no, patient_id, doctor_id, department_id, reg_date, reg_type,
             fee, nxt, visit_status, self.users.get("cash01", 3)))
        rid = cur.lastrowid
        cur.close()
        return rid

    # -----------------------------------------------------------
    # 通用用户 id 便捷属性
    # -----------------------------------------------------------
    @property
    def uid(self):
        return self.users
