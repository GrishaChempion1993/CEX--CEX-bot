"""Check Git publication candidates without printing secrets; optionally export a ZIP.

Standard library only. Checks tracked files even when ignored, plus untracked
non-ignored files. Reads the local .env for exact-match detection, never exports it.
This is a local release guard, not a comprehensive secret scanner or history audit.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PARTS = {
    ".git", ".ssh", ".cloudflared", ".cache", ".local", ".private",
    ".playwright-cli", ".playwright", ".cursor", ".claude", ".codex",
    ".vscode", ".idea", ".venv", "venv", "node_modules", "__pycache__",
    "_tmp", "mexc-futures-sdk-main", "dist", "build", "logs", ".pytest_cache",
}
PRIVATE_NAMES = {".bash_history", ".zsh_history", "install.cmd", "result.json", "`", "pairs.txt"}
PRIVATE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".zip", ".7z", ".pem", ".key", ".p12", ".pfx", ".deb", ".log", ".pyc")
PATTERNS = {
    "telegram-bot-token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "private-chat-id": re.compile(r"(?<!\w)-100\d{8,}\b"),
    "private-key-block": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    "credential-in-url": re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@\"']+:[^\s/@\"']+@", re.I),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "private-key-hex": re.compile(r"\b(?:0x)?[a-fA-F0-9]{64}\b"),
    "personal-email": re.compile(r"\b[\w.+-]+@(?!example\.(?:com|org|net)\b)[\w.-]+\.[a-zA-Z]{2,}\b"),
}
SENSITIVE_NAME = re.compile(
    r"(?:BOT_TOKEN|CHAT_ID|API_KEY|API_SECRET|PRIVATE_KEY|PASSWORD|TOTP_SECRET|"
    r"LOGIN_EMAIL|WEB_TOKEN|WEB_MTOKEN|WEB_P0|WEB_K0|WEB_CHASH|WEB_MHASH|"
    r"PROXY_POOL|PROXY_URL|WEB_PROXY|RPC_URL|WALLET_ADDRESS|PASSPHRASE|MEMO)$"
)


def forbidden_path(name: str) -> bool:
    path = PurePosixPath(name)
    parts = tuple(part.lower() for part in path.parts)
    base = path.name.lower()
    return (
        path.is_absolute() or ".." in parts
        or any(p in PRIVATE_PARTS or p.startswith("parser_server_") for p in parts)
        or base in PRIVATE_NAMES
        or (base.startswith(".env") and name != ".env.example")
        or base.endswith(PRIVATE_SUFFIXES)
        or bool(re.search(r"\.(?:bak|save)(?:\.|$)", base))
        or ("app/data" in path.as_posix() and name != "app/data/.gitkeep")
        or bool(re.search(r"(?:storage[_-]state|cookies|session).*\.json$", base))
    )


def git_candidates(root: Path) -> list[str]:
    if not (root / ".git").exists():
        # An extracted archive must not accidentally use its parent's Git repo.
        names = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            parent = Path(directory).relative_to(root)
            dirs[:] = [d for d in dirs if (parent / d).as_posix() == "app/data" or not forbidden_path((parent / d).as_posix())]
            for name in files:
                relative = (parent / name).as_posix()
                if not forbidden_path(relative):
                    names.append(relative)
        return sorted(names)
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return sorted(set(result.stdout.decode("utf-8").strip("\0").split("\0")) - {""})


def local_secrets(root: Path) -> set[str]:
    """Read values in memory only. Do not log names or values from private files."""
    env_file = root / ".env"
    if not env_file.exists():
        return set()
    result = set()
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if not match or not SENSITIVE_NAME.search(match[1]):
            continue
        value = match[2].split(" #", 1)[0].strip().strip("\"'")
        if len(value) >= 8 and not value.startswith(("app/", "/")):
            # Public RPC endpoints are not credentials. URL passwords are also
            # covered by PATTERNS; skip non-authenticated RPC defaults here.
            if match[1].endswith("RPC_URL"):
                continue
            result.add(value)
            if "PROXY" in match[1]:
                result.update(p for p in re.split(r"[;,\s]+", value) if len(p) >= 8)
    return result


def scan_text(name: str, text: str, secrets: set[str]) -> list[str]:
    findings = set()
    for label, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            findings.add(f"{name}:{line}: {label}")
    for value in secrets:
        start = text.find(value)
        if start >= 0:
            line = text.count("\n", 0, start) + 1
            findings.add(f"{name}:{line}: local-env-value")
    return sorted(findings)


def collect(root: Path, names: list[str], secrets: set[str]) -> tuple[dict[str, bytes], list[str]]:
    files = {}
    findings = []
    for name in names:
        path = root / name
        if forbidden_path(name):
            findings.append(f"{name}: private-path")
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            findings.append(f"{name}: symlink-or-outside-root")
            continue
        if not path.is_file():
            findings.append(f"{name}: missing-file-or-nested-repository")
            continue
        data = path.read_bytes()
        try:
            text = data.decode("utf-8-sig")
            if "\0" in text:
                raise UnicodeError()
        except UnicodeError:
            findings.append(f"{name}: binary-or-non-utf8-needs-review")
            continue
        findings.extend(scan_text(name, text, secrets))
        files[name] = data
    return files, findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", action="store_true", help="Write checked files to dist/parser-open-source.zip")
    args = parser.parse_args()
    try:
        files, findings = collect(ROOT, git_candidates(ROOT), local_secrets(ROOT))
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        print("Unable to inspect release files. Check local file access and Git.")
        return 2
    if findings:
        print("BLOCKED: review the following locations (values are never printed):")
        print("\n".join(findings))
        return 1
    print(f"PASS: {len(files)} publication candidates; no findings from configured checks.")
    print("Scope: current files only; excludes ignored private files, archives and Git history.")
    if args.export:
        output = ROOT / "dist" / "cex-cex-bot-open-source.zip"
        output.parent.mkdir(parents=True, exist_ok=True)
        # Export the exact bytes checked above, never a recursive directory copy.
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(f"cex-cex-bot/{name}", data)
        print("Created dist/cex-cex-bot-open-source.zip (no .git or local credentials).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
