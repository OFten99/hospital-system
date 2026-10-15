#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""医院门诊管理系统 · 浏览器模拟「一键演示」

设计原则：**不改动系统任何代码**。全部操作都发生在真实浏览器里——
自动打开页面、自动填账号密码并登录、按演示动线切换角色，
所以演示时不必在登录框里手打密码，也不用反复「退出登录 / 重新登录」。

依赖
----
1. Web 服务已在运行（双击 run_web.bat；或由 一键演示.bat 自动拉起）
2. browser-skill 守护进程在跑、且有浏览器连上：
        bsk status      →  browsers connected 1

用法
----
    python demo_browser.py                  交互巡览：每到一个站点按回车继续
    python demo_browser.py --auto 8         全自动：每站停留 8 秒后自动翻页
    python demo_browser.py login cash01     只登录到指定角色（停在首页看板）
    python demo_browser.py goto lab01 /lab_tests
    python demo_browser.py roles            列出角色与演示动线
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE_URL = "http://127.0.0.1:5000"
PASSWORD = "123456"

ROLES = {
    "admin01": "系统超级用户",
    "cash01": "收费人员（兼挂号窗口）",
    "doctor01": "医生（接诊工作台）",
    "lab01": "检验科人员",
    "pharm01": "药房人员",
}

# 演示动线：与「演示脚本.md」的 5 个现场节点一一对应
STATIONS = [
    ("cash01", "/patients", "建档：病历号自动生成，身份证/手机号唯一约束", "\u2460"),
    ("cash01", "/registrations", "挂号：P-ACCEPT01，盯着号源 0/30 → 1/30 实时扣减", "\u2460"),
    ("doctor01", "/consultation", "接诊：ACCEPT-B01 待就诊 → 就诊中，一个表单联动写病历/开方/开检验", "\u2461"),
    ("doctor01", "/medical_records", "病历：ACCMR000001 内容完整，可直接 A4 打印", "\u2461"),
    ("cash01", "/payments", "收费：对 ACCPRES000002（53.50 元）收费 —— 这是发药的前置条件", "\u2462"),
    ("lab01", "/lab_tests", "检验：给 LAB20260918142242 录结果，看异常值自动打「↑ 偏高」", "\u2463"),
    ("pharm01", "/pharmacy", "发药：对刚收费的处方发药，看库存复核", "\u2464"),
]


class BrowserError(RuntimeError):
    """浏览器通道不可用，或某一步骤失败。"""


def normalize_path(raw: str) -> str:
    """把用户给的路径归一成站内绝对路径。

    主要防 Git Bash / MSYS 的路径转换：命令行里写 `/lab_tests` 会被它悄悄
    改成 `C:/Program Files/Git/lab_tests`。检测到盘符就只取最后一段。
    另外允许省略开头的斜杠（`lab_tests` 与 `/lab_tests` 等价）。
    """
    text = raw.replace("\\", "/").strip()
    if re.match(r"^[A-Za-z]:/", text) or text.startswith("//"):
        text = text.rstrip("/").split("/")[-1]
    return "/" + text.strip("/")


def find_bsk() -> str:
    found = shutil.which("bsk")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "bsk.exe",
        Path.home() / ".local" / "bin" / "bsk",
    ):
        if candidate.exists():
            return str(candidate)
    raise BrowserError(
        "找不到 bsk 命令（browser-skill）。\n"
        "        请先确认它已安装：C:\\Users\\<你>\\.local\\bin\\bsk.exe"
    )


class Browser:
    """对 bsk CLI 的一层薄封装。"""

    def __init__(self, bsk: str, session: str, verbose: bool = False) -> None:
        self.bsk = bsk
        self.session = session
        self.verbose = verbose

    def _run(self, *args: str, timeout: int = 60) -> str:
        proc = subprocess.run(
            [self.bsk, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise BrowserError((proc.stderr or proc.stdout).strip() or "bsk 执行失败")
        if self.verbose and proc.stdout.strip():
            print("      · %s" % proc.stdout.strip().splitlines()[-1])
        return proc.stdout

    def navigate(self, url: str, wait: str = "load") -> None:
        self._run("navigate", "--session", self.session, url,
                  "--wait-until", wait, "--timeout", "30s")

    def js(self, expression: str) -> str:
        out = self._run("evaluate", "--session", self.session, expression)
        return out.strip()

    def js_json(self, expression: str):
        """求值并把结果当 JSON 解析。

        注意：bsk 返回**裸文本**时字符串不带引号（`/dashboard` 而不是 `"/dashboard"`），
        所以不能直接 json.loads 一个字符串表达式，必须先 JSON.stringify 包一层。
        """
        out = self.js("JSON.stringify(%s)" % expression)
        try:
            return json.loads(out)
        except ValueError:
            return None

    # ---------------------------------------------------------------- 动作

    def login(self, username: str) -> str:
        """自动填表并登录（全程不碰键盘）。返回落地页路径。"""
        self.navigate(BASE_URL + "/logout", wait="domcontentloaded")
        self.navigate(BASE_URL + "/login", wait="load")

        script = (
            "(function(){"
            'var u=document.querySelector(\'input[name="username"]\');'
            'var p=document.querySelector(\'input[name="password"]\');'
            "if(!u||!p){return \"NO_FORM\";}"
            'var set=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,"value").set;'
            'set.call(u,"%s");u.dispatchEvent(new Event("input",{bubbles:true}));'
            'set.call(p,"%s");p.dispatchEvent(new Event("input",{bubbles:true}));'
            'var f=document.querySelector("form.login-form")||document.querySelector("form");'
            "f.submit();return \"SUBMITTED\";"
            "})()" % (username, PASSWORD)
        )
        result = self.js(script)
        if "NO_FORM" in result:
            raise BrowserError("登录页没找到用户名/密码输入框（页面结构变了？）")

        # 提交后等页面跳转完成
        for _ in range(24):
            time.sleep(0.5)
            state = self.js_json("({path: location.pathname})")
            if state and state.get("path") and state["path"] != "/login":
                return state["path"]

        raise BrowserError(
            "登录后仍停留在 /login —— 多半是账号密码不对，或服务未连上数据库。"
        )

    def snapshot(self) -> dict:
        return self.js_json("({path: location.pathname, title: document.title})") or {}

    def goto(self, role: str, path: str) -> str:
        self.login(role)
        self.navigate(BASE_URL + path, wait="load")
        return self.snapshot().get("path") or path


def ensure_ready(bsk: str) -> bool:
    """检查守护进程与浏览器连接情况。"""
    try:
        proc = subprocess.run([bsk, "status"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print("[X] 无法执行 bsk status：%s" % exc)
        return False

    out = proc.stdout or ""
    connected = 0
    for line in out.splitlines():
        if "browsers connected" in line:
            digits = "".join(ch for ch in line if ch.isdigit())
            connected = int(digits or 0)
    if connected < 1:
        print("[X] 没有浏览器连接到 browser-skill。请：")
        print("      1) 打开 Edge；2) 确认 browser-skill 扩展已启用；")
        print("      3) 重跑 `bsk status`，看到 browsers connected 1 再试。")
        return False
    print("[OK] 浏览器通道就绪（browsers connected %d）" % connected)
    return True


def check_service() -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(BASE_URL + "/healthz", timeout=5) as resp:
            ok = resp.status == 200
    except (urllib.error.URLError, OSError) as exc:
        print("[X] Web 服务没起来（%s）：%s" % (BASE_URL, exc))
        print("      先双击 run_web.bat，或直接用 一键演示.bat（它会自动拉起）。")
        return False
    print("[OK] Web 服务在线（%s）" % BASE_URL)
    return ok


def start_session(bsk: str, force_new: bool = False) -> str:
    """复用已有的浏览器会话；没有则新建。

    复用是为了反复演示时不至于每跑一次就多弹一个浏览器窗口。
    """
    if not force_new:
        proc = subprocess.run(
            [bsk, "session", "list"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
        if len(lines) >= 2:
            session = lines[1].split()[0]
            if session and session.upper() != "SESSION":
                print("[OK] 复用已有浏览器会话：%s" % session)
                return session

    proc = subprocess.run(
        [bsk, "session", "start", "--name", "his-demo", "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if proc.returncode != 0:
        raise BrowserError((proc.stderr or proc.stdout).strip() or "session start 失败")
    try:
        return json.loads(proc.stdout)["session_id"]
    except (ValueError, KeyError) as exc:
        raise BrowserError("解析 session 输出失败：%s" % proc.stdout) from exc


# ------------------------------------------------------------------ 子命令

def cmd_login(bsk: str, role: str, path: str, force_new: bool = False) -> int:
    session = start_session(bsk, force_new)
    browser = Browser(bsk, session)
    landed = browser.goto(role, path)
    print("[OK] 已用 %s（%s）登录，停在 %s" % (role, ROLES[role], landed))
    print("     浏览器窗口留着即可，全程不需要手输密码。")
    return 0


def cmd_tour(bsk: str, pause: float, auto: bool, force_new: bool = False) -> int:
    session = start_session(bsk, force_new)
    browser = Browser(bsk, session)

    print()
    print("=" * 64)
    print("  门诊就诊闭环 · 演示动线（%d 站）" % len(STATIONS))
    print("  %s" % ("全自动：每站 %.0f 秒后自动翻页" % pause if auto else "交互：每站按回车继续"))
    print("=" * 64)

    current_role = None
    for index, (role, path, script, mark) in enumerate(STATIONS, start=1):
        print()
        print("-" * 64)
        print("%s [%d/%d] 角色 %s（%s）" % (mark, index, len(STATIONS), role, ROLES[role]))
        print("    页面：%s" % path)
        print("    讲解：%s" % script)
        if role != current_role:
            print("    → 自动切换登录到 %s ..." % role)
        try:
            landed = browser.goto(role, path)
        except BrowserError as exc:
            print("    [X] 这一步失败：%s" % exc)
            print("    已停下，浏览器保持当前页面，不会再往下走。")
            return 1
        current_role = role
        title = (browser.snapshot() or {}).get("title", "")
        if landed != path:
            print("    [!] 请求 %s，实际停在 %s —— 可能被权限规则重定向了" % (path, landed))
        else:
            print("    [OK] 已到达 %s%s" % (landed, "（%s）" % title if title else ""))

        if auto:
            time.sleep(pause)
        else:
            try:
                input("    准备好后按【回车】进入下一站 ... ")
            except EOFError:
                return 0

    print()
    print("=" * 64)
    print("  动线走完。浏览器窗口保持打开，可以继续手工演示或复盘。")
    print("=" * 64)
    return 0


def cmd_roles() -> int:
    print("可用角色（密码统一 %s）：" % PASSWORD)
    for name, label in ROLES.items():
        print("    %-10s %s" % (name, label))
    print()
    print("演示动线：")
    for index, (role, path, script, mark) in enumerate(STATIONS, start=1):
        print("    %s %d. %-10s %-18s %s" % (mark, index, role, path, script))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="浏览器模拟一键演示（不改系统代码，全程真实浏览器操作）",
    )
    parser.add_argument("command", nargs="?", default="tour",
                        choices=["tour", "login", "goto", "roles"],
                        help="tour=按动线走一遍（默认）; login/goto 单站; roles=列出角色")
    parser.add_argument("args", nargs="*", help="login <角色>；goto <角色> <路径>")
    parser.add_argument("--auto", type=float, default=0,
                        help="全自动模式：每站停留秒数（默认 0 = 交互回车）")
    parser.add_argument("--verbose", action="store_true", help="打印 bsk 原始输出")
    parser.add_argument("--new-session", action="store_true",
                        help="强制新开一个浏览器窗口（默认复用已有会话）")
    opts = parser.parse_args(argv)

    if opts.command == "roles":
        return cmd_roles()

    try:
        bsk = find_bsk()
    except BrowserError as exc:
        print("[X] %s" % exc)
        return 2

    if not ensure_ready(bsk) or not check_service():
        return 2

    try:
        if opts.command == "login":
            role = opts.args[0] if opts.args else "admin01"
            if role not in ROLES:
                print("[X] 未知角色 %s，可选：%s" % (role, " / ".join(ROLES)))
                return 2
            return cmd_login(bsk, role, "/dashboard", opts.new_session)

        if opts.command == "goto":
            if len(opts.args) < 2:
                print("[X] 用法：python demo_browser.py goto <角色> <路径>")
                return 2
            role, path = opts.args[0], normalize_path(opts.args[1])
            if role not in ROLES:
                print("[X] 未知角色 %s，可选：%s" % (role, " / ".join(ROLES)))
                return 2
            return cmd_login(bsk, role, path, opts.new_session)

        return cmd_tour(bsk, opts.auto, auto=opts.auto > 0, force_new=opts.new_session)

    except BrowserError as exc:
        print("[X] %s" % exc)
        return 1
    except KeyboardInterrupt:
        print("\n已中断，浏览器保持当前状态。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
