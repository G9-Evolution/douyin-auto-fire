# douyin-auto-fire 部署交接文档

> **新对话接手先看本节**：下方 v2 多账号内容是历史交接记录，账号与定时状态以本节和系统实际查询为准。
> 最近更新：2026-10-01（v2.4.1，配置验证记录与安全交付基线）

## 当前运行状态

- 本机控制台：`http://127.0.0.1:8734`，仅负责配置、手动运行和查看状态；关闭服务不影响 Windows 定时任务。
- 当前启用账号：account1「Og」每天 17:20、account2「G9」每天 20:25；分别由 `CodexDouyinFire-account1`、`CodexDouyinFire-account2` 执行。account3 已停用。
- 旧 `DouyinAutoFire`、`DouyinAutoFire-account1`、`DouyinAutoFire-account2` 均已停用，保留任务定义供核对。
- 已验证：当前 v2.4.1 代码 223 项自动测试通过；Og、G9 各自的无发送试运行成功，发送数均为 0，两个账号的配置验证状态均为 passed。Og 的计划任务在 2026-10-01 17:20 已触发且结果码 0；截至 20:09，G9 当日 20:25 尚未到点。程序侧成功不等于抖音客户端到达或火花验收。
- 手动运行只作用于界面选中的账号；缺少账号、未知账号或停用账号会被后端拒绝。
- 虚拟环境现使用独立的 uv 管理 Python 3.14.7，依赖版本见 `requirements.lock.txt`；旧豆包环境保留在 `.venv-doubao-backup` 供回退。
- 当前版本的安全交付和复现步骤见 `DELIVERY.md`；旧交接 ZIP 含凭证文件且不对应当前代码，不能直接交付。
- 保存账号、好友、消息或时间后，总览显示“待验证”；对应账号无发送试运行成功后留下 `artifacts/<账号>/validation.json`。该状态仅提示，不暂停定时。
- 账号凭证文件已加入 `.gitignore`。若出现抖音侧登录态失效，仍需本人扫码。

---

## 一、历史状态（v2 多账号正式版）

| 项目 | 状态 |
|---|---|
| 项目位置 | `E:\豆包项目库\自动续火花`（Windows 本机） |
| 部署方式 | Windows 本机 + 本地 Web 控制台（`http://127.0.0.1:8734`）+ 任务计划程序 |
| 启动方式 | `启动控制台.bat`（双击启动服务并打开浏览器） |
| 账号模式 | **多账号**：account1「Og」（默认）、account2「小顾」，各自独立任务/凭证/定时 |
| 登录凭证 | 每账号独立 `storage-state-account<id>.json` + `.env.account<id>`（`DOUYIN_STORAGE_STATE` 指向） |
| 任务文件 | 每账号独立 `config/tasks/<id>.json`（好友/消息/间隔/防重复），根 `config.json` 为默认账号 |
| 计划任务 | `DouyinAutoFire`（默认 17:20）、`DouyinAutoFire-account1`（08:30，已建），命名规则 `DouyinAutoFire[-<id>]` |
| 表情库 | `assets/stickers/` 209 个抖音真实表情（webp），`config/stickers.json` 发送映射、`stickers_catalog.json` 前端目录（常用 23 / 贴纸A 136 / 贴纸B 50） |
| 验证记录 | 前端全链路 Playwright 实测 ✅（切换/独立数据/表情选择/保存）、后端 API ✅、run.py 账号分流 ✅ |
| 已知限制 | **account2 抖音侧登录态已失效**（凭证文件在但抖音使 cookie 失效），实际发送前需在界面「重新登录」扫码 |

## 二、v2 架构要点（每账号完全独立，勿合并）

1. **数据隔离**：`config/accounts.json` 定义账号（id/label/enabled/env_file）；每个账号独立：
   - 任务文件 `config/tasks/<id>.json`（前端读写 + run.py 读取）
   - 凭证 `.env.account<id>`（`DOUYIN_STORAGE_STATE=storage-state-account<id>.json`）
   - 计划任务 `DouyinAutoFire-<id>`（schtasks，命令带 `cmd /c "cd /d <项目根> && ... run.py --account <id>"` 保证工作目录）
   - 运行记录 `artifacts/<id>/`（history/result）
2. **前端**：`web/index.html` 顶栏账号切换器（`#cur-account`），切换时 `loadAll()` 按 `?account=` 重拉 bootstrap，所有视图（总览/好友/内容/定时/记录）实时重渲染；`loadSeq` 序号防护防止旧响应覆盖新数据（已修复 refreshRunLock 竞态）。
3. **发送链路**：`run.py [--account <id>]` → `app/account_runner.run_all_accounts(only=...)` 只跑指定账号；`app/sender.py` 原生表情按 `tab_index` 点分类 tab 后选表情（面板 `.componentsemojiemojiPanel`、tab `.emojiEmojisModalTabsubTab`、item `.emojiEmojiItememojiItem`）。
4. **API 路由**：`/api/bootstrap|config|schedule|logs[?account=]`、`/api/stickers`、`/assets/stickers/<分类N-xxx.webp>`（需 urllib.unquote 解码中文路径）、`POST /api/run` body `{dry_run, account}`。

## 三、关键事实（避免重新踩坑）

1. **好友名必须用「小顾」（备注名），不能用「诗酒」**：抖音私信列表显示的是用户设置的**备注名**。
2. **登录态「重启后要重登」根因已确认 = 抖音侧 cookie 失效，非本地存档丢失**：storage-state 文件都在且新（00:12/00:24 更新），但 02:21 dry-run 报 `AuthenticationError: 进入抖音私信页面后登录状态失效`。抖音网页版登录态会被服务端踢掉（无头环境/异地/时长），界面「凭证已保存」≠抖音侧有效。处理：账号卡点「重新登录」弹浏览器扫码（`login_auto.py --account <id>`，支持 `--account`）。
3. **定时任务工作目录**：schtasks `/create` 不支持设置工作目录（Start In=N/A），必须用 `cmd /c "cd /d <项目根> && <python> run.py [--account <id>]"` 形式，否则相对路径配置读不到。
4. **账号文件完整性**：`config/accounts.json` 每个条目必须有 `env_file` 字段（run.py 严格校验），`.env.account<id>` 必须有 `TASK_CONFIG=config/tasks/<id>.json`。
5. **服务运维**：服务仅回环 `127.0.0.1:8734`；重启 = 查 8734 端口进程 → Stop-Process → `Start-Process ".\.venv\Scripts\python.exe" -ArgumentList "web_server.py" -WorkingDirectory <项目根> -WindowStyle Hidden`。PowerShell 复杂命令（&& 链 + 内嵌引号）会被 PS 解析吞掉，须拆简单命令逐条执行。
6. **环境**：Python 3.14.7，虚拟环境 `.\.venv`，Playwright 1.63.0，Chromium 1243。
7. **表情爬取**：`scripts/scrape_stickers.py [--account <id>] [--headful]`（Playwright 打开私信→表情面板→遍历分类 tab，disabled 跳过），产出 `config/stickers_crawled.json` → `gen_stickers.py` 合并为 `stickers.json/catalog.json`。

## 四、常用命令（在项目根目录运行）

```powershell
# 重新登录某账号（凭证失效时；弹浏览器扫码，自动保存 storage-state-account<id>.json）
.\.venv\Scripts\python.exe scripts\login_auto.py --account account2

# 检查模式（只验证登录和好友定位，不发消息；支持 --account <id> 单账号）
.\.venv\Scripts\python.exe run.py --dry-run --account account2

# 正式发送（全部启用账号）
.\.venv\Scripts\python.exe run.py

# 查看日志 / 结果
Get-Content .\artifacts\run.log -Tail 50
Get-Content .\artifacts\account2\result.json

# 重建固定版本依赖（需先建立独立 Python 虚拟环境）
uv pip sync --python .\.venv\Scripts\python.exe requirements.lock.txt
```

## 五、敏感文件清单（勿公开/勿外传）

- `storage-state*.json`（各账号登录凭证）
- `.env*`（含通知密钥等配置）
- `config/`（好友/消息/账号配置，含 accounts.json）
- `artifacts/`（运行日志、截图、发送历史）
- `login_debug/`（登录诊断截图，含脱敏手机号，确认后可删）

## 六、v2 遗留与待办

- account2 抖音侧登录态失效 → 需要用户界面「重新登录」扫码（每次失效都如此，属抖音侧行为）。
- account1（Og）好友列表为空 → 等待用户自行配置续火花好友。
- account2 定时时间未设置（account1 已设 08:30；默认账号 17:20）。
- 表情「贴纸A/B」无中文名（爬取时面板未显示 desc），按索引显示（如 贴纸A-3）。
