"""Grok 注册批处理编排器（多窗口真并行 + 验证弹窗提醒 + 等待换 IP 后自动续跑）。

职责：
  1. 每轮开始前，按 used_emails.txt 从邮箱池中剔除已注册成功的邮箱；
  2. 动态写入本轮配置：register_count=N / multi_thread_workers=N；
  3. 以子进程方式运行注册引擎，实时捕获日志；
  4. 后台线程实时跟踪日志，一旦检测到 Cloudflare Turnstile 验证，立即弹窗通知；
  5. 轮次结束后把成功邮箱记入 used_emails.txt，等待出口 IP 变更（切换节点）后自动进入下一轮；
  6. 邮箱池耗尽时安全退出。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CORE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(CORE_DIR)

CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")
CONFIG_EXAMPLE = os.path.join(BASE_DIR, "config", "config.example.json")
USED_FILE = os.path.join(BASE_DIR, "output", "used_emails.txt")
NOTIFY_PS1 = os.path.join(CORE_DIR, "notify.ps1")
RUNNER = os.path.join(CORE_DIR, "run_multithread.py")
LOG_DIR = os.path.join(BASE_DIR, "output", "logs")

BATCH_SIZE = 5
MAX_ROUNDS = 25
IP_WAIT_TIMEOUT = 180
IP_POLL_INTERVAL = 15


def log(message):
    print(f"[orchestrator] {message}", flush=True)


def load_config():
    target = CONFIG_PATH if os.path.exists(CONFIG_PATH) else CONFIG_EXAMPLE
    if os.path.exists(target):
        with open(target, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "outlook_accounts_file": os.path.join(BASE_DIR, "output", "mailboxes", "outlook-accounts.txt"),
        "proxy": "http://127.0.0.1:7897",
        "register_count": BATCH_SIZE,
        "multi_thread_workers": BATCH_SIZE,
    }


def notify(title, message):
    if not os.path.exists(NOTIFY_PS1):
        log(f"[通知] {title}: {message}")
        return
    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                NOTIFY_PS1,
                "-Title",
                title,
                "-Message",
                message,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log(f"通知发送异常: {exc}")


def load_used():
    if not os.path.exists(USED_FILE):
        return set()
    with open(USED_FILE, "r", encoding="utf-8") as f:
        return {ln.strip().lower() for ln in f if ln.strip()}


def append_used(emails):
    if not emails:
        return
    os.makedirs(os.path.dirname(USED_FILE), exist_ok=True)
    with open(USED_FILE, "a", encoding="utf-8") as f:
        for email in emails:
            f.write(email.strip().lower() + "\n")


def trim_pool(pool_path):
    if not os.path.exists(pool_path):
        return 0, 0
    used = load_used()
    with open(pool_path, "r", encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    kept = [ln for ln in lines if ln.split("----")[0].strip().lower() not in used]
    removed = len(lines) - len(kept)
    with open(pool_path, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")
    return len(kept), removed


IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def current_ip(proxy_url):
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        resp = requests.get("https://api.ipify.org", proxies=proxies, timeout=15)
        text = (resp.text or "").strip()
        if resp.status_code == 200 and IPV4_RE.match(text):
            return text
        return None
    except Exception:
        return None


TURNSTILE_RE = re.compile(r"\[(T\d+)\].*?仍在等待 Cloudflare 人机验证\.\.\. 0s")
SUCCESS_RE = re.compile(r"注册并保存成功:\s*(\S+)")


def watch_log(log_path, round_no, stop_event):
    notified = set()
    pos = 0
    while not stop_event.is_set():
        try:
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                for line in chunk.splitlines():
                    m = TURNSTILE_RE.search(line)
                    if m:
                        worker = m.group(1)
                        if worker not in notified:
                            notified.add(worker)
                            notify(
                                f"第 {round_no} 轮需要验证",
                                f"{worker} 正在等待 Cloudflare 人机验证，窗口已弹出，请点击！",
                            )
            time.sleep(1)
        except Exception:
            time.sleep(1)


def parse_success_from_log(log_path):
    if not os.path.exists(log_path):
        return []
    successes = []
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = SUCCESS_RE.search(line)
            if m:
                email = m.group(1).split("----")[0].strip().lower()
                successes.append(email)
    return list(dict.fromkeys(successes))


def run_one_round(round_no, batch_size, pool_path, proxy_url):
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"batch_round{round_no}.log")

    log(f"--- [第 {round_no} 轮] 准备启动，批次规模: {batch_size} ---")

    stop_event = threading.Event()
    watcher = threading.Thread(target=watch_log, args=(log_file, round_no, stop_event), daemon=True)
    watcher.start()

    env = os.environ.copy()
    env["GROK_BATCH_LOG"] = log_file

    cmd = [sys.executable, RUNNER]
    start_t = time.time()
    try:
        proc = subprocess.Popen(cmd, env=env)
        proc.wait()
    finally:
        stop_event.set()
        watcher.join(timeout=3)

    cost_m = (time.time() - start_t) / 60
    successes = parse_success_from_log(log_file)
    log(f"--- [第 {round_no} 轮结束] 耗时 {cost_m:.1f}m | 成功数: {len(successes)}/{batch_size} ---")

    if successes:
        append_used(successes)

    return len(successes)


def main():
    cfg = load_config()
    pool_path = cfg.get("outlook_accounts_file", os.path.join(BASE_DIR, "output", "mailboxes", "outlook-accounts.txt"))
    proxy_url = cfg.get("proxy", "http://127.0.0.1:7897")
    batch_size = int(cfg.get("register_count", BATCH_SIZE))

    log("=" * 60)
    log("Grok 注册批处理编排引擎启动")
    log("=" * 60)

    initial_ip = current_ip(proxy_url)
    log(f"当前出口 IP: {initial_ip or '未知'}")

    for round_no in range(1, MAX_ROUNDS + 1):
        remain, removed = trim_pool(pool_path)
        log(f"邮箱池剩余可用: {remain} (已清理历史成功: {removed})")
        if remain < batch_size:
            log(f"邮箱池剩余不足单批次需求 ({remain} < {batch_size})，编排结束。")
            break

        run_one_round(round_no, batch_size, pool_path, proxy_url)

        # 检查是否还需要下一轮
        remain_after, _ = trim_pool(pool_path)
        if remain_after < batch_size or round_no >= MAX_ROUNDS:
            break

        # 等待出口 IP 变更（切换节点）
        log(f"第 {round_no} 轮完成，等待切换代理出口 IP 后自动续跑...")
        last_ip = current_ip(proxy_url)
        wait_start = time.time()
        while time.time() - wait_start < IP_WAIT_TIMEOUT:
            time.sleep(IP_POLL_INTERVAL)
            now_ip = current_ip(proxy_url)
            if now_ip and now_ip != last_ip:
                log(f"检测到全新出口 IP: {now_ip}，启动下一轮！")
                break
        else:
            log("等待 IP 切换超时，默认继续执行下一轮。")

    log("所有批处理流程完成！")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
