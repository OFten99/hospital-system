"""离线静态契约用例（不依赖数据库）。

这些用例把「架构约定」固化成断言，改代码时若破坏约定会立刻失败：

  1. 路由规模口径（26 装饰器 / 25 业务端点 / 17 含 POST / 16 写路由）；
  2. 页面模板齐全（22 个）；
  3. 写路由统一的三层异常捕获结构（且顺序不可颠倒）；
  4. Jinja 模板陷阱回归（SQL 别名不得叫 items 等 dict 方法名）；
  5. 密码哈希兼容策略（加盐 + 历史无盐 SHA-256，长度适配列宽）；
  6. 结果标志自动判定规则；
  7. 全局 500 兜底不外泄内部信息；
  8. 已下线功能（挂号员 / 挂号机 / 预约挂号）不得残留；
  9. 演示模式（/demo）的路由存在且默认被门控。

⚠️ 关于第 9 条：演示模式（HIS_DEMO_MODE）是答辩辅助功能，**不属于对外口径里的
业务规模**。下面所有规模统计（装饰器数 / 端点数 / 模板数 / 写路由数）都会把
`/demo*` 路由与 `demo.html` 排除掉，因此「25 条业务路由 / 22 个页面」的对外
口径保持不变；演示相关路由单独由 DemoModeContractTests 守住。
"""

import os
import re
import unittest
from pathlib import Path
from unittest import mock

from tests.base import REPO_ROOT, WEB_APP_DIR, HisTestCase  # noqa: F401


APP_PY = WEB_APP_DIR / "app.py"
DB_PY = WEB_APP_DIR / "db.py"
TEMPLATE_DIR = WEB_APP_DIR / "templates"

# 与 README「项目规模」表一致的对外口径（均不含演示模式）
EXPECTED_DECORATORS = 26
EXPECTED_RULES = 27            # 26 条 @app.route + Flask 自动注册的 /static/<path>
EXPECTED_ENDPOINTS = 26        # 25 个业务端点 + static
EXPECTED_POST_ENDPOINTS = 17   # 含只读表单的 /login
EXPECTED_WRITE_ROUTES = 16     # 排除 /login
EXPECTED_TEMPLATES = 22

# ---- 演示模式（不计入上面的业务口径） ----
DEMO_URL_PREFIX = "/demo"
DEMO_ENDPOINT_PREFIX = "demo_"
DEMO_TEMPLATE = "demo.html"
DEMO_ENDPOINTS = {"demo_console", "demo_switch", "demo_step", "demo_reset"}


def _is_demo_decorator(decorator):
    return '"%s' % DEMO_URL_PREFIX in decorator


def _is_demo_rule(rule):
    return (rule.rule.startswith(DEMO_URL_PREFIX)
            or rule.endpoint.startswith(DEMO_ENDPOINT_PREFIX))


def _business_templates():
    return sorted(p for p in TEMPLATE_DIR.glob("*.html") if p.name != DEMO_TEMPLATE)


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
        # 统计时排除 /demo* 路由：演示模式是答辩辅助功能，不计入对外业务口径
        cls.decorators = [d for d in re.findall(r"@app\.route\([^)]*\)", cls.source)
                          if not _is_demo_decorator(d)]

    def test_decorator_count(self):
        self.assertEqual(len(self.decorators), EXPECTED_DECORATORS)

    def test_runtime_rule_and_endpoint_counts(self):
        import app as app_module
        rules = [r for r in app_module.app.url_map.iter_rules() if not _is_demo_rule(r)]
        endpoints = {e for e in app_module.app.view_functions
                     if not e.startswith(DEMO_ENDPOINT_PREFIX)}
        post_endpoints = {
            r.endpoint for r in rules
            if "POST" in (set(r.methods) - {"HEAD", "OPTIONS"})
        }
        self.assertEqual(len(rules), EXPECTED_RULES, "url_map 规则数")
        self.assertEqual(len(endpoints), EXPECTED_ENDPOINTS, "端点数")
        self.assertEqual(len(post_endpoints), EXPECTED_POST_ENDPOINTS, "含 POST 的端点数")
        # 16 个写路由 / 17 个含 POST 端点（差值即只读表单 /login）
        self.assertIn("login", post_endpoints)
        self.assertEqual(len(post_endpoints) - 1, EXPECTED_WRITE_ROUTES)

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
        # 排除 demo.html：它属于演示模式，不计入「22 个业务页面」的对外口径
        files = _business_templates()
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

    READ_ONLY_POST = {"login"}   # 只读取表单，不需要写库异常分支

    @classmethod
    def setUpClass(cls):
        cls.blocks = _route_blocks(_read(APP_PY))

    def _post_blocks(self):
        for decorators, name, block in self.blocks:
            # 演示路由把异常统一交给 demo_mode.describe_error 翻译，
            # 不在视图层重复三层捕获，因此排除在「写路由」统计之外。
            if name.startswith(DEMO_ENDPOINT_PREFIX):
                continue
            if any("POST" in d for d in decorators):
                yield name, block

    def test_every_write_route_has_db_layers(self):
        """16 个写路由必须全部有前两层 DB 异常捕获，且顺序不可颠倒。"""
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
        """第三层（参数缺失）覆盖 15/16：只有确实不需要它的路由可以省略。

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
        self.assertEqual(len(with_third), 15, "应有 15 个写路由具备第三层")
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
    """标本类型按检验项目类别推导（需要数据库读取项目字典）。"""

    def test_blood_default(self):
        from tests.base import DB_AVAILABLE
        if not DB_AVAILABLE:
            self.skipTest("本地 MySQL 不可用")
        import app as app_module
        self.assertEqual(app_module.derive_sample_type([]), "血液")      # 空 → 默认
        self.assertEqual(app_module.derive_sample_type(["1", "2"]), "血液")  # 血常规项

    def test_urine_and_stool(self):
        from tests.base import DB_AVAILABLE
        if not DB_AVAILABLE:
            self.skipTest("本地 MySQL 不可用")
        import app as app_module
        self.assertEqual(app_module.derive_sample_type(["5"]), "尿液")   # 尿蛋白（尿常规）
        self.assertEqual(app_module.derive_sample_type(["41"]), "粪便")  # 粪便隐血（粪便常规）

    def test_mixed_takes_majority(self):
        from tests.base import DB_AVAILABLE
        if not DB_AVAILABLE:
            self.skipTest("本地 MySQL 不可用")
        import app as app_module
        # 1 项血常规 + 2 项尿常规 → 取项目数最多的「尿常规」
        self.assertEqual(app_module.derive_sample_type(["1", "5", "6"]), "尿液")


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


class DemoModeContractTests(unittest.TestCase):
    """演示模式（HIS_DEMO_MODE）的契约。

    演示模式是答辩/验收辅助功能：免密切换角色 + 一键走完一条就诊闭环。
    它不计入上面的对外业务规模口径，但**自身的安全边界必须被守住** ——
    尤其是「默认关闭」与「默认只允许本机」这两条：免密登录等于绕过认证，
    一旦在公网环境被打开，整套系统就是敞开的。
    """

    DEMO_MODULE = WEB_APP_DIR / "demo_mode.py"

    def test_module_present(self):
        self.assertTrue(self.DEMO_MODULE.exists(), "缺少 web_app/demo_mode.py")

    def test_module_does_not_import_app(self):
        """demo_mode 不能反向 import app，否则会形成循环导入。"""
        source = _read(self.DEMO_MODULE)
        self.assertIsNone(
            re.search(r"^\s*(?:import|from)\s+app\b", source, re.M),
            "demo_mode.py 不得 import app（应通过 helpers 注入）")

    def test_demo_routes_registered(self):
        import app as app_module
        endpoints = set(app_module.app.view_functions)
        missing = DEMO_ENDPOINTS - endpoints
        self.assertFalse(missing, "缺少演示路由：%s" % sorted(missing))

    def test_flag_off_unless_env_set(self):
        import demo_mode
        with mock.patch.dict(os.environ, {"HIS_DEMO_MODE": ""}):
            self.assertFalse(demo_mode.demo_enabled(), "默认必须关闭")
        for truthy in ("1", "true", "YES", "on"):
            with self.subTest(value=truthy):
                with mock.patch.dict(os.environ, {"HIS_DEMO_MODE": truthy}):
                    self.assertTrue(demo_mode.demo_enabled())

    def test_remote_denied_by_default(self):
        """默认只允许本机：免密登录绝不能顺手暴露到局域网/公网。"""
        import demo_mode
        with mock.patch.dict(os.environ, {"HIS_DEMO_ALLOW_REMOTE": ""}):
            self.assertTrue(demo_mode.remote_allowed("127.0.0.1"))
            self.assertTrue(demo_mode.remote_allowed("::1"))
            self.assertTrue(demo_mode.remote_allowed("::ffff:127.0.0.1"))
            self.assertFalse(demo_mode.remote_allowed("8.8.8.8"))
            self.assertFalse(demo_mode.remote_allowed("192.168.1.10"))
        with mock.patch.dict(os.environ, {"HIS_DEMO_ALLOW_REMOTE": "1"}):
            self.assertTrue(demo_mode.remote_allowed("8.8.8.8"))

    def test_routes_are_gated_when_disabled(self):
        """未开演示模式时：/demo 退回登录页，/demo/step 与 /demo/reset 直接 403。"""
        import app as app_module
        with mock.patch.dict(os.environ, {"HIS_DEMO_MODE": ""}):
            client = app_module.app.test_client()
            self.assertEqual(client.get("/demo").status_code, 302)
            self.assertEqual(client.get("/demo/switch/cash01").status_code, 302)
            self.assertEqual(
                client.post("/demo/step", json={"step": "patient"}).status_code, 403)
            self.assertEqual(client.post("/demo/reset").status_code, 403)

    def test_steps_unique_and_ordered(self):
        import demo_mode
        keys = [s["key"] for s in demo_mode.STEPS]
        self.assertEqual(len(keys), len(set(keys)), "演示步骤 key 不能重复")
        self.assertEqual(keys[0], "patient", "主线第一步应为患者建档")
        self.assertEqual(keys[-1], "dispense", "主线最后一步应为发药")

    def test_every_step_is_runnable_and_documented(self):
        import demo_mode
        for step in demo_mode.STEPS:
            with self.subTest(step=step["key"]):
                self.assertIn(step["key"], demo_mode._RUNNERS)
                for field in ("actor", "title", "desc", "page", "page_label"):
                    self.assertTrue(step.get(field),
                                    "%s 缺少字段 %s" % (step["key"], field))

    def test_unknown_step_returns_error_not_exception(self):
        """未知步骤要返回结构化错误，而不是抛异常冒到全局 500。"""
        import demo_mode
        result = demo_mode.run_step("no-such-step", {})
        self.assertFalse(result["ok"])
        self.assertIn("未知", result["error"])

    def test_helpers_reuse_app_implementations(self):
        """演示模式必须复用 app 里的实现，不得自己另写一套逻辑。"""
        import app as app_module
        self.assertIs(app_module._DEMO_HELPERS["make_no"], app_module.make_no)
        self.assertIs(app_module._DEMO_HELPERS["auto_flag"], app_module.auto_flag)
        self.assertIs(app_module._DEMO_HELPERS["derive_sample_type"],
                      app_module.derive_sample_type)

    def test_demo_state_file_is_gitignored(self):
        """演示状态属运行期数据，不得提交到仓库。"""
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("_demo_state.json", gitignore)


if __name__ == "__main__":
    unittest.main(verbosity=2)
