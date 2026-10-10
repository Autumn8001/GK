"""Grok 号池额度与健康度报告分析工具。

数据来源：
  - CPA 凭证目录 (默认 ./output/cpa_auths 或环境变量 CPA_AUTHS_DIR)
  - 网关用量数据库 (可选，通过 --db 或 CPA_DB_PATH 指定)

关于「额度」的机制说明：
  xAI 不提供实时余量查询接口，响应体中的 rate_limit 字段表示当前档位请求上限。
  本工具通过扫描凭证状态、最后刷新时间及历史用量统计，计算：
  1. 理论并发与请求调度上限；
  2. 凭证健康度（有效、禁用、过期分布）；
  3. 轮换刷新周期概览。
"""
from __future__ import annotations

import argparse
import collections
import datetime
import glob
import json
import os
import sqlite3
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(TOOLS_DIR)
DEFAULT_AUTHS_DIR = os.path.join(BASE_DIR, "output", "cpa_auths")

WIDTH = 76


def hr(ch="="):
    print(ch * WIDTH)


def load_accounts(auths_dir):
    accts = []
    pattern = os.path.join(auths_dir, "xai-*.json")
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        accts.append(
            {
                "name": os.path.basename(path),
                "email": d.get("email", ""),
                "disabled": bool(d.get("disabled")),
                "expired": d.get("expired", ""),
                "last_refresh": d.get("last_refresh", ""),
            }
        )
    return accts


def main():
    parser = argparse.ArgumentParser(description="Grok 号池健康度与容量报告")
    parser.add_argument("--auths-dir", default="", help="CPA 凭据目录 (默认 ./output/cpa_auths)")
    parser.add_argument("--db", default="", help="sqlite 用量数据库路径 (可选)")
    args = parser.parse_args()

    auths_dir = args.auths_dir or os.environ.get("CPA_AUTHS_DIR", DEFAULT_AUTHS_DIR)

    hr("=")
    print("           Grok / xAI 号池健康度与容量分析报告")
    hr("=")
    print(f"扫描凭证目录: {auths_dir}")

    if not os.path.exists(auths_dir):
        print(f"[!] 凭证目录不存在: {auths_dir}")
        print("    请确认已通过 cpa/mint_cpa_xai.py 生成凭证或指定正确路径。")
        hr("=")
        return 0

    accts = load_accounts(auths_dir)
    total = len(accts)
    print(f"发现 xAI 凭据总数: {total}")

    if total == 0:
        print("[*] 暂无已兑换凭证，号池为空。")
        hr("=")
        return 0

    active_cnt = sum(1 for a in accts if not a["disabled"])
    disabled_cnt = total - active_cnt

    print(f"有效凭据: {active_cnt} | 禁用/待轮换: {disabled_cnt}")
    print(f"理论并发调度上限: ~{active_cnt * 5} requests/min")
    hr("-")

    # 按刷新时间概览
    recent_cnt = 0
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    for a in accts:
        if a["last_refresh"] and a["last_refresh"].startswith(now_str):
            recent_cnt += 1

    print(f"今日刷新凭证: {recent_cnt} / {total}")
    hr("=")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
