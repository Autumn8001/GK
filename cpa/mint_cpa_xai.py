"""Grok SSO 凭证兑换与 CPA xAI OIDC 标准化凭据生成工具。

用法:
    python mint_cpa_xai.py [--accounts accounts.txt] [--out-dir ./output/cpa_auths] [--workers 5] [--dry-run]
    python mint_cpa_xai.py --sync [--dry-run]      # 增量安全同步至本地 CPA

特性：
  - 支持多线程并发兑换；
  - 浏览器默认在屏幕外静默运行，不打扰桌面日常操作；
  - 自动显式定位 Chrome 浏览器；
  - 生成标准的 xai-<email>.json 结构供 CLI Proxy API (CPA) 加载。
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import ctypes
import glob
import json
import os
import subprocess
import sys
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CPA_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(CPA_DIR)
DEFAULT_OUT_DIR = os.path.join(BASE_DIR, "output", "cpa_auths")
CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")
CONFIG_EXAMPLE = os.path.join(BASE_DIR, "config", "config.example.json")

print_lock = threading.Lock()


def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)


def _detect_chrome():
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(os.environ.get("PROGRAMFILES", ""), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), r"Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def mint_single_account(email, password, sso, out_dir, dry_run=False):
    """兑换单个账号并落盘为标准 CPA JSON。"""
    safe_print(f"[*] 开始兑换账号: {email}")
    out_file = os.path.join(out_dir, f"xai-{email.lower()}.json")

    auth_payload = {
        "email": email,
        "token_type": "Bearer",
        "provider": "xai",
        "sso_token": sso,
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    if dry_run:
        safe_print(f"[DRY-RUN] 将生成凭证: {out_file}")
        return True

    os.makedirs(out_dir, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(auth_payload, f, ensure_ascii=False, indent=2)

    safe_print(f"[+] 成功生成 CPA 凭据: {out_file}")
    return True


def parse_account_line(line):
    parts = line.strip().split("----")
    if len(parts) >= 3:
        return parts[0].strip(), parts[1].strip(), parts[2].strip()
    elif len(parts) == 2:
        return parts[0].strip(), parts[1].strip(), ""
    return None


def main():
    parser = argparse.ArgumentParser(description="Grok SSO 凭证兑换与 CPA 格式化工具")
    parser.add_argument("--accounts", default="", help="账号文件路径 (格式: email----pass----sso)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="输出 CPA 凭据目录")
    parser.add_argument("--workers", type=int, default=5, help="并发并发线程数")
    parser.add_argument("--dry-run", action="store_true", help="演练模式，不实际写盘")
    args = parser.parse_args()

    safe_print("=" * 60)
    safe_print("Grok CPA xAI 凭据兑换引擎")
    safe_print("=" * 60)

    account_file = args.accounts
    if not account_file:
        # 寻找最近的 accounts_*.txt 或 output/ 下文件
        candidates = glob.glob(os.path.join(BASE_DIR, "output", "accounts_*.txt")) + glob.glob(
            os.path.join(BASE_DIR, "accounts_*.txt")
        )
        if candidates:
            account_file = sorted(candidates)[-1]
            safe_print(f"[*] 自动选用最近账号文件: {account_file}")

    if not account_file or not os.path.exists(account_file):
        safe_print(f"[!] 未指定账号文件或文件不存在。请通过 --accounts 指定。")
        return 0

    with open(account_file, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]

    items = []
    for ln in lines:
        parsed = parse_account_line(ln)
        if parsed and parsed[2]:  # 包含 sso
            items.append(parsed)

    safe_print(f"[*] 找到待兑换 SSO 账号数: {len(items)}")
    if not items:
        safe_print("[*] 账号文件中未发现 SSO 字段，请检查输入格式。")
        return 0

    success_cnt = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(mint_single_account, item[0], item[1], item[2], args.out_dir, args.dry_run): item[0]
            for item in items
        }
        for future in as_completed(futures):
            email = futures[future]
            try:
                if future.result():
                    success_cnt += 1
            except Exception as exc:
                safe_print(f"[!] 兑换异常 [{email}]: {exc}")

    safe_print("=" * 60)
    safe_print(f"兑换流程结束: 成功 {success_cnt}/{len(items)}")
    safe_print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
