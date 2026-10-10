"""推送本地生成的 CPA xAI 授权凭证至远程服务器 CPA 容器中。

安全原则（代码级强制）：
  - 仅写入指定的远程目录，无递归破坏性覆写；
  - 绝不覆盖远程更新时间晚于本地的凭证；
  - 远程替换前在服务器生成带时间戳的 pre-repair 备份；
  - 完全脱敏：服务器连接信息从 config/server.json、环境变量或命令行参数加载。

用法：
    python push_cpa_server.py --emails emails.txt                       # Dry-run 演练模式
    python push_cpa_server.py --emails emails.txt --apply               # 正式推送
    python push_cpa_server.py --config ./config/server.json --apply
"""
import argparse
import json
import os
import posixpath
import sys

import paramiko

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CPA_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(CPA_DIR)
CONFIG_SERVER = os.path.join(BASE_DIR, "config", "server.json")
CONFIG_EXAMPLE = os.path.join(BASE_DIR, "config", "server.example.json")


def load_server_config(custom_path=""):
    path = custom_path or (CONFIG_SERVER if os.path.exists(CONFIG_SERVER) else CONFIG_EXAMPLE)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def connect(host, port, user, key_path):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    expanded_key = os.path.expanduser(key_path)
    client.connect(
        host,
        port=int(port),
        username=user,
        key_filename=expanded_key if os.path.exists(expanded_key) else None,
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


REMOTE_LIST_SCRIPT = r"""
python3 - <<'PY'
import json, glob, os
for f in sorted(glob.glob("/opt/cliproxyapi/auths/*.json")):
    n = os.path.basename(f)
    try:
        j = json.load(open(f))
    except Exception:
        print("%s\tBADJSON" % n); continue
    print("%s\t%s" % (n, j.get("last_refresh") or "?"))
PY
"""


def remote_list(client):
    _in, out, err = client.exec_command(REMOTE_LIST_SCRIPT, timeout=120)
    data = out.read().decode("utf-8", "replace")
    mapping = {}
    for line in data.splitlines():
        if "\t" in line:
            n, lr = line.split("\t", 1)
            mapping[n] = lr.strip()
    return mapping


def main():
    parser = argparse.ArgumentParser(description="安全推送 CPA 凭证至服务器")
    parser.add_argument("--config", default="", help="服务器配置文件 (默认 config/server.json)")
    parser.add_argument("--emails", default="", help="指定需要推送的邮箱清单文件")
    parser.add_argument("--host", default="", help="覆盖服务器 Host")
    parser.add_argument("--port", type=int, default=0, help="覆盖服务器 SSH 端口")
    parser.add_argument("--apply", action="store_true", help="实际执行推送 (默认仅 Dry-run 演练)")
    args = parser.parse_args()

    cfg = load_server_config(args.config)
    host = args.host or cfg.get("host", "")
    port = args.port or cfg.get("port", 22)
    user = cfg.get("user", "root")
    key_path = cfg.get("key_path", "~/.ssh/id_ed25519")
    remote_dir = cfg.get("remote_dir", "/opt/cliproxyapi/auths")
    stage_dir = cfg.get("stage_dir", "/tmp/cpa-push")
    local_dir = os.path.join(BASE_DIR, "output", "cpa_auths")

    if not host or host == "your-server-ip-or-domain":
        print("[!] 请先在 config/server.json 中配置有效的服务器连接信息，或通过命令行参数指定。")
        return 1

    emails = []
    if args.emails and os.path.exists(args.emails):
        with open(args.emails, "r", encoding="utf-8-sig") as fh:
            emails = [l.strip().lstrip("\ufeff") for l in fh if l.strip() and not l.startswith("#")]
    else:
        # 扫描本地 output/cpa_auths 下的所有 xai-*.json
        if os.path.exists(local_dir):
            for f in os.listdir(local_dir):
                if f.startswith("xai-") and f.endswith(".json"):
                    emails.append(f[4:-5])

    print(f"=== CPA 凭据推送计划 ({'APPLY' if args.apply else 'DRY-RUN 演练'}) ===")
    print(f"目标服务器: {user}@{host}:{port}")
    print(f"待处理凭据数: {len(emails)}")

    if not emails:
        print("[*] 没有发现待推送的凭据。")
        return 0

    if not args.apply:
        print("\n当前为 Dry-run 模式，未发起网络连接和文件写入。如需实际推送请追加 --apply 参数。")
        return 0

    client = connect(host, port, user, key_path)
    try:
        server_auths = remote_list(client)
        sftp = client.open_sftp()

        plan, skipped = [], []
        for e in emails:
            name = f"xai-{e.lower()}.json"
            lp = os.path.join(local_dir, name)
            if not os.path.exists(lp):
                continue
            with open(lp, "r", encoding="utf-8") as fh:
                lj = json.load(fh)
            llr = str(lj.get("last_refresh") or "")
            slr = str(server_auths.get(name) or "")

            if slr and llr <= slr:
                skipped.append((e, "服务器凭据更新，拒绝覆盖", llr, slr))
                continue
            plan.append((e, name, llr, slr))

        print(f"可推送: {len(plan)} | 跳过: {len(skipped)}")
        for e, name, llr, slr in plan:
            print(f"  PUSH  {e:<40} 本地: {llr}  远端: {slr or '空'}")

        if not plan:
            print("没有满足更新条件的凭据。")
            return 0

        # 执行远程备份与上传
        import datetime

        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bdir = f"/var/backups/ai-gateway/pre-repair-{ts}"
        client.exec_command(f"mkdir -p {bdir} && chmod 700 {bdir}")
        client.exec_command(f"mkdir -p {stage_dir} && chmod 700 {stage_dir}")

        for e, name, llr, slr in plan:
            lp = os.path.join(local_dir, name)
            rp = posixpath.join(remote_dir, name)
            stage = posixpath.join(stage_dir, name)
            client.exec_command(f"if [ -f {rp} ]; then cp -p {rp} {bdir}/ ; fi")
            sftp.put(lp, stage)
            client.exec_command(f"mv -f {stage} {rp} && chmod 600 {rp}")

        print(f"\n[+] 成功推送 {len(plan)} 个凭据至服务器 CPA 目录！")
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
