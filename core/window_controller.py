"""Grok 注册窗口控制器：默认隐藏浏览器，仅在需要人工验证时逐个弹出，验证完自动收回。

设计：
  - 浏览器启动时通过 --window-position=-32000,-32000 放到屏幕外（对 Chrome 来说仍是
    "可见"窗口，不会被判定为遮挡而降频，比 minimize/SW_HIDE 更安全）；
  - 控制器实时跟踪本轮日志，按 worker 解析出它的调试端口，进而定位对应的
    Chrome 进程与顶层窗口句柄；
  - 当某个 worker 进入 Cloudflare 人机验证等待时，把它加入"待弹出队列"，
    并保证同一时刻只弹出 1 个窗口（满足"一个个弹出"）；
  - 当该 worker 的验证完成后，把窗口移回屏幕外，并弹出队列中的下一个；
  - 全部 worker 结束后停止。

不修改 grok-register-aaron 的任何源码。
"""
import ctypes
import re
import threading
import time
from ctypes import wintypes

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

user32 = ctypes.windll.user32

# 屏幕外坐标（足够远，任何多显示器布局都不会命中）
OFFSCREEN_X = -32000
OFFSCREEN_Y = -32000
ONSCREEN_X = 120
ONSCREEN_Y = 80
WINDOW_W = 1180
WINDOW_H = 900

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SW_RESTORE = 9

kernel32 = ctypes.windll.kernel32

# worker 前缀单独提取，避免可选分组在正则里产生歧义（并行模式下会误判）
WORKER_PREFIX_RE = re.compile(r"\[(T\d+)\]")
PROFILE_RE = re.compile(r"\[Debug\]\s*当前浏览器资料目录:\s*(.+?)\s*$")
WAIT_RE = re.compile(r"仍在等待 Cloudflare 人机验证")
WAIT_READY_RE = re.compile(r"请在当前浏览器窗口完成验证")
DONE_RE = re.compile(r"Cloudflare 人机验证已完成")

# 单账号串行模式下日志没有 [T<id>] 前缀，统一归到一个虚拟 worker 上
DEFAULT_WORKER = "T1"


def _worker_of(line):
    m = WORKER_PREFIX_RE.search(line)
    return m.group(1) if m else DEFAULT_WORKER


def _enum_windows_for_pid(pid, title_keywords=None):
    """返回该 PID 的可见顶层窗口句柄列表。

    title_keywords 非空时只返回标题命中关键字的窗口 —— 避免把 Chrome 的
    空白新标签页 / 状态托盘窗口当成注册窗口弹出来。
    """
    handles = []

    def callback(hwnd, _lparam):
        wnd_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wnd_pid))
        if wnd_pid.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        if title_keywords:
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if not any(k in buf.value for k in title_keywords):
                return True
        handles.append(hwnd)
        return True

    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(callback)
    user32.EnumWindows(proc, 0)
    return handles


# 注册窗口标题特征（用于排除空白新标签页等无关窗口）
REG_TITLE_KEYWORDS = ("创建您的 Grok 账户", "Grok", "ChatGPT", "accounts.x.ai", "auth.openai.com")


def _find_pids_by_port(port):
    """通过 --remote-debugging-port 找出所有相关 Chrome 进程 PID。

    注意：同一个调试端口下会有多个 Chrome 进程（浏览器主进程 + 各类子进程）
    都带着这个参数，而只有浏览器主进程才拥有顶层窗口。
    因此必须返回全部 PID，逐个检查哪个拥有窗口，不能只取第一个。
    """
    if psutil is None:
        return []
    marker = f"--remote-debugging-port={port}"
    pids = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if str(proc.info.get("name") or "").lower() != "chrome.exe":
                continue
            cmdline = proc.cmdline()
            if any(marker == str(part) for part in cmdline):
                pids.append(proc.info["pid"])
        except Exception:
            continue
    return pids


class WindowController:
    """跟踪 worker → Chrome 窗口，按需弹出/收回。"""

    def __init__(self, log_path, logger=None, one_at_a_time=True):
        self.log_path = log_path
        self.log = logger or (lambda message: None)
        self.one_at_a_time = one_at_a_time

        self._pos = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self._hwnd = {}          # worker -> hwnd
        self._port = {}          # worker -> debug port
        self._pending = []       # 等待弹出的 worker 队列
        self._visible = None     # 当前可见的 worker
        self._resolved = set()   # 已成功解析过窗口的 worker
        self._thread = None

    # ---------- 窗口操作 ----------

    def _move(self, hwnd, x, y, activate):
        flags = SWP_NOZORDER | SWP_SHOWWINDOW
        if not activate:
            flags |= SWP_NOACTIVATE
        user32.SetWindowPos(
            hwnd, 0, int(x), int(y), WINDOW_W, WINDOW_H, flags
        )

    def _move_window(self, hwnd, x, y):
        """移动窗口位置。

        ⚠️ 实测踩坑：对 Chrome 窗口调用 SetWindowPos 改坐标**无效**（会被弹回原位），
        必须用 MoveWindow / SetWindowPlacement 才能真正移动。
        SetWindowPos 只用来改 Z 序（配合 SWP_NOMOVE | SWP_NOSIZE）。
        """
        user32.MoveWindow(hwnd, int(x), int(y), WINDOW_W, WINDOW_H, True)

    def _set_topmost(self, hwnd, on):
        flag = HWND_TOPMOST if on else HWND_NOTOPMOST
        user32.SetWindowPos(hwnd, flag, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)

    def _force_foreground(self, hwnd):
        """把窗口移回屏幕、置顶并夺取焦点；保持 TOPMOST 直到被收回。"""
        try:
            user32.ShowWindow(hwnd, SW_RESTORE)
            # 1) 用 MoveWindow 真正把窗口移回屏幕内
            self._move_window(hwnd, ONSCREEN_X, ONSCREEN_Y)
            # 2) 用 SetWindowPos 只改 Z 序（不动坐标）
            self._set_topmost(hwnd, True)
            user32.BringWindowToTop(hwnd)

            # 3) 绕过 Windows 焦点抢占保护
            fg = user32.GetForegroundWindow()
            tid_fg = user32.GetWindowThreadProcessId(fg, None)
            tid_me = kernel32.GetCurrentThreadId()
            if tid_fg and tid_fg != tid_me:
                user32.AttachThreadInput(tid_me, tid_fg, True)
                user32.SetForegroundWindow(hwnd)
                user32.AttachThreadInput(tid_me, tid_fg, False)
            else:
                user32.SetForegroundWindow(hwnd)
            return True
        except Exception as exc:
            self.log(f"强制置顶失败: {exc}")
            return False

    def _keep_on_top(self, worker):
        """等待人工验证期间周期性重申位置与 TOPMOST，防止被遮挡或漂移。"""
        hwnd = self._hwnd.get(worker)
        if not hwnd:
            return
        try:
            self._move_window(hwnd, ONSCREEN_X, ONSCREEN_Y)
            self._set_topmost(hwnd, True)
        except Exception:
            pass

    def hide(self, worker):
        hwnd = self._hwnd.get(worker)
        if not hwnd:
            return
        try:
            self._set_topmost(hwnd, False)
            self._move_window(hwnd, OFFSCREEN_X, OFFSCREEN_Y)
            self.log(f"窗口已收回: {worker}")
        except Exception as exc:
            self.log(f"收回窗口失败 {worker}: {exc}")

    def show(self, worker):
        hwnd = self._hwnd.get(worker)
        if not hwnd:
            return False
        try:
            self._force_foreground(hwnd)
            self.log(f"窗口已弹出并置顶，等待人工验证: {worker}")
            return True
        except Exception as exc:
            self.log(f"弹出窗口失败 {worker}: {exc}")
            return False

    # ---------- worker → 窗口解析 ----------

    def _resolve_window(self, worker, profile_dir):
        port = None
        tail = str(profile_dir).rstrip("\\/").split("\\")[-1].split("/")[-1]
        if tail.isdigit():
            port = int(tail)
        if port is None:
            self.log(f"{worker} 无法解析调试端口: {profile_dir}")
            return
        self._port[worker] = port

        for attempt in range(40):
            pids = _find_pids_by_port(port)
            for pid in pids:
                # 优先按标题匹配注册窗口，避免绑定到空白新标签页/托盘窗口
                handles = _enum_windows_for_pid(pid, title_keywords=REG_TITLE_KEYWORDS)
                if not handles:
                    handles = _enum_windows_for_pid(pid)
                if handles:
                    with self._lock:
                        # 可能已被标题兜底绑定并正在显示，此时不能覆盖句柄，更不能收回
                        if worker in self._hwnd:
                            self._resolved.add(worker)
                            return
                        self._hwnd[worker] = handles[0]
                        self._resolved.add(worker)
                    self.log(f"{worker} 窗口已绑定 (pid={pid}, port={port})，先隐藏")
                    self.hide(worker)
                    return
            time.sleep(0.5)
        self.log(f"{worker} 未能定位 Chrome 窗口 (port={port}, 候选进程 {len(pids)})")

    # ---------- 日志跟踪 ----------

    def _handle_line(self, line):
        m = PROFILE_RE.search(line)
        if m:
            worker, profile_dir = _worker_of(line), m.group(1)
            # 串行模式下同一个 worker 名会对应「新启动的浏览器」，
            # 端口变化时必须重新绑定窗口，否则会一直指着已关闭的旧窗口。
            new_port = None
            tail = str(profile_dir).rstrip("\\/").split("\\")[-1].split("/")[-1]
            if tail.isdigit():
                new_port = int(tail)
            if worker not in self._resolved or self._port.get(worker) != new_port:
                with self._lock:
                    self._hwnd.pop(worker, None)
                    self._resolved.discard(worker)
                threading.Thread(
                    target=self._resolve_window,
                    args=(worker, profile_dir),
                    daemon=True,
                ).start()
            return

        if WAIT_RE.search(line) or WAIT_READY_RE.search(line):
            worker = _worker_of(line)
            with self._lock:
                if worker not in self._pending and worker != self._visible:
                    self._pending.append(worker)
                    self.log(f"{worker} 需要人工验证，已入队（队列 {len(self._pending)}）")
            self._pump()
            return

        if DONE_RE.search(line):
            worker = _worker_of(line)
            with self._lock:
                if worker == self._visible:
                    self._visible = None
            self.hide(worker)
            self._pump()

    def _claim_offscreen_window(self):
        """按窗口标题找一个还没被占用、停在屏幕外的注册窗口。

        调试端口绑定失败时的兜底：注册窗口标题固定为「创建您的 Grok 账户」，
        且坐标在屏幕外（left < -500）。只返回尚未分配给其它 worker 的句柄。
        调用方需持有 self._lock。
        """
        taken = set(self._hwnd.values())
        found = []

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) <= 0:
                return True
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if "创建您的 Grok 账户" not in buf.value:
                return True
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            if rect.left < -500 or rect.left > 4000:
                found.append(hwnd)
            return True

        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(callback)
        user32.EnumWindows(proc, 0)
        for hwnd in found:
            if hwnd not in taken:
                return hwnd
        return None

    def _pump(self):
        """弹出队列中的下一个（同一时刻只弹一个）。"""
        with self._lock:
            if self.one_at_a_time and self._visible is not None:
                return
            worker = None
            skipped = []
            while self._pending:
                cand = self._pending.pop(0)
                if cand not in self._hwnd:
                    hwnd = self._claim_offscreen_window()
                    if hwnd:
                        self._hwnd[cand] = hwnd
                        self._resolved.add(cand)
                        self.log(f"{cand} 端口绑定失败，已按窗口标题兜底绑定")
                if cand in self._hwnd:
                    worker = cand
                    self._visible = cand
                    break
                skipped.append(cand)
            if skipped:
                self._pending = skipped + self._pending
            if worker is None:
                return
        if not self.show(worker):
            # 兜底：窗口句柄绑定失败时，把所有调试窗口拉回屏幕，保证用户点得到
            self.rescue_show_all()

    def rescue_show_all(self):
        """兜底救场：把所有带调试端口的 Chrome 窗口移到屏幕内并置顶。"""
        moved = 0
        try:
            if psutil is None:
                return
            ports = set()
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    if str(proc.info.get("name") or "").lower() != "chrome.exe":
                        continue
                    for part in proc.cmdline():
                        if str(part).startswith("--remote-debugging-port="):
                            ports.add(str(part).split("=", 1)[1])
                except Exception:
                    continue
            for port in ports:
                for pid in _find_pids_by_port(int(port)):
                    for hwnd in _enum_windows_for_pid(pid, title_keywords=REG_TITLE_KEYWORDS) or _enum_windows_for_pid(pid):
                        try:
                            self._force_foreground(hwnd)
                            moved += 1
                        except Exception:
                            pass
        except Exception as exc:
            self.log(f"兜底救场失败: {exc}")
        if moved:
            self.log(f"兜底救场：已把 {moved} 个浏览器窗口拉回屏幕")

    def _loop(self):
        last_top_assert = 0.0
        while not self._stop.is_set():
            try:
                import os
                import time as _time

                if os.path.exists(self.log_path):
                    # 日志编码可能是 UTF-8 或 UTF-16（PowerShell 重定向会产出 UTF-16），
                    # 这里自动探测，否则会读到空内容导致窗口永远不弹。
                    chunk = ""
                    for enc in ("utf-8", "utf-16", "gbk"):
                        try:
                            with open(self.log_path, encoding=enc, errors="strict") as f:
                                f.seek(self._pos)
                                chunk = f.read()
                                self._pos = f.tell()
                            break
                        except (UnicodeDecodeError, UnicodeError):
                            chunk = ""
                            continue
                        except Exception:
                            break
                    for line in chunk.splitlines():
                        self._handle_line(line)

                # 每 2 秒重申一次 TOPMOST，防止等待验证期间被其它程序盖住
                now = _time.time()
                if now - last_top_assert >= 2.0:
                    last_top_assert = now
                    with self._lock:
                        visible = self._visible
                    if visible:
                        self._keep_on_top(visible)
                # 当前弹出的窗口如果已经消失（句柄失效），不能一直占着
                # "正在显示"的名额，否则队列里其余窗口永远弹不出来。
                with self._lock:
                    visible = self._visible
                if visible and not user32.IsWindow(self._hwnd.get(visible) or 0):
                    self.log(f"{visible} 的窗口已消失，释放并弹出下一个")
                    with self._lock:
                        self._hwnd.pop(visible, None)
                        self._resolved.discard(visible)
                        if self._visible == visible:
                            self._visible = None
                    self._pump()
                # 端口绑定失败的 worker 没有句柄，周期性地按窗口标题兜底弹出
                with self._lock:
                    need_pump = self._visible is None and bool(self._pending)
                if need_pump:
                    self._pump()
            except Exception:
                pass
            time.sleep(1)

    # ---------- 生命周期 ----------

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.log("窗口控制器已启动（默认隐藏，验证时逐个弹出）")

    def stop(self):
        self._stop.set()
        for worker in list(self._hwnd.keys()):
            self.hide(worker)
