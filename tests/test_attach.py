"""Attach mode ("ใช้ Chrome ที่เปิดอยู่") against a Chrome this test starts itself.

The test Chrome gets a fresh profile under tests/out/attach and --remote-debugging-port=0, so it writes
DevToolsActivePort the way Chrome's chrome://inspect toggle does and accepts the same browser-socket
client (minus the Allow dialog, which needs a person: see README). PCM_ATTACH_DIR points the app at that
profile only, so no real browser folder is ever looked at. Only the test's own Chrome (by PID) is killed.

usage: python tests/test_attach.py
env:   PCM_TEST_PORT (site port, 0 = any free one), PCM_HEADLESS (default 1), PCM_CHROME (chrome.exe path)
"""
import asyncio
import functools
import http.server
import json
import logging
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out", "attach")
os.makedirs(OUT, exist_ok=True)
# before anything from pcm is imported: the app must never look at a real browser's folder in this test
os.environ["PCM_ATTACH_DIR"] = os.path.join(OUT, "not-started-yet")
os.environ["PCM_HEADLESS"] = os.environ.get("PCM_HEADLESS", "1")
sys.path.insert(0, os.path.dirname(HERE))

import mycdp as cdp
import psutil

from pcm import attach
from pcm.attach import AttachError, Conn, scan
from pcm.cdp import CDPError
from pcm.config import Settings
from pcm.session import Session
from test_session import SITE, Quiet, serve, site_url

HEADLESS = os.environ["PCM_HEADLESS"] == "1"
CHROME = next((p for p in (os.environ.get("PCM_CHROME"),
                           r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                           os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")) if p and os.path.exists(p)), None)
RESULTS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)
    RESULTS.append(bool(cond))
    return cond


class Grab(logging.Handler):
    """The app's log lines, to check what the user was told."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def has(self, text):
        return any(text in ln for ln in self.lines)


LOGS = Grab()


# ------------------------------------------------------------ test Chrome ---
def launch_chrome(urls):
    profile = tempfile.mkdtemp(prefix="chrome_", dir=OUT)
    args = [CHROME, f"--user-data-dir={profile}", "--remote-debugging-port=0", "--no-first-run",
            "--no-default-browser-check", "--disable-sync", "--lang=en-US", "--window-size=1100,900"]
    if HEADLESS:
        args.append("--headless=new")
    proc = subprocess.Popen(args + urls[:1], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < 30 and proc.poll() is None:
        if attach._read_port_file(profile):
            for url in urls[1:]:   # headless Chrome takes one URL on the command line
                new_tab(endpoint(profile), url)
            return proc, profile
        time.sleep(0.2)
    kill_tree(proc.pid)
    raise SystemExit("test Chrome did not write DevToolsActivePort")


def kill_tree(pid):
    """Only the Chrome this test started (its PID and that process's children)."""
    try:
        root = psutil.Process(pid)
        procs = root.children(recursive=True) + [root]
    except psutil.Error:
        return
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=10)


def remove_profile(profile):
    if os.path.dirname(os.path.abspath(profile)) != os.path.abspath(OUT):
        return
    for _ in range(20):
        shutil.rmtree(profile, ignore_errors=True)
        if not os.path.exists(profile):
            return
        time.sleep(0.5)


def endpoint(profile):
    ep, why = scan([("Test Chrome", profile)], check_owner=False)
    assert ep, why
    return ep


async def _with_conn(ep, fn):
    conn = await Conn.open(ep, 10)
    try:
        return await fn(conn)
    finally:
        await conn.close()


async def _pages(conn):
    return [t for t in await conn.send(cdp.target.get_targets()) if t.type_ == "page" and not t.subtype]


async def _eval(conn, tid, expr, gesture=False):
    sid = await conn.send(cdp.target.attach_to_target(tid, flatten=True))
    try:
        remote, exc = await conn.send(cdp.runtime.evaluate(expr, return_by_value=True, await_promise=True,
                                                           user_gesture=gesture), sid, 15)
        return None if exc is not None or remote is None else remote.value
    finally:
        await conn.send(cdp.target.detach_from_target(session_id=sid))


def pages(ep):
    """{target id: url} of the test Chrome's tabs (seen from a separate client)."""
    return {t.target_id: t.url for t in asyncio.run(_with_conn(ep, _pages))}


def evaluate(ep, tid, expr, gesture=False):
    return asyncio.run(_with_conn(ep, lambda c: _eval(c, tid, expr, gesture)))


def new_tab(ep, url):
    return asyncio.run(_with_conn(ep, lambda c: c.send(cdp.target.create_target(url))))


STATE = """JSON.stringify({gm: typeof window.__gm, toggle: window.__gm ? typeof __gm.toggle : '',
  done: document.querySelectorAll('img[data-gm-state=done]').length,
  shown: [...document.querySelectorAll('img[data-gm-state=done]')].filter(i => i.complete && i.naturalWidth > 0).length,
  canvas: window.__gm ? __gm.canv.size : 0, any: document.querySelectorAll('[data-gm-id]').length,
  chip: window.__gm && __gm.chip ? __gm.chip.textContent : ''})"""


def state(ep, tid):
    return json.loads(evaluate(ep, tid, STATE) or "{}")


def wait_for(fn, timeout, step=0.5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


# --------------------------------------------------------------- sessions ---
class Run:
    def __init__(self, url="", **kw):
        s = Settings()
        s.engine, s.browser_mode, s.last_url, s.keep_browser_profile = "google", "attach", url, False
        for k, v in kw.items():
            setattr(s, k, v)
        self.q = queue.Queue()
        self.events = []
        self.sess = Session(s, self.q)
        self.t0 = time.time()
        self.sess.start()

    def pump(self):
        while True:
            try:
                self.events.append(self.q.get_nowait())
            except queue.Empty:
                return

    def wait(self, pred, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.pump()
            if pred(self):
                return True
            time.sleep(0.25)
        self.pump()
        return pred(self)

    def of(self, kind):
        return [e for e in self.events if e[0] == kind]

    def progress(self):
        p = self.of("progress")
        return p[-1][1:] if p else (0, 0, 0, 0)

    def stop(self, timeout=30):
        self.sess.stop()
        self.sess.join(timeout)
        self.pump()
        return not self.sess.running


def translated(run, n):
    done, total, failed, busy = run.progress()
    return done >= n and busy == 0


# ------------------------------------------------------------------ cases ---
def case_front_tab(site, other):
    """No link: translate the tab in front, follow tabs opened from it, leave everything else alone."""
    print("\n== front tab (no link) ==", flush=True)
    manga, decoy = site_url(site, "index.html"), site_url(other, "flip.html")
    proc, profile = launch_chrome([decoy, manga])     # the last one is the active tab
    try:
        ep = endpoint(profile)
        os.environ["PCM_ATTACH_DIR"] = profile
        tabs = wait_for(lambda: len(pages(ep)) >= 2 and pages(ep), 15)
        mid = next(t for t, u in tabs.items() if u == manga)
        did = next(t for t, u in tabs.items() if u == decoy)
        asyncio.run(_with_conn(ep, lambda c: c.send(cdp.target.activate_target(mid))))   # the user is on the manga tab
        time.sleep(1)
        print("visible:", {u.rsplit("/", 1)[-1]: evaluate(ep, t, "document.visibilityState") for t, u in tabs.items()})
        run = Run()
        ok = run.wait(lambda r: translated(r, 2) or not r.sess.running, 150)
        check("front tab: images translated", ok and run.progress()[0] >= 2, f"progress={run.progress()}")
        st = state(ep, mid)
        check("front tab: page agent in the manga tab", st.get("gm") == "object" and st.get("done", 0) + st.get("canvas", 0) >= 1, st)
        check("front tab: unrelated tab untouched", state(ep, did) == {"gm": "undefined", "toggle": "", "done": 0, "shown": 0,
                                                                       "canvas": 0, "any": 0, "chip": ""}, state(ep, did))
        check("front tab: CSP page loaded before attach -> F5 hint", wait_for(lambda: LOGS.has("กด F5"), 10), "")
        check("status asked the user to Allow", any("กด Allow" in e[1] for e in run.of("status")), run.of("status")[:1])

        # a tab opened from the manga tab (another site: followed because of its opener)
        opened_url = "http://localhost:%d/chapter_b.html" % site.server_address[1]
        evaluate(ep, mid, "(()=>{const a=document.createElement('a');a.href=%r;a.target='_blank';"
                          "document.body.append(a);a.click();return 1})()" % opened_url, gesture=True)
        oid = wait_for(lambda: next((t for t, u in pages(ep).items() if u == opened_url), None), 15)
        check("opener tab: opened", bool(oid))
        ost = wait_for(lambda: (lambda s: s.get("shown", 0) >= 1 and s)(state(ep, oid)), 90) if oid else {}
        check("opener tab: followed and translated", bool(ost), ost or state(ep, oid))

        # a new tab of another site with no opener (like the user opening Gmail): never touched
        uid = new_tab(ep, site_url(other, "chapter_b.html"))
        # a new tab of the manga site with no opener (ctrl+click on "next chapter"): followed
        sid = new_tab(ep, site_url(site, "chapter_b.html") + "?next")
        sst = wait_for(lambda: (lambda s: s.get("shown", 0) >= 1 and s)(state(ep, sid)), 90)
        check("same-site new tab: followed and translated", bool(sst), sst or state(ep, sid))
        time.sleep(3)
        check("unrelated new tab untouched", state(ep, uid).get("gm") == "undefined" and state(ep, uid).get("any") == 0,
              state(ep, uid))
        check("decoy still untouched", state(ep, did).get("gm") == "undefined", state(ep, did))

        before = pages(ep)
        stopped = run.stop()
        check("stop: session ended", stopped and bool(run.of("stopped")))
        check("stop: test Chrome still running", proc.poll() is None)
        after = pages(ep)
        check("stop: every tab still open", set(after) == set(before) and len(after) == 5, f"{len(before)} -> {len(after)}")
        check("stop: DevToolsActivePort left alone", attach._read_port_file(profile) is not None)
        check("stop: no own-mode profile", run.sess._profile_tmp is None)
        st = state(ep, mid)
        check("after detach: Alt+T agent still in the page", st.get("toggle") == "function", st)
        check("after detach: chip says stopped", st.get("chip") == attach.BYE, st.get("chip"))
        flipped = evaluate(ep, mid, "__gm.toggle().then(o => [o, document.querySelector('img[data-gm-state=done]').src])")
        evaluate(ep, mid, "__gm.toggle(false)")
        check("after detach: toggle shows the original", bool(flipped) and flipped[0] is True and "blob:" not in flipped[1], flipped)
        check("log: stopped message keeps Chrome open", LOGS.has("Chrome ของคุณยังเปิดอยู่"))
    finally:
        kill_tree(proc.pid)
        remove_profile(profile)


def case_link(site):
    """A link: opened in a new tab of the user's Chrome, prepared before it loads (CSP bypass works)."""
    print("\n== link opens a new tab ==", flush=True)
    proc, profile = launch_chrome([site_url(site, "flip.html")])
    try:
        ep = endpoint(profile)
        os.environ["PCM_ATTACH_DIR"] = profile
        before = wait_for(lambda: pages(ep), 10)
        link = site_url(site, "index.html") + "?link"
        run = Run(link)
        ok = run.wait(lambda r: translated(r, 2) or not r.sess.running, 150)
        check("link: images translated", ok, f"progress={run.progress()}")
        now = pages(ep)
        new = [t for t in now if t not in before]
        check("link: opened in one new tab", len(new) == 1 and now[new[0]] == link, {t[:6]: u for t, u in now.items()})
        st = state(ep, new[0]) if new else {}
        check("link: translations shown (no CSP block)", st.get("shown", 0) >= 1 and st.get("shown") == st.get("done"), st)
        check("link: the tab that was already open is untouched", all(state(ep, t).get("gm") == "undefined" for t in before))
        # the guard: commands that would close the user's tabs are never sent
        try:
            asyncio.run(_with_conn(ep, lambda c: c.send(cdp.target.close_target(new[0]))))
            refused = False
        except CDPError:
            refused = True
        check("Target.closeTarget refused by the attach connection", refused and new[0] in pages(ep))
        stopped = run.stop()
        after = pages(ep)
        check("link: stop keeps Chrome and all tabs", stopped and proc.poll() is None and set(after) == set(now), len(after))

        # the browser socket drops (Chrome closed / permission turned off): stop, never reconnect silently
        LOGS.lines.clear()
        run = Run(link)
        run.wait(lambda r: r.of("progress"), 60)
        asyncio.run_coroutine_threadsafe(run.sess.browser.conn.ws.close(), run.sess.loop)
        ended = run.wait(lambda r: bool(r.of("stopped")), 15)
        check("dropped socket: session stops by itself", ended, f"{time.time() - run.t0:.1f}s")
        check("dropped socket: user is told", LOGS.has("การเชื่อมต่อกับ Chrome หลุด"))
        check("dropped socket: Chrome and tabs untouched", proc.poll() is None and len(pages(ep)) == len(after) + 1)
    finally:
        kill_tree(proc.pid)
        remove_profile(profile)


class HangServer:
    """Accepts TCP and never answers: Chrome waiting for the user to press Allow."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.conns = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                self.conns.append(self.sock.accept()[0])
            except OSError:
                return

    def close(self):
        for c in self.conns:
            c.close()
        self.sock.close()


class DenyServer:
    """Answers the websocket handshake with 403, as Chrome does when the user presses Cancel."""

    def __init__(self):
        import websockets
        self.port = None
        ready = threading.Event()

        def deny(conn, request):
            return conn.respond(403, "Connection rejected")

        async def main():
            async with websockets.serve(lambda ws: None, "127.0.0.1", 0, process_request=deny) as srv:
                self.port = srv.sockets[0].getsockname()[1]
                self.stop = asyncio.Event()
                self.loop = asyncio.get_running_loop()
                ready.set()
                await self.stop.wait()

        threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
        ready.wait(10)

    def close(self):
        self.loop.call_soon_threadsafe(self.stop.set)


def port_dir(port, path="/devtools/browser/test"):
    d = tempfile.mkdtemp(prefix="ep_", dir=OUT)
    with open(os.path.join(d, "DevToolsActivePort"), "w", encoding="ascii", newline="") as f:
        f.write(f"{port}\n{path}")
    return d


def failure(name, folder, kind, timeout=None, stop_after_status=False):
    os.environ["PCM_ATTACH_DIR"] = folder
    if timeout:
        os.environ["PCM_ATTACH_TIMEOUT"] = str(timeout)
    try:
        run = Run()
        if stop_after_status:
            run.wait(lambda r: r.of("status"), 10)
            time.sleep(0.5)
            t0 = time.time()
            run.sess.stop()
            ended = run.wait(lambda r: bool(r.of("stopped")), 10)
            check(f"{name}: Stop during the Allow wait ends quickly", ended and time.time() - t0 < 4, f"{time.time() - t0:.1f}s")
            check(f"{name}: no failure dialog, Cancel hint logged", not run.of("attach_failed") and LOGS.has("ยกเลิกการเชื่อมต่อ"))
        else:
            ended = run.wait(lambda r: bool(r.of("stopped")), (timeout or 5) + 15)
            got = [e[1] for e in run.of("attach_failed")]
            check(f"{name}: friendly '{kind}' error", ended and got == [kind], f"{got} after {time.time() - run.t0:.1f}s")
            if got:
                print("   message:", run.of("attach_failed")[0][2])
        check(f"{name}: nothing half-started", run.sess._profile_tmp is None and run.sess.renderer._browser is None
              and not run.sess.chapters and not run.sess.running)
        return run
    finally:
        os.environ.pop("PCM_ATTACH_TIMEOUT", None)


def case_failures():
    print("\n== failures (no browser) ==", flush=True)
    empty = tempfile.mkdtemp(prefix="ep_", dir=OUT)
    failure("missing file", empty, "missing")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    failure("stale file (port closed)", port_dir(closed_port), "stale")
    garbage = tempfile.mkdtemp(prefix="ep_", dir=OUT)
    with open(os.path.join(garbage, "DevToolsActivePort"), "w") as f:
        f.write("hello\nworld")
    failure("garbage file", garbage, "missing")

    hang = HangServer()
    try:
        run = failure("no answer to Allow", port_dir(hang.port), "timeout", timeout=3)
        check("timeout: status asked to press Allow", any("กด Allow" in e[1] for e in run.of("status")))
        failure("Stop pressed", port_dir(hang.port), "", timeout=60, stop_after_status=True)
    finally:
        hang.close()
    deny = DenyServer()
    try:
        failure("user pressed Cancel", port_dir(deny.port), "denied")
    finally:
        deny.close()

    # a port file left by an exited browser whose port another program took over
    other = HangServer()
    try:
        ep, why = scan([("x", port_dir(other.port))], check_owner=True)
        check("port owned by a non-browser program -> stale", ep is None and why == "stale",
              f"owner={attach._exe_name(attach._listener_pid(other.port))}")
        # PCM_ATTACH_DIR set: the real browser folders are never consulted, even with a live one there
        fake_local = tempfile.mkdtemp(prefix="local_", dir=OUT)
        os.makedirs(os.path.join(fake_local, "Google", "Chrome", "User Data"))
        with open(os.path.join(fake_local, "Google", "Chrome", "User Data", "DevToolsActivePort"), "w") as f:
            f.write(f"{other.port}\n/devtools/browser/x")
        real_local = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"], os.environ["PCM_ATTACH_DIR"] = fake_local, empty
        try:
            only = attach.find_endpoint()
            os.environ.pop("PCM_ATTACH_DIR")
            fallback = attach.find_endpoint()
        finally:
            os.environ["LOCALAPPDATA"] = real_local
            os.environ["PCM_ATTACH_DIR"] = empty
        check("PCM_ATTACH_DIR: only that folder is searched", only == (None, "missing"), only)
        check("default folders: a listener that isn't a browser is rejected", fallback == (None, "stale"), fallback)
    finally:
        other.close()
    check("settings: unknown browser_mode falls back to own", _mode_fallback())


def _mode_fallback():
    from pcm import config
    d = tempfile.mkdtemp(prefix="cfg_", dir=OUT)
    real = os.environ.get("APPDATA")
    os.environ["APPDATA"] = d
    try:
        os.makedirs(os.path.join(d, config.DATA_DIR_NAME), exist_ok=True)
        with open(os.path.join(d, config.DATA_DIR_NAME, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"browser_mode": "bogus"}, f)
        bad = Settings.load().browser_mode
        with open(os.path.join(d, config.DATA_DIR_NAME, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"browser_mode": "attach"}, f)
        good = Settings.load().browser_mode
    finally:
        os.environ["APPDATA"] = real
    return bad == "own" and good == "attach" and Settings().browser_mode == "own"


def case_ui():
    """The main window: attach mode starts without a link, and a missing setup opens the guide."""
    print("\n== window ==", flush=True)
    import ttkbootstrap as ttk
    import poomcatomanga
    os.environ["PCM_ATTACH_DIR"] = tempfile.mkdtemp(prefix="ep_", dir=OUT)
    root = ttk.Window()
    app = poomcatomanga.App(root)
    app.settings.save = lambda: None             # never write the user's settings.json
    app.use_open_chrome.set(True)
    app._sync_mode()
    app.url.set("")
    seen = {"started": False, "t0": time.time()}

    def tick():
        if not seen["started"]:
            seen["started"] = True
            app.start()
            seen["state"] = app._state
        elif (app._guide is not None and app._state == "idle") or time.time() - seen["t0"] > 20:
            seen["guide"] = app._guide is not None and app._guide.winfo_exists()
            seen["mode"] = app.session.s.browser_mode if app.session else ""
            root.destroy()
            return
        root.after(200, tick)

    root.after(300, tick)
    root.mainloop()
    check("window: Start works without a link in attach mode", seen.get("state") == "starting" and seen.get("mode") == "attach",
          seen)
    check("window: missing setup opens the guide", seen.get("guide"), seen)


def main():
    if not CHROME:
        raise SystemExit("chrome.exe not found (set PCM_CHROME)")
    if not os.path.isdir(SITE):
        subprocess.run([sys.executable, os.path.join(HERE, "make_site.py")], check=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "seleniumbase", "websockets", "asyncio", "urllib3", "chrome_lens_py"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for very_noisy in ("uc", "uc.connection", "uc.browser", "uc.tab"):
        logging.getLogger(very_noisy).setLevel(logging.CRITICAL)
    logging.getLogger("poomcatomanga").addHandler(LOGS)
    site = serve()
    other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=SITE))
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        case_failures()
        case_front_tab(site, other)
        case_link(site)
        case_ui()
    finally:
        site.shutdown()
        other.shutdown()
        for d in os.listdir(OUT):
            if d.startswith(("ep_", "local_", "cfg_")):
                shutil.rmtree(os.path.join(OUT, d), ignore_errors=True)
    print("\nALL PASS" if all(RESULTS) else "\nSOME FAILED (%d of %d)" % (RESULTS.count(False), len(RESULTS)))
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
