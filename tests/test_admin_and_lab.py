"""字典维护（科室/用户/药品/公告）、检验科全流程、诊断与报表导出。

覆盖 26 个写路由里此前没有被前面几个文件触达的部分：

* 科室管理   ／注册账号、编码唯一、被引用不可删
* 用户管理   ／sp_add_user、sp_add_doctor、重置密码、启用↔锁定、删自己、级联删除保护
* 公告管理   ／仅管理员可发布/删除、空标题拦截、全员可见
* 药品目录   ／新增、编码唯一、改价改库存、检索
* 检验科流程 ／开单 → 收费 → 录结果（状态自动流转）→ 作废；标志位按参考范围自动判定
* 诊断管理   ／医生维护患者「最近诊断」，缺参数走第三层
* 报表 / 导出／CSV 转义（逗号与双引号）、未知导出类型、越权拦截

设计约定与 ``tests/base.py`` 一致：所有业务数据挂在测试自建患者名下自动清理；
字典类数据（科室/用户/药品/公告）在用例中显式登记删除语句，``tearDown`` 逆序执行。
"""

import re
import time
import unittest

from tests.base import HisTestCase, requires_db, unique


def phone():
    """生成一个不会撞 unique 索引的手机号（微秒级，够用且不依赖随机）。"""
    return "138%08d" % (int(time.time() * 1_000_000) % 100_000_000)


def logged_in(client):
    """判定测试客户端是否真的建立了登录会话。

    只看「响应里没有报错文案」会把「因别的原因（锁定/越权）跳回登录页」
    误判成登录成功，所以这里直接检查 Flask 会话里的 ``user`` 键。
    """
    with client.session_transaction() as sess:
        return "user" in sess


class _AdminFixture(HisTestCase):
    """管理员用例的公共夹具：登录管理员 + 通过 /users 界面建测试账号。

    单独抽出来是为了让「科室管理」与「用户管理」两组用例各自独立成测试类
    （直接继承会让子类把父类用例再跑一遍）。
    """

    def setUp(self):
        super().setUp()
        self.admin = self.client_as("admin01")

    def _add_user(self, role_code="CASHIER", department_id=None, username=None):
        """通过 /users 界面注册一个测试账号，返回 (resp, user_id, username)。"""
        sfx = unique()
        uname = username or ("tstd_%s" % sfx.lower())
        resp = self.admin.post("/users", data={
            "action": "add_user",
            "user_no": "TSTU%s" % sfx,
            "user_name": "测试账号%s" % sfx,
            "gender": "女",
            "role_id": self.roles[role_code]["role_id"],
            "username": uname,
            "password": "123456",
            "phone": phone(),
            "department_id": department_id,
        }, follow_redirects=True)
        uid = self._track_user(uname)
        return resp, uid, uname

    def _track_user(self, uname):
        """把指定登录名的账号登记进清理队列，返回 user_id（不存在返回 None）。

        登记顺序 users → doctors → logs：``extra_cleanup`` 是 LIFO，
        真正执行时是 logs → doctors → users，满足外键依赖。
        """
        row = self.one("SELECT user_id FROM users WHERE username = %s", (uname,))
        if not row:
            return None
        uid = row["user_id"]
        self.extra_cleanup.append(("DELETE FROM users WHERE user_id = %s", (uid,)))
        self.extra_cleanup.append(("DELETE FROM doctors WHERE user_id = %s", (uid,)))
        self.extra_cleanup.append(("DELETE FROM operation_logs WHERE user_id = %s", (uid,)))
        return uid


# ----------------------------------------------------------------------
# 科室管理（管理员维护科室字典）
# ----------------------------------------------------------------------
@requires_db
class DepartmentAdminTests(_AdminFixture):

    def _add(self, client=None, **over):
        sfx = unique()
        data = {
            "action": "add",
            "department_code": "TSTD%s" % sfx,
            "department_name": "测试科室%s" % sfx,
            "description": "自动化测试用科室",
        }
        data.update(over)
        resp = (client or self.admin).post("/departments", data=data, follow_redirects=True)
        return resp, data

    def _track(self, code):
        row = self.one("SELECT department_id FROM departments WHERE department_code = %s", (code,))
        if row:
            self.extra_cleanup.append(
                ("DELETE FROM departments WHERE department_id = %s", (row["department_id"],)))
            return row["department_id"]
        return None

    def test_add_department(self):
        resp, data = self._add()
        self.assertIn("科室新增成功", self.flash_text(resp))
        did = self._track(data["department_code"])
        self.assertIsNotNone(did)
        row = self.one("SELECT * FROM departments WHERE department_id = %s", (did,))
        self.assertEqual(row["department_name"], data["department_name"])
        self.assertEqual(row["description"], "自动化测试用科室")

    def test_duplicate_code_reports_field(self):
        resp, data = self._add()
        self._track(data["department_code"])
        dup = dict(data)
        dup["department_name"] = "另一个名字%s" % unique()
        resp2 = self.admin.post("/departments", data=dup, follow_redirects=True)
        self.assertIn("科室编码已存在", self.flash_text(resp2))

    def test_missing_fields_rejected(self):
        resp = self.admin.post("/departments", data={
            "action": "add", "department_code": "", "department_name": ""},
            follow_redirects=True)
        self.assertIn("科室编码与名称不能为空", self.flash_text(resp))

    def test_edit_department(self):
        _, data = self._add()
        did = self._track(data["department_code"])
        resp = self.admin.post("/departments", data={
            "action": "edit",
            "department_id": did,
            "department_code": data["department_code"],
            "department_name": "改名后的科室%s" % unique(),
            "description": "已更新",
        }, follow_redirects=True)
        self.assertIn("科室信息已更新", self.flash_text(resp))
        row = self.one("SELECT department_name, description FROM departments "
                       "WHERE department_id = %s", (did,))
        self.assertEqual(row["description"], "已更新")

    def test_delete_department_without_users(self):
        _, data = self._add()
        did = self._track(data["department_code"])
        resp = self.admin.post("/departments",
                               data={"action": "delete", "department_id": did},
                               follow_redirects=True)
        self.assertIn("科室已删除", self.flash_text(resp))
        self.assertIsNone(self.one("SELECT 1 AS x FROM departments WHERE department_id = %s",
                                   (did,)))

    def test_delete_department_with_user_blocked(self):
        """科室下还有账号时删除被外键拦下，应给出可读提示而不是 500。"""
        _, data = self._add()
        did = self._track(data["department_code"])
        # 在新建科室下放一个测试账号（清理语句先登记科室，再登记账号 →
        # 逆序执行时先删账号后删科室，满足外键依赖）
        self._add_user(role_code="CASHIER", department_id=did)
        resp = self.admin.post("/departments",
                               data={"action": "delete", "department_id": did},
                               follow_redirects=True)
        body = self.flash_text(resp)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("引用", body)
        self.assertIsNotNone(self.one("SELECT 1 AS x FROM departments WHERE department_id = %s",
                                      (did,)))

    def test_cashier_denied(self):
        cash = self.client_as("cash01")
        resp = cash.get("/departments", follow_redirects=True)
        self.assertIn("当前角色无权访问该功能", self.flash_text(resp))


# ----------------------------------------------------------------------
# 用户管理（超级用户为医院工作人员注册账号）
# ----------------------------------------------------------------------
@requires_db
class UserAdminTests(_AdminFixture):

    def test_add_cashier_user_and_login(self):
        resp, uid, uname = self._add_user(role_code="CASHIER")
        self.assertIn("新账号注册成功", self.flash_text(resp))
        self.assertIsNotNone(uid)
        row = self.one(
            """
            SELECT u.username, u.user_no, u.account_status, r.role_code
            FROM users u JOIN roles r ON u.role_id = r.role_id
            WHERE u.user_id = %s
            """, (uid,))
        self.assertEqual(row["role_code"], "CASHIER")
        self.assertEqual(row["account_status"], "启用")
        # 新账号可以真正登录（走 sp_add_user 写入的哈希必须能被校验通过）
        c = self.app.test_client()
        self.login(uname, "123456", client=c)
        self.assertTrue(logged_in(c), "%s 应当能用初始密码登录" % uname)

    def test_add_doctor_user_creates_profile(self):
        """角色为医生时走 sp_add_doctor：一次生成 users + doctors 两份档案。"""
        sfx = unique()
        uname = "tstdr_%s" % sfx.lower()
        resp = self.admin.post("/users", data={
            "action": "add_user",
            "user_no": "TSTDR%s" % sfx,
            "user_name": "测试医生%s" % sfx,
            "gender": "男",
            "role_id": self.roles["DOCTOR"]["role_id"],
            "username": uname,
            "password": "123456",
            "department_id": self.departments["内科"]["department_id"],
            "title": "副主任医师",
            "specialty": "自动化测试",
            "consultation_fee": "25",
            "max_daily_registrations": "40",
        }, follow_redirects=True)
        self.assertIn("新账号注册成功", self.flash_text(resp))
        row = self.one(
            """
            SELECT u.user_id, d.doctor_id, d.title, d.consultation_fee,
                   d.max_daily_registrations
            FROM users u JOIN doctors d ON d.user_id = u.user_id
            WHERE u.username = %s
            """, (uname,))
        self.assertIsNotNone(row, "医生账号必须同时生成 doctors 档案")
        uid = row["user_id"]
        self.extra_cleanup.append(("DELETE FROM users WHERE user_id = %s", (uid,)))
        self.extra_cleanup.append(("DELETE FROM doctors WHERE user_id = %s", (uid,)))
        self.extra_cleanup.append(("DELETE FROM operation_logs WHERE user_id = %s", (uid,)))
        self.assertEqual(row["title"], "副主任医师")
        self.assertEqual(float(row["consultation_fee"]), 25.0)
        self.assertEqual(row["max_daily_registrations"], 40)

    def test_duplicate_username_reports_field(self):
        resp, uid, uname = self._add_user()
        self.assertIsNotNone(uid)
        resp2 = self.admin.post("/users", data={
            "action": "add_user",
            "user_no": "TSTU%s" % unique(),
            "user_name": "重名测试",
            "gender": "女",
            "role_id": self.roles["CASHIER"]["role_id"],
            "username": uname,
            "password": "123456",
            "phone": phone(),
        }, follow_redirects=True)
        self.assertIn("登录名已存在", self.flash_text(resp2))

    def test_missing_password_rejected(self):
        resp = self.admin.post("/users", data={
            "action": "add_user",
            "user_no": "TSTU%s" % unique(),
            "user_name": "缺密码",
            "gender": "女",
            "role_id": self.roles["CASHIER"]["role_id"],
            "username": "tstnopwd_%s" % unique().lower(),
            "password": "",
        }, follow_redirects=True)
        self.assertIn("用户名与密码不能为空", self.flash_text(resp))

    def test_reset_password(self):
        _, uid, uname = self._add_user()
        resp = self.admin.post("/users", data={
            "action": "reset_pwd", "user_id": uid, "new_password": "abc12345",
        }, follow_redirects=True)
        self.assertIn("密码已重置", self.flash_text(resp))
        c = self.app.test_client()
        self.login(uname, "abc12345", client=c)
        self.assertTrue(logged_in(c), "重置后应当能用新密码登录")

    def test_toggle_status_locks_and_unlocks(self):
        """account_status 是 ENUM('启用','锁定')，切换后锁定的账号不能登录。"""
        _, uid, uname = self._add_user()
        self.admin.post("/users", data={"action": "toggle_status", "user_id": uid},
                        follow_redirects=True)
        self.assertEqual(self.one("SELECT account_status FROM users WHERE user_id = %s",
                                  (uid,))["account_status"], "锁定")
        c = self.app.test_client()
        body = self.flash_text(self.login(uname, "123456", client=c))
        self.assertIn("锁定", body)
        self.admin.post("/users", data={"action": "toggle_status", "user_id": uid},
                        follow_redirects=True)
        self.assertEqual(self.one("SELECT account_status FROM users WHERE user_id = %s",
                                  (uid,))["account_status"], "启用")

    def test_cannot_delete_self(self):
        me = self.one("SELECT user_id FROM users WHERE username = 'admin01'")
        resp = self.admin.post("/users", data={"action": "delete_user",
                                               "user_id": me["user_id"]},
                               follow_redirects=True)
        self.assertIn("不能删除当前登录账号", self.flash_text(resp))

    def test_delete_user_with_business_blocked(self):
        """账号已有业务留痕（挂号/处方）时删除必须被拦下，且不能留下半删数据。

        这里给测试医生挂一条真实挂号：删除应当报「已被其它业务记录引用」，
        并且 users 与 doctors 两份档案都必须还在（execute_atomic 整体回滚）。
        """
        sfx = unique()
        uname = "tstdr_%s" % sfx.lower()
        self.admin.post("/users", data={
            "action": "add_user",
            "user_no": "TSTDR%s" % sfx,
            "user_name": "待删医生%s" % sfx,
            "gender": "男",
            "role_id": self.roles["DOCTOR"]["role_id"],
            "username": uname,
            "password": "123456",
            "department_id": self.departments["内科"]["department_id"],
            "title": "医师",
            "consultation_fee": "10",
            "max_daily_registrations": "20",
        }, follow_redirects=True)
        row = self.one("SELECT u.user_id, d.doctor_id FROM users u "
                       "JOIN doctors d ON d.user_id = u.user_id WHERE u.username = %s",
                       (uname,))
        self.assertIsNotNone(row)
        uid, did = row["user_id"], row["doctor_id"]
        self.extra_cleanup.append(("DELETE FROM users WHERE user_id = %s", (uid,)))
        self.extra_cleanup.append(("DELETE FROM doctors WHERE user_id = %s", (uid,)))
        self.extra_cleanup.append(("DELETE FROM operation_logs WHERE user_id = %s", (uid,)))

        pid = self.mk_patient()
        self.mk_registration(pid, did)

        resp = self.admin.post("/users", data={"action": "delete_user", "user_id": uid},
                               follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("引用", self.flash_text(resp))
        self.assertIsNotNone(self.one("SELECT 1 AS x FROM users WHERE user_id = %s", (uid,)))
        self.assertIsNotNone(self.one("SELECT 1 AS x FROM doctors WHERE doctor_id = %s", (did,)))

    def test_doctor_cannot_open_user_management(self):
        doc = self.client_as("doctor01")
        body = self.flash_text(doc.get("/users", follow_redirects=True))
        self.assertIn("当前角色无权访问该功能", body)


# ----------------------------------------------------------------------
# 公告管理（管理员发布，全员可见）
# ----------------------------------------------------------------------
@requires_db
class AnnouncementTests(HisTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.client_as("admin01")

    def _publish(self, title=None, content="自动化测试公告内容", client=None):
        title = title or ("TST公告%s" % unique())
        resp = (client or self.admin).post("/announcements", data={
            "action": "publish", "title": title, "content": content,
        }, follow_redirects=True)
        row = self.one("SELECT announcement_id FROM announcements WHERE title = %s", (title,))
        if row:
            self.extra_cleanup.append(
                ("DELETE FROM announcements WHERE announcement_id = %s",
                 (row["announcement_id"],)))
            return resp, row["announcement_id"], title
        return resp, None, title

    def test_admin_publish_and_list(self):
        resp, aid, title = self._publish()
        self.assertIn("公告发布成功", self.flash_text(resp))
        self.assertIsNotNone(aid)
        row = self.one("SELECT * FROM announcements WHERE announcement_id = %s", (aid,))
        self.assertEqual(row["title"], title)
        self.assertEqual(row["is_published"], 1)
        self.assertEqual(row["publisher_id"],
                         self.users["admin01"])
        # 列表页能看到
        self.assertIn(title, self.flash_text(self.admin.get("/announcements")))

    def test_empty_title_or_content_rejected(self):
        resp = self.admin.post("/announcements",
                               data={"action": "publish", "title": "  ", "content": ""},
                               follow_redirects=True)
        self.assertIn("标题与内容不能为空", self.flash_text(resp))

    def test_non_admin_cannot_publish(self):
        doc = self.client_as("doctor01")
        before = self.one("SELECT COUNT(*) AS v FROM announcements")["v"]
        resp = doc.post("/announcements",
                        data={"action": "publish", "title": "越权公告",
                              "content": "不应写入"}, follow_redirects=True)
        self.assertIn("仅管理员可发布或删除公告", self.flash_text(resp))
        self.assertEqual(self.one("SELECT COUNT(*) AS v FROM announcements")["v"], before)

    def test_non_admin_cannot_delete(self):
        _, aid, title = self._publish()
        doc = self.client_as("doctor01")
        resp = doc.post("/announcements",
                        data={"action": "delete", "announcement_id": aid},
                        follow_redirects=True)
        self.assertIn("仅管理员可发布或删除公告", self.flash_text(resp))
        self.assertIsNotNone(self.one(
            "SELECT 1 AS x FROM announcements WHERE announcement_id = %s", (aid,)))

    def test_admin_delete(self):
        _, aid, _ = self._publish()
        resp = self.admin.post("/announcements",
                               data={"action": "delete", "announcement_id": aid},
                               follow_redirects=True)
        self.assertIn("公告已删除", self.flash_text(resp))
        self.assertIsNone(self.one(
            "SELECT 1 AS x FROM announcements WHERE announcement_id = %s", (aid,)))

    def test_visible_to_every_role(self):
        _, aid, title = self._publish()
        for username in ("admin01", "cash01", "doctor01", "lab01", "pharm01"):
            c = self.client_as(username)
            self.assertIn(title, self.flash_text(c.get("/announcements")),
                          "%s 应当能看到公告" % username)


# ----------------------------------------------------------------------
# 药品目录管理
# ----------------------------------------------------------------------
@requires_db
class MedicineAdminTests(HisTestCase):

    def setUp(self):
        super().setUp()
        self.pharm = self.client_as("pharm01")

    def _add(self, client=None, **over):
        code = "TSTM%s" % unique()
        data = {
            "action": "add",
            "medicine_code": code,
            "medicine_name": "测试药品%s" % unique(),
            "specification": "0.25g*24片",
            "unit": "盒",
            "manufacturer": "测试药厂",
            "unit_price": "12.50",
            "stock_quantity": "100",
            "medicine_type": "西药",
        }
        data.update(over)
        resp = (client or self.pharm).post("/medicines", data=data, follow_redirects=True)
        row = self.one("SELECT medicine_id FROM medicines WHERE medicine_code = %s",
                       (data["medicine_code"],))
        if row:
            self.extra_cleanup.append(
                ("DELETE FROM medicines WHERE medicine_id = %s", (row["medicine_id"],)))
            return resp, row["medicine_id"], data
        return resp, None, data

    def test_add_medicine(self):
        resp, mid, data = self._add()
        self.assertIn("药品新增成功", self.flash_text(resp))
        self.assertIsNotNone(mid)
        row = self.one("SELECT * FROM medicines WHERE medicine_id = %s", (mid,))
        self.assertEqual(row["medicine_name"], data["medicine_name"])
        self.assertEqual(float(row["unit_price"]), 12.5)
        self.assertEqual(row["stock_quantity"], 100)
        self.assertEqual(row["status"], "启用")

    def test_duplicate_code_reports_field(self):
        _, _, data = self._add()
        dup = dict(data)
        dup["medicine_name"] = "同名不同码%s" % unique()
        resp = self.pharm.post("/medicines", data=dup, follow_redirects=True)
        self.assertIn("药品编码已存在", self.flash_text(resp))

    def test_missing_required_field_is_handled(self):
        """缺必填字段走第三层参数异常，不该冒成 500。"""
        resp = self.pharm.post("/medicines", data={
            "action": "add", "medicine_code": "TSTM%s" % unique(),
            "medicine_name": "缺单位", "unit_price": "1", "stock_quantity": "1",
            "medicine_type": "西药",
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("必填参数", self.flash_text(resp))

    def test_invalid_enum_value_is_handled(self):
        """药品类型不在 ENUM 里 → 1265 Data truncated，由第二层兜成可读提示。"""
        resp = self.pharm.post("/medicines", data={
            "action": "add", "medicine_code": "TSTM%s" % unique(),
            "medicine_name": "非法类型", "unit": "盒", "unit_price": "1",
            "stock_quantity": "1", "medicine_type": "不存在的类型",
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("允许的范围内", self.flash_text(resp))

    def test_update_price_and_stock(self):
        _, mid, _ = self._add()
        resp = self.pharm.post("/medicines", data={
            "action": "update", "medicine_id": mid,
            "unit_price": "9.90", "stock_quantity": "7", "status": "停用",
        }, follow_redirects=True)
        self.assertIn("药品信息已更新", self.flash_text(resp))
        row = self.one("SELECT unit_price, stock_quantity, status FROM medicines "
                       "WHERE medicine_id = %s", (mid,))
        self.assertEqual(float(row["unit_price"]), 9.9)
        self.assertEqual(row["stock_quantity"], 7)
        self.assertEqual(row["status"], "停用")

    def test_search_by_keyword(self):
        _, mid, data = self._add()
        body = self.flash_text(self.pharm.get("/medicines?keyword=%s" % data["medicine_code"]))
        self.assertIn(data["medicine_code"], body)

    def test_cashier_denied(self):
        cash = self.client_as("cash01")
        self.assertIn("当前角色无权访问该功能",
                      self.flash_text(cash.get("/medicines", follow_redirects=True)))


# ----------------------------------------------------------------------
# 检验科全流程：开单 → 收费 → 录结果 → 作废
# ----------------------------------------------------------------------
@requires_db
class LabWorkflowTests(HisTestCase):

    DOCTOR_ACCOUNT = "doctor01"
    LAB_ACCOUNT = "lab01"

    def setUp(self):
        super().setUp()
        self.doctor = self.client_as(self.DOCTOR_ACCOUNT)
        self.lab = self.client_as(self.LAB_ACCOUNT)
        self.cash = self.client_as("cash01")
        self.doctor_id = self.doctor_id_of(self.DOCTOR_ACCOUNT)
        self.assertIsNotNone(self.doctor_id, "基线医生账号 doctor01 必须存在")

    # ---- 夹具 ----
    def _visit(self, doctor_id=None):
        """建一位测试患者 + 一条「待就诊」挂号，返回 (patient_id, registration_id)。"""
        pid = self.mk_patient()
        rid = self.mk_registration(pid, doctor_id or self.doctor_id)
        return pid, rid

    def _items(self, category=None, numeric_range=False):
        sql = "SELECT item_id, item_name, item_category, reference_range " \
              "FROM lab_items WHERE status = '启用'"
        args = ()
        if category:
            sql += " AND item_category = %s"
            args = (category,)
        sql += " ORDER BY item_id"
        rows = self.all(sql, args)
        if numeric_range:
            rows = [r for r in rows
                    if r["reference_range"]
                    and re.match(r"^\s*-?\d+(\.\d+)?-\d+(\.\d+)?\s*$",
                                 str(r["reference_range"]))]
        return rows

    def _order(self, patient_id, registration_id, item_ids, client=None):
        return (client or self.doctor).post("/lab_tests", data={
            "action": "add_test",
            "patient_id": patient_id,
            "registration_id": registration_id,
            "item_ids": [str(i) for i in item_ids],
        }, follow_redirects=True)

    def _test_id(self, patient_id):
        row = self.one("SELECT test_id FROM lab_tests WHERE patient_id = %s "
                       "ORDER BY test_id DESC LIMIT 1", (patient_id,))
        return row["test_id"] if row else None

    def _charge_lab(self, test_id, client=None):
        return (client or self.cash).post("/payments", data={
            "action": "charge_lab", "test_id": test_id, "pay_method": "现金",
        }, follow_redirects=True)

    def _results(self, test_id):
        return self.all(
            """
            SELECT r.result_id, li.item_name, li.reference_range
            FROM lab_test_results r JOIN lab_items li ON r.item_id = li.item_id
            WHERE r.test_id = %s ORDER BY li.item_id
            """, (test_id,))

    def _record(self, test_id, values, client=None):
        """values: {result_id: 结果值}；未列出的条目留空。"""
        data = {"action": "record"}
        for r in self._results(test_id):
            if r["result_id"] in values:
                data["result_%s" % r["result_id"]] = values[r["result_id"]]
        return (client or self.lab).post("/lab_tests/%s/results" % test_id, data=data,
                                         follow_redirects=True)

    def _row(self, test_id):
        return self.one("SELECT * FROM v_lab_test_list WHERE test_id = %s", (test_id,))

    # ---- 开单 ----
    def test_doctor_orders_lab_test(self):
        pid, rid = self._visit()
        items = self._items(category="血常规")
        self.assertTrue(items, "基线数据里应当有血常规项目")
        resp = self._order(pid, rid, [items[0]["item_id"]])
        self.assertIn("检验申请开具成功", self.flash_text(resp))
        tid = self._test_id(pid)
        self.assertIsNotNone(tid)
        row = self._row(tid)
        self.assertEqual(row["test_status"], "待检验")
        self.assertEqual(row["pay_status"], "未收费")
        self.assertEqual(row["sample_type"], "血液")
        self.assertEqual(len(self._results(tid)), 1)

    def test_sample_type_derived_from_category(self):
        """标本类型由勾选项目类别推导：尿常规 → 尿液。"""
        pid, rid = self._visit()
        items = self._items(category="尿常规")
        self.assertTrue(items, "基线数据里应当有尿常规项目")
        resp = self._order(pid, rid, [items[0]["item_id"]])
        self.assertIn("标本类型：尿液", self.flash_text(resp))
        self.assertEqual(self._row(self._test_id(pid))["sample_type"], "尿液")

    def test_order_requires_at_least_one_item(self):
        pid, rid = self._visit()
        resp = self.doctor.post("/lab_tests", data={
            "action": "add_test", "patient_id": pid, "registration_id": rid,
        }, follow_redirects=True)
        self.assertIn("请至少勾选一项检验项目", self.flash_text(resp))

    def test_order_for_cancelled_registration_rejected(self):
        pid, rid = self._visit()
        self.exec_sql("UPDATE registrations SET visit_status = '已退号' "
                      "WHERE registration_id = %s", (rid,))
        items = self._items()
        resp = self._order(pid, rid, [items[0]["item_id"]])
        self.assertIn("不能关联开具检验申请", self.flash_text(resp))
        self.assertIsNone(self._test_id(pid))

    def test_order_for_other_doctors_registration_rejected(self):
        """只能给本次接诊医生名下的挂号开单，防止给别人的患者补单。"""
        other = self.doctor_id_of("doctor02")
        pid, rid = self._visit(doctor_id=other)
        items = self._items()
        resp = self._order(pid, rid, [items[0]["item_id"]])
        self.assertIn("只能为本次接诊医生名下的挂号开具检验申请", self.flash_text(resp))
        self.assertIsNone(self._test_id(pid))

    def test_order_for_mismatched_patient_rejected(self):
        """挂号与所选患者不是同一人时必须挡掉。"""
        pid, rid = self._visit()
        other_pid = self.mk_patient()
        items = self._items()
        resp = self._order(other_pid, rid, [items[0]["item_id"]])
        self.assertIn("不是同一位患者", self.flash_text(resp))
        self.assertIsNone(self._test_id(pid))

    # ---- 录结果 ----
    def test_record_before_payment_rejected(self):
        pid, rid = self._visit()
        items = self._items()
        resp = self._order(pid, rid, [items[0]["item_id"]])
        self.assertIn("检验申请开具成功", self.flash_text(resp))
        tid = self._test_id(pid)
        results = self._results(tid)
        resp2 = self._record(tid, {results[0]["result_id"]: "5.0"})
        self.assertIn("尚未收费", self.flash_text(resp2))
        self.assertEqual(self._row(tid)["test_status"], "待检验")

    def test_recording_all_items_advances_to_done(self):
        pid, rid = self._visit()
        items = self._items(category="血常规")[:2]
        self.assertTrue(items)
        self._order(pid, rid, [i["item_id"] for i in items])
        tid = self._test_id(pid)
        self.assertIn("检验收费成功", self.flash_text(self._charge_lab(tid)))
        self.assertEqual(self._row(tid)["pay_status"], "已收费")

        results = self._results(tid)
        resp = self._record(tid, {r["result_id"]: "4.0" for r in results})
        self.assertIn("检验结果已保存", self.flash_text(resp))
        row = self._row(tid)
        self.assertEqual(row["test_status"], "已完成")
        self.assertIsNotNone(self.one(
            "SELECT completed_at FROM lab_tests WHERE test_id = %s", (tid,))["completed_at"])
        for r in results:
            self.assertEqual(
                self.one("SELECT result_value FROM lab_test_results WHERE result_id = %s",
                         (r["result_id"],))["result_value"], "4.0")

    def test_partial_recording_sets_in_progress(self):
        """只录一部分 → 检验中。"""
        pid, rid = self._visit()
        items = self._items(category="血常规")[:2]
        self.assertTrue(len(items) >= 2, "至少需要两个血常规项目")
        self._order(pid, rid, [i["item_id"] for i in items])
        tid = self._test_id(pid)
        self._charge_lab(tid)
        results = self._results(tid)
        self._record(tid, {results[0]["result_id"]: "4.0"})
        self.assertEqual(self._row(tid)["test_status"], "检验中")

    def test_abnormal_value_sets_flag_automatically(self):
        """结果标志按参考范围自动判定：超出上限 → 偏高。"""
        pid, rid = self._visit()
        numeric = self._items(numeric_range=True)
        # 优先用血常规，没有就退而用任意数值型项目（标本类型与标志判定无关）
        candidates = [i for i in numeric if i["item_category"] == "血常规"] or numeric
        self.assertTrue(candidates, "基线数据里应当存在带数值参考范围的检验项目")
        item = candidates[0]
        m = re.match(r"^\s*(-?\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\s*$",
                     str(item["reference_range"]))
        self.assertIsNotNone(m, "参考范围应当能解析成上下限：%s" % item["reference_range"])
        hi = float(m.group(2))
        self._order(pid, rid, [item["item_id"]])
        tid = self._test_id(pid)
        self._charge_lab(tid)
        results = self._results(tid)
        self._record(tid, {results[0]["result_id"]: str(hi * 10)})
        flag = self.one("SELECT result_flag FROM lab_test_results WHERE result_id = %s",
                        (results[0]["result_id"],))["result_flag"]
        self.assertEqual(flag, "偏高")

    def test_doctor_cannot_record_result(self):
        """只有检验技师或管理员可以录结果。"""
        pid, rid = self._visit()
        items = self._items()
        self._order(pid, rid, [items[0]["item_id"]])
        tid = self._test_id(pid)
        self._charge_lab(tid)
        results = self._results(tid)
        resp = self._record(tid, {results[0]["result_id"]: "4.0"}, client=self.doctor)
        self.assertIn("仅检验技师或管理员可以录入检验结果", self.flash_text(resp))
        self.assertIsNone(
            self.one("SELECT result_value FROM lab_test_results WHERE result_id = %s",
                     (results[0]["result_id"],))["result_value"])

    # ---- 作废 ----
    def test_cancel_pending_test(self):
        pid, rid = self._visit()
        items = self._items()
        self._order(pid, rid, [items[0]["item_id"]])
        tid = self._test_id(pid)
        resp = self.doctor.post("/lab_tests",
                                data={"action": "cancel_test", "test_id": tid},
                                follow_redirects=True)
        self.assertIn("检验单已作废", self.flash_text(resp))
        self.assertEqual(self._row(tid)["test_status"], "已作废")

    def test_cancel_twice_rejected(self):
        pid, rid = self._visit()
        items = self._items()
        self._order(pid, rid, [items[0]["item_id"]])
        tid = self._test_id(pid)
        self.doctor.post("/lab_tests", data={"action": "cancel_test", "test_id": tid},
                         follow_redirects=True)
        resp = self.doctor.post("/lab_tests", data={"action": "cancel_test", "test_id": tid},
                               follow_redirects=True)
        self.assertIn("已作废", self.flash_text(resp))

    def test_cannot_cancel_finished_test(self):
        pid, rid = self._visit()
        items = self._items()
        self._order(pid, rid, [items[0]["item_id"]])
        tid = self._test_id(pid)
        self._charge_lab(tid)
        results = self._results(tid)
        self._record(tid, {results[0]["result_id"]: "4.0"})
        resp = self.doctor.post("/lab_tests", data={"action": "cancel_test", "test_id": tid},
                               follow_redirects=True)
        self.assertIn("已完成检验不能作废", self.flash_text(resp))
        self.assertEqual(self._row(tid)["test_status"], "已完成")

    def test_charge_lab_twice_rejected(self):
        pid, rid = self._visit()
        items = self._items()
        self._order(pid, rid, [items[0]["item_id"]])
        tid = self._test_id(pid)
        self.assertIn("检验收费成功", self.flash_text(self._charge_lab(tid)))
        resp = self._charge_lab(tid)
        body = self.flash_text(resp)
        self.assertNotIn("检验收费成功", body)
        self.assertIn("已收费", body)

    def test_cashier_cannot_order_lab_test(self):
        """收费窗口只能收费，不能开检验单。"""
        pid, rid = self._visit()
        items = self._items()
        resp = self._order(pid, rid, [items[0]["item_id"]], client=self.cash)
        self.assertIn("当前角色无权访问该功能", self.flash_text(resp))
        self.assertIsNone(self._test_id(pid))


# ----------------------------------------------------------------------
# 诊断维护（医生直接维护患者「最近诊断」）
# ----------------------------------------------------------------------
@requires_db
class DiagnosisTests(HisTestCase):

    def test_doctor_can_save_diagnosis(self):
        pid = self.mk_patient()
        doc = self.client_as("doctor01")
        resp = doc.post("/diagnosis", data={
            "action": "save", "patient_id": pid, "diagnosis": "急性上呼吸道感染",
        }, follow_redirects=True)
        self.assertIn("诊断结果已保存", self.flash_text(resp))
        self.assertEqual(self.one("SELECT diagnosis FROM patients WHERE patient_id = %s",
                                  (pid,))["diagnosis"], "急性上呼吸道感染")

    def test_admin_can_save_diagnosis(self):
        pid = self.mk_patient()
        admin = self.client_as("admin01")
        resp = admin.post("/diagnosis", data={
            "action": "save", "patient_id": pid, "diagnosis": "高血压2级",
        }, follow_redirects=True)
        self.assertIn("诊断结果已保存", self.flash_text(resp))

    def test_missing_patient_id_reports_param_error(self):
        """patient_id 缺失/非数字 → int() 抛错，必须归第三层而不是 500。"""
        doc = self.client_as("doctor01")
        resp = doc.post("/diagnosis", data={"action": "save", "diagnosis": "x"},
                        follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("必填参数", self.flash_text(resp))

        resp2 = doc.post("/diagnosis", data={
            "action": "save", "patient_id": "abc", "diagnosis": "x"},
            follow_redirects=True)
        self.assertEqual(resp2.status_code, 200)
        self.assertIn("提交参数格式错误", self.flash_text(resp2))

    def test_cashier_denied(self):
        pid = self.mk_patient()
        cash = self.client_as("cash01")
        resp = cash.post("/diagnosis", data={
            "action": "save", "patient_id": pid, "diagnosis": "越权写入"},
            follow_redirects=True)
        self.assertIn("当前角色无权访问该功能", self.flash_text(resp))
        self.assertIsNone(self.one("SELECT diagnosis FROM patients WHERE patient_id = %s",
                                   (pid,))["diagnosis"])


# ----------------------------------------------------------------------
# 报表与 CSV 导出
# ----------------------------------------------------------------------
@requires_db
class ReportAndExportTests(HisTestCase):

    def test_reports_page_renders_for_admin(self):
        admin = self.client_as("admin01")
        resp = admin.get("/reports")
        self.assertEqual(resp.status_code, 200)
        body = self.flash_text(resp)
        self.assertIn("报表查询", body)
        self.assertIn("今日挂号统计", body)

    def test_codes_page_renders_for_admin(self):
        admin = self.client_as("admin01")
        resp = admin.get("/codes")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("编号参考", self.flash_text(resp))

    def test_cashier_cannot_open_reports(self):
        cash = self.client_as("cash01")
        self.assertIn("当前角色无权访问该功能",
                      self.flash_text(cash.get("/reports", follow_redirects=True)))

    def test_export_patients_csv_escapes_commas_and_quotes(self):
        """字段里的逗号 / 双引号必须被 csv 模块转义，否则 Excel 打开会错列。

        这是回归守卫：早期实现用 ",".join() 手工拼接，患者地址或诊断里
        只要有逗号就会整行错位。
        """
        weird = '测试,患者"引号"'
        pid = self.mk_patient(name=weird)
        self.assertIsNotNone(pid)
        admin = self.client_as("admin01")
        resp = admin.get("/export/patients")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.headers.get("Content-Type", ""))
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))

        body = resp.get_data(as_text=True)
        self.assertTrue(body.startswith("\ufeff"), "CSV 必须带 UTF-8 BOM，否则 Excel 中文乱码")
        self.assertIn("patient_no", body.splitlines()[0])
        # 该字段含逗号与双引号 → 整体加引号，内部双引号翻倍
        self.assertIn('"测试,患者""引号"""', body)
        # 行数 = 表头 + 全部患者；这里只校验目标患者确实落在某一行里
        matched = [ln for ln in body.splitlines() if weird.replace('"', '""') in ln]
        self.assertTrue(matched, "转义后的患者姓名应当出现在 CSV 中")

    def test_export_unknown_type_redirects(self):
        admin = self.client_as("admin01")
        resp = admin.get("/export/不存在的类型", follow_redirects=True)
        self.assertIn("导出类型不存在", self.flash_text(resp))

    def test_export_requires_permission(self):
        doc = self.client_as("doctor01")
        resp = doc.get("/export/patients", follow_redirects=True)
        self.assertIn("当前角色无权访问该功能", self.flash_text(resp))

    def test_export_every_declared_type_succeeds(self):
        admin = self.client_as("admin01")
        for name in ("patients", "registrations", "prescriptions", "payments", "medicines"):
            resp = admin.get("/export/%s" % name)
            self.assertEqual(resp.status_code, 200, "%s 导出应当成功" % name)
            self.assertEqual(resp.mimetype, "text/csv")


if __name__ == "__main__":
    unittest.main()
