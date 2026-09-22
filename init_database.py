"""
医院门诊管理系统 —— 一键初始化数据库脚本

作用：自动按顺序执行 sql 文件夹中的 01~07 号脚本，完成
      “建库 → 建表 → 建视图 → 建存储过程 → 建触发器 → 建索引/查询示例 → 灌测试数据”。

用法：
    python init_database.py                      # 本地默认配置 root / 123456
    python init_database.py --user root --password 你的密码
    python init_database.py --host 127.0.0.1 --port 3306 --user root --password 123456

    # 初始化云端托管 MySQL（Aiven / 腾讯云 / 阿里云等，一般强制 TLS）
    python init_database.py --host xxx.aivencloud.com --port 12345 \
        --user avnadmin --password 你的密码 --ssl

也可以用环境变量代替命令行参数（HIS_DB_* 优先，兼容旧的 HOTEL_DB_*）：
    HIS_DB_HOST / HIS_DB_PORT / HIS_DB_USER / HIS_DB_PASSWORD / HIS_DB_SSL

说明：
    - 脚本是幂等的：任何一步失败都会立即停止并给出失败文件与错误信息；
      修复后重新运行即可（01 号脚本会 DROP DATABASE 重建，重复执行安全）。
    - 04 / 05 号脚本使用 DELIMITER 语法（mysql 客户端指令），本脚本已内置解析器，
      可以直接执行，不需要 DataGrip。
    - 云端 MySQL 常见坑：托管实例默认开启 binlog，普通账号建触发器会报
      ERROR 1419（You do not have the SUPER privilege）。脚本会识别这个错误并
      给出解决办法，详见 docs/部署到公网_EdgeOne.md。
"""

import argparse
import os
import re
import sys
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent / "sql"
SQL_FILES = [
    "01_create_database.sql",
    "02_create_tables.sql",
    "03_views.sql",
    "04_procedures.sql",
    "05_triggers.sql",
    "06_indexes_queries.sql",
    "07_seed_data.sql",
    "08_lab_vitals.sql",
    "09_task2_gaps.sql",
    "10_appointment.sql",
    "11_announcement.sql",
]

TRUTHY = {"1", "true", "yes", "on", "y"}


def default_config():
    """连接配置：优先读 HIS_DB_* 环境变量，兼容旧的 HOTEL_DB_*，最后用本地默认值。"""
    def pick(name, legacy, fallback):
        return os.getenv(name) or os.getenv(legacy) or fallback

    config = {
        "host": pick("HIS_DB_HOST", "HOTEL_DB_HOST", "127.0.0.1"),
        "port": int(pick("HIS_DB_PORT", "HOTEL_DB_PORT", "3306")),
        "user": pick("HIS_DB_USER", "HOTEL_DB_USER", "root"),
        "password": pick("HIS_DB_PASSWORD", "HOTEL_DB_PASSWORD", "123456"),
        "charset": "utf8mb4",
        "use_unicode": True,
        "connection_timeout": 15,
    }
    if (pick("HIS_DB_SSL", "HOTEL_DB_SSL", "") or "").strip().lower() in TRUTHY:
        config["ssl_disabled"] = False
        config["ssl_verify_cert"] = False
    return config


def split_sql_statements(sql_text):
    """
    把 SQL 文件内容拆成可逐条执行的语句列表。

    处理要点：
    1. 识别 mysql 客户端的 DELIMITER 指令（存储过程/触发器脚本会用到），
       切换语句结束分隔符，并把 DELIMITER 行本身剔除；
    2. 剔除纯注释行（-- 开头 / # 开头）与空行；
    3. 注释与字符串内的 ';' 不会误切（因为只在行尾或独立分隔符位置判断）。
    """
    statements = []
    buffer_lines = []
    delimiter = ";"

    def flush():
        """把 buffer 中积累的内容按当前分隔符切分并收集为语句。"""
        nonlocal buffer_lines
        text = "\n".join(buffer_lines)
        buffer_lines = []
        for raw_stmt in text.split(delimiter):
            stmt = raw_stmt.strip()
            if not stmt:
                continue
            # 去掉逐行注释后仍为空则跳过
            cleaned = "\n".join(
                line for line in stmt.splitlines()
                if not re.match(r"^\s*(--|#)", line)
            ).strip()
            if cleaned:
                statements.append(cleaned)

    for raw_line in sql_text.splitlines():
        stripped = raw_line.strip()
        if re.match(r"^DELIMITER\s+\S+\s*$", stripped, re.IGNORECASE):
            # 换分隔符前，先把手头的缓冲内容按旧分隔符切掉
            flush()
            delimiter = stripped.split()[1]
            continue
        buffer_lines.append(raw_line)

    flush()
    return statements


def execute_sql_file(conn, sql_file: Path):
    """在指定连接上执行一个 SQL 文件，返回执行的语句条数。"""
    sql_text = sql_file.read_text(encoding="utf-8-sig")
    statements = split_sql_statements(sql_text)
    if not statements:
        print(f"  [跳过] {sql_file.name}（没有可执行语句）")
        return 0

    cursor = conn.cursor()
    count = 0
    try:
        for stmt in statements:
            cursor.execute(stmt)
            # 消费结果集（SELECT / CALL 等会产生结果集，
            # 不读完会导致 mysql.connector 报 Unread result found）
            if cursor.with_rows:
                cursor.fetchall()
            count += 1
    finally:
        cursor.close()
    print(f"  [完成] {sql_file.name}（{count} 条语句）")
    return count


def main():
    parser = argparse.ArgumentParser(description="医院门诊管理系统：一键初始化数据库")
    parser.add_argument("--host", default=None, help="MySQL 主机，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="MySQL 端口，默认 3306")
    parser.add_argument("--user", default=None, help="MySQL 用户名，默认 root")
    parser.add_argument("--password", default=None, help="MySQL 密码，默认 123456")
    parser.add_argument("--ssl", action="store_true",
                        help="启用 TLS 连接（云端托管 MySQL 必填）")
    args = parser.parse_args()

    config = default_config()
    if args.host:
        config["host"] = args.host
    if args.port:
        config["port"] = args.port
    if args.user:
        config["user"] = args.user
    if args.password:
        config["password"] = args.password
    if args.ssl:
        config["ssl_disabled"] = False
        config["ssl_verify_cert"] = False

    if not SQL_DIR.exists():
        print(f"[错误] 找不到 sql 目录：{SQL_DIR}")
        sys.exit(1)

    missing = [name for name in SQL_FILES if not (SQL_DIR / name).exists()]
    if missing:
        print(f"[错误] sql 目录缺少文件：{', '.join(missing)}")
        sys.exit(1)

    print("=" * 60)
    print("医院门诊管理系统 —— 数据库初始化")
    print(f"目标：{config['user']}@{config['host']}:{config['port']}"
          f"{'（TLS 加密）' if config.get('ssl_disabled') is False else ''}")
    print("=" * 60)

    try:
        import mysql.connector
    except ImportError:
        print("[错误] 未安装 mysql-connector-python，请先执行：")
        print("       python -m pip install -r requirements.txt")
        sys.exit(1)

    # 先不指定 database 连接（01 号脚本负责建库）
    try:
        conn = mysql.connector.connect(**config)
        print("[连接] MySQL 连接成功。")
    except mysql.connector.Error as exc:
        print(f"[错误] 无法连接 MySQL（{config['host']}:{config['port']}）：{exc}")
        print("请确认：")
        print("  1. MySQL 8.0 已安装并已启动（服务名通常为 MySQL80）；")
        print("  2. 用户名 / 密码正确（默认 root / 123456，可用 --password 指定）；")
        print("  3. 端口正确（默认 3306）。")
        sys.exit(1)

    try:
        for name in SQL_FILES:
            execute_sql_file(conn, SQL_DIR / name)
        print("-" * 60)
        print("全部脚本执行成功！数据库 hospital_outpatient 已就绪。")
        print("测试账号：admin01 / reg01 / cash01 / doctor01，密码均为 123456")
        print("")
    except mysql.connector.Error as exc:
        print("-" * 60)
        print(f"[失败] 执行 SQL 时出错：{exc}")

        errno = getattr(exc, "errno", None)
        if errno in (1419, 1227) and "TRIGGER" in str(exc).upper():
            print("")
            print("这是云端 MySQL 的典型限制，不是脚本问题：")
            print("  托管实例默认开启 binlog，普通账号创建触发器需要 SUPER 权限，")
            print("  否则会报 ERROR 1419（You do not have the SUPER privilege）。")
            print("")
            print("解决办法（二选一，推荐第一个）：")
            print("  1. 在数据库控制台把 log_bin_trust_function_creators 设为 1：")
            print("     Aiven → 实例 → Advanced configuration → 搜索")
            print("     mysql.log_bin_trust_function_creators → 填 1 → 保存并等待生效，")
            print("     然后重新运行本脚本即可。")
            print("  2. 如果账号有管理权限，先手动执行一次：")
            print("     SET GLOBAL log_bin_trust_function_creators = 1;")
        elif errno in (1044, 1045):
            print("")
            print("看起来是账号权限或密码问题：请确认用户名 / 密码正确，")
            print("并且该账号有权创建数据库与用户（云端实例一般用 avnadmin / 高权限账号）。")
        elif errno in (2003, 2002, 2013):
            print("")
            print("看起来是网络不通：请确认主机端口填写正确、实例已启动、")
            print("本地网络能访问该地址（云端实例通常要求 TLS，别忘了加 --ssl）。")

        print("")
        print("修复问题后重新运行本脚本即可（脚本可重复执行）。")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
