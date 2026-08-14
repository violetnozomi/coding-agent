# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-directory definition for the Windows NZ-Coder product."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata


spec_root = Path(SPECPATH)
project_root = spec_root.parents[1]
datas, binaries, hiddenimports = collect_all("nz_coder")
datas += copy_metadata("nz-coder")

analysis = Analysis(
    [str(spec_root / "nz_coder_entry.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "ruff", "sentence_transformers"],
    noarchive=False,
)
python_archive = PYZ(analysis.pure)
executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="nz-coder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    target_arch="x86_64",
)
distribution = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="NZ-Coder",
)
