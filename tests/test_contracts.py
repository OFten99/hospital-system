"""离线静态契约用例（不依赖数据库）。

这些用例把「架构约定」固化成断言，改代码时若破坏约定会立刻失败：

  1. 路由规模口径（34 装饰器 / 34 端点 / 19 含 POST / 18 写路由）；
  2. 页面模板齐全（22 个）；
  3. 写路由统一的三层异常捕获结构（且顺序不可颠倒）；
  4. Jinja 模板陷阱回归（SQL 别名不得叫 items 等 dict 方法名）；
  5. 密码哈希兼容策略（加盐 + 历史无盐 SHA-256，长度适配列宽）；
  6. 结果标志自动判定规则；
  7. 全局 500 兜底不外泄内部信息；
  8. 已下线功能（挂号员 / 挂号机 / 预约挂号）不得残留。
"""

import re
import unittest
from pathlib import Path

from tests.base import REPO_ROOT, WEB_APP_DIR, HisTestCase  # noqa: F401


APP_PY = WEB_APP_DIR / "app.py"
DB_PY = WEB_APP_DIR / "db.py"
TEMPLATE_DIR = WEB_APP_DIR / "templates"

# 与 README「项目规模」表一致的对外口径
# 2026-10-23 更新：补入小程序线上挂号 API（6 个端点，其中 2 个 POST）后的实际规模
EXPECTED_DECORATORS = 35
EXPECTED_RULES = 36            # 35 条 @app.route + Flask 自动注册的 /static/<path>
EXPECTED_ENDPOINTS = 35        # 29 个后台端点 + 6 个小程序 API
EXPECTED_POST_ENDPOINTS = 20   # 含只读表单的 /login 与纯计算的 /api/triage
EXPECTED_WRITE_ROUTES = 18     # 排除 /login 与 /api/triage（两者都不写库）
EXPECTED_TEMPLATES = 22


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def _route_blocks(source):
    """把 app.py 按 @app.route 分组切分成函数块。

    返回 [(decorators, view_name, block), ...]。

    注意：一个视图可能挂着多条 `@app.route`（如 `/consultation` 与
    `/consultation/<int:registration_id>` 共用一个视图），因此连续的
    `@app.route` 行必须归为同一组，直到遇见 `def` 为止。
    """
    lines = source.split("\n")
    groups = []          # [([decorator_lines], start_line)]
    current = None
    for i, line in enumerate(lines):
        if line.startswith("@app.route("):
            if current is None:
                current = {"decorators": [], "start": i}
            current["decorators"].append(line)
        elif line.startswith("def ") and current is not None:
            current["name"] = line[4:].split("(")[0].strip()
            groups.append(current)
            current = None

    blocks = []
    for idx, group in enumerate(groups):
        end = groups[idx + 1]["start"] if idx + 1 < len(groups) else len(lines)
        block = "\n".join(lines[group["start"]:end])
        blocks.append((group["decorators"], group.get("name", "?"), block))
    return blocks


class RouteInventoryTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.source = _read(APP_PY)
        cls.decorators = re.findall(r"@app\.route\(", cls.source)

    def test_decorator_count(self):
        self.assertEqual(len(self.decorators), EXPECTED_DECORATORS)

    def test_runtime_rule_and_endpoint_counts(self):
        import app as app_module
        rules = list(app_module.app.url_map.iter_rules())
        endpoints = set(app_module.app.view_functions)
        post_endpoints = {
            r.endpoint for r in rules
            if "POST" in (set(r.methods) - {"HEAD", "OPTIONS"})
        }
        self.assertEqual(len(rules), EXPECTED_RULES, "url_map 规则数")
        self.assertEqual(len(endpoints), EXPECTED_ENDPOINTS, "端点数")
        self.assertEqual(len(post_endpoints), EXPECTED_POST_ENDPOINTS, "含 POST 的端点数")
        # 16 个写路由 / 17 个含 POST 端点（差值即只读表单 /login）
        self.assertIn("login", post_endpoints)
        # 20 个 POST 端点 - /login（只读表单）- /api/triage（纯向量计算，不写库）
        self.assertEqual(len(post_endpoints) - 2, EXPECTED_WRITE_ROUTES,
                         "写路由数 = POST 端点数 - 2（/login 与 /api/triage）")

    def test_consultation_two_rules_one_view(self):
        """/consultation 与 /consultation/<id> 共用同一个视图函数。"""
        import app as app_module
        self.assertIs(app_module.app.view_functions["consultation"], app_module.consultation)

    def test_key_endpoints_exist(self):
        import app as app_module
        expected = {
            "index", "healthz", "login", "logout", "dashboard", "patients",
            "registrations", "schedules", "prescriptions", "consultation",
            "payments", "medicines", "reports", "codes", "export_csv", "vitals",
            "medical_records", "medical_record_print", "lab_tests", "lab_results",
            "diagnosis", "pharmacy", "users", "announcements", "departments",
        }
        self.assertTrue(expected.issubset(set(app_module.app.view_functions)))


class TemplateTests(unittest.TestCase):

    def test_template_count(self):
        files = sorted(TEMPLATE_DIR.glob("*.html"))
        self.assertEqual(len(files), EXPECTED_TEMPLATES)

    def test_base_and_login_present(self):
        self.assertTrue((TEMPLATE_DIR / "base.html").exists())
        self.assertTrue((TEMPLATE_DIR / "login.html").exists())

    def test_all_referenced_templates_exist(self):
        """app.py 里 render_template 引用的模板都必须真实存在。"""
        source = _read(APP_PY)
        names = set(re.findall(r"render_template\(\s*[\"']([^\"']+)[\"']", source))
        self.assertTrue(names)
        for name in sorted(names):
            with self.subTest(template=name):
                self.assertTrue((TEMPLATE_DIR / name).exists(),
                                "缺少模板文件：%s" % name)


class ExceptionLayeringTests(unittest.TestCase):
    """所有写路由必须套用「三层捕获」且顺序不可颠倒。"""

    READ_ONLY_POST = {"login", "api_triage"}   # 只读表单 / 纯计算接口，不写库

    @classmethod
    def setUpClass(cls):
        cls.blocks = _route_blocks(_read(APP_PY))

    def _post_blocks(self):
        for decorators, name, block in self.blocks:
            if any("POST" in d for d in decorators):
                yield name, block

    def test_every_write_route_has_db_layers(self):
        """18 个写路由必须全部有前两层 DB 异常捕获，且顺序不可颠倒。"""
        checked = 0
        for name, block in self._post_blocks():
            if name in self.READ_ONLY_POST:
                continue
            checked += 1
            with self.subTest(view=name):
                self.assertIn("except mysql.connector.errors.IntegrityError", block,
                              "%s 缺少第一层 IntegrityError 捕获" % name)
                self.assertIn("except mysql.connector.Error", block,
                              "%s 缺少第二层 mysql.connector.Error 捕获" % name)
            # 第一层必须写在第二层之前（IntegrityError 是 Error 的子类）
            first = block.index("except mysql.connector.errors.IntegrityError")
            second = block.index("except mysql.connector.Error")
            self.assertLess(first, second, "%s 的异常捕获顺序颠倒" % name)
        self.assertEqual(checked, EXPECTED_WRITE_ROUTES,
                         "被检查的写路由数量与对外口径不符")

    def test_third_layer_coverage(self):
        """第三层（参数缺失）覆盖 17/18：只有确实不需要它的路由可以省略。

        约定：**只要路由里直接索引了表单字段（``request.form["x"]``）**，
        就必须有第三层，否则漏填字段会冒到全局 500。
        ``lab_results`` 全程使用 ``request.form.get(...)``，不会抛 KeyError，
        是唯一一个可以只有两层的写路由。
        """
        with_third, without_third = [], []
        for name, block in self._post_blocks():
            if name in self.READ_ONLY_POST:
                continue
            has_third = "except (KeyError" in block
            indexes_form = bool(re.search(r"request\.form\[", block))
            if has_third:
                with_third.append(name)
            else:
                without_third.append(name)
                self.assertFalse(
                    indexes_form,
                    "%s 直接索引了表单字段却没有第三层捕获" % name)
        self.assertEqual(len(with_third), 17, "应有 17 个写路由具备第三层")
        self.assertEqual(without_third, ["lab_results"],
                         "缺少第三层的路由应仅限 lab_results")

    def test_global_handler_is_neutral(self):
        """全局 500 兜底不得回显异常类型或原文（注释里的说明性文字不算）。"""
        source = _read(APP_PY)
        self.assertIn("@app.errorhandler(Exception)", source)
        seg = source[source.index("@app.errorhandler(Exception)"):]
        # 截到下一个顶级分节注释，只保留本处理函数自身的实现
        seg = seg[:seg.index("\n# ---")]
        code = "\n".join(
            line for line in seg.split("\n") if not line.strip().startswith("#"))
        self.assertNotIn("str(error)", code)
        self.assertNotIn("type(error).__name__", code)
        self.assertIn("exc_info=True", code)   # 堆栈仍完整写入服务端日志
        self.assertIn("500", code)

    def test_global_handler_returns_neutral_500(self):
        import app as app_module
        resp = app_module.handle_unexpected_error(RuntimeError("SECRET-TABLE-NAME"))
        self.assertEqual(resp.status_code, 500)
        body = resp.get_data(as_text=True)
        self.assertNotIn("SECRET-TABLE-NAME", body)
        self.assertIn("服务端内部错误", body)


class JinjaTrapRegressionTests(unittest.TestCase):
    """Jinja 取属性时字典的 .items/.keys/.values 方法会遮蔽同名列。"""

    DICT_METHODS = ("items", "keys", "values", "get")

    def test_no_sql_alias_named_like_dict_method(self):
        source = _read(APP_PY)
        for method in self.DICT_METHODS:
            bad = re.findall(r"\bAS\s+%s\b" % method, source, flags=re.IGNORECASE)
            self.assertEqual(bad, [], "SQL 别名不可使用 %s（会被 Jinja 解析成 dict 方法）" % method)

    def test_medical_queries_use_item_names_alias(self):
        source = _read(APP_PY)
        self.assertGreaterEqual(source.count("AS item_names"), 2)

    def test_templates_do_not_call_items_on_rows(self):
        """模板里不得对查询结果行调用 .items（渲染出 <built-in method items ...>）。"""
        offenders = []
        for f in TEMPLATE_DIR.glob("*.html"):
            text = f.read_text(encoding="utf-8")
            for m in re.finditer(r"\{\{[^}]*?\.items\b[^}]*?\}\}", text):
                offenders.append((f.name, m.group(0).strip()))
        self.assertEqual(offenders, [])

    def test_render_has_no_builtin_method_leak(self):
        """用真实数据渲染病历打印页，断言不出现 <built-in method。"""
        import app as app_module
        from tests.base import DB_AVAILABLE
        if not DB_AVAILABLE:
            self.skipTest("本地 MySQL 不可用")
        # CSRF 校验在 TESTING 下豁免：test_client 是「直接 POST」，
        # 没有浏览器里那段自动补 token 的脚本，不豁免会一律 400。
        app_module.app.config["TESTING"] = True
        with app_module.app.test_client() as client:
            client.post("/login", data={"username": "admin01", "password": "123456"},
                        follow_redirects=True)
            row = None
            conn = app_module.get_connection()
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT record_id FROM medical_records ORDER BY record_id DESC LIMIT 1")
            row = cur.fetchone()
            cur.close()
            if not row:
                self.skipTest("库中没有病历数据可供渲染")
            resp = client.get("/medical_records/%d/print" % row["record_id"])
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn("<built-in method", resp.get_data(as_text=True))


class PasswordHashingTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.m = app_module

    def test_hash_is_salted_and_fits_column(self):
        h = self.m.hash_password("123456")
        self.assertTrue(h.startswith(("pbkdf2:", "scrypt:", "argon2")))
        # users.password_hash 列宽 VARCHAR(128)，超长会被截断或报 1406
        self.assertLessEqual(len(h), 128)

    def test_two_hashes_differ(self):
        """加盐：同一明文两次哈希结果不同。"""
        self.assertNotEqual(self.m.hash_password("123456"), self.m.hash_password("123456"))

    def test_verify_salted_ok(self):
        h = self.m.hash_password("p@ss")
        self.assertTrue(self.m.verify_password(h, "p@ss"))
        self.assertFalse(self.m.verify_password(h, "bad"))

    def test_verify_legacy_sha256(self):
        """历史无盐 SHA-256 密码仍可登录（兼容分支）。"""
        legacy = self.m._legacy_sha256("old-pass")
        self.assertTrue(self.m.verify_password(legacy, "old-pass"))
        self.assertFalse(self.m.verify_password(legacy, "wrong"))

    def test_needs_rehash(self):
        self.assertTrue(self.m.password_needs_rehash(self.m._legacy_sha256("x")))
        self.assertFalse(self.m.password_needs_rehash(self.m.hash_password("x")))

    def test_empty_hash_rejected(self):
        self.assertFalse(self.m.verify_password("", "x"))
        self.assertFalse(self.m.verify_password(None, "x"))


class AutoFlagTests(unittest.TestCase):
    """检验结果标志按参考范围自动判定。"""

    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.f = staticmethod(app_module.auto_flag)

    def test_numeric_within_range_is_normal(self):
        self.assertEqual(self.f("3.5-9.5", "6.0"), "正常")
        self.assertEqual(self.f("3.5-9.5", "3.5"), "正常")
        self.assertEqual(self.f("3.5-9.5", "9.5"), "正常")

    def test_numeric_below_low(self):
        self.assertEqual(self.f("3.5-9.5", "2.0"), "偏低")

    def test_numeric_above_high(self):
        self.assertEqual(self.f("3.5-9.5", "12.0"), "偏高")

    def test_qualitative(self):
        self.assertEqual(self.f("阴性", "阴性"), "正常")
        self.assertEqual(self.f("阴性", "阳性"), "异常")

    def test_undecidable_returns_none(self):
        self.assertIsNone(self.f(None, "1"))
        self.assertIsNone(self.f("3.5-9.5", ""))
        self.assertIsNone(self.f("3.5-9.5", "abc"))
        self.assertIsNone(self.f("阴性", None))


class SampleTypeTests(unittest.TestCase):
    """标本类型按检验项目类别推导（需要数据库读取项目字典）。

    这里**不写死项目 ID**：检验项目字典是随 07/08/09 号脚本一起灌进库的，
    ID 会随种子数据增删而整体漂移（尿常规早期是 5、6 号，后来前面插入了血常规
    项目就挪到了 11~14 号）。写死 ID 的断言会随着灌库脚本演进而失效，但
    「尿常规→尿液、粪便常规→粪便、混选取多数」这条规则本身没有变。
    所以按类别去库里取真实 ID，断言仍然针对规则。
    """

    @staticmethod
    def _ids(category, limit=1):
        """按类别取检验项目 ID（字符串形式，模拟表单提交）。"""
        from tests.base import connect, DB_AVAILABLE
        if not DB_AVAILABLE:
            raise unittest.SkipTest("本地 MySQL 不可用")
        conn = connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT item_id FROM lab_items WHERE item_category = %s "
                "ORDER BY item_id LIMIT " + str(int(limit)),
                (category,),
            )
            rows = [str(row[0]) for row in cursor.fetchall()]
            cursor.close()
        finally:
            conn.close()
        if len(rows) < limit:
            raise unittest.SkipTest("库里缺少「%s」类检验项目" % category)
        return rows

    def test_blood_default(self):
        from tests.base import DB_AVAILABLE
        if not DB_AVAILABLE:
            self.skipTest("本地 MySQL 不可用")
        import app as app_module
        self.assertEqual(app_module.derive_sample_type([]), "血液")      # 空 → 默认
        blood = self._ids("血常规", 2)
        self.assertEqual(app_module.derive_sample_type(blood), "血液")   # 血常规项

    def test_urine_and_stool(self):
        from tests.base import DB_AVAILABLE
        if not DB_AVAILABLE:
            self.skipTest("本地 MySQL 不可用")
        import app as app_module
        self.assertEqual(app_module.derive_sample_type(self._ids("尿常规")), "尿液")
        self.assertEqual(app_module.derive_sample_type(self._ids("粪便常规")), "粪便")

    def test_mixed_takes_majority(self):
        from tests.base import DB_AVAILABLE
        if not DB_AVAILABLE:
            self.skipTest("本地 MySQL 不可用")
        import app as app_module
        # 1 项血常规 + 2 项尿常规 → 取项目数最多的「尿常规」
        mixed = self._ids("血常规", 1) + self._ids("尿常规", 2)
        self.assertEqual(app_module.derive_sample_type(mixed), "尿液")


class BusinessNumberTests(unittest.TestCase):
    """业务单号生成必须保证唯一（回归：秒级时间戳会撞唯一索引）。"""

    def test_make_no_unique_within_same_second(self):
        import app as app_module
        nos = [app_module.make_no("REG") for _ in range(500)]
        self.assertEqual(len(set(nos)), len(nos),
                         "同一秒内生成的单号出现重复")

    def test_make_no_prefix_and_length(self):
        import app as app_module
        for prefix, limit in (("REG", 30), ("PRES", 40), ("LAB", 30), ("PAY", 40)):
            with self.subTest(prefix=prefix):
                no = app_module.make_no(prefix)
                self.assertTrue(no.startswith(prefix))
                self.assertLessEqual(len(no), limit)


class RetiredFeatureTests(unittest.TestCase):
    """已下线功能（挂号员 / 挂号机 / 预约挂号）不得在代码中残留入口。"""

    def test_no_retired_roles_in_code(self):
        for path in (APP_PY, DB_PY):
            text = _read(path)
            for token in ("REGISTRAR", "REGMACHINE"):
                self.assertNotIn(token, text, "%s 残留 %s" % (path.name, token))

    def test_no_kiosk_or_appointment_routes(self):
        source = _read(APP_PY)
        self.assertNotIn("/kiosk", source)
        self.assertNotIn("/appointments", source)

    def test_retired_templates_removed(self):
        for name in ("kiosk.html", "appointments.html", "appointment_checkin.html"):
            self.assertFalse((TEMPLATE_DIR / name).exists(), "%s 应已删除" % name)


class DbModuleContractTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.source = _read(DB_PY)

    def test_execute_atomic_exists(self):
        self.assertIn("def execute_atomic(", self.source)

    def test_execute_commits_and_rolls_back(self):
        body = self.source[self.source.index("def execute("):]
        body = body[:body.index("def call_proc(")]
        self.assertIn("conn.commit()", body)
        self.assertIn("conn.rollback()", body)

    def test_call_proc_scalar_documents_out_index(self):
        self.assertIn("def call_proc_scalar(", self.source)
        self.assertIn("out_index", self.source)

    def test_delete_user_uses_atomic(self):
        source = _read(APP_PY)
        seg = source[source.index('if action == "delete_user":'):]
        seg = seg[:seg.index("except mysql.connector.errors.IntegrityError")]
        self.assertIn("execute_atomic(", seg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
