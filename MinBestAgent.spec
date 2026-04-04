# -*- mode: python ; coding: utf-8 -*-
import certifi, os

# templates 폴더 포함
template_dir = os.path.join(os.path.dirname(os.path.abspath(SPEC)), 'templates')

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        (certifi.where(), 'certifi'),
        (template_dir, 'templates'),
    ],
    hiddenimports=[
        'scraper',
        'login',
        'classifier',
        'playwright',
        'playwright.sync_api',
        'playwright._impl._driver',
        'openpyxl',
        'flask',
        'flask_socketio',
        'engineio',
        'engineio.async_drivers.threading',
        'socketio',
        'dotenv',
        'certifi',
        'ssl',
        '_ssl',
        'requests',
        'anthropic',
        'jinja2',
        'werkzeug',
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
