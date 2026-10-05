"""Drive the real Tk window against the local test site (Chrome headless)."""
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["GM5_HEADLESS"] = "1"

import ttkbootstrap as ttk

from gm.config import Settings, app_data_dir
from test_session import OUT, serve
import ghostmanga5

SETTINGS = os.path.join(app_data_dir(), "settings.json")


def main():
    backup = SETTINGS + ".bak_test"
    if os.path.exists(SETTINGS):
        shutil.copy2(SETTINGS, backup)
    httpd = serve()
    root = ttk.Window(themename="superhero")
    app = ghostmanga5.App(root)
    app.url.set(os.environ.get("GM5_TEST_URL", "http://127.0.0.1:8765/index.html"))
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


if __name__ == "__main__":
    shutil.rmtree(OUT, ignore_errors=True)
    main()
