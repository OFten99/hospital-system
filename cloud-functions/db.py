# 本文件由 build_edgeone.py 从 web_app/db.py 自动生成，请勿直接修改。
"""
医院门诊管理系统 —— 数据库连接工具

同一份代码同时支持本地开发和云端部署，切换方式只有「设置环境变量」：

1. 本地 MySQL（默认，零配置）
       python web_app/app.py        # 连 127.0.0.1:3306 的 root/123456
2. 云端托管 MySQL（Aiven / 腾讯云 / 阿里云 RDS 等）
       设置 HIS_DB_HOST 等环境变量即可，云端一般强制 TLS，需同时设 HIS_DB_SSL=1。

环境变量一览（未设置时使用本地默认值）：
    HIS_DB_HOST             数据库主机，默认 127.0.0.1
    HIS_DB_PORT             端口，默认 3306
    HIS_DB_USER             用户名，默认 root
    HIS_DB_PASSWORD         密码，默认 123456
    HIS_DB_NAME             库名，默认 hospital_outpatient
    HIS_DB_SSL              置 1 / true / yes 时启用 TLS 加密连接（云端必填）
    HIS_DB_SSL_CA           可选，CA 证书文件路径；填了则严格校验服务端证书，
                            不填则使用系统默认根证书（只加密、不额外校验）
    HIS_DB_CONNECT_TIMEOUT  连接超时秒数，默认 10（云端网络抖动时建议 15）
    HIS_DB_READ_TIMEOUT     读超时秒数，默认 30
    HIS_DB_POOL             置 0 可关闭“请求级连接复用”，默认开启

设计说明（为什么不是每次调用都新建连接）：
    云函数 / 无服务器环境冷启动时，一次建连（尤其是云端强制 TLS 握手）可能要
    200~400ms。一个页面往往要查好几次库，如果每次都新建连接，页面会慢到不可用；
    而 mysql.connector 的连接如果用 `with conn` 管理，退出时只提交事务、
    **不会关闭连接**，长跑下来会把服务端的 max_connections 打满。
    因此这里做了请求级连接复用：同一个 HTTP 请求内所有查询共用一个连接，
    请求结束时由 app.teardown_appcontext 统一关闭。
"""

import os

import mysql.connector

TRUTHY = {"1", "true", "yes", "on", "y"}


def _env_bool(name, default=False):
    """读取布尔型环境变量。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUTHY


def _env_int(name, default):
    """读取整型环境变量，非法值回退到默认值。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def build_config(with_database=True):
    """
    组装 mysql.connector 的连接参数。

    with_database=False 用于初始化建库脚本（此时目标库还不存在）。
    """
    config = {
        "host": os.getenv("HIS_DB_HOST", "127.0.0.1"),
        "port": _env_int("HIS_DB_PORT", 3306),
        "user": os.getenv("HIS_DB_USER", "root"),
        "password": os.getenv("HIS_DB_PASSWORD", "123456"),
        "charset": "utf8mb4",
        "use_unicode": True,
        "connection_timeout": _env_int("HIS_DB_CONNECT_TIMEOUT", 10),
        "read_timeout": _env_int("HIS_DB_READ_TIMEOUT", 30),
    }

    if with_database:
        config["database"] = os.getenv("HIS_DB_NAME", "hospital_outpatient")

    if _env_bool("HIS_DB_SSL"):
        # 云端托管 MySQL 基本都强制 TLS。指定了 CA 才做严格校验，
        # 否则只加密不校验（避免因缺少根证书直接连不上）。
        config["ssl_disabled"] = False
        ssl_ca = os.getenv("HIS_DB_SSL_CA", "").strip()
        if ssl_ca:
            config["ssl_ca"] = ssl_ca
            config["ssl_verify_cert"] = True
            config["ssl_verify_identity"] = True
        else:
            config["ssl_verify_cert"] = False
            config["ssl_verify_identity"] = False

    return config


# 兼容旧代码：模块级默认配置（导入时快照一次环境变量）
DEFAULT_CONFIG = build_config()


# ---------------------------------------------------------------
# 连接生命周期管理
# ---------------------------------------------------------------

_CONN_KEY = "_his_db_conn"
_CONN_KEY_NODB = "_his_db_conn_nodb"


def _safe_close(conn):
    """关闭连接，忽略关闭过程中的一切异常。"""
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def _healthy(conn):
    """判断连接是否仍然可用；已断开时尝试自动重连。"""
    if conn is None:
        return False
    try:
        conn.ping(reconnect=True)
        return True
    except Exception:
        return False


def _request_context():
    """
    如果当前处在 Flask 应用上下文（也就是一次 HTTP 请求）中，返回 g 对象，
    否则返回 None（命令行脚本 / 初始化脚本等场景）。
    """
    if not _env_bool("HIS_DB_POOL", True):
        return None
    try:
        from flask import g, has_app_context
    except ImportError:
        return None
    if not has_app_context():
        return None
    return g


def get_connection(with_database=True):
    """
    获取一个数据库连接。

    - 在 Flask 请求内：复用该请求的连接（免去重复握手开销）
    - 在请求外（脚本 / 初始化）：每次新建独立连接，由调用方负责关闭
    """
    context = _request_context()
    if context is None:
        return mysql.connector.connect(**build_config(with_database))

    key = _CONN_KEY if with_database else _CONN_KEY_NODB
    conn = getattr(context, key, None)
    if not _healthy(conn):
        _safe_close(conn)
        conn = mysql.connector.connect(**build_config(with_database))
        setattr(context, key, conn)
    return conn


def close_request_connections(exc=None):
    """
    请求结束时回收数据库连接。
    由 app.py 注册到 app.teardown_appcontext 上自动调用。
    """
    context = _request_context()
    if context is None:
        return
    for key in (_CONN_KEY, _CONN_KEY_NODB):
        conn = getattr(context, key, None)
        if conn is not None:
            _safe_close(conn)
            setattr(context, key, None)


# ---------------------------------------------------------------
# 查询辅助函数
# ---------------------------------------------------------------

def fetch_all(sql, params=None):
    """执行查询并返回全部行（字典列表）。"""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(sql, params or ())
        return cursor.fetchall()
    finally:
        cursor.close()


def fetch_one(sql, params=None):
    """执行查询并返回第一行（字典），无结果时返回 None。"""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(sql, params or ())
        return cursor.fetchone()
    finally:
        cursor.close()


def execute(sql, params=None):
    """执行写操作并提交，返回受影响行数。"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params or ())
        conn.commit()
        return cursor.rowcount
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cursor.close()


def call_proc(proc_name, args):
    """调用存储过程并提交。"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.callproc(proc_name, args)
        # 存储过程可能返回结果集，不读完会导致后续语句报 Unread result found
        for result in cursor.stored_results():
            result.fetchall()
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cursor.close()


def call_proc_scalar(proc_name, args, out_index=None, out_type="INT"):
    """调用带 OUT 参数的存储过程，并返回该 OUT 参数的值。

    为什么不用 ``callproc``：mysql-connector 的 ``callproc`` 对 OUT 参数的
    处理是「发送时给占位符、但不回读」，函数返回后拿不到输出值。
    可靠做法是走会话变量：``SET @out = ...`` → ``CALL proc(..., @out)`` →
    ``SELECT @out``，三步在同一连接（同一会话）上执行。

    :param args: 输入参数列表（不含 OUT 参数）
    :param out_index: OUT 参数在存储过程签名中的位置（1 起算）。
                      为 None 时用「最后一个参数」。
    :param out_type: 会话变量初始值类型，INT 或 VARCHAR
    :return: OUT 参数的值（按 out_type 转换）
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # ---- 先算清楚「存储过程一共有几个形参」----
        # 形参总数 = 输入参数个数 + 1（OUT 参数自己占一个位置）。
        # 注意不能写成 max(pos, n_in + 1)：当 out_index 落在输入参数范围内
        # （比如 10 个 IN + 第 11 个是 OUT，调用方却把 out_index 传成 10）
        # 时，max(10, 11) = 11，循环会多拼出一个 "%s"，实参串变成
        #    CALL proc(%s,...,%s,@his_out,%s)
        # 结果 OUT 位置被挪到第 10 个形参、真正的第 11 个形参拿到了一个
        # 字面量占位符，MySQL 直接报
        #   1414 OUT or INOUT argument 11 ... is not a variable
        # 所以这里必须以「形参总数」为准，而不是以 out_index 为准。
        n_in = len(args)
        n_params = n_in + 1
        pos = out_index if out_index is not None else n_params
        if not 1 <= pos <= n_params:
            raise ValueError(
                "out_index=%r 超出存储过程 %s 的形参范围（共 %d 个）"
                % (out_index, proc_name, n_params)
            )

        if out_type.upper() == "VARCHAR":
            cursor.execute("SET @his_out = ''")
        else:
            cursor.execute("SET @his_out = 0")

        # 按签名拼出实参列表，OUT 位置放 @his_out
        placeholders = []
        for i in range(1, n_params + 1):
            placeholders.append("@his_out" if i == pos else "%s")
        call_sql = "CALL %s(%s)" % (proc_name, ", ".join(placeholders))
        cursor.execute(call_sql, tuple(args))

        # 存储过程可能返回结果集，不读完会导致后续语句报 Unread result found
        for result in cursor.stored_results():
            result.fetchall()

        cursor.execute("SELECT @his_out AS v")
        value = cursor.fetchone()[0]
        conn.commit()
        if out_type.upper() == "VARCHAR":
            return value
        return int(value) if value is not None else None
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cursor.close()


def execute_atomic(statements):
    """
    在同一个事务里按顺序执行多条写语句：全部成功才提交，任一条失败整体回滚。

    为什么需要它：``execute()`` 每调用一次就自己 commit。像「删医生档案 ->
    删操作日志 -> 删账号」这种多步删除，一旦中间或最后一步被外键拦下，
    前面几步其实已经落库，会留下「删了一半」的脏数据；业务提示
    「该数据已被其它业务记录引用，不能删除」也就变成假话了。
    需要整体成败一致的场景请改用本函数。

    :param statements: [(sql, params), ...]，params 可为 None
    :return: 最后一条语句的受影响行数
    """
    conn = get_connection()
    cursor = conn.cursor()
    affected = 0
    try:
        for sql, params in statements:
            cursor.execute(sql, params or ())
            affected = cursor.rowcount
        conn.commit()
        return affected
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cursor.close()
