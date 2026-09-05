# gpu-monitor

局域网 GPU 节点监控与预约看板。多台装有 NVIDIA GPU（4090/5090 等）的 Windows 机器跑本地 LLM（llama.cpp / vLLM / Ollama 等）时，让团队成员：

1. 在浏览器里看到各机器的 GPU 利用率、显存、温度、功耗、占卡进程；
2. 以「声明」方式独占预约某台机器一段时间，减少撞车。

**不做硬隔离**：不杀进程、不改 GPU 亲和性。预约 = 看板 + 君子协定。

## 功能

| 功能 | 说明 |
|---|---|
| GPU 实时指标 | 每张卡的利用率、显存 used/total（血条）、温度、功耗 |
| 占卡进程 | NVML PID + 进程名/命令行/内存，按内存倒序，LLM 服务高亮（llama/ollama/vllm/sglang） |
| 在线状态 | 超 20 秒未上报 → 灰点 +「离线」徽章 |
| 近期活跃提醒 | 半小时内计算占用 ≥50% 但没人预约 → 黄色「近期活跃」徽章 + 红色「距上次活跃 x 分钟前」，提示可能有人忘了释放 |
| 登录 | Session Cookie；账号在 `config.yaml`（bcrypt 哈希） |
| 预约 | 整机独占；30m / 1h / 2h / 自定义（≤8h）；本人可续期（在剩余时间上累加）、提前释放；到期自动失效 |
| 冲突处理 | 他人占用中禁止抢占（409）；admin 可强制释放 |
| 节点管理 | admin 可手动移除过时节点（两段式确认）；同一台机器改 node_id 后凭机器指纹自动合并，不留幽灵条目 |
| 节点排序 | 看板按 node_id 升序（相同则按显示名），便于快速定位 |
| 状态徽章 | 离线(灰) > 已预约(红) > 近期活跃(黄) > 高占用(红) > 空闲(绿) |

## 架构

```
Client A (长驻, 每 5s) ──POST /api/report + Bearer Token──┐
Client B (长驻, 每 5s) ──POST /api/report + Bearer Token──┼─▶ Server (FastAPI + Uvicorn)
                                                          │        │
浏览器 ──登录 / 看板 / 预约 (4s 轮询)──────────────────────┘        ├─ 内存 dict：最新 GPU/进程快照
                                                                  └─ SQLite：预约 + 机器指纹映射
```

- 指标只存**内存**（5 秒一刷不落库）；预约存 **SQLite**（重启不丢）
- Client→Server 带共享 Token；Web 用 Session Cookie（局域网 HTTP）
- 前端：单页 HTML + 原生 JS，无框架、无构建

## 目录

```
gpu-monitor/
├── server/
│   ├── main.py            # 全部后端（上报/看板/登录/预约/节点管理）
│   ├── config.yaml        # 端口、token、账号（复制 config.example.yaml 填写）
│   ├── data/app.db        # SQLite，自动创建
│   ├── static/            # index.html / login.html / app.js / style.css
│   ├── requirements.txt
│   ├── setup.bat          # 建 venv + 装依赖
│   ├── start_server.bat   # 启动
│   └── stop_server.bat    # 停止（按命令行匹配进程，路径无关）
└── client/
    ├── agent.py           # 采集 + 上报（NVML + psutil）
    ├── config.yaml        # server_url / token / node_id
    ├── agent.log          # 运行日志
    ├── requirements.txt
    ├── setup.bat
    ├── start_client.bat
    └── stop_client.bat
```

## 快速开始（Windows）

### 1. Server 机

```bat
cd server
setup.bat                    :: 首次：建 venv + pip install
notepad config.yaml          :: 改端口/token/账号（见下）
start_server.bat
```

浏览器访问 `http://<server-ip>:<port>` 登录。

**生成账号密码哈希**（config 里存哈希，不存明文）：

```bat
..\.venv\Scripts\python.exe -c "import bcrypt; print(bcrypt.hashpw(input('pwd: ').encode(), bcrypt.gensalt()).decode())"
```

**生成随机 token / secret**：

```bat
python -c "import secrets; print(secrets.token_hex(16))"
```

### 2. Client 机（每台 GPU 机器）

```bat
cd client
setup.bat
notepad config.yaml          :: 填 server_url 和 token（与 server 相同）
start_client.bat
```

`config.yaml` 字段：

| 字段 | 说明 |
|---|---|
| `server_url` | `http://<server-ip>:<port>` |
| `token` | 与 server 的 `client_token` 相同 |
| `interval_sec` | 上报间隔，默认 5 秒 |
| `node_id` | 节点 ID（看板主键、预约对象）。空则用 hostname；**多机同名时必须手填** |
| `display_name` | 看板显示名，空则用 node_id |

### 3. 开机自启（可选）

- **日常**：把 `start_client.bat` 的快捷方式丢进「启动」文件夹（`shell:startup`）。bat 里用的是 `pythonw` 等价物（venv 的 python，无控制台输出需求时可用 `pythonw.exe agent.py`，日志看 `client/agent.log`）
- **未登录也要采集**：用 NSSM 注册服务，指向同一命令
- Linux 用 systemd `Restart=always`（同一份 `agent.py`，`machine_id` 自动退回 MAC 地址）

## 节点身份与幽灵节点

节点主键是 `node_id`（可读、可自定义），但另有一个**机器指纹** `machine_id`（Windows 读注册表 `MachineGuid`，与 hostname/IP 无关，重装系统前不变）：

- 同一台机器改了 `node_id` 再上报 → Server **自动合并**旧条目（预约、活跃时间跟着迁移），不留「永远离线」的幽灵节点
- 旧版本 client 不带 `machine_id`，Server 兼容（按无指纹处理）
- 兜底：admin 在每张卡片右下角「移除该节点」（两段式确认，删快照 + 删该节点预约 + 清指纹映射）。该机器之后再次上报会正常重新出现

## API

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/report` | `Authorization: Bearer <token>` | Client 上报快照 |
| GET | `/api/nodes` | 已登录 | 节点 + 在线状态 + 有效预约（含 `mine` 标记） |
| POST | `/api/reserve` | 已登录 | body: `{node_id, minutes?}`；本人重复预约 = 重新计时 |
| POST | `/api/reserve/renew` | 已登录 | 本人在**现有到期时间上累加** minutes |
| POST | `/api/reserve/release` | 已登录 | 本人释放；admin 可释放任意节点 |
| POST | `/api/node/remove` | admin | 移除过时节点 |
| POST | `/login` / `GET /logout` | — | Session |

上报 body 字段：`node_id, display_name, hostname, machine_id, gpus[{index,name,util_percent,mem_used_mb,mem_total_mb,temperature_c,power_w,pids}], processes[{pid,name,cmdline,mem_mb,is_llm,gpu_indices}], error`

## 安全边界

- 仅面向**局域网**，HTTP 明文，Cookie 非 Secure——不要暴露到公网
- 防火墙只放行 server 的端口
- 无权限分级（除 admin 强制释放/移除节点）；预约不是硬隔离，别依赖它做资源保障

## 设计取舍（为什么这么简单）

- **不引入 APScheduler**：预约过期在读接口时按 `end_ts` 判断
- **不引入 ORM**：SQLite 单表 + 标准库 `sqlite3`
- **指标不落库**：5 秒一刷的快照存内存 dict；要历史曲线是二期
- **NVML 不走 subprocess**：`nvidia-ml-py` 直接调用，`pythonw` 下无黑窗口
- 个人项目规模：server 一个 `main.py`，client 一个 `agent.py`，一个人能改
