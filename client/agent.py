# -*- coding: utf-8 -*-
"""GPU 监控 Client：每 interval 秒采集本机 GPU 并上报 Server（见 SPEC.md 第 3 节）。

运行:
    pythonw.exe agent.py          # 日常运行（无黑窗口）
    python agent.py --once        # 采集一次，JSON 打印到 stdout（不 POST）
"""
import atexit
import json
import os
import socket
import sys
import time
from datetime import datetime

import psutil
import requests
import yaml
import pynvml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "agent.log")
LLM_KEYWORDS = ("llama", "ollama", "vllm", "sglang")


def log(msg: str) -> None:
    """append 写 agent.log，每行带时间戳。"""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def load_config() -> dict:
    with open(os.path.join(BASE_DIR, "config.yaml"), "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def to_str(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def collect_gpu(handle, index: int) -> dict:
    """采集单张 GPU；温度/功耗/进程分别 try/except，缺失填 null / []。"""
    name = to_str(pynvml.nvmlDeviceGetName(handle))
    util = pynvml.nvmlDeviceGetUtilizationRates(handle)
    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)

    temperature_c = None
    try:
        temperature_c = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
    except Exception:
        pass

    power_w = None
    try:
        power_w = round(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0, 1)  # mW -> W
    except Exception:
        pass

    pids = []
    try:
        seen = set()
        for proc in (pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
                     + pynvml.nvmlDeviceGetGraphicsRunningProcesses(handle)):
            if proc.pid not in seen:
                seen.add(proc.pid)
                pids.append(proc.pid)
    except Exception:
        pass

    return {
        "index": index,
        "name": name,
        "util_percent": util.gpu,
        "mem_used_mb": round(mem.used / 1024 / 1024),
        "mem_total_mb": round(mem.total / 1024 / 1024),
        "temperature_c": temperature_c,
        "power_w": power_w,
        "pids": pids,
    }


def collect_processes(gpus: list) -> list:
    """汇总所有卡的占卡进程，PID 去重，记录 gpu_indices。"""
    pid_to_gpus = {}
    for gpu in gpus:
        for pid in gpu["pids"]:
            pid_to_gpus.setdefault(pid, []).append(gpu["index"])

    processes = []
    for pid, gpu_indices in pid_to_gpus.items():
        try:
            proc = psutil.Process(pid)
            name = proc.name()
            cmdline = " ".join(proc.cmdline())[:200]
            mem_mb = round(proc.memory_info().rss / 1024 / 1024)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue  # 进程可能已退出
        blob = (name + " " + cmdline).lower()
        processes.append({
            "pid": pid,
            "name": name,
            "cmdline": cmdline,
            "mem_mb": mem_mb,
            "is_llm": any(k in blob for k in LLM_KEYWORDS),
            "gpu_indices": gpu_indices,
        })
    return processes


def get_machine_id() -> str:
    """机器指纹：Windows MachineGuid（每机唯一，与 hostname/IP 无关，重装前不变）。

    用途：同一台机器改了 node_id 后，Server 凭此自动合并旧条目，不再留幽灵节点。
    读取失败时退回 MAC 地址；再失败返回空串（Server 侧按无指纹处理）。
    """
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography") as k:
                v, _ = winreg.QueryValueEx(k, "MachineGuid")
                return str(v)
        except OSError:
            pass
    try:
        import uuid
        return f"mac-{uuid.getnode():012x}"
    except Exception:
        return ""


def collect(cfg: dict) -> dict:
    """采集一次，返回上报 JSON。NVML 不可用 → gpus:[] + error。"""
    hostname = socket.gethostname()
    node_id = cfg.get("node_id") or hostname
    display_name = cfg.get("display_name") or node_id

    gpus, error = [], None
    try:
        count = pynvml.nvmlDeviceGetCount()
        gpus = [collect_gpu(pynvml.nvmlDeviceGetHandleByIndex(i), i) for i in range(count)]
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        gpus = []

    return {
        "node_id": node_id,
        "display_name": display_name,
        "hostname": hostname,
        "machine_id": get_machine_id(),
        "gpus": gpus,
        "processes": collect_processes(gpus),
        "error": error,
    }


def post_report(cfg: dict, payload: dict) -> None:
    url = cfg["server_url"].rstrip("/") + "/api/report"
    try:
        r = requests.post(url, json=payload, timeout=10,
                          headers={"Authorization": f"Bearer {cfg['token']}"})
        if r.status_code != 200:
            log(f"report failed: HTTP {r.status_code} {r.text[:200]}")
        else:
            log(f"reported {len(payload['gpus'])} gpu(s) to {url}")
    except requests.RequestException as e:
        log(f"report failed: {e}")


def main() -> None:
    cfg = load_config()
    once = "--once" in sys.argv[1:]

    pynvml.nvmlInit()
    try:
        if once:
            print(json.dumps(collect(cfg), ensure_ascii=False, indent=2))
            return
        atexit.register(log, "agent exiting")
        interval = int(cfg.get("interval_sec", 5))
        log(f"agent started: node_id={cfg.get('node_id') or socket.gethostname()} "
            f"server={cfg.get('server_url')} interval={interval}s")
        while True:
            try:
                post_report(cfg, collect(cfg))
            except Exception as e:  # 任何异常不退出
                log(f"collect/post error: {type(e).__name__}: {e}")
            time.sleep(interval)
    finally:
        pynvml.nvmlShutdown()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        if "--once" in sys.argv[1:]:
            raise
        log(f"fatal: {type(e).__name__}: {e}")
