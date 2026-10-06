"""Drive the real Tk window against the local test site (Chrome headless).

env: PCM_TEST_PORT (site port, see test_session.py), PCM_TEST_URL, PCM_TEST_ENGINE (default google).
Prints the log panel, then what this run wrote to log.txt (per-bubble details the panel leaves out).
"""
import logging
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PCM_HEADLESS"] = "1"

import ttkbootstrap as ttk

from pcm.config import ENGINES, Settings, app_data_dir
from test_session import OUT, serve, site_url
import poomcatomanga

SETTINGS = os.path.join(app_data_dir(), "settings.json")


def main():
    backup = SETTINGS + ".bak_test"
    if os.path.exists(SETTINGS):
        shutil.copy2(SETTINGS, backup)
    httpd = serve()
    root = ttk.Window()
    app = poomcatomanga.App(root)
    app.url.set(os.environ.get("PCM_TEST_URL") or site_url(httpd))
    app.engine.set(poomcatomanga._label(ENGINES, os.environ.get("PCM_TEST_ENGINE", "google")))
    app.save_enabled.set(True)      # saving is hidden/off by default; the test turns it on
    app.save_dir.set(OUT)
    app.auto_save.set(True)
    app.settings.keep_browser_profile = False
    t0 = time.time()
    steps = {"started": False, "toggled": False, "saved": False, "stopped": False}

    def tick():
        el = time.time() - t0
        if not steps["started"]:
            app.start()
            steps["started"] = True
        elif el > 25 and not steps["toggled"]:
            app.toggle(); steps["toggled"] = True
        elif el > 28 and not steps["saved"]:
            app.save_now(); steps["saved"] = True
        elif el > 32 and not steps["stopped"]:
            app.stop(); steps["stopped"] = True
        elif steps["stopped"] and (not app.session.running or el > 60):
            print("progress label:", app.progress.get())
            print("start button state:", str(app.btn_start.cget("state")))
            print("--- log ---")
            print(app.log.get("1.0", "end").strip())
            print_log_file()
            root.destroy()
            return
        root.after(250, tick)

    root.after(300, tick)
    root.mainloop()
    httpd.shutdown()
    if os.path.exists(backup):
        shutil.move(backup, SETTINGS)
    else:
        try:
            os.remove(SETTINGS)
        except OSError:
            pass
    for r, _d, files in os.walk(OUT):
        for f in files:
            print("saved:", os.path.relpath(os.path.join(r, f), OUT))


def print_log_file():
    """This process's part of log.txt (from its launch marker on)."""
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.FileHandler):
            h.flush()
            with open(h.baseFilename, encoding="utf-8", errors="replace") as f:
                text = f.read()
            i = text.rfind(f"pid {os.getpid()},")
            print(f"--- {os.path.basename(h.baseFilename)} ---")
            print(text[text.rfind("\n", 0, i) + 1:].strip() if i >= 0 else "(launch marker not found)")


if __name__ == "__main__":
    shutil.rmtree(OUT, ignore_errors=True)
    main()
