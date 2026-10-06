"""Attach mode: translate in the Chrome the user already has open (Chrome 144+).

The user ticks "Allow remote debugging for this browser instance" at chrome://inspect/#remote-debugging
once. Chrome then writes DevToolsActivePort into its user data folder and accepts one kind of client:
a websocket to /devtools/browser, held in the handshake until the user clicks Allow in Chrome's dialog
(asked again for every connection; /json and per-tab sockets are refused). So everything goes over that
one socket, with a flatten session per followed tab.

Nothing here may close the user's browser, windows or tabs: leaving only ends our sessions and the socket.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import mycdp as cdp
import websockets
from mycdp.util import _event_parsers
from websockets.exceptions import InvalidStatus

from .cdp import CDPError

LOG = logging.getLogger("poomcatomanga")
DETAIL = logging.getLogger("poomcatomanga.detail")

INSPECT_URL = "chrome://inspect/#remote-debugging"
# (name, user data folder under %LOCALAPPDATA%), Chrome stable first
BROWSERS = [
    ("Chrome", r"Google\Chrome\User Data"),
    ("Chrome Beta", r"Google\Chrome Beta\User Data"),
    ("Chrome Dev", r"Google\Chrome Dev\User Data"),
    ("Chrome Canary", r"Google\Chrome SxS\User Data"),
    ("Edge", r"Microsoft\Edge\User Data"),
    ("Brave", r"BraveSoftware\Brave-Browser\User Data"),
]
BROWSER_EXES = {"chrome.exe", "msedge.exe", "brave.exe", "chrome", "msedge", "brave"}
WEB = ("http://", "https://", "file:")
# could close the user's browser, windows or tabs: refused before anything is sent
FORBIDDEN = {"Browser.close", "Browser.crash", "Browser.crashGpuProcess", "Target.closeTarget",
             "Target.disposeBrowserContext", "Page.close", "Page.crash"}
BYE = "PoomCatoManga: หยุดแปลแล้ว • Alt+T สลับต้นฉบับ"

MESSAGES = {
    "missing": "ยังไม่ได้เปิดให้แอปใช้ Chrome ที่เปิดอยู่ — ใน Chrome เปิด " + INSPECT_URL
               + " แล้วติ๊ก \"Allow remote debugging for this browser instance\" (ทำครั้งเดียว)",
    "stale": "ไม่พบ Chrome ที่เปิดการอนุญาตไว้ — เปิด Chrome ก่อน แล้วตรวจว่ายังติ๊ก \"Allow remote debugging\" ที่ "
             + INSPECT_URL + " อยู่",
    "denied": "Chrome ไม่อนุญาตให้เชื่อมต่อ — กดเริ่มอีกครั้ง แล้วกด Allow (อนุญาต) ด้วยเมาส์ในหน้าต่างที่ Chrome ถาม "
              "(ต้องมีหน้าต่าง Chrome เปิดอยู่)",
    "timeout": "รอกด Allow นานเกิน {wait} วินาที — ถ้า Chrome ยังถามอยู่ ให้กด Cancel (ยกเลิก) แล้วกดเริ่มใหม่",
    "notab": "ไม่พบแท็บมังงะที่เปิดอยู่ด้านหน้า — คลิกแท็บตอนที่จะอ่านใน Chrome แล้วกดเริ่มอีกครั้ง "
             "(หรือวางลิงก์ไว้ในช่องลิงก์ แอปจะเปิดให้ในแท็บใหม่)",
}


def attach_wait() -> float:
    """Seconds to wait for the user to answer Chrome's Allow dialog (PCM_ATTACH_TIMEOUT for tests)."""
    try:
        return max(1.0, float(os.environ.get("PCM_ATTACH_TIMEOUT") or 60))
    except ValueError:
        return 60.0


class AttachError(Exception):
    """kind: missing | stale | denied | timeout | notab"""

    def __init__(self, kind: str, msg: str = ""):
        self.kind = kind
        self.msg = msg or MESSAGES.get(kind, kind).format(wait=int(attach_wait()))
        super().__init__(self.msg)


# ------------------------------------------------------------- endpoint ---
@dataclass
class Endpoint:
    name: str
    port: int
    path: str
    pid: int | None = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}{self.path}"


def _read_port_file(folder: str):
    """(port, browser socket path) from DevToolsActivePort. Read only: never written or deleted."""
    try:
        with open(os.path.join(folder, "DevToolsActivePort"), "r", encoding="ascii", errors="replace") as f:
            lines = [ln.strip() for ln in f.read(4096).splitlines() if ln.strip()]
        port, path = int(lines[0]), lines[1]
    except (OSError, ValueError, IndexError):
        return None
    if 0 < port < 65536 and path.startswith("/devtools/browser"):
        return port, path
    return None


def _listening(port: int) -> bool:
    try:   # a bare TCP connect: Chrome only asks the user when a websocket handshake arrives
        with socket.create_connection(("127.0.0.1", port), 0.5):
            return True
    except OSError:
        return False


def _listener_pid(port: int) -> int | None:
    try:
        import psutil
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                return c.pid
    except Exception:
        pass
    return None


def _exe_name(pid: int | None) -> str:
    try:
        import psutil
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


def scan(dirs, check_owner: bool = True):
    """First folder whose DevToolsActivePort names a live browser -> (Endpoint, ""), else (None, why).
    A port file stays behind after the browser exits (and its port may be reused by another program),
    so the port must be listening and, with check_owner, belong to a browser."""
    why = "missing"
    for name, folder in dirs:
        found = _read_port_file(folder)
        if found is None:
            continue
        port, path = found
        if not _listening(port):
            why = "stale"
            continue
        pid = _listener_pid(port)
        exe = _exe_name(pid) if pid else ""
        if check_owner and exe and exe not in BROWSER_EXES:
            why = "stale"
            continue
        return Endpoint(name, port, path, pid), ""
    return None, why


def find_endpoint():
    """The running browser to attach to. PCM_ATTACH_DIR (tests) names the only folder to look in."""
    only = os.environ.get("PCM_ATTACH_DIR")
    if only:
        return scan([("Chrome", only)], check_owner=False)
    base = os.environ.get("LOCALAPPDATA") or ""
    if not base:
        return None, "missing"
    return scan([(name, os.path.join(base, rel)) for name, rel in BROWSERS])


def allow_foreground():
    """Let Chrome bring its Allow dialog to the front while this app is the foreground app."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)   # ASFW_ANY
        except Exception:
            pass


def _front_window_title(pid: int | None) -> str | None:
    """Title of this browser's front-most window ("<tab title> - Google Chrome"), None if unknown."""
    if os.name != "nt" or not pid:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        user32.IsWindowVisible.argtypes = user32.IsIconic.argtypes = (wintypes.HWND,)
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        found = []

        def each(hwnd, _):   # EnumWindows walks top-level windows front to back
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid:
                return True
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            n = user32.GetWindowTextLengthW(hwnd)
            if cls.value != "Chrome_WidgetWin_1" or n <= 0 or r.right - r.left < 200 or r.bottom - r.top < 150:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            found.append(buf.value)
            return False

        user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(each), 0)
        return found[0] if found else None
    except Exception:
        return None


def _norm(s: str) -> str:
    return " ".join((s or "").split())


def _in_window(title: str, pages) -> list:
    """Pages whose tab title the window title shows (the longest match wins)."""
    title = _norm(title)
    hits = [t for t in pages if _norm(t.title) and (title == _norm(t.title) or title.startswith(_norm(t.title) + " "))]
    if not hits:
        return []
    n = max(len(_norm(t.title)) for t in hits)
    return [t for t in hits if len(_norm(t.title)) == n]


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower() if url.startswith(("http://", "https://")) else ""
    except ValueError:
        return ""


# ----------------------------------------------------------- connection ---
class Conn:
    """The one browser websocket: replies matched by id, events routed by flatten session."""

    def __init__(self, ws):
        self.ws = ws
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.tabs: dict[str, "SessionTab"] = {}     # sessionId -> tab
        self.on_event = None                         # browser-level events: fn(method, params)
        self.closed = asyncio.Event()
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def open(cls, ep: Endpoint, wait: float) -> "Conn":
        # Chrome holds the handshake until its "Allow remote debugging?" dialog is answered:
        # Allow -> 101, Cancel / closed dialog / no browser window -> HTTP 403
        try:
            ws = await websockets.connect(ep.url, open_timeout=wait, ping_interval=None, close_timeout=2,
                                          proxy=None, max_size=2 ** 28, compression=None)
        except InvalidStatus as e:
            raise AttachError("denied" if e.response.status_code == 403 else "stale") from None
        except TimeoutError:
            raise AttachError("timeout") from None
        except (OSError, websockets.exceptions.WebSocketException):
            raise AttachError("stale") from None
        return cls(ws)

    async def send(self, cmd, session_id: str | None = None, timeout: float = 15):
        req = next(cmd)
        method = req.get("method", "")
        if method in FORBIDDEN:
            cmd.close()
            raise CDPError(f"{method} is never sent to the user's browser")
        if self.closed.is_set():
            cmd.close()
            raise CDPError(f"{method}: connection to the browser is closed")
        i = next(self.ids)
        req["id"] = i
        if session_id:
            req["sessionId"] = session_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[i] = fut
        try:
            await self.ws.send(json.dumps(req))
            msg = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise CDPError(f"{method} timed out after {timeout:.0f}s") from None
        except (asyncio.CancelledError, CDPError):
            raise
        except Exception as e:
            raise CDPError(f"{method}: {e}") from None
        finally:
            self.pending.pop(i, None)
        if "error" in msg:
            raise CDPError(f"{method}: {msg['error'].get('message')}")
        try:
            cmd.send(msg.get("result", {}))
        except StopIteration as e:
            return e.value
        except Exception as e:
            raise CDPError(f"{method}: {e}") from None
        return None

    async def _read(self):
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if "id" in msg:
                    fut = self.pending.get(msg["id"])
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                    continue
                method, params, sid = msg.get("method", ""), msg.get("params") or {}, msg.get("sessionId")
                if sid:
                    tab = self.tabs.get(sid)
                    if tab is not None:
                        tab._dispatch(method, params)
                    continue
                if method == "Target.detachedFromTarget":
                    tab = self.tabs.pop(params.get("sessionId", ""), None)
                    if tab is not None:
                        tab.alive = False
                if self.on_event is not None:
                    try:
                        self.on_event(method, params)
                    except Exception as e:
                        LOG.debug("attach event %s: %s", method, e)
        except Exception:
            pass   # connection closed
        finally:
            self.closed.set()
            for tab in self.tabs.values():
                tab.alive = False
            for fut in list(self.pending.values()):
                if not fut.done():
                    fut.set_exception(CDPError("connection to the browser closed"))

    async def close(self):
        try:
            await asyncio.wait_for(self.ws.close(), 3)
        except Exception:
            pass
        await asyncio.wait({self._reader}, timeout=2)
        self._reader.cancel()
        self.closed.set()


class SessionTab:
    """A followed tab: a flatten session on the shared socket, shaped like the SeleniumBase Tab the
    Session code uses (target_id, add_handler; pcm.cdp.call goes through send_cdp)."""

    def __init__(self, conn: Conn, target_id: str, session_id: str):
        self.conn, self.target_id, self.session_id = conn, target_id, session_id
        self.session_key = hash((id(conn), session_id))   # pcm.cdp.socket_id
        self.alive = True
        self.handlers: dict[type, list] = {}

    def add_handler(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)

    async def send_cdp(self, cmd, timeout: float = 15):
        if not self.alive:
            cmd.close()
            raise CDPError("the tab is closed or no longer connected")
        return await self.conn.send(cmd, self.session_id, timeout)

    def _dispatch(self, method: str, params: dict):
        cls = _event_parsers.get(method)
        handlers = self.handlers.get(cls) if cls else None
        if not handlers:
            return
        try:
            ev = cls.from_json(params)
        except Exception:
            return
        for h in list(handlers):
            try:
                h(ev, self)
            except Exception:
                pass


# -------------------------------------------------------------- browser ---
class AttachedBrowser:
    """Stands in for the SeleniumBase Browser in attach mode. The Session only ever sees the followed
    tabs: the one chosen at start, tabs opened from a followed tab, and new tabs of the same site."""

    def __init__(self, conn: Conn, ep: Endpoint):
        self.conn, self.endpoint = conn, ep
        self.followed: dict[str, SessionTab] = {}
        self.main_tab: SessionTab | None = None
        self.on_follow = None              # fn(tab): a newly followed tab, to be prepared right away
        self._seen: set[str] = set()       # targets already decided on (followed or left alone)
        conn.on_event = self._on_event

    @classmethod
    async def connect(cls, ep: Endpoint, wait: float) -> "AttachedBrowser":
        conn = await Conn.open(ep, wait)
        br = cls(conn, ep)
        try:
            br._seen = {t.target_id for t in await br._pages()}   # tabs already open are never followed
            await conn.send(cdp.target.set_discover_targets(discover=True), timeout=10)
        except BaseException:
            await conn.close()
            raise
        return br

    @property
    def alive(self) -> bool:
        return not self.conn.closed.is_set()

    @property
    def tabs(self) -> list:
        return [t for t in self.followed.values() if t.alive]

    async def _pages(self):
        return [t for t in await self.conn.send(cdp.target.get_targets(), timeout=10)
                if t.type_ == "page" and not t.subtype]

    async def follow(self, target_id) -> SessionTab:
        target_id = cdp.target.TargetID(target_id)   # ids from raw events are plain str
        self._seen.add(target_id)
        tab = self.followed.get(target_id)
        if tab is None or not tab.alive:
            sid = await self.conn.send(cdp.target.attach_to_target(target_id, flatten=True), timeout=10)
            tab = self.followed[target_id] = SessionTab(self.conn, target_id, sid)
            self.conn.tabs[sid] = tab
        return tab

    async def open_tab(self) -> SessionTab:
        """A new blank tab in the user's browser (Chrome puts it in front, in its last active window).
        The Session prepares it and then navigates it to the link."""
        tid = await self.conn.send(cdp.target.create_target("about:blank"), timeout=15)
        self.main_tab = await self.follow(tid)
        return self.main_tab

    # -- which tab is the user looking at
    async def pick_front(self, stop: asyncio.Event, waiting=None, wait: float = 20) -> SessionTab | None:
        """The active tab of the browser's front-most window. If that is not a web page (e.g. still on
        chrome://inspect) or can't be told, waits up to `wait` s for the user to click the manga tab.
        None if stopped meanwhile; AttachError('notab') when nothing could be chosen."""
        probes: dict[str, str] = {}
        deadline, asked = time.monotonic() + wait, False
        try:
            while True:
                tid = await self._front(probes)
                if tid:
                    self.main_tab = await self.follow(tid)
                    return self.main_tab
                if stop.is_set():
                    return None
                if time.monotonic() > deadline:
                    raise AttachError("notab")
                if not asked and waiting:
                    asked = True
                    waiting()
                try:
                    await asyncio.wait_for(stop.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            for sid in probes.values():
                try:
                    await self.conn.send(cdp.target.detach_from_target(session_id=sid), timeout=2)
                except Exception:
                    pass

    async def _front(self, probes) -> str | None:
        pages = await self._pages()
        title = await asyncio.get_running_loop().run_in_executor(None, _front_window_title, self.endpoint.pid)
        shown = _in_window(title, pages) if title else []
        if shown:
            web = [t for t in shown if t.url.startswith(WEB)]
            if not web:
                return None        # the front window shows a non-web tab (new tab page, chrome://inspect)
            if len(web) == 1:
                return web[0].target_id
            pages = web            # same title in several tabs: see which one is showing
        # no window to go by (headless, other OS, untitled page): ask the pages themselves
        looks = await asyncio.gather(*(self._look(t, probes) for t in pages if t.url.startswith(WEB)))
        visible = [t for t, (vis, _focus) in looks if vis]
        focused = [t for t, (vis, focus) in looks if vis and focus]
        for group in (focused, visible):
            if len(group) == 1:
                return group[0].target_id
        return None

    async def _look(self, info, probes):
        """(visible, focused) of a page, through a short read-only session (detached afterwards)."""
        try:
            sid = probes.get(info.target_id)
            if sid is None:
                sid = probes[info.target_id] = await self.conn.send(
                    cdp.target.attach_to_target(info.target_id, flatten=True), timeout=3)
            remote, _ = await self.conn.send(cdp.runtime.evaluate(
                "[document.visibilityState === 'visible', document.hasFocus()]", return_by_value=True), sid, 2.5)
            vis, focus = remote.value
            return info, (bool(vis), bool(focus))
        except Exception:
            return info, (False, False)

    # -- following
    def _on_event(self, method, params):
        if method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            tid = info.get("targetId")
            if info.get("type") == "page" and tid and tid not in self._seen and info.get("openerId") in self.followed:
                self._seen.add(tid)
                asyncio.ensure_future(self._follow_new(tid, "เปิดจากแท็บที่แปลอยู่"))

    async def _follow_new(self, tid, why):
        try:
            tab = await self.follow(tid)
        except Exception as e:
            DETAIL.warning("ติดตามแท็บใหม่ไม่ได้: %s", e)
            return
        DETAIL.info("ติดตามแท็บใหม่ (%s)", why)
        if self.on_follow is not None:
            self.on_follow(tab)

    async def update_targets(self):
        """Drop followed tabs that were closed; follow tabs opened from a followed tab, and new tabs
        of a followed tab's site (ctrl+click / middle-click on "next chapter" carry no opener).
        Every other tab is left alone."""
        pages = await self._pages()
        live = {t.target_id: t for t in pages}
        for tid in [tid for tid, tab in self.followed.items() if tid not in live or not tab.alive]:
            tab = self.followed.pop(tid)
            self.conn.tabs.pop(tab.session_id, None)
        hosts = {_host(live[tid].url) for tid in self.followed} - {""}
        for t in pages:
            if t.target_id in self._seen:
                continue
            if t.opener_id and t.opener_id in self.followed:
                self._seen.add(t.target_id)
                await self._follow_new(t.target_id, "เปิดจากแท็บที่แปลอยู่")
            elif _host(t.url):
                self._seen.add(t.target_id)
                if _host(t.url) in hosts:
                    await self._follow_new(t.target_id, "เว็บเดียวกัน")
            # still blank / new tab page: decided once it shows a web page

    async def detach(self):
        """Leave the user's browser exactly as it is: end our tab sessions, then close the socket.
        Translated images stay in the page and Alt+T keeps working without the app."""
        async def leave(tab):
            try:
                await tab.send_cdp(cdp.runtime.evaluate(
                    "window.__gm && __gm.setStatus(__gm.done.size || __gm.canv.size ? %s : '')"
                    % json.dumps(BYE, ensure_ascii=False), return_by_value=True), 2)
            except Exception:
                pass
            try:
                await self.conn.send(cdp.target.detach_from_target(session_id=tab.session_id), timeout=2)
            except Exception:
                pass

        try:
            if self.alive:
                await asyncio.gather(*(leave(t) for t in self.tabs))
        finally:
            self.followed.clear()
            await self.conn.close()
