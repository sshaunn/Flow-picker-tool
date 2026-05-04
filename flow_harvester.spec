# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the bundled customer install.

Build with::

    pyinstaller --noconfirm flow_harvester.spec

Output: ``dist/FlowHarvester/FlowHarvester.exe`` plus its DLL / data
folder. Zip the ``dist/FlowHarvester/`` directory and ship to customers
— they unzip and double-click the .exe, no Python needed.

Bundle behavior: cmd window stays open (``console=True``) showing live
logs; the entry point also opens the dashboard URL in the customer's
default browser.
"""

from PyInstaller.utils.hooks import collect_all


# ----- Bundle every package data file these libraries ship -----
hiddenimports = []
datas = []
binaries = []

for pkg in (
    "fastapi", "starlette", "uvicorn", "anyio", "h11",
    "websockets", "wsproto", "multipart", "jinja2", "patchright",
    "pydantic", "pydantic_core",
    # psutil — cross-platform process enumeration. Used to kill orphan
    # Chrome procs holding a profile's SingletonLock before relaunch.
    "psutil",
):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# Project's own template + default config — paths stay flat under the
# bundle root so ``app/web/server.py`` finds them via
# ``Path(__file__).parent / 'templates'`` after the chdir in __main__.
datas += [
    ("app/web/templates", "app/web/templates"),
    ("config/settings.yaml", "config"),
    ("config/flow-selectors.yaml", "config"),
]


a = Analysis(
    ["app/__main__.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + [
        "app.web.routes.pages",
        "app.web.routes.tasks",
        "app.web.routes.workstations",
        "app.web.routes.scheduler",
        "app.web.routes.ws",
        "app.web.routes.login",
        "app.web.routes.files",
        "app.web.routes.mode",
        "app.web.bootstrap",
        "app.scheduler.daemon",
        "app.workstations.repository",
        "app.tasks.repository",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest", "pytest_cov", "_pytest",
        "tkinter",  # Not used; trims ~10MB.
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FlowHarvester",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # cmd window stays open with live logs
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="FlowHarvester",
)
