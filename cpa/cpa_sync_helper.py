"""CPA 凭证目录读写助手（适用于本地网关容器或 WSL 环境）。

由 mint_cpa_xai.py --sync 调用。把「读容器状态」和「推送文件」两件事
放在标准化脚本里执行，避免多环境拼 bash 命令带来的引号转义问题。

用法:
    python3 cpa_sync_helper.py list
        输出 JSON: {"xai-xxx.json": "<last_refresh>", ...}

    python3 cpa_sync_helper.py push <json文件>
        json 文件内容为文件名数组；把本地 cpa_auths 里的这些文件复制到 CPA 目录。
        输出 JSON: {"pushed": [...], "failed": [{"name":..., "error":...}]}

    python3 cpa_sync_helper.py copy <文件名> [<文件名> ...]
        直接复制指定的若干文件。
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sys

# 默认支持通过环境变量灵活配置
CPA_AUTHS_DIR = os.environ.get("CPA_AUTHS_DIR", "./output/cpa_auths")
LOCAL_DIR = os.environ.get("LOCAL_CPA_DIR", "./output/cpa_auths")


def cmd_list() -> int:
    out: dict[str, str] = {}
    for path in sorted(glob.glob(os.path.join(CPA_AUTHS_DIR, "xai-*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        out[os.path.basename(path)] = data.get("last_refresh", "") or ""
    json.dump(out, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _copy_one(name: str) -> None:
    src = os.path.join(LOCAL_DIR, name)
    dst = os.path.join(CPA_AUTHS_DIR, name)
    if not os.path.exists(src):
        raise FileNotFoundError(f"本地不存在: {src}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)


def cmd_push(list_file: str) -> int:
    with open(list_file, encoding="utf-8") as fh:
        names = json.load(fh)
    pushed, failed = [], []
    for name in names:
        try:
            _copy_one(name)
            pushed.append(name)
        except Exception as exc:
            failed.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})
    json.dump({"pushed": pushed, "failed": failed}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 1 if failed else 0


def cmd_copy(names: list[str]) -> int:
    pushed, failed = [], []
    for name in names:
        try:
            _copy_one(name)
            pushed.append(name)
        except Exception as exc:
            failed.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})
    json.dump({"pushed": pushed, "failed": failed}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "list":
        return cmd_list()
    if cmd == "push":
        if not rest:
            print("push 需要传入 json 文件路径", file=sys.stderr)
            return 2
        return cmd_push(rest[0])
    if cmd == "copy":
        if not rest:
            print("copy 需要传入至少一个文件名", file=sys.stderr)
            return 2
        return cmd_copy(rest)
    print(f"未知命令: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
