"""Screenshot the main window and the settings window (both themes) into tests/out/ui_*.png.

usage: python tests/snap_ui.py [output folder]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ttkbootstrap as ttk
from PIL import ImageGrab

import poomcatomanga as app_mod

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT, exist_ok=True)


def grab(win, name):
    win.update_idletasks()
    win.update()
    x, y = win.winfo_rootx(), win.winfo_rooty()
    w, h = win.winfo_width(), win.winfo_height()
    ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(os.path.join(OUT, name))


def main():
    root = ttk.Window()
    app = app_mod.App(root)
    app.settings.save = lambda: None            # don't touch the user's settings file
    app.use_open_chrome.set(False)
    app._sync_mode()
    root.geometry("+40+40")
    root.attributes("-topmost", True)
    shots = []

    def step(i=0):
        if i == 0:
            grab(root, "ui_main_dark.png")
            app._set_state("running")
            app.progress.set("12 / 30 หน้า")
            app.pb.configure(maximum=30, value=12)
            app.failed_lbl.configure(text="กำลังแปล 3   พลาด 1")
            app.toggle_details()
            app.log.insert("end", "00:00:01  หน้าใหม่: ตัวอย่าง\n")
        elif i == 1:
            grab(root, "ui_running_dark.png")
            app.settings.theme = "light"
            app.apply_settings()
        elif i == 2:
            grab(root, "ui_running_light.png")
            app.settings.theme = "dark"
            app.apply_settings()
            app._set_state("idle")
            app.toggle_details()
            shots.append(app_mod.SettingsWindow(app, "save"))
            shots[-1].geometry("+60+60")
        elif i == 3:
            grab(shots[-1], "ui_settings_save.png")
            shots[-1].page_var.set("about")
            shots[-1]._show()
        elif i == 4:
            grab(shots[-1], "ui_settings_about.png")
            shots[-1].destroy()
            app.use_open_chrome.set(True)           # attach mode: link optional, hint + guide link
            app._sync_mode()
            app.url.set("")
        elif i == 5:
            grab(root, "ui_main_attach_dark.png")
            app._set_state("starting")
            app.events.put(("status", "กด Allow ในหน้าต่าง Chrome เพื่ออนุญาต"))
        elif i == 6:
            grab(root, "ui_attach_waiting_dark.png")
            app._set_state("idle")
            app.settings.theme = "light"
            app.apply_settings()
        elif i == 7:
            grab(root, "ui_main_attach_light.png")
            app.settings.theme = "dark"
            app.apply_settings()
            app.show_attach_guide("missing")
            app._guide.attributes("-topmost", True)
        elif i == 8:
            grab(app._guide, "ui_attach_guide.png")
            app._guide.destroy()
            shots.append(app_mod.SettingsWindow(app, "advanced"))
            shots[-1].geometry("+60+60")
        elif i == 9:
            grab(shots[-1], "ui_settings_advanced_attach.png")
            root.destroy()
            return
        root.after(700, step, i + 1)

    root.after(900, step)
    root.mainloop()
    print("saved screenshots to", OUT)


if __name__ == "__main__":
    main()
