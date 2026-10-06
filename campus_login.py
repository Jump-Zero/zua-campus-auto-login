#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校园网自动登录引擎 M1（探测 + 自动登录 + 断线重连）。

适用门户：郑州航院 webauth.do 实名认证系统（普通表单 POST）。
设计要点（对应方案报告 M1）：
  - 全部 HTTP 请求强制直连（ProxyHandler({})，不读环境/系统代理）；
  - 账号密码仅以 Windows DPAPI 密文保存（credential.bin），不落明文、不进日志；
  - 状态机：OFFLINE / UNAUTH / LOGGING_IN / ONLINE / BACKOFF / QUIET；
  - 登录结果以"重新探测"为唯一判定依据（超时/丢包视为未知结果）；
  - 指数退避（默认 1s 起、封顶 60s），支持静默时段（如 00:00-06:00 不发起登录）。

用法：
  python campus_login.py                # 常驻监控（探测→登录→重连）
  python campus_login.py --check        # 只探测一次，打印网络状态
  python campus_login.py --show-form    # 只读抓取登录页，打印表单字段（M0 抓包确认用）
  python campus_login.py --login-once   # 手动触发一次登录后退出
  python campus_login.py --test-account # 调用密码预检接口验证账号密码
  python campus_login.py --set-account  # 保存账号密码（DPAPI 加密）
  python campus_login.py --selftest     # 状态机干跑自检（不需要网络/账号）
"""
from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# 常量与状态定义
# ---------------------------------------------------------------------------

STATE_ONLINE = "ONLINE"
STATE_UNAUTH = "UNAUTH"
STATE_OFFLINE = "OFFLINE"
STATE_LOGGING_IN = "LOGGING_IN"
STATE_BACKOFF = "BACKOFF"
STATE_QUIET = "QUIET"
STATE_INIT = "INIT"

RESULT_SUCCESS_HINT = "SUCCESS_HINT"   # 响应体疑似成功（仅提示，不作判定）
RESULT_FAIL_HINT = "FAIL_HINT"         # 响应体疑似失败（仅提示）
RESULT_UNKNOWN = "UNKNOWN"             # 未知结果：超时/丢包/无法判定

DEFAULT_CONFIG = {
    "portal_base": "http://202.196.169.166",
    "detect_url": "http://www.msftconnecttest.com/redirect",
    "probe_interval_sec": 5,
    "backoff_base_sec": 1,
    "backoff_max_sec": 60,
    "request_timeout_sec": 10,
    "quiet_hours": "",                  # 形如 "00:00-06:00"，留空表示不启用
}

CREDENTIAL_FILE = "credential.bin"
CONFIG_FILE = "config.json"

# 数据目录：开发模式取脚本目录；PyInstaller 冻结模式取 exe 所在目录（便携）
BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))

# ---------------------------------------------------------------------------
# HTTP：强制直连（安全属性 S2：登录/探测请求绝不经过任何代理）
# ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 捕获 3xx 的 Location，不自动跟随


def direct_opener(no_redirect: bool = True):
    # ProxyHandler({}) 显式清空全部代理，等价于 --noproxy / trust_env=False
    handlers = [urllib.request.ProxyHandler({})]
    if no_redirect:
        handlers.append(_NoRedirect())
    return urllib.request.build_opener(*handlers)


def http_request(url, data=None, timeout=10):
    """返回 (status, headers_dict, body_bytes)。网络层异常向上抛出。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) campus-login-m1",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }
    req = urllib.request.Request(url, data=data, headers=headers)
    opener = direct_opener(no_redirect=True)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        body = e.read() if getattr(e, "fp", None) else b""
        return e.code, dict(e.headers or {}), body


def detect_charset(html_text: str, headers: dict) -> str:
    m = re.search(r"charset=[\"']?([\w-]+)", headers.get("Content-Type", ""), re.I)
    if m:
        return m.group(1)
    m = re.search(r"<meta[^>]+charset=[\"']?([\w-]+)", html_text or "", re.I)
    return m.group(1) if m else "utf-8"


def decode_body(body: bytes, headers: dict, meta_hint: str = "") -> str:
    candidates = []
    m = re.search(r"charset=([\w-]+)", headers.get("Content-Type", ""), re.I)
    if m:
        candidates.append(m.group(1))
    if meta_hint:
        candidates.append(meta_hint)
    candidates += ["utf-8", "gb18030"]
    for enc in candidates:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 登录页表单解析（字段无关策略：解析后再回填，见报告 4.3）
# ---------------------------------------------------------------------------


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.meta_charset = ""
        self._cur = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._cur = {"action": a.get("action", ""), "fields": []}
            self.forms.append(self._cur)
        elif tag == "meta" and not self.meta_charset:
            m = re.search(r"charset=([\w-]+)", (a.get("content") or "") + (a.get("charset") or ""), re.I)
            if m:
                self.meta_charset = m.group(1)
        elif tag in ("input", "button") and self._cur is not None:
            name = a.get("name")
            if name:
                self._cur["fields"].append({
                    "name": name,
                    "value": a.get("value", ""),
                    "type": (a.get("type") or "text").lower(),
                })

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None


def pick_form(html_text: str):
    """选中含密码框的表单，返回 {action, fields, user_name, pass_name} 或 None。"""
    parser = FormParser()
    try:
        parser.feed(html_text)
    except Exception:
        return None
    for form in parser.forms:
        pass_field = next((f for f in form["fields"] if f["type"] == "password"), None)
        if not pass_field:
            continue
        user_field = next((f for f in form["fields"]
                           if f["type"] in ("text", "tel", "email") and f["name"] != pass_field["name"]), None)
        if form["fields"] and any(f["name"] == "userId" for f in form["fields"]):
            user_name = "userId"
        elif user_field:
            user_name = user_field["name"]
        else:
            continue
        pass_name = pass_field["name"] if pass_field["name"] else "passwd"
        if any(f["name"] == "passwd" for f in form["fields"]):
            pass_name = "passwd"
        return {
            "action": form["action"],
            "fields": form["fields"],
            "user_name": user_name,
            "pass_name": pass_name,
            "meta_charset": parser.meta_charset,
        }
    return None


def build_post_target(page_url: str, action: str, portal_base: str) -> str:
    """表单提交目标：action 优先（JS 通常设为 /webauth.do），并回传原查询串。"""
    query = urllib.parse.urlsplit(page_url).query
    if action:
        target = urllib.parse.urljoin(page_url, action)
    else:
        target = portal_base.rstrip("/") + "/webauth.do"
    if query and "?" not in target:
        target += "?" + query
    return target


# ---------------------------------------------------------------------------
# 探测与登录（门户协议：webauth.do 表单 POST）
# ---------------------------------------------------------------------------


def probe(detect_url: str, portal_base: str, timeout: float):
    """返回 (状态, 重定向 Location)。判定依据：302 目标 / 响应体特征。"""
    portal_host = urllib.parse.urlsplit(portal_base).netloc
    try:
        status, headers, body = http_request(detect_url, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError):
        return STATE_OFFLINE, ""
    loc = headers.get("Location", "") or headers.get("location", "")
    if status in (301, 302, 303, 307, 308):
        if "webauth.do" in loc or (portal_host and portal_host in loc):
            return STATE_UNAUTH, loc
        return STATE_ONLINE, loc
    text = decode_body(body, headers)
    if "webauth.do" in text or "实名认证" in text:
        return STATE_UNAUTH, ""
    return STATE_ONLINE, ""


def login(login_page_url: str, user_id: str, password: str, portal_base: str, timeout: float):
    """执行一次登录。返回 (结果提示, 提交目标)。真实成功与否由调用方重新探测判定。"""
    try:
        status, headers, body = http_request(login_page_url, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return RESULT_UNKNOWN, "GET 登录页失败: %s" % exc
    html = decode_body(body, headers)
    form = pick_form(html)
    if not form:
        return RESULT_UNKNOWN, "未在登录页找到含密码框的表单"
    fields = {f["name"]: f["value"] for f in form["fields"]}
    fields[form["user_name"]] = user_id
    fields[form["pass_name"]] = password
    charset = detect_charset(html, headers) or "utf-8"
    target = build_post_target(login_page_url, form["action"], portal_base)
    data = urllib.parse.urlencode(fields).encode(charset, errors="ignore")
    try:
        st2, hd2, body2 = http_request(target, data=data, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError):
        return RESULT_UNKNOWN, target  # 未知结果：可能已登录成功
    text2 = decode_body(body2, hd2)
    if "LOGINSUCC" in text2 or st2 in (301, 302, 303):
        return RESULT_SUCCESS_HINT, target
    if "密码" in text2 and ("错" in text2 or "误" in text2):
        return RESULT_FAIL_HINT, target
    return RESULT_UNKNOWN, target


CHECK_CODE_MSG = {
    "0": "账号密码正确",
    "1": "参数为空",
    "2": "账号不存在",
    "3": "密码错误",
}


def check_user_pwd(login_page_url: str, user_id: str, password: str, timeout: float):
    """密码预检接口（/httpservice/checkUserPwd.do），只读校验，不建立会话。"""
    page_id = "5"
    query = urllib.parse.urlsplit(login_page_url).query
    m = re.search(r"(?:^|&)pageid=(\d+)", query)
    if m:
        page_id = m.group(1)
    url = urllib.parse.urlsplit(login_page_url)
    target = "%s://%s/httpservice/checkUserPwd.do" % (url.scheme, url.netloc)
    data = urllib.parse.urlencode({
        "userId": user_id, "passwd": password, "pageid": page_id,
    }).encode("utf-8")
    try:
        _, headers, body = http_request(target, data=data, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, "预检请求失败: %s" % exc
    text = decode_body(body, headers)
    m = re.search(r"\"check\"\s*:\s*\"?(\w+)\"?", text)
    if not m:
        return None, "无法解析预检响应"
    code = m.group(1)
    return code, CHECK_CODE_MSG.get(code, "未知返回码 %s" % code)


# ---------------------------------------------------------------------------
# 凭据存储：Windows DPAPI（安全属性 S1/S3：密码不落明文、不进日志）
# ---------------------------------------------------------------------------


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _to_blob(data: bytes):
    buf = ctypes.create_string_buffer(data)
    blob = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    return blob, buf


def dpapi_protect(data: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    in_blob, _ = _to_blob(data)
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob), u"campus_login_m1", None, None, None, 0x1,
        ctypes.byref(out_blob))
    if not ok:
        raise OSError("CryptProtectData 失败: %s" % ctypes.FormatError(ctypes.get_last_error()))
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def dpapi_unprotect(blob: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    in_blob, _ = _to_blob(blob)
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0x1, ctypes.byref(out_blob))
    if not ok:
        raise OSError("CryptUnprotectData 失败: %s" % ctypes.FormatError(ctypes.get_last_error()))
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def save_credentials(path: str, user_id: str, password: str):
    payload = json.dumps({"user_id": user_id, "password": password}).encode("utf-8")
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:  # 先写临时文件再原子替换，避免崩溃留下残缺文件
        f.write(dpapi_protect(payload))
    os.replace(tmp_path, path)


def load_credentials(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            payload = dpapi_unprotect(f.read())
        data = json.loads(payload.decode("utf-8"))
    except (OSError, ValueError) as exc:
        print("凭据文件无法读取（%s）：%s；请重新执行 --set-account" % (path, exc))
        return None
    return data.get("user_id", ""), data.get("password", "")


# ---------------------------------------------------------------------------
# 配置与工具
# ---------------------------------------------------------------------------


def load_config(path: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except (json.JSONDecodeError, OSError) as exc:
            # 恢复路径：配置损坏/被截断时回退默认值，不阻断启动
            print("配置文件无法解析（%s）：%s；已回退默认配置" % (path, exc))
    return cfg


def parse_quiet_window(spec: str):
    spec = (spec or "").strip()
    if not spec or "-" not in spec:
        return None
    try:
        left, right = spec.split("-", 1)
        lh, lm = left.strip().split(":")
        rh, rm = right.strip().split(":")
        return (int(lh) * 60 + int(lm), int(rh) * 60 + int(rm))
    except ValueError:
        return None


def in_quiet_window(window, now: datetime) -> bool:
    if not window:
        return False
    cur = now.hour * 60 + now.minute
    start, end = window
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end  # 跨午夜


def log(state: str, msg: str):
    print("[%s] [%s] %s" % (datetime.now().strftime("%H:%M:%S"), state, msg), flush=True)


def acquire_single_instance() -> bool:
    """Windows 互斥量，防止重复启动（安全属性 S5）。"""
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, u"Global\\CampusLoginEngineM1")
    already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    return bool(handle) and not already


# ---------------------------------------------------------------------------
# 重连引擎（状态机）
# ---------------------------------------------------------------------------


class Engine:
    """探测 → 登录 → 复测 的重连循环。

    不变量（详见 --selftest 与交付说明）：
      S2 每次网络请求都走强制直连；S4 静默时段绝不发起登录；
      S5 同一时刻至多一次登录尝试；L2 失败后必有下一次尝试且退避封顶；
      U1 登录超时/丢包视为未知结果，必须先复测再决定是否重发。
    """

    def __init__(self, cfg, creds, probe_fn=None, login_fn=None,
                 sleep_fn=time.sleep, now_fn=datetime.now, quiet_window=None,
                 interruptible_sleep=False):
        self.cfg = cfg
        self.creds = creds
        self.probe_fn = probe_fn or (lambda: probe(
            cfg["detect_url"], cfg["portal_base"], float(cfg["request_timeout_sec"])))
        self.login_fn = login_fn or (lambda page_url: login(
            page_url, creds[0], creds[1], cfg["portal_base"], float(cfg["request_timeout_sec"])))
        self.sleep_fn = sleep_fn
        self.now_fn = now_fn
        self.quiet_window = quiet_window
        self.state = STATE_INIT
        self.fail_count = 0
        self.login_attempts = 0       # 运行时不变量检查用
        self._login_in_flight = False
        self.interruptible_sleep = interruptible_sleep
        self.stop = False

    def backoff_sec(self) -> float:
        base = float(self.cfg.get("backoff_base_sec", 1))
        cap = float(self.cfg.get("backoff_max_sec", 60))
        return min(cap, base * (2 ** max(0, self.fail_count - 1)))

    def _sleep(self, sec: float):
        """休眠；interruptible_sleep=True 时分片休眠，stop 置位后尽快返回。"""
        if not self.interruptible_sleep:
            self.sleep_fn(sec)
            return
        remaining = float(sec)
        while remaining > 0 and not self.stop:
            chunk = min(0.5, remaining)
            self.sleep_fn(chunk)
            remaining -= chunk

    def step(self):
        now = self.now_fn()
        quiet = in_quiet_window(self.quiet_window, now)
        state, portal = self.probe_fn()

        if state == STATE_ONLINE:
            self.state = STATE_ONLINE
            self.fail_count = 0
            self._sleep(float(self.cfg["probe_interval_sec"]))
            return self.state

        if state == STATE_OFFLINE:
            self.fail_count += 1
            self.state = STATE_OFFLINE
            self._sleep(self.backoff_sec())
            return self.state

        # UNAUTH：需要登录
        if quiet:
            # S4：静默时段绝不发起登录（仅探测）
            self.state = STATE_QUIET
            self._sleep(60)
            return self.state

        assert not self._login_in_flight, "S5 violated: 并发登录"
        self._login_in_flight = True
        self.login_attempts += 1
        self.state = STATE_LOGGING_IN
        try:
            hint, detail = self.login_fn(portal or "")
        except Exception as exc:  # 未知结果同样按 U1 处理
            hint, detail = RESULT_UNKNOWN, str(exc)
        finally:
            self._login_in_flight = False
        log(STATE_LOGGING_IN, "提交完成（%s）目标: %s" % (hint, detail))

        # U1：未知结果必须由复测裁决，而不是立刻重发
        state2, _ = self.probe_fn()
        if state2 == STATE_ONLINE:
            self.state = STATE_ONLINE
            self.fail_count = 0
            self._sleep(float(self.cfg["probe_interval_sec"]))
        else:
            self.fail_count += 1
            self.state = STATE_BACKOFF
            self._sleep(self.backoff_sec())
        return self.state

    def run(self, max_iterations=None):
        iterations = 0
        while not self.stop:
            state = self.step()
            iterations += 1
            if state in (STATE_BACKOFF, STATE_OFFLINE, STATE_QUIET):
                log(state, "第 %d 次失败，%s 秒后重试" % (
                    self.fail_count, self.backoff_sec() if state != STATE_QUIET else 60))
            if max_iterations is not None and iterations >= max_iterations:
                return


# ---------------------------------------------------------------------------
# 自检（干跑状态机，覆盖恢复用例；不需要网络与账号）
# ---------------------------------------------------------------------------


def selftest():
    def fake_env(probe_script, login_script, quiet=None, start="2026-10-06 12:00:00"):
        script = {"probe": list(probe_script), "login": list(login_script)}
        counters = {"login": 0, "probe": 0, "sleeps": []}
        stamps = {"now": datetime.strptime(start, "%Y-%m-%d %H:%M:%S")}

        def probe_fn():
            counters["probe"] += 1
            item = script["probe"].pop(0) if script["probe"] else (STATE_UNAUTH, "")
            return item

        def login_fn(_page):
            counters["login"] += 1
            item = script["login"].pop(0) if script["login"] else (RESULT_UNKNOWN, "script-exhausted")
            return item

        def sleep_fn(sec):
            counters["sleeps"].append(sec)
            from datetime import timedelta
            stamps["now"] += timedelta(seconds=sec)

        cfg = dict(DEFAULT_CONFIG)
        eng = Engine(cfg, ("u", "p"), probe_fn=probe_fn, login_fn=login_fn,
                     sleep_fn=sleep_fn, now_fn=lambda: stamps["now"], quiet_window=quiet)
        return eng, counters

    # 用例 1：断网→退避递增且封顶（L2）
    eng, c = fake_env([(STATE_OFFLINE, "")] * 9, [], quiet=None)
    eng.run(max_iterations=9)
    assert c["sleeps"] == [1, 2, 4, 8, 16, 32, 60, 60, 60], "退避序列错误: %s" % c["sleeps"]
    assert eng.fail_count == 9

    # 用例 2：未知结果（登录超时）由复测裁决，每轮至多一次登录（U1/S5）
    eng, c = fake_env([(STATE_UNAUTH, "http://p/webauth.do")] * 3,
                      [(RESULT_UNKNOWN, "timeout")] * 3, quiet=None)
    eng.run(max_iterations=3)
    assert c["login"] == 3, "登录次数应等于轮次"
    assert c["probe"] == 6, "每轮登录后必须复测一次（probe=2/轮）"
    assert eng.fail_count == 3, "未知结果按失败退避"

    # 用例 3：静默时段绝不登录（S4）
    eng, c = fake_env([(STATE_UNAUTH, "")] * 3, [(RESULT_SUCCESS_HINT, "x")] * 3,
                      quiet=(0, 6 * 60), start="2026-10-06 02:00:00")
    eng.run(max_iterations=3)
    assert c["login"] == 0, "静默时段不得发起登录"
    assert eng.state == STATE_QUIET

    # 用例 4：登录成功后复测在线，失败计数清零
    eng, c = fake_env([(STATE_UNAUTH, ""), (STATE_ONLINE, ""), (STATE_ONLINE, "")],
                      [(RESULT_SUCCESS_HINT, "ok")], quiet=None)
    eng.run(max_iterations=2)
    assert eng.state == STATE_ONLINE and eng.fail_count == 0
    assert c["login"] == 1, "复测在线后不得重复登录"

    print("selftest PASS：退避封顶 / 未知结果裁决 / 静默时段 / 成功清零 全部通过")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_check(cfg):
    state, loc = probe(cfg["detect_url"], cfg["portal_base"], float(cfg["request_timeout_sec"]))
    print("网络状态：%s" % state)
    if loc:
        print("重定向：%s" % loc)
    return 0


def cmd_show_form(cfg, login_url=""):
    state, loc = probe(cfg["detect_url"], cfg["portal_base"], float(cfg["request_timeout_sec"]))
    print("网络状态：%s" % state)
    if login_url:
        loc = login_url
    elif state != STATE_UNAUTH:
        print("当前未处于未认证状态，仍尝试抓取登录页……")
        loc = cfg["portal_base"].rstrip("/") + "/webauth.do"
    else:
        loc = loc or (cfg["portal_base"].rstrip("/") + "/webauth.do")
    print("登录页：%s" % loc)
    try:
        status, headers, body = http_request(loc, timeout=float(cfg["request_timeout_sec"]))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print("抓取失败：%s" % exc)
        return 1
    html = decode_body(body, headers)
    form = pick_form(html)
    if not form:
        if state == STATE_ONLINE:
            print("未找到登录表单：当前已在线，门户通常只在未认证状态下返回登录表单。")
            print("请在断网（或重连 WiFi 尚未认证）状态下重新运行 --show-form 以确认字段。")
        else:
            print("未找到含密码框的表单（页面可能已变更，请人工确认）")
        return 1
    print("表单 action：%s" % (form["action"] or "/webauth.do"))
    print("提交目标：%s" % build_post_target(loc, form["action"], cfg["portal_base"]))
    print("字符集：%s" % (detect_charset(html, headers) or "utf-8"))
    print("字段清单（值已脱敏）：")
    for f in form["fields"]:
        val = f["value"]
        shown = ("（已填值 %d 字符）" % len(val)) if val else "（空）"
        print("  - %-24s type=%-8s %s" % (f["name"], f["type"], shown))
    print("用户名字段：%s ｜ 密码字段：%s" % (form["user_name"], form["pass_name"]))
    return 0


def cmd_set_account(cred_path):
    user_id = input("校园网账号（学号/工号）: ").strip()
    password = getpass.getpass("校园网密码（输入不回显）: ")
    if not user_id or not password:
        print("账号或密码为空，未保存")
        return 1
    save_credentials(cred_path, user_id, password)
    print("已保存（DPAPI 加密，仅当前 Windows 用户可解密）：%s" % cred_path)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="校园网自动登录引擎 M1（webauth.do 表单协议）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=os.path.join(BASE_DIR, CONFIG_FILE),
                        help="配置文件路径（默认数据目录下 config.json）")
    parser.add_argument("--check", action="store_true", help="只探测一次网络状态")
    parser.add_argument("--show-form", action="store_true", help="抓取并打印登录页表单字段（只读）")
    parser.add_argument("--login-once", action="store_true", help="手动触发一次登录后退出")
    parser.add_argument("--test-account", action="store_true", help="调用密码预检接口验证账号密码")
    parser.add_argument("--set-account", action="store_true", help="保存账号密码（DPAPI 加密）")
    parser.add_argument("--selftest", action="store_true", help="状态机干跑自检")
    parser.add_argument("--login-url", default="",
                        help="指定登录页完整 URL（含 wlanacip 等参数），用于 --show-form / --login-once / --test-account")
    parser.add_argument("--verbose", action="store_true", help="输出探测细节")
    args = parser.parse_args(argv)

    if args.selftest:
        selftest()
        return 0

    cfg = load_config(args.config)
    cred_path = os.path.join(os.path.dirname(os.path.abspath(args.config)), CREDENTIAL_FILE)

    if args.set_account:
        return cmd_set_account(cred_path)
    if args.check:
        return cmd_check(cfg)
    if args.show_form:
        return cmd_show_form(cfg, args.login_url)

    creds = load_credentials(cred_path)
    if not creds:
        print("未找到已保存的账号，请先执行：python campus_login.py --set-account")
        return 1

    if args.test_account:
        state, loc = probe(cfg["detect_url"], cfg["portal_base"], float(cfg["request_timeout_sec"]))
        page = args.login_url or loc or (cfg["portal_base"].rstrip("/") + "/webauth.do")
        code, msg = check_user_pwd(page, creds[0], creds[1], float(cfg["request_timeout_sec"]))
        print("预检结果：%s%s" % (msg, "" if code is None else "（check=%s）" % code))
        return 0 if code == "0" else 1

    if not acquire_single_instance():
        print("已有实例在运行，退出")
        return 1

    quiet = parse_quiet_window(cfg.get("quiet_hours", ""))
    engine = Engine(cfg, creds, quiet_window=quiet)
    log(STATE_INIT, "启动监控：探测间隔 %ss，退避 1s 起封顶 %ss，静默时段 %s" % (
        cfg["probe_interval_sec"], cfg["backoff_max_sec"], cfg.get("quiet_hours") or "未启用"))

    def _stop(_sig, _frm):
        log(STATE_INIT, "收到退出信号，停止监控")
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _stop)

    if args.login_once:
        state, loc = probe(cfg["detect_url"], cfg["portal_base"], float(cfg["request_timeout_sec"]))
        if state == STATE_ONLINE:
            log(STATE_ONLINE, "已在线，无需登录")
            return 0
        page = args.login_url or loc or (cfg["portal_base"].rstrip("/") + "/webauth.do")
        hint, detail = login(page, creds[0], creds[1], cfg["portal_base"], float(cfg["request_timeout_sec"]))
        log(STATE_LOGGING_IN, "提交完成（%s）目标: %s" % (hint, detail))
        state2, _ = probe(cfg["detect_url"], cfg["portal_base"], float(cfg["request_timeout_sec"]))
        log(state2, "复测结果：%s" % state2)
        return 0 if state2 == STATE_ONLINE else 1

    engine.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
