# PyInstaller build for the portable desktop app. Build with:  pyinstaller cv-tailor.spec
# Output: dist/CV-Tailor/ (a folder you can zip, copy or put on a USB stick).
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [("app/static", "app/static"), ("app/fonts", "app/fonts")]
datas += collect_data_files("docx") + collect_data_files("reportlab")
hidden = collect_submodules("uvicorn") + ["app.main"]

a = Analysis(
    ["run_desktop.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "IPython", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="CV-Tailor", console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="CV-Tailor", upx=False)
