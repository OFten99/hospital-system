# -*- coding: utf-8 -*-
"""「AI + 测试」演示台 —— 构建脚本。

作用：把项目里**真实的** schema 与路由信息解析成数据模型，注入单页演示台模板，
      生成 `ai_test_demo/index.html`（自包含，双击即可打开，不依赖服务器与网络）。

数据全部来自仓库自身，不是写死的样例：
  * 表 / 列 / 约束（NOT NULL / UNIQUE / ENUM / CHECK / VARCHAR(n) / FOREIGN KEY）
    以及列的 COMMENT 中文名 —— 解析 `sql/*.sql`
  * 路由、表单必填项、调用的存储过程 —— 解析 `web_app/app.py`
  * 唯一键 / 非空列的中文提示映射、异常兜底文案 —— 解析 `web_app/app.py` 的常量表
  * 每条路由被自动化用例引用的次数 —— 扫描 `tests/*.py`

用法：
    python ai_test_demo/build_demo.py
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "sql"
APP_PY = ROOT / "web_app" / "app.py"
TESTS_DIR = ROOT / "tests"
TEMPLATE = Path(__file__).resolve().parent / "console_template.html"
OUT = Path(__file__).resolve().parent / "index.html"

# 当前推理引擎名称。接入真实大模型时改这里 + 替换模板里的 ask() 实现即可，
# 推导规则、导出格式、页面结构都不用动。
ENGINE_NAME = "本地规则推理引擎（离线，零依赖）"

# 参与解析的建表脚本（按实际执行顺序，后出现的表定义覆盖先前的）
SQL_FILES = [
    "02_create_tables.sql",
    "08_lab_vitals.sql",
    "10_appointment.sql",
    "11_announcement.sql",
    "13_medical_records.sql",
]

# 业务模块 → 页面路由 → 主表（演示台的左侧下拉即来源于此）
MODULES = [
    ("PAT", "患者建档", "/patients", ["patients"], "患者档案创建成功"),
    ("REG", "挂号 / 退号", "/registrations", ["registrations", "doctor_schedules"], "挂号成功"),
    ("SCH", "医生排班维护", "/schedules", ["doctor_schedules"], "排班"),
    ("RX", "处方开具", "/prescriptions", ["prescriptions", "prescription_items"], "处方"),
    ("PAY", "收费 / 退费", "/payments", ["payments"], "收费成功"),
    ("PHA", "药房发药", "/pharmacy", ["prescriptions"], "发药成功"),
    ("MED", "药品目录维护", "/medicines", ["medicines"], "药品"),
    ("USR", "用户管理", "/users", ["users", "doctors"], "新账号注册成功"),
    ("DEPT", "科室管理", "/departments", ["departments"], "科室"),
    ("ANN", "公告管理", "/announcements", ["announcements"], "公告"),
    ("LAB", "检验申请 / 结果录入", "/lab_tests", ["lab_tests", "lab_test_results"], "检验"),
    ("VIT", "体征记录", "/vitals", ["vital_signs"], "体征记录已保存"),
    ("MR", "门诊病历", "/medical_records", ["medical_records"], "病历"),
    ("DIA", "诊断维护", "/diagnosis", ["patients"], "诊断结果已保存"),
]


# ----------------------------------------------------------------------
# SQL 解析
# ----------------------------------------------------------------------
def _strip_line_comments(text):
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("--"))


def _split_top_level(body):
    """按顶层逗号切分（跳过括号内的逗号，如 DECIMAL(10,2) / ENUM('男','女')）。"""
    parts, buf, depth, quote = [], [], 0, None
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _find_blocks(text, header_re):
    """按 header_re 找到语句起点，用括号配平截取整段。返回 [(name, body, start)]。"""
    out = []
    for m in re.finditer(header_re, text, re.IGNORECASE):
        i = text.find("(", m.end() - 1)
        if i < 0:
            continue
        depth, quote, j = 0, None, i
        while j < len(text):
            ch = text[j]
            if quote:
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append((m.group(1), text[i + 1:j], j + 1))
    return out


def parse_schema():
    """返回 {table: {comment, columns: [...]}}。"""
    tables = {}
    for name in SQL_FILES:
        path = SQL_DIR / name
        if not path.exists():
            continue
        text = _strip_line_comments(path.read_text(encoding="utf-8-sig"))

        # ---- CREATE TABLE 形式 ----
        for tname, body, tail_at in _find_blocks(
                text, r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?\s*\("):
            tbl = {"comment": "", "columns": [], "combo_uniques": []}
            m = re.search(r"^[^;]*?COMMENT\s*=\s*'([^']*)'", text[tail_at:], re.DOTALL)
            if m and m.start() < 40:  # 只认紧跟在表定义后面的那句话
                tbl["comment"] = m.group(1)
            for entry in _split_top_level(body):
                fk = _parse_fk(entry)
                if fk:                                   # 表级外键：落到列上
                    for c in tbl["columns"]:
                        if c["name"] == fk["column"]:
                            c["fk"] = fk["ref"]
                    continue
                col = _parse_entry(entry)
                if col:
                    tbl["columns"].append(col)
            tables[tname] = tbl

        # ---- ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY 形式 ----
        for m in re.finditer(r"ALTER\s+TABLE\s+`?(\w+)`?", text, re.IGNORECASE):
            stmt_end = text.find(";", m.end())
            stmt = text[m.end():stmt_end if stmt_end > 0 else len(text)]
            tname = m.group(1)
            if tname not in tables:
                continue
            for fm in re.finditer(
                    r"FOREIGN\s+KEY\s*\(\s*`?(\w+)`?\s*\)\s*REFERENCES\s+`?(\w+)`?\s*\(\s*`?(\w+)`?",
                    stmt, re.IGNORECASE):
                for c in tables[tname]["columns"]:
                    if c["name"] == fm.group(1):
                        c["fk"] = "%s.%s" % (fm.group(2), fm.group(3))
    return tables


def _parse_fk(entry):
    m = re.search(
        r"FOREIGN\s+KEY\s*\(\s*`?(\w+)`?\s*\)\s*REFERENCES\s+`?(\w+)`?\s*\(\s*`?(\w+)`?",
        entry, re.IGNORECASE)
    if not m:
        return None
    return {"column": m.group(1), "ref": "%s.%s" % (m.group(2), m.group(3))}


def _parse_entry(entry):
    fk = _parse_fk(entry)
    if fk:
        return None  # 表级外键，由调用方处理
    if re.match(r"^(PRIMARY\s+KEY|KEY|INDEX|UNIQUE|CHECK|CONSTRAINT)\b", entry, re.IGNORECASE):
        return None

    m = re.match(r"`?(\w+)`?\s+([A-Za-z]+)\s*(?:\(([^)]*)\))?", entry)
    if not m:
        return None
    name, base_type, arg = m.group(1), m.group(2).upper(), m.group(3)

    col = {
        "name": name, "type": base_type, "length": None, "enum": None,
        "not_null": bool(re.search(r"\bNOT\s+NULL\b", entry, re.IGNORECASE)),
        "has_default": bool(re.search(r"\bDEFAULT\b", entry, re.IGNORECASE)),
        "unique": bool(re.search(r"\bUNIQUE\b", entry, re.IGNORECASE)),
        "fk": None, "check": None, "label": name,
    }
    if base_type in ("VARCHAR", "CHAR") and arg:
        col["length"] = int(arg.strip())
    if base_type == "ENUM" and arg:
        col["enum"] = re.findall(r"'([^']*)'", arg)
    cm = re.search(r"COMMENT\s*'([^']*)'", entry)
    if cm:
        col["label"] = cm.group(1)
    return col


def apply_table_level_constraints(schema):
    """把 CONSTRAINT ... UNIQUE (a,b) / CHECK (col >= 0) 落到列上。"""
    for name in SQL_FILES:
        path = SQL_DIR / name
        if not path.exists():
            continue
        text = _strip_line_comments(path.read_text(encoding="utf-8-sig"))
        for tname, body, _ in _find_blocks(
                text, r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?\s*\("):
            tbl = schema.get(tname)
            if not tbl:
                continue
            by_name = {c["name"]: c for c in tbl["columns"]}
            for entry in _split_top_level(body):
                um = re.search(r"UNIQUE\s*\(\s*([^)]*)\)", entry, re.IGNORECASE)
                if um and "CONSTRAINT" in entry.upper():
                    cols = [c.strip().strip("`") for c in um.group(1).split(",")]
                    if len(cols) == 1 and cols[0] in by_name:
                        by_name[cols[0]]["unique"] = True
                    elif len(cols) > 1:
                        # 复合唯一键：例如「同医生 + 同日期 + 同班次」只能有一条排班
                        labels = [by_name[c]["label"] if c in by_name else c for c in cols]
                        nm = re.search(r"CONSTRAINT\s+`?(\w+)`?\s+UNIQUE", entry, re.IGNORECASE)
                        tbl["combo_uniques"].append({
                            "table": tname, "cols": cols, "name": nm.group(1) if nm else "",
                            "label": " + ".join(labels)})
                cm = re.search(r"CHECK\s*\(\s*`?(\w+)`?\s*(>=|>|<=|<)\s*([-\d.]+)\s*\)",
                               entry, re.IGNORECASE)
                if cm and cm.group(1) in by_name:
                    by_name[cm.group(1)]["check"] = {
                        "op": cm.group(2), "value": cm.group(3)}


# ----------------------------------------------------------------------
# app.py 解析
# ----------------------------------------------------------------------
def parse_app():
    text = APP_PY.read_text(encoding="utf-8")

    # 1) 常量映射：唯一键 / 非空列 → 中文名
    def dict_of(var):
        m = re.search(re.escape(var) + r"\s*=\s*\{(.*?)\n\}", text, re.DOTALL)
        if not m:
            return {}
        return dict(re.findall(r'"([^"]+)"\s*:\s*"([^"]*)"', m.group(1)))

    unique_hints = dict_of("_UNIQUE_FIELD_HINTS")
    notnull_hints = dict_of("_NOT_NULL_FIELD_HINTS")

    # 2) 三层异常兜底的固定文案（用于推导「预期结果」）
    messages = {}
    for key, pat in [
        ("required", r"return f\"\{label\}不能为空，请填写后重试。\""),
        ("required_generic", r'return "有必填项未填写，请补全后重试。"'),
        ("unique", r'return f"\{label\}已存在，请更换后重试。"'),
        ("too_long", r'return "输入内容超出字段长度限制，请缩短后重试。"'),
        ("out_of_range", r'return "输入数值超出允许范围，请检查后重试。"'),
        ("bad_enum", r'\(提前.\n?\s*"提交的取值不在该字段允许的范围内'),
        ("fk_missing", r'return "关联的数据不存在或已被删除，请刷新页面后重试。"'),
        ("fk_in_use", r'return "该数据已被其它业务记录引用（挂号、处方或收费等），不能直接删除。"'),
    ]:
        messages[key] = bool(re.search(pat, text))
    messages["required_text"] = "必填参数不能为空，请检查后重试。"
    messages["unique_text"] = "已存在，请更换后重试。"
    messages["too_long_text"] = "输入内容超出字段长度限制，请缩短后重试。"
    messages["out_of_range_text"] = "输入数值超出允许范围，请检查后重试。"
    messages["bad_enum_text"] = "提交的取值不在该字段允许的范围内"
    messages["fk_missing_text"] = "关联的数据不存在或已被删除，请刷新页面后重试。"

    # 3) 路由块（连续多个 @app.route 指向同一视图时要合并，否则会漏掉别名路径）
    routes = []
    pat = re.compile(r'@app\.route\(\s*"([^"]+)"\s*(?:,\s*methods\s*=\s*\[([^\]]*)\])?\s*\)')
    marks = list(pat.finditer(text))
    pending = []
    for idx, m in enumerate(marks):
        end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
        block = text[m.end():end]
        fm = re.search(r"\ndef\s+(\w+)\s*\(", block)
        if not fm:
            pending.append(m)          # 还没有到 def，说明后面还有装饰器
            continue
        body = block[fm.end():]
        # 视图函数体的终点：下一个顶层 def / 装饰器 / 章节分隔注释
        stop = re.search(r"\n(?=@app\.|\ndef\s|\n# =)", body)
        if stop:
            body = body[:stop.start()]
        decorators = pending + [m]
        pending = []
        first = decorators[0]
        methods = [x.strip().strip('"').strip("'")
                   for x in (first.group(2) or '"GET"').split(",") if x.strip()]
        perm = re.search(r'@permission_required\("([^"]+)"\)', block)
        routes.append({
            "path": first.group(1),
            "aliases": [d.group(1) for d in decorators[1:]],
            "methods": methods,
            "endpoint": fm.group(1),
            "perm": perm.group(1) if perm else None,
            "required_fields": sorted(set(re.findall(r'request\.form\["([^"]+)"\]', body))),
            "optional_fields": sorted(set(
                re.findall(r'request\.form\.get\("([^"]+)"', body) +
                re.findall(r'request\.form\.getlist\("([^"]+)"', body))),
            "procs": sorted(set(re.findall(r'call_proc\(\s*"([^"]+)"', body))),
            "actions": sorted(set(re.findall(r'action\s*==\s*"([^"]+)"', body))),
            "flash": sorted(set(re.findall(r'flash\(\s*"([^"\n]{2,40})"', body)))[:8],
        })
    return routes, unique_hints, notnull_hints, messages


def parse_test_coverage(routes):
    """统计每条路由被 tests/ 静态引用的次数（近似：路径字面量是否出现）。"""
    texts = {}
    if TESTS_DIR.exists():
        for p in TESTS_DIR.glob("test_*.py"):
            texts[p.name] = p.read_text(encoding="utf-8")
    for r in routes:
        # 去掉动态段，取静态前缀链，逐级回退匹配；别名路径一并计入
        candidates = [r["path"]] + list(r.get("aliases") or [])
        best_hits, best_files = 0, []
        for cand in candidates:
            segs = [s for s in cand.split("/") if s and not s.startswith("<")]
            for probe_len in range(len(segs), 0, -1):
                probe = "/" + "/".join(segs[:probe_len])
                found = [n for n, t in texts.items() if probe in t]
                if found:
                    hits = sum(t.count(probe) for t in texts.values())
                    if hits > best_hits:
                        best_hits, best_files = hits, sorted(found)
                    break
        r["test_files"] = best_files
        r["test_refs"] = best_hits
    return routes


# ----------------------------------------------------------------------
# 组装模块
# ----------------------------------------------------------------------
def build_modules(schema, routes, unique_hints, notnull_hints, messages):
    def label_of(col, table):
        return col["label"] if col["label"] != col["name"] else col["name"]

    modules = []
    for code, name, route, tables, success_hint in MODULES:
        rt = next((r for r in routes if r["path"] == route), None)
        form_fields = set()
        if rt:
            form_fields = set(rt["required_fields"]) | set(rt["optional_fields"])

        required, unique, enums, checks, varchar, fks = [], [], [], [], [], []
        skipped = []          # 有约束、但应用并不从表单接收（由存储过程/服务端生成）的列

        def relevant(cols):
            """复合唯一键只要有任一列来自表单就算相关。"""
            return any(c in form_fields for c in cols)

        for t in tables:
            tbl = schema.get(t)
            if not tbl:
                continue
            for c in tbl["columns"]:
                lab = label_of(c, t)
                in_form = c["name"] in form_fields
                is_required = (c["not_null"] and not c["has_default"]
                               and c["name"] not in ("created_at", "updated_at"))
                if (is_required or c["unique"] or c["enum"] or c["check"]
                        or c["length"] or c["fk"]) and not in_form:
                    skipped.append({"table": t, "col": c["name"], "label": lab})
                    continue
                if is_required:
                    required.append({"table": t, "col": c["name"], "label": lab})
                if c["unique"]:
                    unique.append({"table": t, "col": c["name"], "label": lab,
                                   "hint": unique_hints.get(c["name"], lab)})
                if c["enum"]:
                    enums.append({"table": t, "col": c["name"], "label": lab,
                                  "values": c["enum"]})
                if c["check"]:
                    checks.append({"table": t, "col": c["name"], "label": lab,
                                   "op": c["check"]["op"], "value": c["check"]["value"]})
                if c["length"] and c["type"] in ("VARCHAR", "CHAR"):
                    varchar.append({"table": t, "col": c["name"], "label": lab,
                                    "length": c["length"]})
                if c["fk"]:
                    fks.append({"table": t, "col": c["name"], "label": lab, "ref": c["fk"]})
            for cu in tbl.get("combo_uniques") or []:
                if not relevant(cu["cols"]):
                    skipped.append({"table": t, "col": ",".join(cu["cols"]),
                                    "label": cu["label"]})
                    continue
                unique.append({"table": t, "col": ",".join(cu["cols"]), "label": cu["label"],
                               "hint": unique_hints.get(cu["name"]) or cu["label"],
                               "combo": True})

        modules.append({
            "code": code, "name": name, "route": route, "tables": tables,
            "method": (rt["methods"] if rt else ["GET"]),
            "perm": (rt["perm"] if rt else None),
            "procs": (rt["procs"] if rt else []),
            "actions": (rt["actions"] if rt else []),
            "flashes": (rt["flash"] if rt else []),
            "form_fields": sorted(form_fields),
            "required_form_fields": (rt["required_fields"] if rt else []),
            "successHint": success_hint,
            "required": required, "unique": unique, "enums": enums,
            "checks": checks, "varchar": varchar, "fks": fks,
            "skipped": skipped,
            # 应用层自己声明的必填项（request.form["x"] 直接下标取值）
            # —— schema 里可能是自增主键或由服务端填，推不出「必填」，但接口契约上是必填
            "app_required": [f for f in (rt["required_fields"] if rt else [])
                             if f not in {x["col"] for x in required}],
            "app_optional": [f for f in (rt["optional_fields"] if rt else [])
                             if f not in {x["col"] for x in required + unique + enums
                                          + checks + varchar + fks}
                             and f != "action"],
        })
    return modules


# ----------------------------------------------------------------------
# 失败归因知识库（含本仓库真实踩过的两个缺陷）
# ----------------------------------------------------------------------
def build_knowledge():
    rules = [
        {"id": "mysql-1062", "match": {"errno": "1062", "kw": ["Duplicate entry"]},
         "title": "唯一约束冲突：写入的值已存在（MySQL 1062）",
         "severity": "高", "layer": "第 1 层 IntegrityError → handle_integrity_error",
         "cause": "唯一索引 / 主键上的值重复。若报的是业务单号（reg_no / prescription_no / "
                  "payment_no / test_no），几乎可以断定是**单号生成器在同一时间粒度内产生了重复值**。",
         "repro": "同一秒内连续发起两次同类型业务（例：连点两次挂号、或脚本化批量验收）。",
         "fix": "检查 make_no() 的时间精度：秒级时间戳必然撞车，应提升到微秒并叠加进程内自增序号；"
                "同时确认该异常仍被第 1 层捕获（不能冒成 500）。",
         "guard": "BusinessNumberTests.test_make_no_unique_within_same_second（连续 500 次生成必须互不相同）"},
        {"id": "mysql-1452", "match": {"errno": "1452", "kw": ["foreign key constraint fails"]},
         "title": "外键约束失败：引用了不存在的父记录（MySQL 1452）",
         "severity": "高", "layer": "第 1 层 IntegrityError → handle_integrity_error",
         "cause": "写入的外键值在父表里不存在。**本仓库最典型的一类**是「把 doctors.doctor_id "
                  "当成 users.user_id 写进审计字段」——两者是不同序列，在种子数据里恰好错位，"
                  "于是有的医生能成功、有的医生每次必失败。",
         "repro": "用 doctor_id 与 user_id 不一致的医生账号发起开方 / 挂号 / 体征录入。",
         "fix": "凡是「写审计人」的字段（operation_logs.user_id、vital_signs.operator_id、"
                "payments.operator_id、registrations.operator_id）都必须先换算成 users.user_id，"
                "例如 SELECT user_id INTO v_uid FROM doctors WHERE doctor_id = p_doctor_id。",
         "guard": "test_cannot_void_other_doctors_prescription"},
        {"id": "mysql-1048", "match": {"errno": "1048", "kw": ["cannot be null"]},
         "title": "NOT NULL 列被写入空值（MySQL 1048）",
         "severity": "中", "layer": "第 1 层 IntegrityError → handle_integrity_error",
         "cause": "表单里某个「看起来可以不填」的字段，在库里是 NOT NULL 且无默认值。",
         "repro": "提交时留空该字段（例：新增用户不填手机号，而 users.phone 仍是 NOT NULL）。",
         "fix": "二选一并保持一致：要么在库里放开为 NULL（迁移脚本），要么在应用层做必填校验；"
                "同时把该列登记进 _NOT_NULL_FIELD_HINTS，保证提示是中文列名而不是「完整性约束」。",
         "guard": "test_missing_required_field_is_handled 系列"},
        {"id": "mysql-1451", "match": {"errno": "1451", "kw": ["Cannot delete or update a parent row"]},
         "title": "删除被外键阻止：仍有子记录引用（MySQL 1451）",
         "severity": "中", "layer": "第 1 层 IntegrityError → handle_integrity_error",
         "cause": "被删对象仍被挂号 / 处方 / 收费 / 账号等子表引用。",
         "repro": "删除已有挂号的医生档案、已有关联账号的科室、已被业务引用的患者档案。",
         "fix": "保持提示可读（「该数据已被其它业务记录引用…」），不要吞掉异常；"
                "多表级联删除必须放在同一个事务里（execute_atomic），否则会留下半删脏数据。",
         "guard": "test_delete_patient_with_registration_blocked / test_delete_department_with_user_blocked"},
        {"id": "mysql-1265", "match": {"errno": "1265", "kw": ["Data truncated", "Data truncated for column"]},
         "title": "ENUM 非法取值（MySQL 1265 Data truncated）",
         "severity": "中", "layer": "第 2 层 mysql.connector.Error → handle_db_error",
         "cause": "提交的值不在 ENUM 允许集合内。注意它抛的是 DatabaseError，不是 IntegrityError，"
                  "**只有第 2 层能接住**——放进第 1 层永远不生效。",
         "repro": "直接构造 POST 把 account_status 传成「停用」（库里是 ENUM('启用','锁定')）。",
         "fix": "确认校验落在 handle_db_error 分支，并给出「取值不在允许范围内」的可读提示。",
         "guard": "test_invalid_enum_value_is_handled"},
        {"id": "mysql-1406", "match": {"errno": "1406", "kw": ["Data too long"]},
         "title": "字段超长（MySQL 1406 Data too long）",
         "severity": "低", "layer": "第 2 层 mysql.connector.Error → handle_db_error",
         "cause": "输入长度超过列宽。同样是 DataError，不在 IntegrityError 分支。",
         "repro": "向 VARCHAR(n) 字段提交 n+1 个字符。",
         "fix": "在 handle_db_error 里给出「输入内容超出字段长度限制」；前端加 maxlength 只是体验优化，"
                "服务端兜底不能省。",
         "guard": "test_third_layer_coverage（要求写路由具备参数层兜底）"},
        {"id": "mysql-1304", "match": {"errno": "1304", "kw": ["already exists"]},
         "title": "对象已存在：重复执行建库脚本（MySQL 1304）",
         "severity": "低", "layer": "运维 / 脚本层",
         "cause": "重复执行了已经存在对象的 DDL。若出现在【增量脚本】里，通常是**语句切分方式不对**："
                  "按 $$ 之类的分隔符粗暴切分含 DELIMITER 的脚本，会切出错误片段。",
         "repro": "用 ad-hoc 脚本重灌 sql/04_procedures.sql（内含 DELIMITER $$ ... DELIMITER ;）。",
         "fix": "复用项目自带的 split_sql_statements()（真正的 DELIMITER 解析器），不要手写切分。"
                "特别注意：先执行的 DROP PROCEDURE 不会随失败回滚，会把对象删成不存在。",
         "guard": "—（脚本层，建议纳入部署前检查）"},
        {"id": "py-none-subscript", "match": {"extype": "TypeError",
                                             "kw": ["'NoneType' object is not subscriptable"]},
         "title": "断言前查库返回 None：写入并未真正可见（TypeError）",
         "severity": "高", "layer": "测试断言 / 事务可见性",
         "cause": "接口返回了成功提示，但随后查库取不到那条记录。两种常见根因："
                  "① 写入未提交（存储过程内缺少 COMMIT / 事务未闭合）；"
                  "② 脚本版本回退——被后续脚本覆盖升级的存储过程被旧版本顶回去了。",
         "repro": "重灌 sql/04_procedures.sql 后跑挂号用例：flash 说「挂号成功」，"
                  "但测试连接 SELECT 不到该 registrations 行。",
         "fix": "增量脚本必须按序号从小到大执行（sp_register 由 10 覆盖 04，"
                "sp_add_lab_test 由 09 覆盖 08）；同时确认存储过程内有 COMMIT。",
         "guard": "RegistrationTests（8 个用例）"},
        {"id": "py-attribute-error", "match": {"extype": "AttributeError",
                                              "kw": ["has no attribute"]},
         "title": "对象属性不存在（AttributeError）",
         "severity": "中", "layer": "应用代码",
         "cause": "拼写错误、字段改名后未同步，或对 None 取属性。",
         "repro": "按报错行定位即可。",
         "fix": "核对改名是否在两处（SQL 列名 / Python 取值）都同步。",
         "guard": "—"},
        {"id": "py-key-error", "match": {"extype": "KeyError", "kw": ["KeyError"]},
         "title": "必填参数缺失（KeyError）",
         "severity": "中", "layer": "第 3 层 KeyError/TypeError/ValueError → handle_param_error",
         "cause": "用 request.form[\"x\"] 直接下标取一个没提交的字段。",
         "repro": "构造 POST 时漏掉某个必填项。",
         "fix": "该异常应被第 3 层接住并回「必填参数不能为空」；若冒成 500，说明该路由缺少第三层兜底。",
         "guard": "test_third_layer_coverage"},
        {"id": "assertion", "match": {"extype": "AssertionError", "kw": ["AssertionError"]},
         "title": "断言失败（AssertionError）",
         "severity": "中", "layer": "测试断言",
         "cause": "期望值与实际值不一致。先判断是**产品缺陷**还是**用例本身写错了前提**，"
                  "不要直接改断言把测试改绿。",
         "repro": "按 traceback 定位到具体断言与实参。",
         "fix": "优先断言业务结果（库里的状态、号源数、库存、金额），其次才断言提示文案——"
                "文案会随产品迭代变动。",
         "guard": "—"},
    ]

    samples = [
        {"name": "① 单号同秒撞唯一索引（MySQL 1062）",
         "expect": "mysql-1062",
         "output": """FAIL: test_queue_number_increases (tests.test_patients_and_registration.RegistrationTests)

======================================================================
FAIL: test_queue_number_increases (tests.test_patients_and_registration.RegistrationTests.test_queue_number_increases)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_patients_and_registration.py", line 189, in test_queue_number_increases
    self.assertEqual(r1["queue_no"], 1)
AssertionError: None != 1

stdout:
  mysql.connector.errors.IntegrityError: 1062 (23000): Duplicate entry
  'REG2026101519301200000001' for key 'registrations.reg_no'

Ran 8 tests in 9.1s
FAILED (failures=1)
"""},
        {"name": "② doctor_id 当 user_id 写审计日志（MySQL 1452）",
         "expect": "mysql-1452",
         "output": """ERROR: test_cannot_void_other_doctors_prescription
 (tests.test_prescription_payment_pharmacy.PrescriptionPaymentPharmacyTests)

======================================================================
ERROR: test_cannot_void_other_doctors_prescription
 (tests.test_prescription_payment_pharmacy.PrescriptionPaymentPharmacyTests.test_cannot_void_other_doctors_prescription)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_prescription_payment_pharmacy.py", line 205, in test_cannot_void_other_doctors_prescription
    self.assertIn("仅待收费", self.flash_text(resp))
AssertionError: '仅待收费' not found in '<!DOCTYPE html>...'

stderr:
  mysql.connector.errors.IntegrityError: 1452 (23000): Cannot add or update
  a child row: a foreign key constraint fails
  (`hospital_outpatient`.`operation_logs`,
   CONSTRAINT `fk_logs_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`user_id`))

  CALL sp_issue_prescription(...) -- 存储过程内 INSERT INTO operation_logs

Ran 16 tests in 21.4s
FAILED (errors=1)
"""},
        {"name": "③ 过程已存在 / 脚本切分错误（MySQL 1304）",
         "expect": "mysql-1304",
         "output": """Traceback (most recent call last):
  File "C:/tmp/apply_fix.py", line 42, in <module>
    cur.execute(stmt)
  File "mysql/connector/connection_cext.py", line 630, in execute
mysql.connector.errors.ProgrammingError: 1304 (42000): PROCEDURE sp_add_user already exists

executed: DROP PROCEDURE IF EXISTS sp_issue_prescription;
"""},
    ]
    return rules, samples


# ----------------------------------------------------------------------
def main():
    schema = parse_schema()
    apply_table_level_constraints(schema)
    routes, unique_hints, notnull_hints, messages = parse_app()
    parse_test_coverage(routes)
    modules = build_modules(schema, routes, unique_hints, notnull_hints, messages)
    rules, samples = build_knowledge()

    payload = {
        "meta": {
            "project": "医院门诊管理系统（HIS）",
            "engine": ENGINE_NAME,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tables": len(schema),
            "columns": sum(len(t["columns"]) for t in schema.values()),
            "routes": len(routes),
            "modules": len(modules),
            "covered_routes": sum(1 for r in routes if r["test_refs"] > 0),
        },
        "modules": modules,
        "routes": routes,
        "unique": unique_hints,
        "notnull": notnull_hints,
        "rules": rules,
        "samples": samples,
    }

    if not TEMPLATE.exists():
        print("[错误] 找不到模板：%s" % TEMPLATE)
        sys.exit(1)
    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("/*__DEMO_DATA__*/null", json.dumps(payload, ensure_ascii=False))
    OUT.write_text(html, encoding="utf-8")

    m = payload["meta"]
    print("=" * 62)
    print("生成 AI + 测试 演示台")
    print("=" * 62)
    print("  解析表        : %d 张，%d 个字段" % (m["tables"], m["columns"]))
    print("  解析路由      : %d 条（其中 %d 条被用例引用）" % (m["routes"], m["covered_routes"]))
    print("  业务模块      : %d 个" % m["modules"])
    print("  归因规则      : %d 条，内置失败样例 %d 个" % (len(rules), len(samples)))
    print("  输出          : %s" % OUT)


if __name__ == "__main__":
    main()
