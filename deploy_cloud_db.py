"""
医院门诊管理系统 —— 云端数据库一键部署脚本

作用：把 sql/ 下的 11 个脚本（建库 → 建表 → 视图 → 存储过程 → 触发器 →
      索引 → 测试数据 → 检验科/体征 → 任务书补全 → 预约 → 公告）
      一次性导入到一台「公网可访问的托管 MySQL」上，供云端 Web 服务连接。

为什么需要它：本地 MySQL 长在你自己电脑上，别人访问不到；
      想让别人在网页上使用你的系统，数据库也得搬到公网。

用法（推荐直接双击 deploy_cloud_db.bat）：
    python deploy_cloud_db.py            # 首次运行会引导你填写连接信息
    python deploy_cloud_db.py --edit     # 重新填写连接信息
    python deploy_cloud_db.py --check    # 只测试连接和权限，不导入
    python deploy_cloud_db.py --verify   # 只统计云端库里已有哪些对象
    python deploy_cloud_db.py --yes      # 跳过确认，直接导入（会先 DROP DATABASE）

连接信息保存在同目录的 my_cloud_db.cnf（已在 .gitignore 中，不会上传 GitHub）。
"""

import argparse
import re
import sys
from getpass import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONF_PATH = ROOT / "my_cloud_db.cnf"
SQL_DIR = ROOT / "sql"

# 期望的数据对象数量（自动从 sql/ 目录统计，不写死，方便日后加脚本）
OBJECT_PATTERNS = {
    "表": ("TABLE", r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`]?([A-Za-z0-9_]+)"),
    "视图": ("VIEW", r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+[`]?([A-Za-z0-9_]+)"),
    "存储过程": ("PROCEDURE", r"CREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\s+[`]?([A-Za-z0-9_]+)"),
    "触发器": ("TRIGGER", r"CREATE\s+(?:OR\s+REPLACE\s+)?TRIGGER\s+[`]?([A-Za-z0-9_]+)"),
}

DB_NAME = "hospital_outpatient"

CONF_TEMPLATE = """# 云端 MySQL 连接配置（医院门诊管理系统）
# 本文件已被 .gitignore 忽略，不会上传到 GitHub，可以放心填密码。
#
# Aiven 控制台 → 你的 MySQL 服务 → Overview → Connection information
# 可以找到 host / port / user / password（主机名形如 xxx.aivencloud.com）。
#
# 每行一个「键=值」，不要加引号。改完保存，直接重新运行脚本即可。
host=
port=
user=
password=
# 云端托管 MySQL 基本都强制 TLS，保持 1 即可；本地库填 0
ssl=1
"""


# ------------------------------------------------------------------
# 配置读写
# ------------------------------------------------------------------

def load_conf(required=True):
    """读取 my_cloud_db.cnf；不存在或字段不全时返回 None。"""
    if not CONF_PATH.exists():
        return None
    conf = {}
    for line in CONF_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        conf[key.strip().lower()] = value.strip()

    if required and not (conf.get("host") and conf.get("port") and conf.get("user")):
        return None
    return conf


def save_conf(conf):
    """把配置写回 my_cloud_db.cnf（保留注释模板，只替换值）。"""
    lines = CONF_TEMPLATE.splitlines()
    out = []
    written = set()
    for line in lines:
        stripped = line.strip().lower()
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=", 1)[0].strip().lower()
            if key in conf:
                out.append(f"{key}={conf[key]}")
                written.add(key)
                continue
        out.append(line)
    for key, value in conf.items():
        if key not in written:
            out.append(f"{key}={value}")
    CONF_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def _read_secret(prompt):
    """
    读取密码，输入时不回显。

    注意：getpass 在没有真实控制台（例如输出被管道 / 重定向接走）时会去打开
    /dev/tty，而那里读不到数据就会**一直卡住**。所以先判断 stdin 是不是终端：
    是终端就用 getpass 隐藏输入，不是就退回普通 input（此时输入本来也看不见）。
    """
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            return getpass(prompt)
    except Exception:
        pass
    return input(prompt)


def ask_conf(existing=None):
    """交互式收集连接信息。"""
    existing = existing or {}
    print()
    print("请填写云端 MySQL 的连接信息")
    print("在 Aiven 控制台 → 你的服务 → Overview → Connection information 里可以找到")
    print()

    def ask(prompt, key, secret=False):
        old = existing.get(key, "")
        if secret:
            # 密码一律隐藏回显，避免被旁边的人看到
            hint = "（直接回车沿用已保存的密码）" if old else ""
            raw = _read_secret(f"{prompt}{hint} ").strip()
        else:
            default = f"[{old}] " if old else ""
            raw = input(f"{prompt} {default}").strip()
        return raw or old

    conf = {
        "host": ask("[1/4] 主机 Host:", "host"),
        "port": ask("[2/4] 端口 Port（不是 3306）:", "port"),
        "user": ask("[3/4] 用户名 User:", "user"),
        "password": ask("[4/4] 密码 Password:", "password", secret=True),
        "ssl": existing.get("ssl", "1") or "1",
    }
    if not conf["host"] or not conf["port"] or not conf["user"]:
        print()
        print("[错误] host / port / user 不能为空。")
        sys.exit(1)

    # 回显一遍（密码打码），方便肉眼核对有没有复制错
    masked = "*" * 8 if conf.get("password") else "（空）"
    print()
    print("-" * 56)
    print(f"  主机：{conf['host']}")
    print(f"  端口：{conf['port']}")
    print(f"  账号：{conf['user']}")
    print(f"  密码：{masked}")
    print(f"  TLS ：{'启用' if conf['ssl'] in ('1', 'true', 'yes', 'on') else '关闭'}")
    print("-" * 56)

    if conf["host"].endswith("aivencloud.com") and conf["port"] == "3306":
        print()
        print("[警告] Aiven 的端口不是 3306，请从控制台重新复制（通常是 5 位随机数字）。")

    save_conf(conf)
    print()
    print(f"[已保存] 配置写入 {CONF_PATH.name}（该文件不会上传 GitHub）")
    return conf


# ------------------------------------------------------------------
# 连接
# ------------------------------------------------------------------

def build_conn_kwargs(conf, with_database=False):
    kwargs = {
        "host": conf["host"],
        "port": int(conf["port"]),
        "user": conf["user"],
        "password": conf.get("password", ""),
        "charset": "utf8mb4",
        "use_unicode": True,
        "connection_timeout": 15,
    }
    if with_database:
        kwargs["database"] = DB_NAME
    if str(conf.get("ssl", "1")).strip().lower() in ("1", "true", "yes", "on"):
        kwargs["ssl_disabled"] = False
        kwargs["ssl_verify_cert"] = False
    return kwargs


def connect(conf, with_database=False):
    import mysql.connector
    return mysql.connector.connect(**build_conn_kwargs(conf, with_database))


def test_connection(conf):
    """连接测试 + 权限预检。返回 True 表示可以继续导入。"""
    try:
        conn = connect(conf)
    except Exception as exc:
        print()
        print(f"[失败] 无法连接 {conf['user']}@{conf['host']}:{conf['port']}")
        print(f"        {exc}")
        print()
        print("排查方向：")
        print("  1. host / port 是否从 Aiven 控制台原样复制（端口通常是 5 位随机数）；")
        print("  2. 用户名是否为 avnadmin，密码是否复制完整（不要带首尾空格）；")
        print("  3. 实例状态是否为 Running（刚创建时要等 1~3 分钟）；")
        print("  4. ssl 是否为 1（Aiven 强制 TLS，填 0 会握手失败）。")
        return False

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT VERSION(), CURRENT_USER()")
        version, current_user = cursor.fetchone()
        print()
        print(f"[连接成功] 服务端版本 {version}")
        print(f"           当前账号 {current_user}")

        # 关键预检：binlog 开启时，普通账号建触发器会报 ERROR 1419
        cursor.execute("SHOW VARIABLES LIKE 'log_bin'")
        row = cursor.fetchone()
        log_bin = (row[1] if row else "?").upper()
        cursor.execute("SHOW VARIABLES LIKE 'log_bin_trust_function_creators'")
        row = cursor.fetchone()
        trust = (row[1] if row else "?").upper()

        needs_trigger = _expected_counts().get("触发器", 0) > 0
        if needs_trigger and log_bin in ("ON", "1") and trust in ("OFF", "0"):
            print()
            print("[警告] 该实例开启了 binlog，但 log_bin_trust_function_creators = OFF。")
            print("       导入到 05_triggers.sql 时会报 ERROR 1419（缺少 SUPER 权限）。")
            print("       请先去控制台把它设为 1：")
            print("       Aiven → 你的服务 → Advanced configuration →")
            print("       搜索 mysql.log_bin_trust_function_creators → 填 1 → 保存并等生效。")
            print("       改完重新运行本脚本即可。")
        else:
            print(f"[预检通过] log_bin={log_bin}  "
                  f"log_bin_trust_function_creators={trust}")

        cursor.execute("SHOW DATABASES LIKE %s", (DB_NAME,))
        if cursor.fetchone():
            print(f"[注意] 云端已存在同名库 {DB_NAME}，导入时会先 DROP 掉重建。")
        cursor.close()
        return True
    finally:
        conn.close()


# ------------------------------------------------------------------
# 导入
# ------------------------------------------------------------------

def _expected_counts():
    """
    从 sql/ 目录统计期望的数据对象数量。

    注意：必须按「对象名去重」再计数，不能数 CREATE 语句的出现次数。
    因为 09~11 号补丁脚本会重建前面脚本里已存在的同名视图 / 存储过程，
    按出现次数统计会虚高（实测会多出 2 个视图、2 个存储过程）。
    """
    names = {label: set() for label in OBJECT_PATTERNS}
    if not SQL_DIR.exists():
        return {label: 0 for label in OBJECT_PATTERNS}
    for sql_file in sorted(SQL_DIR.glob("*.sql")):
        text = sql_file.read_text(encoding="utf-8-sig", errors="ignore")
        for label, (_, pattern) in OBJECT_PATTERNS.items():
            for match in re.finditer(pattern, text, re.IGNORECASE):
                names[label].add(match.group(1).lower())
    return {label: len(found) for label, found in names.items()}


def run_import(conf):
    """复用 init_database.py 的导入逻辑（通过环境变量传参）。"""
    import os
    os.environ["HIS_DB_HOST"] = conf["host"]
    os.environ["HIS_DB_PORT"] = str(conf["port"])
    os.environ["HIS_DB_USER"] = conf["user"]
    os.environ["HIS_DB_PASSWORD"] = conf.get("password", "")
    if str(conf.get("ssl", "1")).strip().lower() in ("1", "true", "yes", "on"):
        os.environ["HIS_DB_SSL"] = "1"
    else:
        os.environ.pop("HIS_DB_SSL", None)

    sys.path.insert(0, str(ROOT))
    import init_database

    argv_backup = sys.argv[:]
    sys.argv = ["init_database.py"]        # 避免 argparse 解析到本脚本的参数
    try:
        init_database.main()
    finally:
        sys.argv = argv_backup


# ------------------------------------------------------------------
# 校验
# ------------------------------------------------------------------

def verify(conf):
    """连上去统计实际存在的数据对象，与期望值对比。"""
    expected = _expected_counts()
    try:
        conn = connect(conf, with_database=True)
    except Exception as exc:
        print(f"[失败] 无法连接到 {DB_NAME}：{exc}")
        return False

    try:
        cursor = conn.cursor()

        def count(sql, args=()):
            cursor.execute(sql, args)
            return cursor.fetchone()[0]

        actual = {
            "表": count(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema=%s AND table_type='BASE TABLE'", (DB_NAME,)),
            "视图": count(
                "SELECT COUNT(*) FROM information_schema.views "
                "WHERE table_schema=%s", (DB_NAME,)),
            "存储过程": count(
                "SELECT COUNT(*) FROM information_schema.routines "
                "WHERE routine_schema=%s AND routine_type='PROCEDURE'", (DB_NAME,)),
            "触发器": count(
                "SELECT COUNT(*) FROM information_schema.triggers "
                "WHERE trigger_schema=%s", (DB_NAME,)),
        }

        print()
        print("=" * 56)
        print("云端数据库校验结果")
        print("=" * 56)
        all_ok = True
        for label in OBJECT_PATTERNS:
            exp, act = expected.get(label, 0), actual[label]
            flag = "[OK]" if exp == act else "[!!]"
            if exp != act:
                all_ok = False
            print(f"  {flag} {label:<8} 期望 {exp:>3}  实际 {act:>3}")

        # 抽查一条业务数据，确认种子数据也灌进去了
        cursor.execute("SELECT COUNT(*) FROM patient")
        patients = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users")
        users = cursor.fetchone()[0]
        print(f"  [OK] 患者记录 {patients} 条，系统账号 {users} 个")
        print("=" * 56)
        cursor.close()

        if all_ok:
            print("数据库已就绪，可以把这套连接信息填到 EdgeOne Pages 的环境变量里了。")
        else:
            print("数量有出入：如果「触发器」对不上，多半是 log_bin_trust_function_creators")
            print("没设为 1；设好后重新运行本脚本即可（脚本可重复执行）。")
        return all_ok
    finally:
        conn.close()


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="医院门诊管理系统：云端数据库一键部署")
    parser.add_argument("--edit", action="store_true", help="重新填写连接信息")
    parser.add_argument("--check", action="store_true", help="只测试连接与权限，不导入")
    parser.add_argument("--verify", action="store_true", help="只统计云端库已有对象")
    parser.add_argument("--yes", action="store_true", help="跳过确认直接导入")
    args = parser.parse_args()

    print("=" * 56)
    print("医院门诊管理系统 —— 云端数据库一键部署")
    print("=" * 56)

    if args.verify:
        conf = load_conf()
        if not conf:
            print("[错误] 还没有配置。先运行 python deploy_cloud_db.py 填写连接信息。")
            sys.exit(1)
        sys.exit(0 if verify(conf) else 1)

    if not SQL_DIR.exists():
        print(f"[错误] 找不到 sql 目录：{SQL_DIR}")
        sys.exit(1)

    conf = None if args.edit else load_conf()
    if not conf:
        print()
        print("没有找到 my_cloud_db.cnf（或内容不完整），先填写连接信息。")
        print("如果你还没创建云端 MySQL，请先按 docs/部署到公网_EdgeOne.md 第 1~2 节")
        print("注册 Aiven 免费 MySQL（约 5 分钟，无需信用卡）。")
        conf = ask_conf(conf)

    if not test_connection(conf):
        sys.exit(1)

    if args.check:
        print()
        print("连接与权限检查完成（--check 模式，未执行导入）。")
        return

    if not args.yes:
        print()
        print("-" * 56)
        print("接下来会把 sql/ 下 11 个脚本导入云端，")
        print(f"注意：01 号脚本会先 DROP DATABASE {DB_NAME} 再重建。")
        answer = input("确认继续？输入 y 回车：").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消，未做任何改动。")
            return

    print()
    print("-" * 56)
    run_import(conf)
    verify(conf)


if __name__ == "__main__":
    main()
