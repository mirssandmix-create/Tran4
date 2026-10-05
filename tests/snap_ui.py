"""Screenshot the main window and the settings window (both themes) into tests/out/ui_*.png."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ttkbootstrap as ttk
from PIL import ImageGrab

import poomcatomanga as app_mod

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
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
    root.geometry("+40+40")
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
            root.destroy()
            return
        root.after(700, step, i + 1)

    root.after(900, step)
    root.mainloop()
    print("saved screenshots to", OUT)


if __name__ == "__main__":
    main()
