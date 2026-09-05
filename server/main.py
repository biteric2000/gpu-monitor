"""GPU 监控系统 Server（M4：上报 + 节点列表 + 看板 + 登录 + 预约）。"""
import sqlite3
import time
from pathlib import Path

import bcrypt
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
CFG = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8"))
DB_PATH = BASE_DIR / "data" / "app.db"

app = FastAPI(title="GPU Monitor")
app.add_middleware(
    SessionMiddleware, secret_key=CFG["session_secret"], https_only=False
)

# 内存节点表：node_id -> snapshot
nodes: dict[str, dict] = {}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS reservations ("
            " node_id TEXT PRIMARY KEY,"
            " user TEXT NOT NULL,"
            " start_ts REAL NOT NULL,"
            " end_ts REAL NOT NULL)"
        )
        # 机器指纹 → node_id 映射：同一台机器改 node_id 后自动合并旧条目
        conn.execute(
            "CREATE TABLE IF NOT EXISTS machine_map ("
            " machine_id TEXT PRIMARY KEY,"
            " node_id TEXT NOT NULL)"
        )


_init_db()


def _fmt_ts(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _get_active(node_id: str) -> dict | None:
    """返回该节点的有效预约（未过期），过期/无预约返回 None。"""
    row = _db().execute(
        "SELECT user, start_ts, end_ts FROM reservations WHERE node_id = ?",
        (node_id,),
    ).fetchone()
    if row is None or row["end_ts"] <= time.time():
        return None
    return {"user": row["user"], "start_ts": row["start_ts"], "end_ts": row["end_ts"]}


class ReserveRequest(BaseModel):
    node_id: str
    minutes: int | None = None


class ReleaseRequest(BaseModel):
    node_id: str


class RemoveNodeRequest(BaseModel):
    node_id: str


def _minutes(body_minutes: int | None) -> int:
    minutes = body_minutes if body_minutes is not None else CFG["default_reserve_minutes"]
    if not (1 <= minutes <= CFG["max_reserve_minutes"]):
        raise HTTPException(
            status_code=422,
            detail=f"minutes 需在 1 到 {CFG['max_reserve_minutes']} 之间",
        )
    return minutes


def _auth_report(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing Bearer token")
    token = authorization[len("Bearer "):].strip()
    if token != CFG["client_token"]:
        raise HTTPException(status_code=401, detail="invalid token")


def _merge_machine(machine_id: str, node_id: str) -> None:
    """同一台机器（machine_id 相同）换了 node_id → 把旧条目合并到新 node_id。

    内存快照迁移（保留 last_seen/last_busy_ts），SQLite 里的预约和映射跟着改。
    旧 node_id 从列表里消失，不再留「永远离线」的幽灵节点。
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT node_id FROM machine_map WHERE machine_id = ?", (machine_id,)
        ).fetchone()
        old = row["node_id"] if row else None
        if old is None:
            conn.execute(
                "INSERT OR REPLACE INTO machine_map (machine_id, node_id) VALUES (?, ?)",
                (machine_id, node_id),
            )
            return
        if old == node_id:
            return
        old_snap = nodes.pop(old, None)
        # 新 node_id 若已有别的条目（另一台机器抢了同名），不覆盖
        if node_id not in nodes:
            if old_snap is not None:
                old_snap["node_id"] = node_id
                nodes[node_id] = old_snap
        conn.execute("UPDATE reservations SET node_id = ? WHERE node_id = ?", (node_id, old))
        conn.execute("UPDATE machine_map SET node_id = ? WHERE machine_id = ?", (node_id, machine_id))
        conn.execute("DELETE FROM machine_map WHERE node_id = ? AND machine_id != ?", (old, machine_id))


@app.post("/api/report")
def report(payload: dict, authorization: str | None = Header(None)):
    _auth_report(authorization)
    node_id = str(payload.get("node_id") or "").strip()
    if not node_id:
        raise HTTPException(status_code=422, detail="node_id required")
    machine_id = str(payload.get("machine_id") or "").strip()
    if machine_id:
        _merge_machine(machine_id, node_id)
    gpus = payload.get("gpus") or []
    snap = nodes.get(node_id, {})
    now = time.time()
    # 任一 GPU 计算占用 ≥50% → 记录最近一次「活跃」时间
    if any((g.get("util_percent") or 0) >= 50 for g in gpus if isinstance(g, dict)):
        snap["last_busy_ts"] = now
    nodes[node_id] = {
        "node_id": node_id,
        "display_name": payload.get("display_name") or node_id,
        "hostname": payload.get("hostname") or "",
        "gpus": gpus,
        "processes": payload.get("processes") or [],
        "error": payload.get("error"),
        "last_seen": now,
        "last_busy_ts": snap.get("last_busy_ts"),
    }
    return {"ok": True}


def _require_login(request: Request) -> dict:
    s = request.session
    if not s.get("username"):
        raise HTTPException(status_code=401, detail="not logged in")
    return s


@app.get("/api/nodes")
def get_nodes(request: Request):
    s = _require_login(request)
    now = time.time()
    threshold = CFG["offline_threshold_sec"]
    out = []
    for snap in nodes.values():
        res = _get_active(snap["node_id"])
        if res is not None:
            res = {**res, "mine": res["user"] == s["username"]}
        out.append({
            "node_id": snap["node_id"],
            "display_name": snap["display_name"],
            "hostname": snap["hostname"],
            "online": now - snap["last_seen"] <= threshold,
            "last_seen": snap["last_seen"],
            "last_busy_ts": snap.get("last_busy_ts"),
            "error": snap["error"],
            "gpus": snap["gpus"],
            "processes": snap["processes"],
            "reservation": res,
        })
    # 展示顺序：node_id 优先，相同则按显示名（便于快速定位机器）
    out.sort(key=lambda x: (x["node_id"], x["display_name"]))
    return out


@app.get("/api/me")
def me(request: Request):
    s = _require_login(request)
    return {
        "username": s["username"],
        "role": s.get("role", "user"),
        "max_reserve_minutes": CFG["max_reserve_minutes"],
    }


@app.post("/api/node/remove")
def node_remove(req: RemoveNodeRequest, request: Request):
    """管理员手动移除过时节点（清掉幽灵条目）。节点若之后再次上报会正常重新出现。"""
    s = _require_login(request)
    if s.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可移除节点")
    node_id = req.node_id.strip()
    if node_id not in nodes:
        raise HTTPException(status_code=404, detail=f"节点 {node_id} 不存在")
    with _db() as conn:
        # 该节点若有有效预约，先删（移除即视为放弃预约）
        conn.execute("DELETE FROM reservations WHERE node_id = ?", (node_id,))
        # 把指向该 node_id 的指纹映射也清掉，让同一台机器下次上报按全新节点处理
        conn.execute("DELETE FROM machine_map WHERE node_id = ?", (node_id,))
    nodes.pop(node_id, None)
    return {"ok": True}


@app.post("/api/reserve")
def reserve(req: ReserveRequest, request: Request):
    s = _require_login(request)
    minutes = _minutes(req.minutes)
    node_id = req.node_id.strip()
    if node_id not in nodes:
        raise HTTPException(status_code=404, detail=f"节点 {node_id} 不存在")
    active = _get_active(node_id)
    if active is not None:
        if active["user"] == s["username"]:
            pass  # 本人再次预约 = 从现在起重新计时（续期）
        else:
            raise HTTPException(
                status_code=409,
                detail=f"该节点已被 {active['user']} 占用至 {_fmt_ts(active['end_ts'])}",
            )
    now = time.time()
    end_ts = now + minutes * 60
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO reservations (node_id, user, start_ts, end_ts)"
            " VALUES (?, ?, ?, ?)",
            (node_id, s["username"], now, end_ts),
        )
    return {"ok": True, "end_ts": end_ts}


@app.post("/api/reserve/renew")
def reserve_renew(req: ReserveRequest, request: Request):
    s = _require_login(request)
    minutes = _minutes(req.minutes)
    node_id = req.node_id.strip()
    active = _get_active(node_id)
    if active is None:
        raise HTTPException(status_code=404, detail="该节点当前没有有效预约")
    if active["user"] != s["username"]:
        raise HTTPException(status_code=403, detail="只能续期自己的预约")
    # 在现有到期时间基础上累加（剩余时间已不足 1 分钟则从现在起算）
    base = max(active["end_ts"], time.time() + 60)
    end_ts = base + minutes * 60
    with _db() as conn:
        conn.execute(
            "UPDATE reservations SET end_ts = ? WHERE node_id = ?",
            (end_ts, node_id),
        )
    return {"ok": True, "end_ts": end_ts}


@app.post("/api/reserve/release")
def reserve_release(req: ReleaseRequest, request: Request):
    s = _require_login(request)
    node_id = req.node_id.strip()
    active = _get_active(node_id)
    if active is None:
        return {"ok": True}  # 无/已过期：幂等成功
    if active["user"] != s["username"] and s.get("role") != "admin":
        raise HTTPException(status_code=403, detail="只能释放自己的预约（管理员可强制释放）")
    with _db() as conn:
        conn.execute("DELETE FROM reservations WHERE node_id = ?", (node_id,))
    return {"ok": True}


@app.post("/login")
def login(payload: dict, request: Request):
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    user = next((u for u in CFG["users"] if u["username"] == username), None)
    if user is None or not bcrypt.checkpw(
        password.encode(), user["password_hash"].encode()
    ):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    request.session.update({"username": username, "role": user["role"]})
    return RedirectResponse("/", status_code=302)


@app.get("/login")
def login_page(request: Request):
    if request.session.get("username"):
        return RedirectResponse("/", status_code=302)
    return FileResponse(BASE_DIR / "static" / "login.html")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/")
def index(request: Request):
    if not request.session.get("username"):
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/static/index.html", status_code=302)


@app.get("/static/index.html")
def index_html(request: Request):
    if not request.session.get("username"):
        return RedirectResponse("/login", status_code=302)
    return FileResponse(BASE_DIR / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CFG["host"], port=CFG["port"])
