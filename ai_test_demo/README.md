# AI + 测试 演示台

一个**自包含的单页演示台**，演示「AI 辅助测试」在真实项目里能落成什么样子。

它不写死示例，而是直接**解析本仓库自己的 `sql/*.sql` 与 `web_app/app.py`**：
把表约束、路由表单、存储过程、异常分层解析成数据模型，再据此推导测试用例、
给失败日志做归因、算路由覆盖缺口。

> 打开方式：直接双击 `index.html`。**不需要服务器、不需要网络、不需要装任何依赖。**

---

## 目录

| 文件 | 说明 |
| --- | --- |
| `index.html` | **交付物**。自包含单页演示台（约 78 KB），已把数据内联进去，双击即用 |
| `build_demo.py` | 构建脚本：解析仓库 → 生成 `index.html`（约 33 KB） |
| `console_template.html` | 页面模板：版式 + 全部推理规则（约 28 KB） |

重新生成（改了 `sql/` 或 `app.py` 后需要跑一次）：

```bash
python ai_test_demo/build_demo.py
```

输出：

```
解析表        : 19 张，185 个字段
解析路由      : 25 条（其中 24 条被用例引用）
业务模块      : 14 个
归因规则      : 11 条，内置失败样例 3 个
输出          : ai_test_demo/index.html
```

---

## 三个标签页

### ① 智能用例生成

选一个业务模块，引擎从「该模块涉及的表的约束」∩「该模块表单真正能提交的字段」
推出测试用例，并按约束类型分组：

`正常流` · `必填校验` · `唯一性` · `取值校验` · `边界值` · `长度边界` · `外键完整性` · `业务规则`

以「患者建档 `/patients`」为例，得到 **22 条**（P1 4 / P2 12 / P3 6，覆盖 6 类约束、
来自 3 处真实唯一键）。全部 14 个模块合计可推导 **183 条**：

| 模块 | 条数 | 模块 | 条数 | 模块 | 条数 |
| --- | --- | --- | --- | --- | --- |
| `PAT` 患者建档 | 22 | `MED` 药品目录维护 | 20 | `LAB` 检验申请/结果录入 | 14 |
| `USR` 用户管理 | 32 | `SCH` 医生排班维护 | 15 | `RX` 处方开具 | 13 |
| `REG` 挂号/退号 | 12 | `PAY` 收费/退费 | 12 | `MR` 门诊病历 | 11 |
| `DEPT` 科室管理 | 11 | `ANN` 公告管理 | 7 | `VIT` 体征记录 | 7 |
| `PHA` 药房发药 | 4 | `DIA` 诊断维护 | 3 | | |

导出 **禅道格式 CSV**（10 列，与仓库里 `docs/黑盒测试用例_*.csv` 同构），
可直接导入禅道/Excel。

### ② 失败智能归因

粘贴 `unittest` 的 Traceback、MySQL 错误码或存储过程报错，引擎按
**错误码 / 异常类型 / 关键字** 三路打分，给出：

命中规则 → 置信度 → 严重级 → **所属异常层** → 命中依据 → 根因分析 → 复现步骤 →
修复建议 → **回归守卫（指回 `tests/` 里已固化的用例名）**

内置 11 条规则，其中最关键的一条是**分层路由**：

| 错误码 | 归到哪一层 | 说明 |
| --- | --- | --- |
| 1062 / 1048 / 1451 / 1452 | 第 1 层 `handle_integrity_error` | `IntegrityError`，可读出 `errno` |
| **1406 / 1265** | **第 2 层 `handle_db_error`** | **`DataError`/`DatabaseError`，不是 `IntegrityError`，第 1 层抓不到** |
| 1304 | 脚本层 | 建库脚本重复执行，建议纳入部署前检查 |

> 1406（字段超长）与 1265（ENUM 非法值）这一条是**真实踩过的坑**：
> 因为不是 `IntegrityError`，只能由第 2 层兜底。`web_app/app.py:346-358` 的注释里写明了这一点。

导出**缺陷报告 Markdown**（含根因、复现、修复建议、回归守卫）。

### ③ 覆盖缺口分析

扫 `tests/*.py` 对每条路由的**路径静态引用**，画出「路由 × 用例」覆盖表：

- 25 条路由，**24 条被用例触达，1 条缺口**（`/` 首页 `index()`，纯 GET 无逻辑）
- 每行显示：路径 / 方法 / 权限键 / 调用的存储过程 / 用例引用次数 / 覆盖状态
- 点「只看未覆盖」即得到下一批用例的编写清单

---

## 关键设计：用例是怎么推出来的

### 精确规则：`数据库约束 ∩ 表单字段`

最容易把「AI 生成用例」做成**假用例**的地方，是对着建表语句里每一列都生成一条用例。
但业务单号、操作员、审计时间这些列**用户根本提交不了**——它们由存储过程或服务端生成。
对着它们生成「不填 `reg_no` 应报错」是没有意义的。

所以引擎只对 **同时满足两个条件** 的列生成用例：

1. 该列带有可测约束（`NOT NULL` / `UNIQUE` / `ENUM` / `CHECK` / `VARCHAR(n)` / `FOREIGN KEY`）
2. 该字段出现在该路由的表单里（`request.form["x"]` 或 `request.form.get("x")`）

不满足第 2 条的，**移入「已自动跳过」清单而不是硬编一条用例**，页面上可点开查看：

| 模块 | 表单字段 | 被主动跳过的约束 |
| --- | --- | --- |
| `PAT` 患者建档 | 11 | （无，全部可提交） |
| `MED` 药品目录维护 | 11 | （无） |
| `DEPT` 科室管理 | 5 | （无） |
| `SCH` 医生排班维护 | 8 | `doctor_schedules.registered_count` |
| `USR` 用户管理 | 15 | `users.password_hash`、`users.account_status`、`doctors.doctor_no` |
| `REG` 挂号/退号 | 7 | 16 处（`reg_no` / `queue_no` / `operator_id` / `visit_status` … 全由 `sp_register` 生成） |
| `PAY` 收费/退费 | 5 | 9 处（`payment_no` / `pay_amount` / `operator_id` … 全由 `sp_charge` 生成） |
| `DIA` 诊断维护 | 3 | 8 处（`patients.*` 走的是"按 `patient_id` 改诊断"，不重建档） |

除了 schema 约束，引擎还会补上**接口层必填**（`request.form["x"]` 直接下标取值、
缺失会抛 `KeyError` 的参数），例如 `PAT` 的 `birth_date` / `blood_type` / `patient_id`。

### 首选的约束来源是真实 schema，不是猜测

- `UNIQUE` 的中文名来自 `app.py` 的 `_UNIQUE_FIELD_HINTS`（如 `uk_schedule_doctor_date`
  → "该医生当天该班次的排班"），字段中文名取自列的 `COMMENT`
- 提示文案取自 `app.py` 的兜底消息常量（如「必填参数不能为空，请检查后重试。」），
  所以用例的「预期结果」和系统真实回显一致
- 表级约束（`CONSTRAINT uk_x UNIQUE (a,b)`）会被提升到列上；复合唯一键按**索引名**保留，
  以便生成「同一天同一医生同一班次重复排班」这类组合冲突用例
- 外键支持两种写法：表内 `FOREIGN KEY (x) REFERENCES y(z)`，以及
  `ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY ...`

`build_demo.py` 里的 `_split_top_level()` 按**顶层逗号**切分列定义（跳过引号与括号），
因此 `DECIMAL(10,2)`、`ENUM('男','女')` 不会被切坏；`_find_blocks()` 用花括号配对
提取过程体，能正确越过字符串里的注释符号和分号。

---

## 内置的 3 个真实失败样例

不是编的，是本仓库真实踩过的坑，点一下按钮就能看到完整归因：

| 样例 | 真实成因 | 回归守卫 |
| --- | --- | --- |
| ① 单号同秒撞唯一索引（MySQL 1062） | `make_no()` 早期只用**秒级**时间戳，同一秒连续挂号两次会撞 `reg_no` 唯一索引 | `BusinessNumberTests.test_make_no_unique_within_same_second`（连续 500 次生成必须互不相同） |
| ② `doctor_id` 当 `user_id` 写审计日志（MySQL 1452） | `sp_issue_prescription` 把 `doctors.doctor_id` 写进了 `operation_logs.user_id`，对部分医生**每次开方都被外键打回、整笔回滚** | `test_cannot_void_other_doctors_prescription` |
| ③ 过程已存在 / 脚本切分错误（MySQL 1304） | 手工按 `$$` 切分 `04_procedures.sql` 导致语句错段，且已执行的 `DROP PROCEDURE` 不随之后的错误回滚，数据库被留在**只有 15/18 个过程**的半损坏状态 | —（脚本层，建议纳入部署前检查） |

样例 ③ 的处置办法也记录在案：**必须复用 `init_database.py` 的 `split_sql_statements()`
（DELIMITER 感知）**，不要手写 `$$` 切分；重放脚本还要注意顺序——
单独重跑 `sql/04_procedures.sql` 会用早期版 `sp_register` **覆盖掉**
`sql/10_appointment.sql` 装上去的版本。

---

## 引擎是可插拔的

页面顶部横幅会显示当前引擎。模板里把「**怎么推**」和「**谁来推**」分开了：

```js
/* 引擎接口层：当前走本地规则，换模型时只改这里 */
function ask(task, payload) {
  switch (task) {
    case "gen_cases":  return gen(payload.module);   // 用例推导
    case "diagnose":   return diag(payload.text);    // 失败归因
    default: throw new Error("未知任务：" + task);
  }
}
```

两个按钮（`#btnGen` / `#btnDiag`）都只调 `ask()`。要接真实大模型：

1. 把 `ask()` 改成异步请求，prompt 里带上**已解析好的结构化事实**
   （`DATA.modules[i]` 里就有：涉及表、采纳约束、表单字段、存储过程、权限键），
   让模型只负责把它组织成自然语言的用例标题/步骤，而不是让它凭空猜表结构；
2. `btnGen` / `btnDiag` 走 async 分支；
3. 改 `build_demo.py` 里的 `ENGINE_NAME`，让页面横幅如实显示。

**推理规则、导出格式（禅道 CSV / 缺陷报告 MD）、页面结构都不用动。**

> 本演示台当前运行在**本地规则推理引擎**上，全程离线、零依赖。
> 这样做的好处是**结论可复核、可复现**：同样的 schema 与路由，永远推出一样的用例，
> 适合放进课程设计里讲清楚「AI 生成用例」到底在生成什么、依据是什么。
> 而「把约束翻译成用例」的规则本身就是这套东西真正的价值所在——
> 模型换不换，这些规则都得先存在。

---

## 数据来源

| 内容 | 来源 |
| --- | --- |
| 表 / 列 / `NOT NULL` / `UNIQUE` / `ENUM` / `CHECK` / `VARCHAR(n)` / `FOREIGN KEY` / 列中文注释 | `sql/02_create_tables.sql`、`sql/08_lab_vitals.sql`、`sql/10_appointment.sql`、`sql/11_announcement.sql`、`sql/13_medical_records.sql` |
| 路由 / 方法 / 权限键 / 表单必填与可选字段 / 调用的存储过程 / 动作 | `web_app/app.py` |
| 唯一键与非空列的中文提示映射、异常兜底文案 | `web_app/app.py` 的常量表 |
| 每条路由被自动化用例引用的次数 | 扫描 `tests/*.py` 的路径静态引用 |

统计随代码变化会自动更新。当前：**19 张表 / 185 个字段 / 25 条路由 / 14 个业务模块 /
11 条归因规则 / 183 条可推导用例**。

---

## 已知边界

- **覆盖判定用「路径静态引用」近似**：用例里以字符串形式出现的路径才算触达，
  通过 `url_for()` 动态拼出来的路径可能被漏判。所以 ③ 是**下界**指标，
  用来找「一眼就能看出没人测」的缺口，不宣称精确覆盖率。
- **用例是模板化推导，不是自然语言润色**：步骤与预期结果是按约束类型套出来的，
  保证「依据可追溯、不编造」；要更有文采的措辞可以接大模型（见上一节）。
- **`index.html` 是生成物**：直接改 `index.html` 会在下次 `build_demo.py` 时被覆盖，
  要改样式或规则请改 `console_template.html` / `build_demo.py`。
- 只解析 `web_app/app.py` 与 `sql/*.sql` 的**文本**，不连数据库；因此解析不到
  运行期才出现的约束（例如触发器里手动 `SIGNAL` 抛出的业务错误）。
