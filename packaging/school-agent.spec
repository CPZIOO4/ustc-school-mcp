# Build only allowlisted source assets; never include .local or an existing user's profile.
from pathlib import Path
import hashlib
import json
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, copy_metadata

root = Path(SPECPATH).parent
source = root / 'src/school_mcp'
build_manifest = root / 'build/diagnostic-build.json'
build_manifest.parent.mkdir(exist_ok=True)
inventory = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in source.rglob('*') if p.is_file() and (p.suffix in {'.py','.js','.css','.html'} or p.relative_to(source).as_posix() == 'agent/catalog_data.json') and '__pycache__' not in p.parts}
build_manifest.write_text(json.dumps({'files':[p for p in inventory if p.endswith('.py')],
    'fingerprint':hashlib.sha256(json.dumps(inventory,sort_keys=True).encode()).hexdigest()}),encoding='utf-8')
datas = [(str(root / 'src/school_mcp/desktop/web'), 'school_mcp/desktop/web'),
         (str(build_manifest), 'school_mcp'),
         (str(root / 'src/school_mcp/agent/catalog_data.json'), 'school_mcp/agent'),
         (str(root / 'src/school_mcp/vlab/runner.py'), 'school_mcp/vlab'),
         (str(root / '.agents/skills/school-agent'), 'school-skills/school-agent'),
         (str(root / '.agents/skills/school-services'), 'school-skills/school-services')]
for dependency in ('mcp', 'pydantic', 'pydantic_core', 'httpx', 'beautifulsoup4', 'playwright'):
    datas += copy_metadata(dependency)
datas += collect_data_files('playwright')
datas += collect_data_files('rapidocr_onnxruntime')
a = Analysis([str(root / 'src/school_mcp/desktop/entry.py')], pathex=[str(root / 'src')],
             binaries=[], datas=datas, hiddenimports=collect_submodules('school_mcp'),
             hookspath=[], runtime_hooks=[], excludes=['tkinter', 'pytest', 'unittest', 'pandas', 'IPython'], noarchive=False)
pyz = PYZ(a.pure)
native_engine = os.environ.get('SCHOOL_BUILD_NATIVE_ENGINE') == '1'
name = 'USTC-School-Engine' if native_engine else 'USTC-School-Agent'
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=name,
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=native_engine)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=name)
