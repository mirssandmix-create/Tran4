"""Full session against the local test site (headless Chrome).

usage: python tests/test_session.py [seconds] [engine]
Serves tests/site on http://127.0.0.1:8765, runs a Session, saves the results to
tests/out/session/, and prints the log.
"""
import functools
import http.server
import logging
import os
import queue
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PCM_HEADLESS"] = os.environ.get("PCM_HEADLESS", "1")

from pcm.config import Settings
from pcm.session import Session

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, "site")
OUT = os.path.join(HERE, "out", "session")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve():
    handler = functools.partial(Quiet, directory=SITE)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main(seconds, engine, page="index.html", settle=6):
    shutil.rmtree(OUT, ignore_errors=True)
    httpd = serve()
    events = queue.Queue()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "seleniumbase", "websockets", "asyncio", "urllib3", "chrome_lens_py"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for very_noisy in ("uc", "uc.connection", "uc.browser", "uc.tab"):
        logging.getLogger(very_noisy).setLevel(logging.CRITICAL)
    s = Settings()
    s.last_url = "http://127.0.0.1:8765/" + page
    s.engine = engine
    s.keep_browser_profile = False
    s.save_dir = OUT
    s.auto_save = False
    sess = Session(s, events)
    sess.start()
    t0 = time.time()
    last, changed = None, time.time()
    while time.time() - t0 < seconds and sess.running:
        try:
            ev = events.get(timeout=0.5)
        except queue.Empty:
            ev = None
        if ev and ev[0] == "progress" and ev[1:] != last:
            last, changed = ev[1:], time.time()
            print("progress done=%d total=%d failed=%d busy=%d" % ev[1:], flush=True)
        if last and last[1] and last[0] + last[2] >= last[1] and last[3] == 0 and time.time() - changed > settle:
            break
    sess.request_save(OUT)
    time.sleep(1)
    # what does each translated <img> show now vs. what it was translated from?
    import asyncio, json
    from pcm import cdp as safe

    async def probe():
        tab = sess._tab
        return await safe.evaluate(tab, """JSON.stringify([...document.images].map(i=>({
            cur: (i.dataset.gmState==='done' ? 'translated:'+i.dataset.gmOrig.split('/').pop() : 'orig:'+(i.currentSrc||i.src).split('/').pop())})))""", 10)
    try:
        fut = asyncio.run_coroutine_threadsafe(probe(), sess.loop)
        print("page state:", json.loads(fut.result(15)))
    except Exception as e:
        print("probe failed:", e)
    time.sleep(3)
    sess.stop()
    sess.join(20)
    httpd.shutdown()
    print("saved:", sorted(os.listdir(OUT)) if os.path.isdir(OUT) else "nothing")
    for root, _d, files in os.walk(OUT):
        for f in sorted(files):
            print("  ", os.path.relpath(os.path.join(root, f), OUT))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 120, sys.argv[2] if len(sys.argv) > 2 else "google",
         sys.argv[3] if len(sys.argv) > 3 else "index.html", int(sys.argv[4]) if len(sys.argv) > 4 else 6)
