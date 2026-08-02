# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH)
common_hidden = [
    "win32timezone", "servicemanager", "win32service", "win32serviceutil", "win32event",
    "paramiko", "croniter", "tzdata",
]

gui = Analysis([str(root / "main.py")], pathex=[str(root)], binaries=[], datas=[(str(root / "icon.ico"), ".")], hiddenimports=common_hidden)
gui_pyz = PYZ(gui.pure)
gui_exe = EXE(gui_pyz, gui.scripts, [], exclude_binaries=True, name="ServiceManagerGUI", console=False, icon=str(root / "icon.ico"), uac_admin=True)

engine = Analysis([str(root / "windows_service.py")], pathex=[str(root)], binaries=[], datas=[], hiddenimports=common_hidden)
engine_pyz = PYZ(engine.pure)
engine_exe = EXE(engine_pyz, engine.scripts, [], exclude_binaries=True, name="ServiceManagerEngine", console=False, icon=str(root / "icon.ico"))

cli = Analysis([str(root / "service_manager_cli.py")], pathex=[str(root)], binaries=[], datas=[], hiddenimports=common_hidden)
cli_pyz = PYZ(cli.pure)
cli_exe = EXE(cli_pyz, cli.scripts, [], exclude_binaries=True, name="servicemgr", console=True, icon=str(root / "icon.ico"))

COLLECT(
    gui_exe, gui.binaries, gui.datas,
    engine_exe, engine.binaries, engine.datas,
    cli_exe, cli.binaries, cli.datas,
    strip=False, upx=True, name="ServiceManager",
)
