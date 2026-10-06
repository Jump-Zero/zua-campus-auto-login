# -*- coding: utf-8 -*-
"""校园网自动登录 M2 图形界面（tkinter，零第三方依赖）。

功能：状态可视化、账号密码保存（DPAPI）、开机自启、自动重连开关、
静默时段/探测间隔设置、立即登录/测试登录、Clash 直连规则一键生成、日志查看。

运行：
  python gui.py               # 打开设置窗口并启动监控
  python gui.py --minimized   # 开机自启用：启动后最小化
  python gui.py --selfcheck   # 构建界面自检（不进入主循环）

安全边界（对应 client-application-security 检查表）：
  - 门户是唯一权威：登录成败只以门户复测为准，界面状态仅作提示；
  - 密码仅存 DPAPI 密文，界面用掩码显示，永不写入日志/配置；
  - 无远程入口：界面输入与配置文件是仅有的外部输入，均限长并本地使用；
  - 本工具不含任何随附密钥/凭据（binary 无 secret）。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
from datetime import datetime

# 强制 UTF-8 输出：Windows 控制台/CI 默认编码（如 cp1252）无法打印中文日志
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

if getattr(sys, "frozen", False):  # PyInstaller 单文件：数据目录 = exe 所在目录（便携）
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, BASE_DIR)  # 开发模式下导入同目录模块

import campus_login as core  # noqa: E402
import tray_icon  # noqa: E402

try:
    import tkinter as tk  # noqa: E402
    from tkinter import messagebox, scrolledtext, ttk  # noqa: E402
except ModuleNotFoundError:  # 当前解释器未带 tkinter
    sys.stderr.write("当前 Python 缺少 tkinter，请改用系统 Python 运行本界面：\n"
                     "  py gui.py        （或 pyw gui.py 免控制台）\n")
    raise SystemExit(1)

AUTOSTART_NAME = "CampusAutoLoginM1"

CLASH_SNIPPET = """# ===== 校园网认证直连规则（CampusAutoLogin M2 生成）=====
# 用法：合并进 mihomo/Clash 配置，或放进客户端的 Merge / 覆写文件。
# 注意：rules 片段必须放在你原有 MATCH 兜底规则之前。
dns:
  fake-ip-filter:
    - '+.msftconnecttest.com'
    - '+.msftncsi.com'
    - 'connectivitycheck.gstatic.com'
    - 'captive.apple.com'
rules:
  - IP-CIDR,202.196.169.166/32,DIRECT,no-resolve
  - IP-CIDR,10.0.0.0/8,DIRECT,no-resolve
  - IP-CIDR,172.16.0.0/12,DIRECT,no-resolve
  - IP-CIDR,192.168.0.0/16,DIRECT,no-resolve
  - DOMAIN-SUFFIX,msftconnecttest.com,DIRECT
  - DOMAIN-SUFFIX,msftncsi.com,DIRECT
tun:
  route-exclude-address:
    - 10.0.0.0/8
    - 202.196.169.166/32
"""

MAX_LOG_LINES = 1000


# ---------------------------------------------------------------------------
# 开机自启（HKCU Run 键）
# ---------------------------------------------------------------------------


def _winreg():
    try:
        import winreg
        return winreg
    except ImportError:
        return None


def autostart_command() -> str:
    exe = sys.executable
    if getattr(sys, "frozen", False):  # PyInstaller 单文件：自启指向 exe 本身
        return '"%s" --minimized' % exe
    if exe.lower().endswith("python.exe"):
        cand = exe[:-10] + "pythonw.exe"
        if os.path.exists(cand):
            exe = cand
    return '"%s" "%s" --minimized' % (exe, os.path.abspath(__file__))


def get_autostart() -> bool:
    winreg = _winreg()
    if not winreg:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
        return True
    except OSError:
        return False


def set_autostart(enable: bool):
    winreg = _winreg()
    if not winreg:
        raise RuntimeError("当前平台不支持注册表自启")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Run",
                        0, winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------


class App:
    def __init__(self, root: tk.Tk, auto_start: bool = True):
        self.root = root
        self.cfg_path = os.path.join(BASE_DIR, core.CONFIG_FILE)
        self.cred_path = os.path.join(BASE_DIR, core.CREDENTIAL_FILE)
        self.cfg = core.load_config(self.cfg_path)
        try:
            self.creds = core.load_credentials(self.cred_path)
        except OSError:
            self.creds = None
        self.engine = None
        self.worker = None
        self.log_queue = queue.Queue()
        self.action_queue = queue.Queue()  # 托盘线程只入队，UI 线程统一消费
        self.tray = None
        core.log = self._queue_log  # 界面接管日志输出（密码永不入日志）

        self._build_ui()
        self._init_tray()
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_close)
        self.root.after(100, self._poll_logs)
        self.root.after(150, self._poll_actions)
        if auto_start and self.auto_reconnect_var.get():
            self.start_monitor()

    # ---- UI 构建 ----

    def _build_ui(self):
        self.root.title("校园网自动登录 - 设置")
        self.root.minsize(620, 680)

        pad = {"padx": 10, "pady": 6}

        # 状态区
        frm_state = ttk.LabelFrame(self.root, text="状态")
        frm_state.pack(fill="x", **pad)
        self.state_var = tk.StringVar(value="未启动")
        self.detail_var = tk.StringVar(value="探测与登录均由后台引擎完成，登录成败以门户复测为准")
        ttk.Label(frm_state, text="当前状态：").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self.state_label = ttk.Label(frm_state, textvariable=self.state_var, font=("", 11, "bold"))
        self.state_label.grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(frm_state, textvariable=self.detail_var, foreground="#666666").grid(
            row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 6))
        ttk.Button(frm_state, text="立即登录", command=self.on_login_now).grid(row=0, column=2, padx=8)
        ttk.Button(frm_state, text="探测一次", command=self.on_probe_once).grid(row=0, column=3, padx=8)

        # 账号区
        frm_acc = ttk.LabelFrame(self.root, text="账号（预存，DPAPI 加密保存）")
        frm_acc.pack(fill="x", **pad)
        ttk.Label(frm_acc, text="账号：").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self.user_entry = ttk.Entry(frm_acc, width=24)
        self.user_entry.grid(row=0, column=1, sticky="w", padx=4)
        if self.creds:
            self.user_entry.insert(0, self.creds[0])
        ttk.Label(frm_acc, text="密码：").grid(row=0, column=2, sticky="w", padx=8)
        self.pass_entry = ttk.Entry(frm_acc, width=24, show="*")
        self.pass_entry.grid(row=0, column=3, sticky="w", padx=4)
        if self.creds:
            self.pass_entry.insert(0, self.creds[1])
        ttk.Button(frm_acc, text="保存账号", command=self.on_save_account).grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=6)
        ttk.Button(frm_acc, text="测试登录（密码预检）", command=self.on_test_account).grid(row=1, column=2, columnspan=2, sticky="w", padx=8, pady=6)

        # 运行设置
        frm_run = ttk.LabelFrame(self.root, text="运行设置")
        frm_run.pack(fill="x", **pad)
        self.auto_reconnect_var = tk.BooleanVar(value=True)
        self.autostart_var = tk.BooleanVar(value=get_autostart())
        ttk.Checkbutton(frm_run, text="自动重连（断网自动登录）", variable=self.auto_reconnect_var).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(frm_run, text="开机自启（静默启动并最小化）", variable=self.autostart_var,
                        command=self.on_toggle_autostart).grid(row=0, column=2, columnspan=2, sticky="w", padx=8, pady=2)

        ttk.Label(frm_run, text="探测间隔（秒）：").grid(row=1, column=0, sticky="w", padx=8, pady=2)
        self.interval_var = tk.StringVar(value=str(self.cfg.get("probe_interval_sec", 5)))
        ttk.Spinbox(frm_run, from_=2, to=120, textvariable=self.interval_var, width=6).grid(row=1, column=1, sticky="w")
        ttk.Label(frm_run, text="静默时段（如 00:00-06:00）：").grid(row=1, column=2, sticky="w", padx=8)
        self.quiet_var = tk.StringVar(value=str(self.cfg.get("quiet_hours", "")))
        ttk.Entry(frm_run, textvariable=self.quiet_var, width=14).grid(row=1, column=3, sticky="w")

        btns = ttk.Frame(frm_run)
        btns.grid(row=2, column=0, columnspan=4, sticky="w", padx=8, pady=6)
        ttk.Button(btns, text="保存设置", command=self.on_save_settings).pack(side="left", padx=4)
        self.start_btn = ttk.Button(btns, text="启动监控", command=self.start_monitor)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btns, text="停止监控", command=self.stop_monitor, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="退出程序", command=self.on_exit).pack(side="left", padx=4)

        # Clash 共存
        frm_clash = ttk.LabelFrame(self.root, text="Clash 共存")
        frm_clash.pack(fill="x", **pad)
        ttk.Label(frm_clash, text="生成直连规则片段（portal IP / 私网段 / 连通性检测域名 → DIRECT），",
                  foreground="#666666").grid(row=0, column=0, sticky="w", padx=8, pady=(6, 0))
        ttk.Label(frm_clash, text="合并进 mihomo 配置或 Merge/覆写文件后即可与 Clash 长期共存。",
                  foreground="#666666").grid(row=1, column=0, sticky="w", padx=8)
        ttk.Button(frm_clash, text="生成 Clash 直连规则文件", command=self.on_write_clash_rules).grid(
            row=2, column=0, sticky="w", padx=8, pady=8)

        # 日志
        frm_log = ttk.LabelFrame(self.root, text="日志（不含密码）")
        frm_log.pack(fill="both", expand=True, **pad)
        self.log_text = scrolledtext.ScrolledText(frm_log, height=12, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)
        ttk.Button(frm_log, text="清空日志", command=self.on_clear_log).pack(anchor="e", padx=8, pady=(0, 8))

    # ---- 日志 ----

    def _queue_log(self, state, msg):
        self.log_queue.put("[%s] [%s] %s" % (datetime.now().strftime("%H:%M:%S"), state, msg))

    def _poll_logs(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n")
                lines = int(self.log_text.index("end-1c").split(".")[0])
                if lines > MAX_LOG_LINES:
                    self.log_text.delete("1.0", "2.0")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_logs)

    def _set_state(self, state, detail=""):
        def apply():
            self.state_var.set(state)
            if detail:
                self.detail_var.set(detail)
            if self.tray:
                self.tray.set_tip("校园网自动登录 - %s" % state)
        self.root.after(0, apply)

    # ---- 托盘 ----

    def _init_tray(self):
        candidates = []
        if getattr(sys, "frozen", False):  # 打包内图标（sys._MEIPASS）
            candidates.append(os.path.join(getattr(sys, "_MEIPASS", BASE_DIR), "assets", "tray.ico"))
        candidates.append(os.path.join(BASE_DIR, "assets", "tray.ico"))  # exe/脚本旁兜底
        icon_path = next((p for p in candidates if p and os.path.exists(p)), None)
        if not tray_icon.available:
            self._queue_log("TRAY", "托盘不可用（缺少 pywin32），降级为纯窗口模式")
            return
        if not icon_path:
            self._queue_log("TRAY", "未找到托盘图标 tray.ico，降级为纯窗口模式")
            return
        self.tray = tray_icon.TrayIcon("校园网自动登录", icon_path, self.action_queue.put)
        self.tray.start()
        self._queue_log("TRAY", "托盘已启用：左键显示窗口，右键弹出操作菜单")

    def _poll_actions(self):
        try:
            while True:
                self._handle_action(self.action_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(150, self._poll_actions)

    def _handle_action(self, action):
        if action == "show":
            self.root.deiconify()
            self.root.lift()
        elif action == "login":
            self.on_login_now()
        elif action == "start":
            self.start_monitor()
        elif action == "stop":
            self.stop_monitor()
        elif action == "exit":
            self.on_exit()

    def on_window_close(self):
        if self.tray:
            self.root.withdraw()
            self._queue_log("TRAY", "已最小化到托盘（右键托盘图标 → 退出 可彻底退出）")
        else:
            self.on_exit()

    def on_exit(self):
        if self.engine:
            self.engine.stop = True
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    # ---- 监控线程 ----

    def _current_creds(self):
        user = self.user_entry.get().strip()
        password = self.pass_entry.get()
        if user and password:
            return user, password
        return self.creds

    def start_monitor(self):
        if self.worker and self.worker.is_alive():
            return
        creds = self._current_creds()
        if not creds:
            messagebox.showwarning("缺少账号", "请先填写账号密码并点击「保存账号」")
            return
        quiet = core.parse_quiet_window(self.quiet_var.get())
        self.engine = core.Engine(self.cfg, creds, quiet_window=quiet, interruptible_sleep=True)
        self.worker = threading.Thread(target=self._monitor_loop, daemon=True)
        self.worker.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._queue_log(core.STATE_INIT, "监控已启动（静默时段：%s）" % (self.quiet_var.get() or "未启用"))

    def stop_monitor(self):
        if self.engine:
            self.engine.stop = True
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._queue_log(core.STATE_INIT, "停止指令已发出，监控将在当前休眠结束后停止")
        self._set_state("已停止")

    def _monitor_loop(self):
        while self.engine and not self.engine.stop:
            try:
                self.engine.step()
            except Exception as exc:  # 引擎异常不退出，退避后继续
                self._queue_log("ERROR", "监控异常：%s" % exc)
                self.engine._sleep(5)
            self._set_state(self.engine.state)

    # ---- 按钮动作 ----

    def on_save_account(self):
        user = self.user_entry.get().strip()
        password = self.pass_entry.get()
        if not user or not password:
            messagebox.showwarning("输入不完整", "账号和密码都不能为空")
            return
        try:
            core.save_credentials(self.cred_path, user, password)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self.creds = (user, password)
        self._queue_log("ACCOUNT", "账号已保存（DPAPI 加密，仅当前 Windows 用户可解密）")

    def on_test_account(self):
        creds = self._current_creds()
        if not creds:
            messagebox.showwarning("缺少账号", "请先填写账号密码")
            return

        def work():
            self._queue_log("TEST", "正在调用密码预检接口……")
            try:
                page = self.cfg["portal_base"].rstrip("/") + "/webauth.do"
                code, msg = core.check_user_pwd(page, creds[0], creds[1],
                                                float(self.cfg["request_timeout_sec"]))
                self._queue_log("TEST", "预检结果：%s" % msg)
            except Exception as exc:
                self._queue_log("TEST", "预检失败：%s" % exc)

        threading.Thread(target=work, daemon=True).start()

    def on_login_now(self):
        creds = self._current_creds()
        if not creds:
            messagebox.showwarning("缺少账号", "请先填写账号密码")
            return

        def work():
            try:
                state, loc = core.probe(self.cfg["detect_url"], self.cfg["portal_base"],
                                        float(self.cfg["request_timeout_sec"]))
                if state == core.STATE_ONLINE:
                    self._queue_log(core.STATE_ONLINE, "已在线，无需登录")
                    return
                page = loc or (self.cfg["portal_base"].rstrip("/") + "/webauth.do")
                hint, detail = core.login(page, creds[0], creds[1], self.cfg["portal_base"],
                                          float(self.cfg["request_timeout_sec"]))
                self._queue_log(core.STATE_LOGGING_IN, "提交完成（%s）目标: %s" % (hint, detail))
                state2, _ = core.probe(self.cfg["detect_url"], self.cfg["portal_base"],
                                       float(self.cfg["request_timeout_sec"]))
                self._queue_log(state2, "复测结果：%s" % state2)
                self._set_state(state2)
            except Exception as exc:
                self._queue_log("ERROR", "登录失败：%s" % exc)

        threading.Thread(target=work, daemon=True).start()

    def on_probe_once(self):
        def work():
            try:
                state, loc = core.probe(self.cfg["detect_url"], self.cfg["portal_base"],
                                        float(self.cfg["request_timeout_sec"]))
                self._queue_log(state, "探测结果：%s%s" % (state, ("｜" + loc) if loc else ""))
                self._set_state(state)
            except Exception as exc:
                self._queue_log("ERROR", "探测失败：%s" % exc)

        threading.Thread(target=work, daemon=True).start()

    def on_save_settings(self):
        try:
            self.cfg["probe_interval_sec"] = max(2, int(self.interval_var.get()))
        except ValueError:
            messagebox.showwarning("格式错误", "探测间隔必须是整数秒")
            return
        self.cfg["quiet_hours"] = self.quiet_var.get().strip()
        tmp_path = self.cfg_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.cfg_path)  # 原子替换，避免崩溃留下空配置
        self._queue_log("CONFIG", "设置已保存：%s" % self.cfg_path)

    def on_toggle_autostart(self):
        try:
            set_autostart(self.autostart_var.get())
            self._queue_log("AUTOSTART", "开机自启已%s" % ("开启" if self.autostart_var.get() else "关闭"))
        except Exception as exc:
            self.autostart_var.set(not self.autostart_var.get())
            messagebox.showerror("设置失败", str(exc))

    def on_write_clash_rules(self):
        path = os.path.join(BASE_DIR, "clash_campus_direct.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(CLASH_SNIPPET)
        self._queue_log("CLASH", "已生成直连规则：%s" % path)
        try:
            os.startfile(path)  # 用默认编辑器打开，便于复制/合并
        except OSError:
            pass

    def on_clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # （关闭窗口 / 退出逻辑见 on_window_close / on_exit）


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def run_diag() -> int:
    """自诊断：输出到控制台并写入数据目录 diag.txt（窗口版排障用）。"""
    cfg_path = os.path.join(BASE_DIR, core.CONFIG_FILE)
    cred_path = os.path.join(BASE_DIR, core.CREDENTIAL_FILE)
    icon_bundled = os.path.join(getattr(sys, "_MEIPASS", ""), "assets", "tray.ico")
    icon_beside = os.path.join(BASE_DIR, "assets", "tray.ico")
    lines = [
        "frozen=%s" % bool(getattr(sys, "frozen", False)),
        "BASE_DIR=%s" % BASE_DIR,
        "_MEIPASS=%s" % getattr(sys, "_MEIPASS", ""),
        "tray_available(pywin32)=%s" % tray_icon.available,
        "icon_bundled=%s exists=%s" % (icon_bundled, os.path.exists(icon_bundled)),
        "icon_beside=%s exists=%s" % (icon_beside, os.path.exists(icon_beside)),
        "config_exists=%s" % os.path.exists(cfg_path),
        "credential_exists=%s" % os.path.exists(cred_path),
    ]
    text = "\n".join(lines)
    print(text)
    try:
        with open(os.path.join(BASE_DIR, "diag.txt"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass
    return 0


def selfcheck() -> int:
    root = tk.Tk()
    root.withdraw()
    app = App(root, auto_start=False)
    root.update_idletasks()
    print("gui selfcheck PASS：界面构建正常｜已保存凭据=%s｜开机自启=%s｜托盘=%s" % (
        "是" if app.creds else "否", "是" if get_autostart() else "否",
        "是" if app.tray else "否"))
    app.on_exit()
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--selfcheck" in argv:
        return selfcheck()
    if "--diag" in argv:
        return run_diag()
    if not core.acquire_single_instance():
        print("已有实例在运行，退出")
        return 1
    root = tk.Tk()
    app = App(root, auto_start=True)
    if "--minimized" in argv:
        root.withdraw()  # 托盘模式：不占任务栏
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
