# PyInstaller spec for Ghost Manga 5 (one-folder build: starts fast, nothing to unpack each launch)
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hidden = (collect_submodules("chrome_lens_py") + collect_submodules("mycdp")
          + collect_submodules("seleniumbase.undetected.cdp_driver"))
datas = [("gm/page.js", "gm"), ("gm/render.js", "gm"), ("icon.ico", "."), ("logo.png", ".")]
datas += collect_data_files("ttkbootstrap")

a = Analysis(
    ["ghostmanga5.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    excludes=["matplotlib", "IPython", "jedi", "parso", "PySide6", "PyQt5", "PyQt6", "notebook", "nbformat",
              "scipy", "pandas", "pytest", "Cython", "zmq", "tornado", "seleniumwire", "mitmproxy", "pydivert",
              "pdbp", "behave", "pynose", "sbvirtualdisplay", "pyotp", "pyautogui", "tabcompleter"],
    noarchive=False,
)
# We drive Chrome over CDP, so selenium's driver manager binaries (~21 MB, 4 platforms) are dead weight.
a.datas = [d for d in a.datas if "selenium-manager" not in d[0].replace("\\", "/")]
a.binaries = [b for b in a.binaries if "selenium-manager" not in b[0].replace("\\", "/")]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GhostManga5",
    icon="icon.ico",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="GhostManga5")
