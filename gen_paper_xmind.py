# -*- coding: utf-8 -*-
"""
生成《医院门诊信息系统的设计与实现》论文内容 XMind 思维导图。
内容覆盖论文第一~三章：系统需求分析、系统总体设计、系统详细实现与界面展示，以及图表清单。
（.xmind 为 zip 包：content.json + metadata.json + manifest.json，兼容 XMind 8/2020 及 WPS 打开）
"""
import json
import uuid
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

OUT = Path(__file__).resolve().parent / "docs" / "医院门诊信息系统论文内容.xmind"


def tid():
    return str(uuid.uuid4())


def topic(title, children=None, note=None):
    t = {"id": tid(), "title": title}
    if note:
        t["notes"] = {"plain": {"content": note}}
    if children:
        t["children"] = {"attached": children}
    return t


def sheet(title, root):
    return {
        "id": tid(),
        "class": "sheet",
        "title": title,
        "rootTopic": root,
    }


def build():
    root = topic("医院门诊信息系统的设计与实现", children=[
        # ===================== 第1章 =====================
        topic("第一章 系统需求分析", children=[
            topic("1.1 功能性需求分析", children=[
                topic("1.1.1 患者端功能需求", children=[
                    topic("身份识别与建档：身份证/手机号识别，未建档现场建档，再次就诊直接识别"),
                    topic("当日挂号与预约挂号：查排班→选科室医生号别→挂号生成排队号；预约未来排班生成预约流水号与挂号时限，当日取号转正式挂号，逾期作废释放号源"),
                    topic("信息查询：医生排班、历史就诊记录、缴费记录、检验报告单"),
                ]),
                topic("1.1.2 医生端功能需求", children=[
                    topic("查看本人当日就诊队列，按序开始接诊（待就诊→就诊中）"),
                    topic("查看患者病历、体征与检验结果"),
                    topic("录入诊断结果，开具处方"),
                    topic("完成就诊（就诊中→已就诊）"),
                    topic("数据安全：只能操作本人名下挂号与本人开具处方"),
                ]),
                topic("1.1.3 管理员端功能需求", children=[
                    topic("用户管理：账号、角色与科室归属维护"),
                    topic("科室管理：科室字典新增、编辑、删除"),
                    topic("医生排班管理：排班维护与号源管理"),
                    topic("挂号管理：挂号与退号处理"),
                    topic("数据统计：门诊运营看板、科室挂号统计"),
                    topic("公告管理：公告发布与删除"),
                    topic("权限管理：不同角色功能菜单配置"),
                ]),
            ]),
            topic("1.2 非功能性需求分析", children=[
                topic("系统安全性：口令 SHA-256 哈希存储、参数化查询防 SQL 注入、按角色划分功能权限"),
                topic("系统稳定性：核心业务走存储过程与事务，号源、就诊状态由数据库约束兜底"),
                topic("系统易用性：统一后台风格、菜单按角色动态显示、步骤引导"),
                topic("系统可扩展性：三层 B/S 架构、业务模块独立拆分、可平滑接入外部系统"),
                topic("响应速度：单次请求秒级完成，支持挂号高峰期多终端并发"),
            ]),
            topic("1.3 可行性分析", children=[
                topic("技术可行性：Python + Flask + MySQL 开源成熟，B/S 零安装部署"),
                topic("经济可行性：技术栈免费开源，普通 PC 即可部署"),
                topic("操作可行性：流程与门诊习惯一致，自助终端身份证识别降低使用门槛"),
                topic("法律可行性：自主开发，患者信息仅用于门诊业务，符合个人信息保护要求"),
            ]),
            topic("1.4 业务流程分析", children=[
                topic("1.4.1 门诊挂号流程（图1-1）", children=[
                    topic("身份识别 → 未建档先建档 → 选科室/医生/号别 → 确认并缴费 → 生成挂号单与排队号 → 候诊"),
                ]),
                topic("1.4.2 患者就诊流程（图1-2）", children=[
                    topic("候诊叫号 → 接诊（待就诊→就诊中）→ 问诊检查 → 开方/检验 → 缴费取药或检查 → 完成就诊"),
                ]),
                topic("1.4.3 医生接诊开单流程（图1-3）", children=[
                    topic("查看就诊队列 → 开始接诊 → 查看病历体征 → 录入诊断 → 开具处方提交 → 缴费发药后完成就诊"),
                ]),
                topic("1.4.4 管理员后台管理流程（图1-4）", children=[
                    topic("登录 → 权限校验 → 选择管理模块增删改查 → 保存至数据库 → 看板查看运行情况 → 退出"),
                ]),
            ]),
            topic("1.5 系统用例分析", children=[
                topic("1.5.1 患者用例图（图1-5）", children=[
                    topic("身份识别与建档 / 当日挂号 / 提前预约挂号 / 查看医生排班 / 查看就诊记录 / 缴费查询 / 查看检验报告单"),
                ]),
                topic("1.5.2 医生用例图（图1-6）", children=[
                    topic("登录 / 查看当日就诊队列 / 开始接诊 / 开具处方 / 录入诊断与检验申请 / 查看病历与检验结果 / 完成就诊"),
                ]),
                topic("1.5.3 管理员用例图（图1-7）", children=[
                    topic("登录 / 用户管理 / 科室管理 / 医生排班管理 / 挂号管理 / 数据统计 / 公告管理 / 权限管理"),
                ]),
            ]),
        ]),

        # ===================== 第2章 =====================
        topic("第二章 系统总体设计", children=[
            topic("2.1 系统总体架构设计", children=[
                topic("2.1.1 B/S 架构概述（图2-1）", children=[
                    topic("表现层：管理员后台 / 医生工作站 / 挂号收费窗口 / 自助挂号终端 / 药房检验工作站"),
                    topic("业务逻辑层：Flask Web 服务（八功能模块）"),
                    topic("数据层：MySQL 统一存储业务数据"),
                    topic("交互：HTTP 请求 + SQL 读写，层次清晰职责分明"),
                ]),
                topic("2.1.2 系统功能模块架构", children=[
                    topic("用户与权限管理 / 挂号预约管理 / 医生接诊管理 / 收费退费管理"),
                    topic("检验检查管理 / 药房发药管理 / 科室与公告管理 / 数据统计报表"),
                ]),
            ]),
            topic("2.2 核心功能模块设计", children=[
                topic("2.2.1 用户与权限管理模块", children=[
                    topic("角色-权限映射表 PERMISSIONS 控制菜单访问"),
                    topic("用户名+密码哈希校验，会话保存角色与基础信息"),
                    topic("权限装饰器校验，无权限访问拦截跳转"),
                ]),
                topic("2.2.2 患者挂号就诊模块", children=[
                    topic("建档/当日挂号/预约挂号全流程"),
                    topic("预约流水号与挂号时限（上午 11:00 前、下午 17:00 前取号）"),
                    topic("就诊状态：待就诊→就诊中→已就诊/已退号"),
                ]),
                topic("2.2.3 医生接诊处方模块", children=[
                    topic("本人当日就诊队列、按号接诊"),
                    topic("诊断录入、处方提交，金额由存储过程统一计算"),
                    topic("处方状态机：待收费→已收费→已发药，支持作废退费"),
                ]),
                topic("2.2.4 科室与排班管理模块", children=[
                    topic("科室字典（编码/名称/说明）供挂号选择引用"),
                    topic("排班设置出诊时段/诊室/号源上限，挂号预约实时扣减"),
                ]),
                topic("2.2.5 收费退费与药房模块", children=[
                    topic("三类费用：挂号费/药品费/检查费；四种支付：现金/微信/支付宝/医保"),
                    topic("收费同步更新挂号、处方与检验单状态"),
                    topic("药房按已收费处方发药并扣减库存"),
                ]),
                topic("2.2.6 数据统计与公告管理模块", children=[
                    topic("运营看板：今日挂号数/已就诊数/待收费处方数/收费金额/科室挂号统计"),
                    topic("公告发布与删除，首页与公告页面向全体展示"),
                ]),
            ]),
            topic("2.3 数据库设计", children=[
                topic("2.3.1 概念结构设计（图2-2 E-R）", children=[
                    topic("核心实体：患者/医生/科室/系统用户/挂号单/处方/公告（7 实体）"),
                    topic("主要联系：科室-医生 1:n、用户-医生 1:1、医生-挂号 1:n、患者-挂号 1:n、患者-处方 1:n、医生-处方 1:n、挂号-处方 1:1、用户-公告 1:n"),
                ]),
                topic("2.3.2 逻辑结构设计（表2-1~2-7）", children=[
                    topic("表2-1 用户表 users（12 列）"),
                    topic("表2-2 患者表 patients（11 列）"),
                    topic("表2-3 医生表 doctors（8 列）"),
                    topic("表2-4 科室表 departments（5 列）"),
                    topic("表2-5 挂号记录表 registrations（12 列，含排队号/就诊状态/挂号员）"),
                    topic("表2-6 处方表 prescriptions（9 列，状态机）"),
                    topic("表2-7 公告表 announcements（7 列）"),
                    topic("其他：预约表/缴费表/药品表/检验相关表，外键关联构成完整数据模型"),
                ]),
                topic("2.3.3 数据库访问与安全设计", children=[
                    topic("mysql.connector 参数化查询防 SQL 注入"),
                    topic("号源扣减、金额计算、退号处理封装存储过程，事务保证一致性"),
                    topic("数据库最小授权 + 应用层功能权限纵深防御"),
                ]),
            ]),
        ]),

        # ===================== 第3章 =====================
        topic("第三章 系统详细实现与界面展示", children=[
            topic("3.1 开发与运行环境（表3-1）", children=[
                topic("操作系统：Windows 10/11；语言：Python 3.12"),
                topic("Web 框架：Flask（Jinja2 模板、Werkzeug）；数据库：MySQL 8.0"),
                topic("前端：HTML5 / CSS3 / JavaScript；浏览器：Chrome/Edge"),
                topic("运行方式：本地 Flask 开发服务器（127.0.0.1:5000）"),
            ]),
            topic("3.2 登录功能实现（图3-1）", children=[
                topic("SHA-256 哈希比对 + 账号状态校验"),
                topic("会话保存角色信息并按角色跳转"),
                topic("核心代码：登录校验"),
            ]),
            topic("3.3 患者挂号与排班查询实现（图3-2、图3-3）", children=[
                topic("当日挂号与提前预约两种方式"),
                topic("sp_create_appointment 生成预约流水号（APT+时间戳）与挂号时限"),
                topic("自助终端：就诊记录/缴费记录/检验报告单查询"),
                topic("核心代码：预约创建"),
            ]),
            topic("3.4 医生接诊与处方实现（图3-4）", children=[
                topic("页面顶部本人当日就诊队列"),
                topic("开始接诊：待就诊→就诊中；完成就诊：就诊中→已就诊"),
                topic("条件更新语句保证状态顺序流转"),
                topic("核心代码：接诊与完成就诊"),
            ]),
            topic("3.5 管理员后台管理实现（图3-5、3-6、3-7）", children=[
                topic("运营看板：最新公告 + 今日门诊数据概览 + 科室挂号统计"),
                topic("公告管理：发布新公告、删除公告，同步首页公告栏"),
                topic("科室管理：新增、行内编辑、查看医生数与用户数"),
                topic("核心代码：公告发布"),
            ]),
            topic("3.6 核心代码展示", children=[
                topic("登录校验核心代码（CODE_LOGIN）"),
                topic("权限控制核心代码（CODE_PERM）"),
                topic("预约挂号核心代码（CODE_APPT）"),
                topic("接诊与完成就诊核心代码（CODE_VISIT）"),
                topic("公告发布核心代码（CODE_ANN）"),
            ]),
            topic("参考文献", children=[
                topic("[1] Flask Documentation（Pallets Projects）"),
                topic("[2] MySQL 8.0 Reference Manual（Oracle）"),
                topic("[3] Python 3.12 Documentation"),
                topic("[4] 王珊, 萨师煊. 数据库系统概论. 第6版. 高等教育出版社, 2023"),
                topic("[5] 张海藩, 牟永敏. 软件工程导论. 第6版. 清华大学出版社, 2013"),
            ]),
        ]),

        # ===================== 图表清单 =====================
        topic("论文图表清单", children=[
            topic("图：共 16 张", children=[
                topic("第1章：图1-1~1-4 业务流程、图1-5~1-7 用例图"),
                topic("第2章：图2-1 总体架构、图2-2 E-R 图"),
                topic("第3章：图3-1~3-7 系统界面截图"),
            ]),
            topic("表：共 8 张", children=[
                topic("第2章：表2-1~2-7 核心数据表结构"),
                topic("第3章：表3-1 开发与运行环境"),
            ]),
            topic("素材位置", children=[
                topic("论文 Word：论文素材/医院门诊信息系统的设计与实现（第一至三章）.docx"),
                topic("图表 PNG：论文素材/fig/_shots/、论文素材/screens/"),
            ]),
        ]),
    ])

    content = [sheet("医院门诊信息系统论文内容", root)]
    metadata = {
        "creator": {"name": "Doubao Agent", "version": "1.0.0"},
    }
    manifest = [
        {"file-path": "content.json", "content": "content"},
        {"file-path": "metadata.json", "content": "metadata"},
    ]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(OUT, "w", ZIP_DEFLATED) as zf:
        zf.writestr("content.json", json.dumps(content, ensure_ascii=False, indent=2))
        zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    print("已生成：%s" % OUT)


if __name__ == "__main__":
    build()
