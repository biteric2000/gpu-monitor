const POLL_MS = 4000;
let ME = { username: "", role: "user", max_reserve_minutes: 480 };
const cardErrors = {};   // node_id -> 服务端报错文本
const flashes = {};      // node_id -> {msg, until} 操作成功提示（6 秒内显示）

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

let toastTimer = null;
function showToast(msg, ms) {
  let t = document.getElementById("toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "toast";
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add("show");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}

function fmtTime(ts) {
  if (ts == null) return "--";
  return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}

function fmtAgo(ts) {
  if (ts == null) return null;
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + "秒前";
  if (s < 3600) return Math.floor(s / 60) + "分钟前";
  if (s < 86400) return (s / 3600).toFixed(1) + "小时前";
  return Math.floor(s / 86400) + "天前";
}

function gpuRow(g) {
  const util = g.util_percent ?? 0;
  const temp = g.temperature_c == null ? "--" : g.temperature_c + "°C";
  const power = g.power_w == null ? "--" : g.power_w.toFixed(1) + " W";
  const pct = g.mem_total_mb > 0 ? Math.min(100, Math.round(g.mem_used_mb / g.mem_total_mb * 100)) : 0;
  const memCls = pct >= 85 ? "bar-fill-crit" : pct >= 60 ? "bar-fill-high" : "";
  return `
    <div class="gpu">
      <div class="gpu-head">
        <span class="gpu-name" title="${esc(g.name)}">GPU ${g.index} · ${esc(g.name)}</span>
        <span class="gpu-temps">
          <span>🌡 ${temp}</span>
          <span>⚡ ${power}</span>
        </span>
      </div>
      <div class="bar-row">
        <div class="bar"><div class="bar-fill" style="width:${Math.min(util, 100)}%"></div></div>
        <span class="bar-label">${util}%</span>
      </div>
      <div class="bar-row">
        <div class="bar bar-mem"><div class="bar-fill ${memCls}" style="width:${pct}%"></div></div>
        <span class="bar-label" title="${g.mem_used_mb} / ${g.mem_total_mb} MB">${pct}%</span>
      </div>
      <div class="muted mem-detail">显存 ${g.mem_used_mb} / ${g.mem_total_mb} MB</div>
    </div>`;
}

function procRow(p) {
  const cmd = esc((p.cmdline || "").slice(0, 80));
  const llm = p.is_llm ? ' <span class="llm-tag">LLM</span>' : "";
  return `
    <li class="${p.is_llm ? "proc-llm" : ""}">
      <span class="proc-name">${esc(p.name)} (${p.pid})</span>${llm}
      <span class="muted">· ${cmd}${(p.cmdline || "").length > 80 ? "…" : ""} · ${p.mem_mb} MB</span>
    </li>`;
}

function busyLine(n) {
  const ago = fmtAgo(n.last_busy_ts);
  if (!ago) return '<div class="busy-line muted">距上次活跃：无记录</div>';
  const secs = Date.now() / 1000 - (n.last_busy_ts || 0);
  const recent = secs < 1800; // 30 分钟内 → 醒目红色
  return recent
    ? `<div class="busy-line busy-recent">⚠ 距上次活跃：${ago}（GPU 计算占用 &gt;50%）—— 可能有人在使用</div>`
    : `<div class="busy-line">距上次活跃：${ago}</div>`;
}

function reserveBox(n) {
  const r = n.reservation;
  const nodeAttr = `data-node="${esc(n.node_id)}"`;
  const err = cardErrors[n.node_id]
    ? `<div class="reserve-err">⚠ ${esc(cardErrors[n.node_id])}</div>` : "";
  const fl = flashes[n.node_id] && Date.now() < flashes[n.node_id].until
    ? `<div class="reserve-ok">✓ ${esc(flashes[n.node_id].msg)}</div>` : "";
  if (!r) {
    return `<div class="reserve-box">
      <span class="muted">空闲 · 预约：</span>
      <span class="reserve-btns">
        <button class="btn" ${nodeAttr} data-action="reserve" data-minutes="30">30分钟</button>
        <button class="btn" ${nodeAttr} data-action="reserve" data-minutes="60">1小时</button>
        <button class="btn" ${nodeAttr} data-action="reserve" data-minutes="120">2小时</button>
        <input class="custom-min" type="number" min="1" max="${ME.max_reserve_minutes}"
          placeholder="自定义" ${nodeAttr} aria-label="自定义分钟数">
        <button class="btn" ${nodeAttr} data-action="reserve" data-custom="1">确定</button>
      </span>${fl}${err}
    </div>`;
  }
  const who = r.mine ? `你（${esc(r.user)}）` : esc(r.user);
  let btns = "";
  if (r.mine) {
    btns = `
      <button class="btn" ${nodeAttr} data-action="renew" data-minutes="30">续期30m</button>
      <button class="btn" ${nodeAttr} data-action="renew" data-minutes="60">续期1h</button>
      <button class="btn" ${nodeAttr} data-action="release">释放</button>`;
  } else if (ME.role === "admin") {
    btns = `<button class="btn btn-danger" ${nodeAttr} data-action="release">强制释放</button>`;
  }
  return `<div class="reserve-box">
    <div>${who} 占用中，至 ${fmtTime(r.end_ts)}</div>
    ${btns ? `<span class="reserve-btns">${btns}</span>` : ""}${fl}${err}
  </div>`;
}

async function doReserve(nodeId, action, minutes, custom) {
  if (action === "remove") {
    // 两段式确认（不用 confirm()：部分 WebView 禁用弹窗会静默失败）
    const btn = document.querySelector(`button[data-action="remove"][data-node="${CSS.escape(nodeId)}"]`);
    if (btn && btn.dataset.armed === "1") {
      await removeNode(nodeId);
      return;
    }
    if (btn) {
      btn.dataset.armed = "1";
      btn.textContent = "再点一次确认移除";
      btn.classList.add("btn-danger");
      setTimeout(() => { btn.dataset.armed = "0"; btn.textContent = "移除该节点"; btn.classList.remove("btn-danger"); refresh(); }, 5000);
    }
    return;
  }
  if (custom) {
    const btn = document.querySelector(`button[data-node="${CSS.escape(nodeId)}"][data-custom="1"]`);
    const inp = btn ? btn.parentElement.querySelector(".custom-min") : null;
    minutes = Number(inp && inp.value);
    if (!Number.isInteger(minutes) || minutes < 1 || minutes > ME.max_reserve_minutes) {
      cardErrors[nodeId] = `分钟数需在 1 到 ${ME.max_reserve_minutes} 之间（在「自定义」输入框里填数字后点确定）`;
      return;
    }
  }
  const url = { reserve: "/api/reserve", renew: "/api/reserve/renew" }[action] ?? "/api/reserve/release";
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(action === "release" ? { node_id: nodeId } : { node_id: nodeId, minutes }),
    });
    if (res.status === 401) { location.href = "/login"; return; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      cardErrors[nodeId] = data.detail || `HTTP ${res.status}`;
    } else {
      delete cardErrors[nodeId];
      flashes[nodeId] = {
        msg: action === "release" ? "已释放"
            : action === "renew" ? `已续期 +${minutes} 分钟，至 ${fmtTime(data.end_ts)}`
            : `已预约 ${minutes} 分钟，至 ${fmtTime(data.end_ts)}`,
        until: Date.now() + 6000,
      };
      if (action === "reserve") showToast(`已预约 ${minutes} 分钟，至 ${fmtTime(data.end_ts)}。用完请及时释放。`, 10000);
      if (action === "renew") showToast(`已续期 +${minutes} 分钟，至 ${fmtTime(data.end_ts)}。用完请及时释放。`, 10000);
    }
  } catch (_) {
    cardErrors[nodeId] = "Server 不可达";
  }
  refresh();
}

async function removeNode(nodeId) {
  try {
    const res = await fetch("/api/node/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node_id: nodeId }),
    });
    if (res.status === 401) { location.href = "/login"; return; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { cardErrors[nodeId] = data.detail || `HTTP ${res.status}`; }
    else { delete cardErrors[nodeId]; showToast(`已移除节点 ${nodeId}；若它之后再次上报会重新出现`, 8000); }
  } catch (_) {
    cardErrors[nodeId] = "Server 不可达";
  }
  refresh();
}

document.getElementById("nodes").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  doReserve(btn.dataset.node, btn.dataset.action,
    Number(btn.dataset.minutes), btn.dataset.custom === "1");
});

function nodeCard(n) {
  const dot = n.online ? 'dot dot-green' : 'dot dot-gray';
  const busy = Math.max(0, ...(n.gpus || []).map(g => g.util_percent ?? 0)) >= 50;
  const recent = n.last_busy_ts != null && Date.now() / 1000 - n.last_busy_ts < 1800;
  const resBadge = !n.online
    ? '<span class="badge badge-off" title="超过 20 秒未上报">离线</span>'
    : n.reservation
    ? '<span class="badge badge-busy">已预约</span>'
    : recent
    ? '<span class="badge badge-recent">近期活跃</span>'
    : busy ? '<span class="badge badge-busy">高占用</span>'
    : '<span class="badge badge-free">空闲</span>';
  const err = n.error
    ? `<div class="node-error">无 GPU 数据：${esc(n.error)}</div>` : "";
  const gpus = (n.gpus || []).map(gpuRow).join("");
  const procs = (n.processes || [])
    .slice()
    .sort((a, b) => (b.mem_mb || 0) - (a.mem_mb || 0) || (a.pid || 0) - (b.pid || 0))
    .map(procRow).join("");
  return `
    <section class="card">
      <div class="card-head">
        <span class="${dot}"></span>
        <h2>${esc(n.display_name)}</h2>
        <span class="muted node-meta" title="${esc(n.node_id)} · 最近上报 ${fmtTime(n.last_seen)}">${esc(n.node_id)} · ${fmtTime(n.last_seen)}</span>
        ${resBadge}
      </div>
      ${err}
      ${gpus || '<p class="muted">（无 GPU 信息）</p>'}
      ${busyLine(n)}
      ${reserveBox(n)}
      ${ME.role === "admin" ? `<div class="admin-row"><button class="btn btn-admin" data-node="${esc(n.node_id)}" data-action="remove">移除该节点</button></div>` : ""}
      ${procs ? `<div class="proc-title">GPU 相关进程（按内存倒序）</div><ul class="procs">${procs}</ul>` : ""}
    </section>`;
}

async function loadMe() {
  try {
    const res = await fetch("/api/me", { cache: "no-store" });
    if (res.status === 401) { location.href = "/login"; return; }
    ME = await res.json();
    document.getElementById("user-name").textContent =
      ME.username + (ME.role === "admin" ? "（管理员）" : "");
  } catch (_) {}
}

async function refresh() {
  const banner = document.getElementById("banner");
  try {
    const res = await fetch("/api/nodes", { cache: "no-store" });
    if (res.status === 401) { location.href = "/login"; return; }
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    banner.classList.add("hidden");
    document.getElementById("update-time").textContent =
      "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
    const main = document.getElementById("nodes");
    main.innerHTML = data.length
      ? data.map(nodeCard).join("")
      : '<p class="muted">暂无节点上报。</p>';
  } catch (e) {
    banner.classList.remove("hidden");
  }
}

loadMe();
refresh();
setInterval(refresh, POLL_MS);
