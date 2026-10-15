# 自动化测试套件

医院门诊管理系统的自动化测试，**零额外依赖**：只用 Python 标准库 `unittest`，
不需要安装 pytest。目标是让「改完代码心里没底」变成「跑一遍就知道」。

---

## 一、怎么跑

在**仓库根目录**执行：

```bash
python -m unittest discover -s tests -v
```

Windows 上也可以直接双击仓库根目录的 `run_tests.bat`（会顺手检查 Python 与依赖）。

跑单个文件 / 单个类 / 单个用例：

```bash
python -m unittest tests.test_contracts -v
python -m unittest tests.test_admin_and_lab.LabWorkflowTests -v
python -m unittest tests.test_admin_and_lab.LabWorkflowTests.test_doctor_orders_lab_test -v
```

### 前提条件

| 用例类型 | 需要什么 |
| --- | --- |
| 离线校验（路由清单、异常分层、密码哈希、结果标志判定……） | **什么都不需要**，克隆下来就能跑 |
| 业务集成（走 Flask 测试客户端 + 真实 HTTP 处理链路） | 本地 MySQL `127.0.0.1:3306`，库 `hospital_outpatient` |

集成用例通过 `tests/base.py` 里的 `requires_db` 装饰器做**优雅跳过**：
数据库连不上时它们不会报错，而是打印 skip 原因，离线用例照常运行。

准备本地库：

```bash
# 1) 启动 MySQL（或双击 start_mysql.bat）
# 2) 初始化数据库（01~14 号脚本，含建表/视图/存储过程/触发器/种子数据）
python init_database.py
```

连接参数可用环境变量覆盖（与 `web_app/db.py` 的默认值一致）：
`HIS_DB_HOST` / `HIS_DB_PORT` / `HIS_DB_USER` / `HIS_DB_PASSWORD` / `HIS_DB_NAME`。

---

## 二、文件一览（共 183 个用例）

| 文件 | 用例数 | 覆盖内容 |
| --- | ---: | --- |
| `test_contracts.py` | 49 | **离线静态契约**：路由/模板清单、三层异常分层、Jinja `.items` 陷阱、密码哈希、结果标志自动判定、单号唯一性、已下线功能守卫、**演示模式的开关与访问边界** |
| `test_admin_and_lab.py` | 56 | 科室 / 用户 / 公告 / 药品字典维护，**检验科全流程**（开单→收费→录结果→作废），诊断维护，报表与 CSV 导出 |
| `test_patients_and_registration.py` | 23 | 患者建档（唯一性、外键保护、检索）、挂号/退号/号源、排班冲突与停诊 |
| `test_consultation_and_records.py` | 23 | 医生接诊工作台（开始/完成接诊）、门诊病历保存与打印、检验开单关联挂号、体征内联录入 |
| `test_prescription_payment_pharmacy.py` | 16 | 开方、收费扣库存、退费恢复库存、作废、药房发药 |
| `test_auth_and_permissions.py` | 16 | 五角色登录、错误密码统一文案（防枚举）、锁定账号、18 个菜单 URL × 5 角色权限矩阵、`/healthz` |
| `base.py` | — | 公共夹具：连接、登录、字典数据快照、测试数据构造与**自清理** |
| `__init__.py` | — | 包说明 |

---

## 三、被测试固化的接口契约

`test_contracts.py` 把这些数字写成断言。**改动接口时必须同步更新这里**，
否则 CI/本地会立刻失败——这是故意的，目的是让「不小心删了一个路由」变成显式决策。

| 契约项 | 期望值 | 说明 |
| --- | ---: | --- |
| `@app.route` 装饰器数 | 26 | `/consultation` 有两个装饰器指向同一视图 |
| `url_map` 规则数 | 27 | 含 Flask 自动注册的 `/static/<path:filename>` |
| endpoint 数 | 26 | 含 `static` |
| 支持 POST 的 endpoint 数 | 17 | |
| 写路由数（排除只读 `/login`） | 16 | |
| 模板文件数 | 22 | 且每个 `render_template("x.html")` 都必须真实存在 |
| 有数据库异常兜底（第 1+2 层）的写路由 | 16 / 16 | |
| 有第三层参数异常兜底（`KeyError/TypeError/ValueError`）的写路由 | 15 | 唯一例外是 `lab_results` |
| 演示模式路由 / 模板 | 4 / 1 | `/demo*` 与 `demo.html`（`HIS_DEMO_MODE=1` 时启用），**不计入**上面的业务口径 |
| 演示主线步骤数 | 12 | `demo_mode.STEPS`：key 唯一，首尾固定为 `patient` / `dispense` |

> **演示模式为什么不计入业务口径**：`/demo*` 是答辩辅助功能（免密切换角色 + 一键跑完主线），
> 不属于系统对外承诺的业务规模，因此 `test_decorator_count` / `test_runtime_rule_and_endpoint_counts` /
> `test_template_count` / `_post_blocks()` 都会先把它们排除掉，「25 条业务路由 / 22 个页面」
> 的口径保持不变。演示相关路由改由 `DemoModeContractTests`（11 条）单独守住，
> 重点断言**默认关闭**、**默认只允许本机**、未开启时 `/demo` 退登录页且 `/demo/step` 返回 403，
> 以及「演示必须复用 app 里的 `make_no` / `auto_flag` / `derive_sample_type`」这条一致性约束。

关于 `lab_results`：它全程用 `request.form.get()` 取值，从不直接下标访问表单，
因此不会抛 `KeyError`；`TESTING=True` 下若真出现参数异常会直接冒成 500，
这样反而更容易暴露问题。这个例外被显式记录在 `test_third_layer_coverage` 里。

---

## 四、数据自清理与可重复运行

集成用例反复运行不会污染既有业务数据，也不会因为上次残留而失败：

1. **业务数据挂在测试患者名下**。所有挂号、处方、检验、收费、病历、体征都由
   `mk_patient()` 造出的测试患者发起；`tearDown` 从 `patients` 出发按外键依赖顺序
   反查删除（收费 → 检验结果 → 检验单 → 处方明细 → 处方 → 体征 → 病历 → 挂号 → 患者）。
2. **测试自建账号用专属医生**。挂号类用例通过 `mk_doctor()` 建一个独立医生，
   避开 `doctor_schedules` 的「同医生同日期同班次」唯一键与基线排班冲突。
3. **字典类数据显式登记**。科室 / 用户 / 药品 / 公告 / 排班通过 `extra_cleanup`
   登记删除语句，`tearDown` **逆序**执行，保证 `logs → doctors → users`、
   `账号 → 科室` 这类外键依赖顺序正确。
4. **操作日志按时间窗清理**。触发器与存储过程写入的审计留痕，
   在用例结束时按 `created_at >= 用例开始时间` 清掉。

> 前提：测试假定运行期间没有其他人在同时操作这套本地库
> （本地课程设计场景的标准前提）。

---

## 五、写新用例

```python
from tests.base import HisTestCase, requires_db, unique


@requires_db                      # 需要数据库；不可用则跳过
class MyFeatureTests(HisTestCase):

    def setUp(self):
        super().setUp()
        self.client = self.client_as("doctor01")   # 已登录的测试客户端

    def test_something(self):
        pid = self.mk_patient()                     # 自动纳入清理
        resp = self.client.post("/somewhere", data={...}, follow_redirects=True)
        self.assertIn("期望的提示文案", self.flash_text(resp))
```

常用夹具（都在 `tests/base.py`）：

| 方法 | 用途 |
| --- | --- |
| `client_as(username)` | 返回一个已登录的测试客户端（密码默认 `123456`） |
| `mk_patient()` / `track_patient(patient_no)` | 造测试患者 / 把 HTTP 建档的患者纳入清理 |
| `mk_registration(pid, doctor_id)` | 直接插入一条挂号（不走存储过程） |
| `mk_doctor()` | 造一个独立测试医生（含 users 档案，自动清理） |
| `mk_schedule(doctor_id, ...)` | 造一条排班 |
| `mk_medicine()` / `mk_user()` | 造药品 / 账号 |
| `one(sql, params)` / `all(sql, params)` / `exec_sql(sql, params)` | 直接查库断言 |
| `flash_text(resp)` | 取响应正文，用于断言 flash 提示 |

断言原则：**优先断言业务结果（库里的状态、号源数、库存、金额），
其次才断言提示文案**。文案会随产品迭代变动，状态变化才是真正要守住的东西。

---

## 六、测试发现并修复过的真实缺陷（回归守卫已就位）

这套用例不是摆设，写的过程中真的抓出过两个会伤到用户的 bug：

1. **业务单号同秒撞唯一索引**
   `make_no()` 早期用秒级时间戳，同一秒内连续两次挂号/开方/收费会撞
   `reg_no` / `prescription_no` / `payment_no` / `test_no` 的唯一索引，
   用户看到的是「挂号单号已存在，请更换后重试」这种莫名其妙的提示。
   现已改为「微秒时间戳 + 进程内自增序号」，并由
   `BusinessNumberTests.test_make_no_unique_within_same_second` 守着（连续生成 500 个必须全不同）。

2. **`sp_issue_prescription` 把 `doctor_id` 当成 `user_id` 写进审计日志**
   存储过程直接拿 `p_doctor_id`（`doctors.doctor_id` 序列）写
   `operation_logs.user_id`（`users.user_id` 序列）。两者在种子数据里恰好错位，
   后果是：部分医生**每次开方都因外键 1452 回滚失败**（医生01 能开方、医生02 必失败），
   另一些医生则被**静默记到别人名下**。现已改为先 `SELECT user_id FROM doctors`
   换算，并由 `test_cannot_void_other_doctors_prescription` 守着。

同类陷阱还有 `vital_signs.operator_id` / `payments.operator_id` /
`registrations.operator_id`——凡是「写审计人」的字段，一律必须是 `users.user_id`。
