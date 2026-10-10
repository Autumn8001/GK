"""邮箱池管理与清洗辅助工具。

功能：
  - 格式标准化与去重；
  - 剔除已使用/历史封禁的邮箱；
  - 快速分割批次文件供多窗口并行消耗。

用法：
    python mailbox_helper.py --clean --pool ./output/mailboxes/outlook-accounts.txt --used ./output/used_emails.txt
    python mailbox_helper.py --split --pool ./output/mailboxes/outlook-accounts.txt --batch-size 5
"""
from __future__ import annotations

import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def clean_pool(pool_path: str, used_path: str) -> None:
    if not os.path.exists(pool_path):
        print(f"[!] 邮箱池文件不存在: {pool_path}")
        return

    used = set()
    if os.path.exists(used_path):
        with open(used_path, "r", encoding="utf-8") as f:
            used = {ln.strip().lower() for ln in f if ln.strip()}

    with open(pool_path, "r", encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f if ln.strip()]

    kept = []
    removed = 0
    for ln in lines:
        parts = ln.split("----")
        email = parts[0].strip().lower()
        if email in used:
            removed += 1
        else:
            kept.append(ln)

    with open(pool_path, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")

    print(f"[+] 清洗完成: 保留 {len(kept)} 行可用邮箱，剔除 {removed} 行已使用邮箱。")


def split_pool(pool_path: str, batch_size: int, out_dir: str) -> None:
    if not os.path.exists(pool_path):
        print(f"[!] 邮箱池文件不存在: {pool_path}")
        return

    os.makedirs(out_dir, exist_ok=True)
    with open(pool_path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]

    total = len(lines)
    parts_cnt = (total + batch_size - 1) // batch_size
    print(f"[*] 总计 {total} 个账号，每批 {batch_size} 个，将切分为 {parts_cnt} 份。")

    for i in range(parts_cnt):
        chunk = lines[i * batch_size : (i + 1) * batch_size]
        sub_file = os.path.join(out_dir, f"batch_{i + 1}.txt")
        with open(sub_file, "w", encoding="utf-8") as f:
            f.write("\n".join(chunk) + "\n")
        print(f"  已生成批次 {i + 1}: {sub_file} ({len(chunk)} 个)")


def main():
    parser = argparse.ArgumentParser(description="邮箱池管理辅助工具")
    parser.add_argument("--clean", action="store_true", help="按已使用清单清洗邮箱池")
    parser.add_argument("--split", action="store_true", help="切分邮箱池为小批次")
    parser.add_argument("--pool", required=True, help="邮箱池文件路径")
    parser.add_argument("--used", default="./output/used_emails.txt", help="已使用邮箱记录文件")
    parser.add_argument("--batch-size", type=int, default=5, help="切分批次大小")
    parser.add_argument("--out-dir", default="./output/batches", help="切分输出目录")
    args = parser.parse_args()

    if args.clean:
        clean_pool(args.pool, args.used)
    elif args.split:
        split_pool(args.pool, args.batch_size, args.out_dir)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
