# -*- coding: utf-8 -*-
"""系统托盘图标（pywin32 实现）。

gui.py 依赖本模块提供托盘能力；当 pywin32 缺失或图标文件不存在时，
gui.py 自动降级为纯窗口模式（模块的 available 属性用于探测）。
回调 on_action(cmd) 可能在托盘线程触发，gui.py 侧只做入队，不做 UI 操作。
"""
from __future__ import annotations

import threading

try:
    import win32api
    import win32con
    import win32gui
    available = True
except ImportError:
    win32api = win32con = win32gui = None
    available = False

if available:
    WM_TRAYICON = win32con.WM_USER + 1
else:  # 占位，保证模块可导入
    WM_TRAYICON = 0x8001

# 托盘菜单命令 ID
MENU_SHOW = 101
MENU_LOGIN = 102
MENU_START = 103
MENU_STOP = 104
MENU_EXIT = 105

_TRAY_ID = 1024
_CLASS_NAME = "CampusAutoLoginTrayWindow"


class TrayIcon:
    """单实例托盘图标：左键/双击显示窗口，右键弹出操作菜单。"""

    def __init__(self, tip: str, icon_path: str, on_action):
        self.tip = tip
        self.icon_path = icon_path
        self.on_action = on_action
        self.hwnd = None
        self.hicon = None
        self._thread = None

    # ---- 生命周期 ----

    def start(self):
        if not available:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="tray-icon")
        self._thread.start()

    def stop(self):
        if self.hwnd and win32gui:
            win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)

    def set_tip(self, tip: str):
        self.tip = tip
        self._notify(win32gui.NIM_MODIFY)

    # ---- Win32 消息循环（独立线程） ----

    def _run(self):
        message_map = {
            win32con.WM_DESTROY: self._on_destroy,
            win32con.WM_COMMAND: self._on_command,
            WM_TRAYICON: self._on_tray,
        }
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = _CLASS_NAME
        wc.lpfnWndProc = message_map
        try:
            win32gui.RegisterClass(wc)
        except win32gui.error:
            pass  # 类已注册（例如同进程重建）
        self.hwnd = win32gui.CreateWindow(
            _CLASS_NAME, "CampusAutoLoginTray",
            0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
        self.hicon = win32gui.LoadImage(
            wc.hInstance, self.icon_path, win32con.IMAGE_ICON, 0, 0,
            win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE)
        self._notify(win32gui.NIM_ADD)
        win32gui.PumpMessages()

    def _notify(self, op: int):
        if not self.hwnd:
            return
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        nid = (self.hwnd, _TRAY_ID, flags, WM_TRAYICON, self.hicon, self.tip)
        try:
            win32gui.Shell_NotifyIcon(op, nid)
        except win32gui.error:
            pass  # 资源管理器重启等场景忽略

    def _on_tray(self, hwnd, msg, wparam, lparam):
        if lparam in (win32con.WM_RBUTTONUP, win32con.WM_CONTEXTMENU):
            self._show_menu()
        elif lparam in (win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK):
            self.on_action("show")
        return 0

    def _show_menu(self):
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_SHOW, "显示窗口")
        win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_LOGIN, "立即登录")
        win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_START, "启动监控")
        win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_STOP, "停止监控")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_EXIT, "退出")
        x, y = win32gui.GetCursorPos()
        win32gui.SetForegroundWindow(self.hwnd)
        win32gui.TrackPopupMenu(menu, win32con.TPM_RIGHTBUTTON, x, y, 0, self.hwnd, None)
        win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        win32gui.DestroyMenu(menu)

    def _on_command(self, hwnd, msg, wparam, lparam):
        cmd = win32api.LOWORD(wparam)
        mapping = {
            MENU_SHOW: "show", MENU_LOGIN: "login", MENU_START: "start",
            MENU_STOP: "stop", MENU_EXIT: "exit",
        }
        if cmd in mapping:
            self.on_action(mapping[cmd])
        return 0

    def _on_destroy(self, hwnd, msg, wparam, lparam):
        self._notify(win32gui.NIM_DELETE)
        win32gui.PostQuitMessage(0)
        return 0
