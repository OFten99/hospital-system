"""登录鉴权与角色权限边界。

覆盖：
  * 五类角色账号均可登录，且登录后会话中不含密码哈希；
  * 错误密码 / 不存在账号返回统一提示（防用户名枚举）；
  * 锁定账号被拒绝；
  * 未登录访问受保护路由一律重定向到登录页；
  * PERMISSIONS 权限矩阵在真实 HTTP 链路上生效（有权限 200，无权限 302 回首页）；
  * 退出登录清空会话；
  * /healthz 自检页可用。
"""

import unittest

from tests.base import HisTestCase, requires_db


# 权限键 → 代表性 URL（GET 即可触发 permission_required）
MENU_URLS = {
    "dashboard": "/dashboard",
    "patients": "/patients",
    "registrations": "/registrations",
    "schedules": "/schedules",
    "prescriptions": "/prescriptions",
    "payments": "/payments",
    "medicines": "/medicines",
    "reports": "/reports",
    "export": "/export/patients",
    "codes": "/codes",
    "vitals": "/vitals",
    "lab": "/lab_tests",
    "diagnosis": "/diagnosis",
    "medical_records": "/medical_records",
    "dispense": "/pharmacy",
    "users": "/users",
    "departments": "/departments",
    "consultation": "/consultation",
}

# 预期权限矩阵（与 app.PERMISSIONS 同源；ADMIN 为全量）
EXPECTED = {
    "CASHIER": {"dashboard", "patients", "registrations", "schedules", "payments"},
    "DOCTOR": {"consultation", "dashboard", "schedules", "prescriptions",
               "medical_records", "lab", "vitals", "diagnosis"},
    "LAB_TECH": {"dashboard", "lab"},
    "PHARMACIST": {"dashboard", "dispense", "medicines"},
}

ROLE_ACCOUNTS = {
    "ADMIN": "admin01",
    "CASHIER": "cash01",
    "DOCTOR": "doctor01",
    "LAB_TECH": "lab01",
    "PHARMACIST": "pharm01",
}


@requires_db
class AuthAndPermissionTests(HisTestCase):

    # ---------------- 登录 ----------------
    def test_all_five_roles_can_login(self):
        for role, account in ROLE_ACCOUNTS.items():
            with self.subTest(role=role, account=account):
                client = self.app.test_client()
                resp = self.login(account, client=client)
                self.assertEqual(resp.status_code, 200)
                body = self.flash_text(resp)
                self.assertIn("登录成功", body)
                # 会话里保存的是用户名与角色，且不含密码哈希
                with client.session_transaction() as sess:
                    self.assertIn("user", sess)
                    self.assertNotIn("password_hash", sess["user"])
                    self.assertEqual(sess["user"]["role_code"], role)

    def test_wrong_password_rejected(self):
        client = self.app.test_client()
        resp = self.login("admin01", password="wrong-password", client=client)
        self.assertIn("用户名或密码错误", self.flash_text(resp))
        with client.session_transaction() as sess:
            self.assertNotIn("user", sess)

    def test_unknown_user_gives_same_message(self):
        """账号不存在与密码错误提示一致，避免被用来枚举用户名。"""
        client = self.app.test_client()
        resp = self.login("no_such_user_zzz", password="whatever", client=client)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("用户名或密码错误", self.flash_text(resp))

    def test_locked_account_rejected(self):
        uid = self.mk_user(role_code="CASHIER", status="锁定")
        row = self.one("SELECT username FROM users WHERE user_id = %s", (uid,))
        client = self.app.test_client()
        resp = self.login(row["username"], client=client)
        self.assertIn("账号已被锁定", self.flash_text(resp))
        with client.session_transaction() as sess:
            self.assertNotIn("user", sess)

    # ---------------- 会话与跳转 ----------------
    def test_index_redirects_anonymous_to_login(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_index_redirects_logged_in_to_dashboard(self):
        client = self.client_as("admin01")
        resp = client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard", resp.headers["Location"])

    def test_protected_routes_require_login(self):
        for url in ("/dashboard", "/patients", "/medical_records", "/users",
                    "/pharmacy", "/lab_tests", "/payments"):
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 302)
                self.assertIn("/login", resp.headers["Location"])

    def test_logout_clears_session(self):
        client = self.client_as("admin01")
        resp = client.get("/logout", follow_redirects=True)
        self.assertIn("已退出登录", self.flash_text(resp))
        with client.session_transaction() as sess:
            self.assertNotIn("user", sess)
        # 退出后再访问受保护页面应被弹回登录页
        self.assertEqual(client.get("/dashboard").status_code, 302)

    # ---------------- 权限矩阵 ----------------
    def test_permission_matrix_via_http(self):
        for role, account in ROLE_ACCOUNTS.items():
            allowed = EXPECTED.get(role)
            if role == "ADMIN":
                allowed = set(MENU_URLS)          # 管理员全量
            client = self.client_as(account)
            for key, url in MENU_URLS.items():
                with self.subTest(role=role, menu=key):
                    resp = client.get(url)
                    if allowed is None or key in allowed:
                        self.assertEqual(
                            resp.status_code, 200,
                            "%s 应有权访问 %s" % (role, url))
                    else:
                        self.assertEqual(
                            resp.status_code, 302,
                            "%s 不应有权访问 %s" % (role, url))
                        self.assertIn("/dashboard", resp.headers["Location"])

    def test_denied_access_gives_readable_flash(self):
        client = self.client_as("lab01")           # 检验科：无患者档案权限
        resp = client.get("/patients", follow_redirects=True)
        self.assertIn("无权访问", self.flash_text(resp))

    def test_announcements_visible_to_all_roles(self):
        """公告页只需登录即可查看（发布/删除仍限管理员）。"""
        for account in ROLE_ACCOUNTS.values():
            with self.subTest(account=account):
                client = self.client_as(account)
                self.assertEqual(client.get("/announcements").status_code, 200)

    # ---------------- 医生菜单写死分支 ----------------
    def test_permissions_constant_shape(self):
        """PERMISSIONS 常量必须恰好是五类角色，且与预期矩阵一致。"""
        import app as app_module
        self.assertEqual(set(app_module.PERMISSIONS),
                         {"ADMIN", "DOCTOR", "CASHIER", "LAB_TECH", "PHARMACIST"})
        for role, keys in EXPECTED.items():
            with self.subTest(role=role):
                self.assertEqual(app_module.PERMISSIONS[role], keys)
        # 管理员的授权集合应覆盖其余四类角色的全部权限键
        others = set().union(*EXPECTED.values())
        self.assertTrue(others.issubset(app_module.PERMISSIONS["ADMIN"]),
                        "ADMIN 权限未覆盖：%s" % (others - app_module.PERMISSIONS["ADMIN"]))
        # 注意：ADMIN 访问任何菜单都走 role_allowed() 的短路径（直接 return True），
        # 因此像 departments（科室管理）这类未列进 ADMIN 集合的键，仍只有管理员可用。
        self.assertNotIn("departments", others)

    def test_doctor_menu_matches_permissions(self):
        """base.html 里 DOCTOR 分支写死的菜单项必须与 PERMISSIONS['DOCTOR'] 一一对应。

        base.html 的医生侧边栏是硬编码的（不走 role_allowed），一旦与
        PERMISSIONS 脱节就会出现「有权限没入口」或「有入口无权访问」。
        这里按「权限键 → 端点名」映射做双向校验（dashboard 由登录后跳转进入，
        不单独设菜单项，故排除）。
        """
        import app as app_module

        key_to_endpoint = {
            "consultation": "consultation",
            "medical_records": "medical_records",
            "prescriptions": "prescriptions",
            "lab": "lab_tests",
            "vitals": "vitals",
            "diagnosis": "diagnosis",
            "schedules": "schedules",
        }
        expected_endpoints = {key_to_endpoint[k]
                              for k in app_module.PERMISSIONS["DOCTOR"] if k in key_to_endpoint}

        menu = (self.app_module.APP_DIR / "templates" / "base.html").read_text(encoding="utf-8")
        start = menu.index("current_user.role_code == 'DOCTOR'")
        end = menu.index("{% else %}", start)
        doctor_branch = menu[start:end]

        for endpoint in expected_endpoints:
            with self.subTest(endpoint=endpoint):
                self.assertIn(
                    "url_for('%s')" % endpoint, doctor_branch,
                    "base.html 医生菜单缺少端点 %s（PERMISSIONS 已授权）" % endpoint)

        # 反向：医生菜单里的入口不能超出 PERMISSIONS 授权范围（除登录后跳转的 dashboard）
        import re
        hrefs = set(re.findall(r"url_for\('([a-z_]+)'\)", doctor_branch))
        self.assertTrue(hrefs.issubset(expected_endpoints),
                        "医生菜单出现未授权入口：%s" % (hrefs - expected_endpoints))


@requires_db
class HealthzTests(HisTestCase):

    def test_healthz_ok(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("部署自检", body)
        self.assertIn("数据库连通性", body)
        self.assertIn("连接成功", body)

    def test_healthz_masks_password(self):
        resp = self.client.get("/healthz")
        body = resp.get_data(as_text=True)
        self.assertNotIn("123456", body,
                         "/healthz 不应回显明文数据库口令")


class NoDbSanityTests(unittest.TestCase):
    """不依赖数据库的最小冒烟：确保应用对象可被导入。"""

    def test_app_importable(self):
        from tests.base import HisTestCase  # noqa: F401
        import app as app_module
        self.assertTrue(hasattr(app_module, "app"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
