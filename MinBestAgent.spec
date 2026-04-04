# -*- mode: python ; coding: utf-8 -*-
import certifi, os

_browsers_src = os.environ.get('PLAYWRIGHT_BROWSERS_PATH', './playwright-browsers')

a = Analysis(
    ['local_agent.py'],
    pathex=[],
    binaries=[],
    datas=[
        (certifi.where(), 'certifi'),
        (_browsers_src, 'playwright-browsers'),
    ],
    hiddenimports=[
        'scraper',
        'login',
        'playwright',
        'playwright.sync_api',
        'openpyxl',
        'engineio.async_drivers.threading',
        'socketio',
        'dotenv',
        'websocket',
        'websocket._http',
        'websocket._socket',
        'websocket._ssl_compat',
        'websocket._utils',
        'websocket._logging',
        'certifi',
        'ssl',
        '_ssl',
        'requests',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MinBestAgent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
