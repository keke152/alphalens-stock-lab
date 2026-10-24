# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH)
datas = [(str(root / folder), folder) for folder in ('static', 'data', 'docs', 'examples')]
datas += [(str(root / 'README.md'), '.'), (str(root / 'LICENSE'), '.'),
          (str(root / 'assets' / 'app.ico'), 'assets')]
a = Analysis(
    [str(root / 'desktop_launcher.py')], pathex=[str(root)], binaries=[], datas=datas,
    hiddenimports=['uvicorn.loops.asyncio', 'uvicorn.protocols.http.h11_impl', 'uvicorn.lifespan.on',
                   'sklearn.ensemble._forest', 'sklearn.neighbors._partition_nodes',
                   'sklearn.utils._typedefs', 'sklearn.utils._heap', 'sklearn.utils._sorting',
                   'sklearn.utils._vector_sentinel'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'matplotlib', 'IPython', 'pytest', 'tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='AlphaLens',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False,
          icon=str(root / 'assets' / 'app.ico'), version=str(root / 'assets' / 'version_info.txt'))
