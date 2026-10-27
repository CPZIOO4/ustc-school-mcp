"""Read-only native ZIP audit against the locally built, allowlisted artifacts."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile


def main():
    root=Path(__file__).resolve().parents[1]
    package=root/'dist/USTC-School-Agent-Native-Windows.zip'
    sha=hashlib.sha256(package.read_bytes()).hexdigest()
    assert package.with_suffix('.zip.sha256').read_text().strip()==sha
    with zipfile.ZipFile(package) as archive:
        assert archive.testzip() is None
        names=archive.namelist()
        for name in names:
            path=PurePosixPath(name.replace('\\','/'))
            assert not path.is_absolute() and '..' not in path.parts
            assert path.parts[0] in {'engine','USTC-School-Agent.exe','school-core.exe','LICENSE','native-desktop.md','rust-migration.md','safety-logs.md','release-manifest.json'}
            assert not set(path.parts)&{'.local','.git','.venv','node_modules','profile','diagnostics','native-toolchain'}
            assert not path.name.endswith(('.dpapi','.sqlite3','.log','.pid','.har'))
        release=json.loads(archive.read('release-manifest.json').decode('utf-8-sig'))
        assert release['profile']=='release'
        for name, digest in release['sha256'].items():
            assert hashlib.sha256(archive.read(name)).hexdigest()==digest
        assert archive.read('USTC-School-Agent.exe')==(root/'native/target/release/ustc-school-desktop.exe').read_bytes()
        assert archive.read('school-core.exe')==(root/'native/core/target/release/school-core.exe').read_bytes()
        assert archive.read('engine/USTC-School-Engine.exe')==(root/'dist/USTC-School-Engine/USTC-School-Engine.exe').read_bytes()
        assert archive.read('engine/_internal/school_mcp/agent/catalog_data.json')==(root/'src/school_mcp/agent/catalog_data.json').read_bytes()
        assert archive.read('engine/_internal/school_mcp/desktop/chat_resources.json')==(root/'src/school_mcp/desktop/chat_resources.json').read_bytes()
        for name in ('index.html','app.js','transport.js','style.css'):
            assert archive.read('engine/_internal/school_mcp/desktop/web/'+name)==(root/'src/school_mcp/desktop/web'/name).read_bytes()
        source=root/'src/school_mcp'
        inventory={p.relative_to(source).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in source.rglob('*') if p.is_file() and (p.suffix in {'.py','.js','.css','.html'} or p.relative_to(source).as_posix() in {'agent/catalog_data.json', 'desktop/chat_resources.json'}) and '__pycache__' not in p.parts}
        manifest=json.loads(archive.read('engine/_internal/school_mcp/diagnostic-build.json'))
        assert manifest['fingerprint']==hashlib.sha256(json.dumps(inventory,sort_keys=True).encode()).hexdigest()
    print(json.dumps({'passed':True,'entries':len(names),'zip_sha256':sha,'size_bytes':package.stat().st_size,
        'native_exe_matches':True,'engine_exe_matches':True,'frontend_matches':True,'engine_manifest_matches':True,
        'private_paths_found':False,'source_private_data_read':False}))


if __name__=='__main__':main()
