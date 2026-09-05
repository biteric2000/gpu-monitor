# GPU 节点监控与预约系统 · 实现规格（Windows 一期）

> 本文件是开发规格，代码必须与此一致。范围：仅 Windows；只做一期（F1–F9），不做历史曲线（F10）、到期提醒（F11）、Linux/systemd。

## 1. 架构

- Client：每 5 秒采集本机 GPU → `POST /api/report`（Bearer Token）。
- Server：FastAPI + Uvicorn，单进程。最新快照存**内存 dict**（不写库）；预约存 **SQLite**（标准库 sqlite3，不要 sqlmodel）；账号在 **config.yaml**（bcrypt hash）。
- 看板：单页 HTML + 原生 JS，浏览器每 4 秒拉 `GET /api/nodes`。
- 不做硬隔离，预约 = 看板 + 君子协定。
- 时间：`last_seen`、`start_ts`、`end_ts` 一律用 **Server 收到/处理时的 Unix 时间戳（time.time()）**，不信 Client 时钟。

## 2. 目录

```
gpu-monitor/
├── SPEC.md                  # 本文件
├── .venv/                   # 共享虚拟环境（已建好，勿动）
├── server/
│   ├── main.py
│   ├── config.yaml
│   ├── data/app.db          # 运行时自动创建
│   ├── static/index.html
│   ├── static/login.html
│   ├── static/app.js
│   ├── static/style.css
│   └── requirements.txt
└── client/
    ├── agent.py
    ├── config.yaml
    ├── agent.log            # 运行时生成
    └── requirements.txt
```

## 3. Client（client/agent.py）

### 3.1 config.yaml

```yaml
server_url: "http://127.0.0.1:8000"
token: "<32位十六进制随机串>"
interval_sec: 5
node_id: ""        # 空则用 socket.gethostname()
display_name: ""   # 空则同 node_id
```

### 3.2 采集

- 启动时 `pynvml.nvmlInit()` 一次，退出（finally/atexit）`nvmlShutdown()`。
- 每张 GPU 采集字段：

```
index, name, util_percent, mem_used_mb, mem_total_mb,
temperature_c | null, power_w | null, pids: [int]
```

- 温度、功耗、进程列表**分别 try/except**：缺功耗的驱动/笔记本不能让整次上报失败，缺失填 null。
- 名称兼容 str/bytes（`isinstance(b, bytes)` 则 `b.decode()`）。
- 占卡进程：对每张卡取 `nvmlDeviceGetComputeRunningProcesses` + `nvmlDeviceGetGraphicsRunningProcesses`，PID 去重。
- 进程信息用 `psutil.Process(pid)` 取 `name()`、`cmdline()`（join 后截断 200 字符）、`memory_info().rss`（换算 MB）。进程可能已退出 → try/except 跳过。
- 额外打标：进程名或 cmdline 小写后包含 `llama` / `ollama` / `vllm` / `sglang` 之一 → `is_llm: true`，否则 false（仅 UI 高亮用）。
- **NVML 完全不可用**（nvmlInit 失败或枚举失败）：日志记错误，上报 `gpus: []` 和 `error: "<错误信息>"`，节点仍算在线（Agent 活着）。
- 不要每 5 秒 `psutil.process_iter()` 全机扫描；不报 CPU%。

### 3.3 上报

```
POST {server_url}/api/report
Authorization: Bearer <token>
Content-Type: application/json

{
  "node_id": "pc-4090-a",
  "display_name": "客厅4090",
  "hostname": "DESKTOP-XXX",
  "gpus": [ {index, name, util_percent, mem_used_mb, mem_total_mb, temperature_c, power_w, pids} ],
  "processes": [ {"pid": 1234, "name": "llama-server.exe", "cmdline": "...", "mem_mb": 512, "is_llm": true, "gpu_indices": [0]} ],
  "error": null
}
```

- `gpu_indices`：该进程出现在哪些卡的 PID 列表里（1-based 用卡的 index 字段值即可）。
- 失败（网络错误、非 200）**只写日志，不退出、不重试堆积**。
- `print` 在 pythonw 下不可见：所有日志写 `agent.log`（append，每行带时间戳；简单 append 即可）。
- 主循环 `while True: collect → post → sleep(interval_sec)`，只启动一次。

### 3.4 无黑窗口（F4）

- 不 `subprocess` 调 nvidia-smi；采集全走 NVML。
- 日常运行：`pythonw.exe agent.py`（工作目录 client/）。代码里不需要任何 Windows 注册逻辑。

### 3.5 测试钩子

`python agent.py --once`：采集一次，把上报 JSON **打印到 stdout**（不 POST），退出。用于验证采集正确，不进日志。

## 4. Server（server/main.py）

### 4.1 config.yaml

```yaml
host: "0.0.0.0"
port: 8000
client_token: "<与 client/config.yaml 的 token 相同>"
session_secret: "<另一串 32+ 位随机值>"
offline_threshold_sec: 20
default_reserve_minutes: 30
max_reserve_minutes: 480
users:
  - username: admin
    password_hash: "<bcrypt hash>"
    role: admin
  - username: alice
    password_hash: "<bcrypt hash>"
    role: user
```

### 4.2 存储

- 内存：`nodes: dict[node_id, snapshot]`，snapshot 含 last_seen 与最近一次 report 的全部内容。
- SQLite 表：

```sql
CREATE TABLE IF NOT EXISTS reservations (
  node_id  TEXT PRIMARY KEY,
  user     TEXT NOT NULL,
  start_ts REAL NOT NULL,
  end_ts   REAL NOT NULL
);
```

- 一期一节点一行；写入新预约时覆盖旧行（INSERT OR REPLACE）。过期行留着，读时按 `end_ts < now` 判空闲。
- 账号校验：`bcrypt.checkpw`，失败 401。

### 4.3 接口

| 方法 | 路径 | 鉴权 | 行为 |
|---|---|---|---|
| POST | /api/report | Bearer client_token | 更新内存快照（last_seen=now）。token 不符 401 |
| GET | /api/nodes | 已登录 Session | 全部节点 + 在线状态 + 有效预约。未登录 401 |
| POST | /api/reserve | 已登录 | body: {node_id, minutes?}；默认 default_reserve_minutes；本人对有效预约再 reserve = 从现在重新计时（续期） |
| POST | /api/reserve/renew | 已登录 | body: {node_id, minutes?}；仅本人；end_ts = now + minutes |
| POST | /api/reserve/release | 已登录 | body: {node_id}；本人可释放自己的；admin 可释放任意 |
| GET/POST | /login | — | Session 登录页 / 校验 |
| GET | /logout | — | 清 session 跳回 /login |

规则：
- 预约对象是**整机**；同节点同时只能一条有效预约。
- 本人对已有效预约再次 reserve / renew = 从现在（renew）重新计时到 now+minutes。
- 他人占用中（有效且 user≠当前用户）reserve → **409**，detail 说明被谁占用。
- 时长校验：`1 <= minutes <= max_reserve_minutes`，否则 422。
- `ReserveRequest` 的 body **不含 user**（user 取 Session）。
- 释放不存在的/已过期的预约 → 幂等成功（200）。
- 未登录访问 API → 401 JSON；未登录访问页面（/ 或 /login 之外）→ 302 跳 /login。
- Session：`SessionMiddleware`，`secret=cfg.session_secret`，`https_only=False`；session 存 `{username, role}`。

### 4.4 GET /api/nodes 响应

```
[
  {
    "node_id": "...", "display_name": "...", "hostname": "...",
    "online": true,                      # now - last_seen <= offline_threshold_sec
    "last_seen": 1756900000.0,
    "error": null,
    "gpus": [ ... ], "processes": [ ... ],
    "reservation": null | {"user": "alice", "start_ts": ..., "end_ts": ..., "mine": true}
  }
]
```

- 无有效预约（含已过期）时 `reservation: null`；`mine` 相对当前 Session 用户。
- 内存里从未见过的 node_id 不出现在列表。

### 4.5 前端（static/）

- `login.html`：账号+密码表单，POST /login，失败显示错误，不存密码。
- `index.html` + `app.js` + `style.css`：看板，中文界面。
  - 每张节点卡片：display_name + 绿/红在线圆点（离线红点）；error 非空时显示「无 GPU 数据：<error>」。
  - 每张 GPU：名称、利用率进度条（百分比）、显存 used/total MB、温度（null 显示 --）、功耗（null 显示 --）。
  - 占卡进程列表：name (pid) · cmdline 截断 · mem_mb；`is_llm: true` 高亮（加色/加粗 + 「LLM」标记）。
  - 预约区：空闲 → 按钮 [30分钟] [1小时] [2小时] [自定义…（prompt 输入分钟数）]；已预约 → 「alice 占用中，至 14:32」；`mine: true` 时额外显示 [续期30m] [续期1h] [释放]；admin 对他人预约显示 [强制释放]。
  - Unix 时间戳用浏览器本地时区格式化。
  - 4 秒轮询；请求失败时页面顶部显示「Server 不可达」横幅，不白屏。
  - 不要 React/框架；Tailwind CDN 可用也可不用，原生 CSS 优先。
- 静态文件用 `StaticFiles` 挂载；`/` 未登录跳 /login，已登录进看板。

### 4.6 运行

`uvicorn main:app --host 0.0.0.0 --port 8000`（不加 --reload）。config.yaml 从 main.py 同目录读取。

## 5. 验收（每个模块交付后我会实际跑）

1. `client/.venv 共用 ../.venv`：`gpu-monitor\.venv\Scripts\python.exe client\agent.py --once` 能打出含本机 2 张 2080Ti 真实数据的 JSON。
2. Server 起来后 curl POST /api/report（带 token）→ 200；GET /api/nodes（带 session）返回该节点 online=true 且 gpus 非空。
3. 登录：正确密码 302/200，错误密码 401；未登录 /api/nodes → 401。
4. 预约：alice reserve 空闲节点 200 → nodes 里 reservation.user=alice、mine 正确；admin reserve 同节点 → 409；alice renew → end_ts 变化；minutes=9999 → 422；alice release → 200 且 reservation=null；alice 释放 admin 的预约 → 403（admin 强制释放任意节点 → 200）。
5. 浏览器看板：卡片、利用率条、进程列表、预约按钮均正常；断网 server 时显示不可达横幅。
