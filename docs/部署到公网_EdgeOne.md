# 部署到公网（腾讯云 EdgeOne Makers + 免费云 MySQL）

让任何人都能通过一个网址访问本系统。整套方案 **完全免费**，且你的数据库亮点（18 张表、
9 个视图、16 个存储过程、4 个触发器）**原样保留**，不需要为迁就云平台而改造。

---

## 一、为什么是这个方案

| 平台 | 能不能跑本项目 |
|---|---|
| **腾讯云 EdgeOne Makers**（本方案，原 EdgeOne Pages） | ✅ 支持 Python 3.10 + Flask（原生支持），连 GitHub 自动部署 |
| Cloudflare Pages | ❌ 纯静态 + 边缘函数，Flask 要改写成 JS，MySQL 也连不上 |
| GitHub Pages | ❌ 只能放静态文件，没有任何服务端能力 |
| 腾讯云 CloudPages（云建站） | ❌ 那是拖拽式无代码建站工具，不是代码托管平台 |

## 二、整体架构

```
        访问者浏览器
             │  https://xxxx.edgeone.site
             ▼
   ┌─────────────────────────────┐
   │  EdgeOne 全球边缘网络（CDN） │
   └──────────────┬──────────────┘
                  ▼
   ┌─────────────────────────────┐
   │  Cloud Function（云函数）    │  ← 代码来自本仓库 cloud-functions/
   │  Python 3.10 + Flask        │     入口 [[default]].py
   └──────────────┬──────────────┘
                  │  TLS 加密连接
                  ▼
   ┌─────────────────────────────┐
   │  Aiven 免费 MySQL 8         │  ← 建表 / 视图 / 存储过程 / 触发器
   └─────────────────────────────┘
```

关键点：**云函数本身不带数据库**，所以必须有一台公网可访问的 MySQL。这里选 Aiven
的永久免费 MySQL 8（无需信用卡，真 MySQL，支持存储过程与触发器）。

## 三、准备工作

- [ ] 一个 GitHub 账号
- [ ] 一个腾讯云账号，且**已完成实名认证**（EdgeOne Makers 要求）
- [ ] **（可选）一个自己的域名** —— 想给老师一个长期可用的链接才需要，
      只是课堂现场演示的话可以不买。原因见第八之二节
- [ ] 一个邮箱（注册 Aiven 用）
- [ ] 本地已装 Python 与 MySQL 客户端工具（用于导入 SQL，本机已有）

---

## 四、第 1 步：创建 Aiven 免费 MySQL

1. 打开 <https://console.aiven.io/signup>，用邮箱注册
2. 控制台点 **Create service** → 服务类型选 **MySQL** → 计划选 **Free**。
3. 区域建议选 **Singapore（新加坡）**；如果列表里有 **Hong Kong** 更快。
4. 起个名字（如 `hospital-db`）→ **Create service**，等状态变成 **Running**（约 2~5 分钟）。
5. 进入服务页 → 左侧 **Overview** → **Connection information**，记下这五项：

   | 项目 | 示例 | 说明 |
   |---|---|---|
   | Host | `hospital-db-xxx.aivencloud.com` | 主机 |
   | Port | `12345` | **不是 3306**，每台实例随机分配 |
   | User | `avnadmin` | 默认管理员 |
   | Password | `AVNS_xxxxxxxx` | 点眼睛图标显示 |
   | SSL mode | `REQUIRED` | 云端强制 TLS |

6. **这一步最容易漏，漏了第 2 步一定失败**：
   左侧 **Advanced configuration** → 搜索 `log_bin_trust_function_creators`
   → 设为 **1** → Save。

   > 托管 MySQL 默认开启 binlog，普通账号建触发器需要 SUPER 权限，否则报
   > `ERROR 1419`。本项目有 4 个触发器 + 16 个存储过程，必须先打开这个开关。
   > 忘了也没关系：第 2 步的脚本连接成功后会自动检查这个开关并提前警告。

7. 如果开了 IP 白名单（IP filter），要允许本机公网 IP 和云函数出口 IP；
   实在不确定就先不加限制，跑通后再收紧。

---

## 五、第 2 步：把数据库结构和数据导入云端（一条命令）

回到项目根目录，**双击 `deploy_cloud_db.bat`**，或者在命令行执行：

```cmd
python deploy_cloud_db.py
```

### 推荐路径：用记事本填配置（避开命令行粘贴失效）

首次运行会自动在项目根目录生成 **`my_cloud_db.cnf`**。填完之后：

1. 双击 **`edit_db_config.bat`**（它会用记事本打开该文件；`.cnf` 双击没有默认打开方式，所以做了这个入口）；
2. 把第 1 步记下的 4 个值填在 `=` 后面，保存并关闭记事本；
3. 双击 `deploy_cloud_db.bat`，它会直接读取配置开始导入，不再问任何问题。

> Windows 命令行（尤其老式控制台的密码提示）经常**粘贴不进去**，
> 用记事本填配置完全没有这个问题。

### 备选路径：直接在命令行输入

脚本会依次问你 4 个值（Host / Port / User / Password）；已在配置文件里填过的项
直接回车沿用。密码这一项**支持粘贴、且会明文显示**——这是为了兼容粘贴刻意做的取舍。

配置统一保存在 `my_cloud_db.cnf`，**该文件已在 `.gitignore` 里，不会上传 GitHub**。

然后它自动完成三件事：

1. **连接测试 + 权限预检** —— 连上后会读 `log_bin_trust_function_creators`，
   没开启就提前警告（而不是等建触发器时才报 `ERROR 1419`）；
2. **导入** —— 依次执行 `sql/01` ~ `sql/11`：建库 → 建表 → 视图 → 存储过程 →
   触发器 → 索引 → 测试数据 → 检验/体征 → 预约 → 公告；
3. **校验** —— 导入完立刻连上去数一遍，和源码里定义的对象数量对比：

```
========================================================
云端数据库校验结果
========================================================
  [OK] 表       期望  18  实际  18
  [OK] 视图     期望   9  实际   9
  [OK] 存储过程 期望  16  实际  16
  [OK] 触发器   期望   4  实际   4
  [OK] 患者记录 xx 条，系统账号 x 个
========================================================
数据库已就绪，可以把这套连接信息填到 EdgeOne Makers 的环境变量里了。
```

常用参数：

| 命令 | 作用 |
|---|---|
| `python deploy_cloud_db.py` | 首次填写配置并导入 |
| `python deploy_cloud_db.py --check` | 只测连接与权限，不动数据 |
| `python deploy_cloud_db.py --verify` | 只统计云端库已有对象，核对数量 |
| `python deploy_cloud_db.py --edit` | 重新填写连接信息（换库时用） |
| `python deploy_cloud_db.py --yes` | 跳过确认，直接导入 |

<details>
<summary>不想用脚本？也可以用环境变量手动调用 init_database.py</summary>

```cmd
set HIS_DB_HOST=hospital-db-xxx.aivencloud.com
set HIS_DB_PORT=12345
set HIS_DB_USER=avnadmin
set HIS_DB_PASSWORD=AVNS_xxxxxxxx
set HIS_DB_SSL=1

python init_database.py
```

> PowerShell 写法：`$env:HIS_DB_HOST="hospital-db-xxx.aivencloud.com"`，其余同理。
>
> 也可以直接用命令行参数：
> ```cmd
> python init_database.py --host hospital-db-xxx.aivencloud.com --port 12345 ^
>     --user avnadmin --password AVNS_xxxxxxxx --ssl
> ```
</details>

看到下面这行就成功了：

```
全部脚本执行成功！数据库 hospital_outpatient 已就绪。
测试账号：admin01 / reg01 / cash01 / doctor01，密码均为 123456
```

> 脚本是**幂等**的：中途失败可以修好问题后重跑（01 号脚本会 DROP DATABASE 重建）。

---

## 六、第 3 步：先在本地连云端库验证（强烈建议）

上云前先确认「云端库 + 应用代码」能跑通，可以把问题范围缩小一半。
把第 2 步填过的值再设成环境变量（脚本存的是文件，Web 服务读的是环境变量）：

```cmd
set HIS_DB_HOST=hospital-db-xxx.aivencloud.com
set HIS_DB_PORT=12345
set HIS_DB_USER=avnadmin
set HIS_DB_PASSWORD=AVNS_xxxxxxxx
set HIS_DB_SSL=1

python web_app\app.py
```

浏览器打开 <http://127.0.0.1:5000>，用 `admin01 / 123456` 登录，点几个页面看看。
如果本地就报错，先解决它，不要急着去部署。

**注意**：跨地域访问（本地 → 新加坡）会有几十毫秒延迟，页面点起来比连本地库慢是正常的。

---

## 七、第 4 步：推送到 GitHub

如果你改过 `web_app/` 下的任何代码，**必须先重新生成云函数目录**：

```cmd
python build_edgeone.py
```

然后提交推送：

```cmd
git add .
git commit -m "支持部署到 EdgeOne Pages"
git push origin main
```

> `cloud-functions/` 是 `build_edgeone.py` 自动生成的，**不要手动改里面的文件**。
> 要改功能就改 `web_app/`，然后重跑脚本。

---

## 八、第 5 步：在 EdgeOne Makers 部署


1. 打开控制台 <https://console.tencentcloud.com/edgeone> 登录（微信扫码或腾讯云账号；
   首次使用腾讯云需先完成**实名认证**）。
2. 切换到 **Makers**：
   - 首次登录、账号下没有任何资源 → 会直接进入「**场景选择大厅**」；
   - 账号下已有其他资源 → 点页面**最上方的 `Makers` Tab** 切过去。
3. 鼠标移到 **创建项目** 上，在展开的创建方式里选 **导入 Git 仓库**。

   另外三种是「从模板开始」「直接上传」「创建 Agent」，都不适合本项目。
   尤其**别选「直接上传」**——官方明确说明选了它之后该项目**无法再切换到 Git 集成**，
   以后就没有自动部署了。

4. **连接 GitHub**（只做一次，之后不用重复授权）：
   - 点页面上的 **Github** 图标；
   - 跳转到 GitHub 授权页，点 **Authorize EO Makers**；
   - 选择授权范围：建议选 **Only select repositories**，只勾 `hospital-system`
     （最小权限原则，别把全部仓库都交出去），然后点 **Install**。
5. 回到控制台，**选择仓库** `OFten99/hospital-system`，**分支**选 `main`。
6. **构建配置**（本项目没有前端构建步骤，大部分留空）：
   - **框架预设**：选「其他 / Other」
   - **根目录**：保持默认 `./`
   - **构建命令**：留空
   - **输出目录**：留空或填 `/`

   本项目是「云函数」项目，静态部分为空，所有请求由
   `cloud-functions/[[default]].py` 处理。
7. **加速区域**（要认真选，见下一节）：
   - 「**全球可用区（不含中国大陆）**」→ 绑定自定义域名**免备案**，但国内用户走海外节点；
   - 「**中国大陆可用区**」或「**全球可用区**」→ 国内访问最快，
     但**绑定自定义域名必须完成工信部 ICP 备案**。
8. 点 **开始部署**，等 1~3 分钟，控制台会实时显示构建状态。
9. 部署完成后，在 **构建部署** 菜单或 **项目概览** 界面**右上角点「预览」**，
   生成可访问链接。

---

## 八之二、⚠️ 免费分配的域名在中国大陆有 3 小时时效限制

这一条必须提前知道，否则你会以为部署坏了。官方文档
[域名管理概览](https://pages.edgeone.ai/zh/document/domain-overview) 原文：

> 为保障内容合规，通过**项目域名**及**部署域名**访问 Makers 站点需遵循以下规则：
> 1. **中国大陆网络环境：须使用系统生成的预览链接进行访问，该链接有效期为 3 小时，
>    超时后将返回 401 错误。**可通过控制台「项目概览」界面右上角的「预览」按钮
>    定期更新有效链接。
> 2. 非中国大陆网络环境可直接访问。
> 建议绑定自定义域名以建立稳定访问通道，全球可用区（不含中国大陆）无需备案。
|---|---|
| 直接把平台分配的 `xxx.edgeone.site` 项目域名发给老师 | ❌ **过了 3 小时返回 401**（非中国大陆网络才可直接访问） |
| 演示前在控制台点一次「预览」拿新链接，当场发给老师 | ✅ 可以，但 3 小时内得看完 |
| 绑定自己的域名（选「全球可用区（不含中国大陆）」，免备案） | ✅ 长期稳定可用 |

> 注意：**云函数的运行地域和加速区域是两回事**。即使加速区域选了不含中国大陆，
> 云函数仍然可以部署在广州（默认 `ap-guangzhou`），数据库访问速度不受影响。

---

## 九、第 6 步：配置环境变量（最关键的一步）

这时候打开网址会报数据库连接错误 —— 因为云函数还不知道你的数据库地址。

进入 **项目设置 → 环境变量**，添加下列变量：

| 变量名 | 值 | 必填 |
|---|---|---|
| `HIS_DB_HOST` | `hospital-db-xxx.aivencloud.com` | ✅ |
| `HIS_DB_PORT` | `12345` | ✅ |
| `HIS_DB_USER` | `avnadmin` | ✅ |
| `HIS_DB_PASSWORD` | `AVNS_xxxxxxxx` | ✅ |
| `HIS_DB_NAME` | `hospital_outpatient` | 可选（默认就是这个） |
| `HIS_DB_SSL` | `1` | ✅ **云端必填，漏了连不上** |
| `HIS_SECRET_KEY` | 随便一串长随机字符 | 建议填 |

保存后 **重新部署一次**（环境变量改完必须重新部署才生效，这一步最容易漏）。

> **不想在控制台一个个点？** 也可以用官方 CLI 批量设置：
>
> ```bash
> npm install -g edgeone
> edgeone login                                # 浏览器登录
> edgeone makers link                          # 关联到你的项目（按提示输入项目名）
> edgeone makers env set HIS_DB_HOST "xxxx.aivencloud.com"
> edgeone makers env set HIS_DB_PORT "16806"
> edgeone makers env set HIS_DB_USER "avnadmin"
> edgeone makers env set HIS_DB_PASSWORD "你的密码"
> edgeone makers env set HIS_DB_SSL "1"
> edgeone makers env set HIS_SECRET_KEY "你的随机串"
> edgeone makers env ls                        # 确认都写进去了
> ```
>
> 设完同样要回控制台**重新部署一次**。

> 在 **项目设置 → 函数管理** 里可以选云函数的运行地域。默认为
> 中国大陆 `ap-guangzhou`（广州）、中国大陆以外 `ap-singapore`（新加坡），
> 保持默认即可。本项目数据库在 Aiven 上，跨洋访问每次查询多几十毫秒，
> 页面能正常用，但别指望像本地那么快。

---

## 十、验证与分享

先在控制台 **项目概览 → 右上角「预览」** 生成访问链接
（记住第八之二节说的：中国大陆访问时该链接 **3 小时**后失效、超时返回 401，
届时再点一次「预览」拿新链接即可）。用下列账号登录（密码统一 `123456`）：

| 账号 | 角色 | 可测试功能 |
|---|---|---|
| `admin01` | 管理员 | 全部功能、报表、用户管理、公告 |
| `reg01` | 挂号员 | 患者档案、挂号退号、排班、预约 |
| `doctor01` | 医生 | 接诊、开处方、体征录入、诊断 |
| `cash01` | 收费员 | 收费、退费 |
| `lab01` | 检验技师 | 检验申请与结果 |
| `pharm01` | 药房 | 发药 |
| `regm01` | 自助机 | 自助挂号终端 |

把链接发给老师/同学就能用了（现场演示场景，注意链接有时效）。

---

## 十一、免费额度与已知限制

### 平台技术限制（官方文档明确写的，会直接影响你）

| 内容 | 限制 |
|---|---|
| 云函数代码包大小 | 128 MB（本项目 `cloud-functions/` 约 190 KB，远远够用） |
| 请求 / 响应 body | 6 MB |
| 单次执行时长 | 默认 30 秒，最大可配到 120 秒 |
| Python 运行时版本 | **3.10**（本地用的是 3.12；本项目语法兼容，但别用 3.11+ 才有的新特性） |
| 项目域名中国大陆访问 | **仅 3 小时有效的预览链接**，超时 401（见第八之二节） |

> 免费套餐的流量、构建次数等额度官方会调整，**以控制台里实际显示的为准**，
> 这里不写死数字以免误导。

Aiven 免费 MySQL：1GB 存储、单节点，够课程设计使用。

**需要注意的限制：**

1. **冷启动**：函数闲置一段时间后，首次访问要等 1~3 秒（要重新加载 Python 与依赖）。
   之后就是正常速度。
2. **跨地域延迟**：云函数在广州、数据库在新加坡，每次查询多几十毫秒。
   页面能正常用，但别指望像本地那么快。
3. **会话在 Cookie 里**：登录状态由 Flask 的签名 Cookie 保存，函数无状态也能正常登录，
   不需要额外配置。
4. **Aiven 免费实例会休眠/维护重启**：遇到短暂连不上，等几分钟或去控制台重启实例。

---

## 十二、常见问题排查

### 0. 先看自检页：`/healthz`（所有故障的第一步）

遇到任何报错，**先在浏览器打开 `https://你的域名/healthz`**。它是系统内置的部署自检页，
不需要登录，会直接列出：

- 运行时 Python / Flask / mysql-connector 版本；
- 每个 `HIS_DB_*` 环境变量**是否真的生效**（还是仍在用本地默认值 `127.0.0.1` / `root`），
  未设置的会直接标出「<- 云端应填……」的提示；
- 数据库**真实连接结果**：连上了会显示 server 版本、库名、表数量；
  连不上会显示原始错误（如 `2003 Can't connect to ...`、`1045 Access denied`）。

看到「未设置」或「仍是本地默认值」，那就是环境变量没配或没重新部署，见第九节。

> 顺带：系统的 500 错误页也改成了可读版本，会把异常信息和排查建议直接显示出来
> （原来的页面只有一句「Internal Server Error」，完全没有线索）。
> 完整堆栈会写进日志，在控制台 **项目 → 构建部署 → 函数日志** 里能看到。

### 1. 部署成功但打开是 404

说明平台没有把 `cloud-functions/[[default]].py` 当成「根路径全匹配」的入口。
解决办法（不需要改代码）：

1. 把文件移动成 `cloud-functions/his/[[default]].py`（此时对外路径变成 `/his/...`）；
2. 重新运行 `python build_edgeone.py` 并推送；
3. 在 EdgeOne 项目设置里加环境变量 `HIS_URL_PREFIX` = `/his`；
4. 重新部署，访问 `https://xxxx.edgeone.site/his`。

> 代码里已经预留了这个前缀支持（见 `web_app/app.py` 顶部的 URL 前缀小节），
> 不设这个变量就是普通的根路径模式。

### 2. 页面报数据库连接错误

**先开 `/healthz`**，它会直接告诉你哪个变量没生效、数据库报的什么错。然后按顺序检查：

- `HIS_DB_SSL` 是不是 `1`？（云端强制 TLS，这是最常见的漏配）
- Host / Port 是否照抄 Aiven 的值？**Port 不是 3306**。
- 改完环境变量**有没有重新部署**？
- Aiven 实例是否 Running？

### 3. 导入 SQL 时报 `ERROR 1419`

见第四步第 6 点：把 Aiven 的 `log_bin_trust_function_creators` 设为 `1`，等生效后重跑。

### 4. 导入时卡住 / 超时

跨国网络抖动，重跑即可（脚本幂等）。如果反复失败，把 Aiven 区域换成
香港或新加坡，或在网络更稳定的环境执行。

### 5. 构建日志报依赖安装失败

确认 `cloud-functions/requirements.txt` 存在且内容为：

```
Flask>=3.0.0
mysql-connector-python>=8.3.0
```

如果确实装不上 `mysql-connector-python`，可以改用纯 Python 的 `PyMySQL`
（需要同步修改 `db.py` 的 import 与连接参数），这种情况把构建日志发给 AI 助手处理。

### 6. 首次访问很慢

冷启动的正常现象，见第十一节第 1 点。

---

## 十三、安全注意事项

1. **数据库口令只放在平台的环境变量里**，绝对不要写进代码或提交到 GitHub。
   本仓库的 `.gitignore` 已排除 `my_his.cnf`（本地库口令）与 `my_cloud_db.cnf`
   （云端库口令），这两个文件都不会被 push。
2. 建议设置 `HIS_SECRET_KEY` 为随机字符串，避免使用代码里的默认值（否则别人可以伪造登录 Cookie）。
3. 这是课程设计演示系统，被测数据都是模拟数据；但**不要**把真实的患者信息录进去。
4. 公开网址后，任何人都能访问登录页。如果担心被乱改数据，可以：
   - 在 Aiven 控制台做定期备份；
   - 或者部署完演示完就下线项目。

---

## 十四、以后改代码怎么更新

```cmd
rem 1. 改 web_app/ 下的源码
rem 2. 重新生成云函数目录
python build_edgeone.py

rem 3. 提交推送，EdgeOne 会自动重新构建部署
git add .
git commit -m "更新说明"
git push origin main
```

大概 1~2 分钟后线上就更新了。
