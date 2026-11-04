# PyInstaller onedir spec. Run from the project root:
#   python -m PyInstaller --noconfirm --clean packaging/token-burn.spec
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

a = Analysis(
    [os.path.join(ROOT, "token_burn", "__main__.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "web"), "web")],
    hiddenimports=[
        "collector", "collector.collect", "collector.util", "collector.adapters",
        "collector.adapters.claude", "collector.adapters.cursor", "collector.adapters.opencode",
        "collector.plan", "collector.plan.opencode_plan",
        "server", "token_burn.cli", "token_burn.scheduler",
        "token_burn.scheduler.windows", "token_burn.scheduler.macos", "token_burn.scheduler.linux",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="token-burn", console=True)
# Windowless twin for scheduled tasks (GUI subsystem: no console window, no focus stealing).
exe_w = EXE(pyz, a.scripts, [], exclude_binaries=True, name="token-burnw", console=False)
coll = COLLECT(exe, exe_w, a.binaries, a.datas, name="token-burn")
