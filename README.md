# ZUA 校园网自动登录 · zua-campus-auto-login

> 郑州航空工业管理学院（**ZUA**）校园网自动登录工具：开机自动认证、断线自动重连、Clash 长期共存、托盘可视化、账号本地加密。
>
> **标注：`zua`** ｜ 协议：`webauth.do` 实名认证（普通表单 POST）｜ 平台：Windows 10/11

## 功能特性

- **开机自动认证**：联网探测发现未认证即自动提交登录，无需手动打开认证页
- **断线自动重连**：指数退避重试（1s 起、封顶 60s），登录结果以"重新探测"裁决，超时/丢包安全重试不重复提交
- **Clash 共存**：三层直连防线（mihomo 直连规则 + 程序强制直连 + 兜底降级），Clash 开着也能认证，支持一键生成直连规则片段
- **托盘可视化**：状态实时显示（在线/未认证/重连中），左键显示窗口、右键快捷操作，关窗收进托盘
- **账号本地加密**：DPAPI（当前 Windows 用户）加密保存，不明文落盘、不进日志、不进仓库
- **开关齐全**：开机自启、自动重连、静默时段（如 `00:00-06:00` 夜间断网免打扰）、探测间隔、密码预检（测试登录）
- **轻量**：单文件便携 exe（约 12MB）；源码模式零第三方依赖（纯标准库 + tkinter）

## 快速开始

### 方式一：单文件便携版（推荐）

1. 获取 `CampusAutoLogin.exe`（Releases 附件，或本地构建 `dist/CampusAutoLogin.exe`）
2. 双击运行 → 填写账号密码 → **保存账号** → **测试登录**（验证密码正确）
3. 勾选**自动重连**与**开机自启** → **保存设置** → **启动监控**
4. 点**生成 Clash 直连规则文件**，把片段并入 mihomo/Clash 配置（放在 `MATCH` 兜底规则之前）

> 便携使用：把 `exe` 与同目录的 `config.json`、`credential.bin` 一起拷贝即可（凭据仅限同一 Windows 用户解密）。

### 方式二：源码运行

```powershell
# 图形界面（需 Python 3.11+，tkinter）
py gui.py

# 纯命令行（零依赖，任何 Python 3.8+）
python campus_login.py --set-account   # 保存账号（仅首次）
python campus_login.py                 # 常驻监控
```

## 界面与托盘

| 操作 | 说明 |
|---|---|
| 托盘左键 / 双击 | 显示设置窗口 |
| 托盘右键 | 显示窗口 / 立即登录 / 启动监控 / 停止监控 / 退出 |
| 窗口 ✕ | 不退出，收进托盘 |
| 立即登录 | 手动触发一次"探测→登录→复测" |
| 测试登录 | 调用密码预检接口验证账号密码（不建立会话） |
| 生成 Clash 直连规则 | 生成 `clash_campus_direct.yaml` 并打开，按注释合并进配置 |

## 命令行参考（campus_login.py）

| 命令 | 作用 |
|---|---|
| `--check` | 探测一次网络状态（ONLINE / UNAUTH / OFFLINE） |
| `--show-form` | 只读抓取登录页并打印表单字段（协议确认用） |
| `--login-once` | 手动触发一次登录后退出 |
| `--test-account` | 密码预检（check=0 正确 / 2 账号不存在 / 3 密码错误） |
| `--set-account` | 保存账号密码（DPAPI 加密） |
| `--selftest` | 状态机干跑自检（无需网络与账号） |
| `--login-url <URL>` | 指定登录页完整 URL（含 wlanacip 等参数） |

图形界面另支持 `py gui.py --selfcheck`（界面自检）与 `py gui.py --diag`（自诊断，输出 `diag.txt`）。

## 配置文件 config.json

| 字段 | 默认值 | 说明 |
|---|---|---|
| `portal_base` | `http://202.196.169.166` | 认证服务器地址 |
| `detect_url` | `http://www.msftconnecttest.com/redirect` | 连通性探测地址（302 到门户即未认证） |
| `probe_interval_sec` | `5` | 在线检测间隔（秒） |
| `backoff_base_sec` / `backoff_max_sec` | `1` / `60` | 重试退避基数与上限 |
| `request_timeout_sec` | `10` | 单次请求超时 |
| `quiet_hours` | `""` | 静默时段，如 `00:00-06:00`（时段内不发起登录） |

## Clash 共存原理

认证期流量必须绕开代理直连网关，三层防线保证 Clash 全程开启：

1. **配置层**：mihomo 把 `202.196.169.166`、私网段与连通性检测域名置 `DIRECT`（`no-resolve`），探测域名加入 `fake-ip-filter`，TUN 模式用 `route-exclude-address` 排除认证网段
2. **程序层**：登录/探测请求强制直连（清空代理环境与系统代理），任何代理形态下均不经过代理
3. **兜底层**：连续失败时引导"一键写规则 / 关系统代理 / 手动开认证页"恢复

## 项目结构

```
campus-auto-login/
├── campus_login.py        # 核心引擎：探测 / 表单登录 / 断线重连 / 状态机
├── gui.py                 # 图形界面（tkinter）：设置、日志、Clash 协作
├── tray_icon.py           # 系统托盘（pywin32，缺失时自动降级纯窗口）
├── config.example.json    # 配置模板
├── assets/tray.ico        # 托盘/程序图标
├── docs/research-report/  # 调研与方案报告（含 GitHub 项目对比、协议分析）
├── LICENSE                # MIT
└── .gitignore             # 排除 dist/、config.json、credential.bin 等
```

## 开发与构建

```powershell
# 自检
python campus_login.py --selftest      # 引擎状态机不变量断言
py gui.py --selfcheck                  # 界面构建自检
py gui.py --diag                       # 运行环境自诊断

# 构建单文件 exe（图标随包）
py -m PyInstaller --noconfirm --onefile --noconsole --name CampusAutoLogin `
  --icon assets\tray.ico --add-data "assets\tray.ico;assets" gui.py
```

构建产物清单见 `dist/build_info.json`（含 SHA256、工具链版本、源码哈希，支持等价重建回滚）。

## 安全说明

- 账号密码仅以 **DPAPI（CurrentUser）密文** 保存于 `credential.bin`，`.gitignore` 强制排除，绝不提交
- 日志与界面对密码/字段值脱敏；`credential.bin` 换机器/换用户无法解密
- 登录请求全程强制直连；密码提交为门户协议本身的明文表单（HTTP），属校园网认证系统限制
- 请仅使用**本人账号**，遵守学校网络管理规定

## 常见问题

| 问题 | 处理 |
|---|---|
| Clash 开着无法认证 | 点「生成 Clash 直连规则」并合并到配置；TUN 模式片段含 `route-exclude-address` |
| 托盘图标看不到 | 任务栏「显示隐藏的图标」(^) 中查找；或运行 `--diag` 查看 `tray_icon`/图标状态 |
| 改了密码 | 界面改密码 → 保存账号 → 测试登录 |
| 换校区 / 网关参数变化 | 无需改动，`wlanuserip/mac/wlanacname` 等参数每次联网实时解析 |
| 夜间断网频繁重试 | 设置静默时段 `00:00-06:00` |

## 免责声明

本工具仅供学习与个人便捷使用，请遵守郑州航空工业管理学院校园网管理规定；使用风险自负。

## License

[MIT](LICENSE) · **标注：`zua`**
