"""Read-only publish-candidate checks; never read ignored runtime files or print matches."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = {".local", ".venv", "attachments", "downloads", "diagnostics", "traces", "mail-setup"}
PRIVATE_PATTERNS = ("*.dpapi*", "*.log", "*.pid", "*.har", "storage-state*.json", "storage_state*.json", "mail.json*", "mail.credentials.tmp", "*.session.tmp", "*-session.tmp", "trace.zip", "*.login-status.*", "*-login-status.*")
CANARIES = (".local/mail.json", ".local/ustc-identity.device.dpapi", "nested/mail.json", "nested/mail.credentials.tmp", "nested/test.session.dpapi.old", "nested/test.session.tmp", "nested/nan7-session.tmp", "nested/nan7-login-status.json", "nested/nan7-login-status.tmp", "nested/attachments/test.eml", "nested/storage-state-test.json", "nested/trace.zip", "nested/test.har", "nested/mail-setup-test/mail.json.new")
LITERAL = re.compile(r'''(?i)(?:password|token|secret|authorization)["']?\s*[:=]\s*["']([^"'\r\n]{8,})["']''')
EMAIL = re.compile(r"([A-Za-z0-9_.+-]+)@(?:mail\.)?ustc\.edu\.cn", re.I)
SYNTHETIC_NAMES = {"student", "id", "your-name", "other", "someone", "office"}
CANARIES += ('nested/mail-outbox.sqlite3', 'nested/mail-outbox.sqlite3-journal', 'nested/mail-exports/example.eml')
CANARIES += ('nested/bb-submissions.sqlite3', 'nested/bb-submissions.sqlite3-wal')


def git(*args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout


def private(path: str) -> bool:
    parts = PurePosixPath(path.lower()).parts
    if 'mail-exports' in parts:
        return True
    if parts[-1].startswith(('mail-outbox.sqlite3', 'bb-submissions.sqlite3')):
        return True
    return any(part in PRIVATE_DIRS or part.startswith("mail-setup-") for part in parts) or any(fnmatch.fnmatch(parts[-1], pattern) for pattern in PRIVATE_PATTERNS) or any(part.startswith(".env") and part != ".env.example" for part in parts)


def scan(content: str, name: str, version: str, findings: list) -> None:
    for number, line in enumerate(content.splitlines(), 1):
        literal = LITERAL.search(line)
        if literal and not any(marker in literal.group(1).lower() for marker in ("synthetic", "test-only", "fixture", "example", "not-a-real", "should-not-return")):
            findings.append({"path": name, "version": version, "line": number, "kind": "review_credential_literal"})
        if any(local.lower() not in SYNTHETIC_NAMES and not local.lower().startswith("synthetic") for local in EMAIL.findall(line)):
            findings.append({"path": name, "version": version, "line": number, "kind": "review_school_account_literal"})
        if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", line):
            findings.append({"path": name, "version": version, "line": number, "kind": "private_key_marker"})


def main() -> int:
    entries = git("ls-files", "--stage")
    staged = set(git("ls-files", "--cached", "-z").decode("utf-8").split("\0")) - {""}
    untracked = set(git("ls-files", "--others", "--exclude-standard", "-z").decode("utf-8").split("\0")) - {""}
    findings = []
    for name in sorted(staged | untracked):
        if private(name):
            findings.append({"path": name, "kind": "private_runtime_path"})
            continue  # Do not read it, even if accidentally staged.
        path = ROOT / name
        if path.suffix not in {".py", ".md", ".toml", ".ps1", ".yaml", ".yml", ".json"}:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            findings.append({"path": name, "kind": "external_or_symlink_candidate"})
            continue
        if name in staged:
            scan(git("show", ":" + name).decode("utf-8"), name, "index", findings)
        if path.exists():
            scan(path.read_text(encoding="utf-8"), name, "worktree", findings)
    ignored = set(git("check-ignore", "--no-index", "-z", "--stdin", data=("\0".join(CANARIES) + "\0").encode()).decode().split("\0"))
    missing = sorted(set(CANARIES) - ignored)
    result = {"staged_files": len(staged), "unstaged_new_files": len(untracked),
              "index_fingerprint": hashlib.sha256(entries).hexdigest(),
              "private_files_read": False, "ignore_canaries": len(CANARIES), "unignored_canaries": missing,
              "findings": findings, "heuristic_only": True,
              "license_present": (ROOT / "LICENSE").is_file(), "committed_or_pushed": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(findings or missing))


if __name__ == "__main__":
    raise SystemExit(main())
