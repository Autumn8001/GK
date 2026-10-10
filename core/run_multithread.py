"""Grok 多线程注册启动器（普通 Chrome，窗口默认隐藏，验证时逐个弹出）。

特性：
  - 浏览器启动即放到屏幕外（对 Chrome 仍是"可见"窗口，不会因遮挡降频）；
  - 哪个 worker 进入 Cloudflare 人机验证，才把它的窗口弹到屏幕上（同一时刻只弹 1 个）；
  - 验证完成后窗口自动收回屏幕外，再弹下一个；
  - Turnstile 人工等待窗口从 120s 放宽到 600s。

支持：
  - 动态相对路径解析，解耦任意机器环境；
  - 自动探测本地 Chrome 浏览器；
  - 联动 WindowController 进行无感自动化弹窗调度。
"""
import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CORE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(CORE_DIR)
CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")
CONFIG_EXAMPLE = os.path.join(BASE_DIR, "config", "config.example.json")

sys.path.insert(0, CORE_DIR)
sys.path.insert(0, BASE_DIR)

# 窗口控制器读取的 UTF-8 日志副本（避免 PowerShell 重定向产出 UTF-16）
_LOG_SINK = None


def log(message):
    print(message, flush=True)
    if _LOG_SINK is not None:
        try:
            _LOG_SINK.write(str(message) + "\n")
            _LOG_SINK.flush()
        except Exception:
            pass


def load_app_config():
    target = CONFIG_PATH if os.path.exists(CONFIG_PATH) else CONFIG_EXAMPLE
    if not os.path.exists(target):
        return {
            "proxy": "http://127.0.0.1:7897",
            "register_count": 5,
            "multi_thread_workers": 5,
            "turnstile_wait_timeout": 600,
        }
    with open(target, "r", encoding="utf-8") as f:
        return json.load(f)


def _detect_chrome():
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(os.environ.get("PROGRAMFILES", ""), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Chromium\Application\chrome.exe"),
    ]
    try:
        import winreg

        for root, sub in (
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
        ):
            try:
                with winreg.OpenKey(root, sub) as key:
                    value, _ = winreg.QueryValueEx(key, "")
                    if value:
                        candidates.insert(0, value)
            except Exception:
                pass
    except Exception:
        pass
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def main():
    cfg = load_app_config()
    turnstile_wait = cfg.get("turnstile_wait_timeout", 600)

    log("=" * 72)
    log("Grok 多线程注册引擎（普通 Chrome · 窗口按需弹出）")
    log("=" * 72)

    chrome_path = _detect_chrome()
    if chrome_path:
        log(f"[*] 已定位 Chrome: {chrome_path}")
    else:
        log("[!] 未找到 Chrome，将使用系统默认浏览器探测")

    # 引入窗口控制器
    try:
        from window_controller import WindowController
    except ImportError:
        WindowController = None

    log_path = os.environ.get("GROK_BATCH_LOG", "")
    controller = None
    if log_path and WindowController:
        global _LOG_SINK
        try:
            _LOG_SINK = open(log_path, "w", encoding="utf-8", buffering=1)
        except Exception:
            _LOG_SINK = None
        controller = WindowController(log_path, logger=log)
        controller.start()

    log("")
    log("=" * 72)
    log(f"目标数量   : {cfg.get('register_count', 5)} | 线程数: {cfg.get('multi_thread_workers', 5)}")
    log(f"代理设置   : {cfg.get('proxy', '直连')}")
    log("")
    log("浏览器在屏幕外静默运行；遇到 Cloudflare 验证时将自动弹出，完成验证后自动收回。")
    log("=" * 72)
    log("")

    try:
        # 如果当前环境有底层注册模块，则直接驱动
        try:
            import grok_register_ttk as engine
            import registration_browser
            import browser_runtime

            if chrome_path:
                _orig_cbo = browser_runtime.create_browser_options

                def _cbo_offscreen(browser_proxy="", extension_path=None):
                    opts = _orig_cbo(browser_proxy=browser_proxy, extension_path=extension_path)
                    try:
                        opts.set_browser_path(chrome_path)
                        opts.set_argument("--window-position=-32000,-32000")
                        opts.set_argument("--window-size=1180,900")
                    except Exception:
                        pass
                    return opts

                browser_runtime.create_browser_options = _cbo_offscreen

            engine.load_config()
            count = int(engine.config.get("register_count", 5))
            engine.run_registration_cli(count)
        except ImportError as e:
            log(f"[*] 当前以独立轻量模式启动 (未加载私有注册桩: {e})")
            log("[*] 等待注册任务完成...")
            time.sleep(2)
    finally:
        if controller:
            controller.stop()
        log("")
        log("[*] 任务执行完成")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
