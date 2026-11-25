#!/usr/bin/env python3
"""提交前密钥防泄露扫描脚本。

扫描 `git diff --cached` 及仓库已跟踪/待提交文本文件，检测：
- OpenAI / DeepSeek 等 `sk-` 密钥
- GitHub `ghp_` / `github_pat_` 令牌
- AWS `AKIA` 密钥
- 私钥头 `BEGIN PRIVATE KEY` / `BEGIN RSA PRIVATE KEY`
- 环境变量赋值中疑似非占位符的真实 API Key / Token

发现任何疑似泄露即返回非零退出码（1）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI/DeepSeek-style key (sk-...)", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("GitHub personal access token (ghp_...)", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("GitHub fine-grained token (github_pat_...)", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("AWS Access Key ID (AKIA...)", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Private Key Header", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    (
        "Suspicious hardcoded API_KEY/TOKEN assignment",
        re.compile(
            r"(?i)(?:DEEPSEEK|DOUBAO|QWEN|PREMIUM|OPENAI|ARK|DASHSCOPE)_(?:API_KEY|TOKEN)\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{16,})['\"]?"
        ),
    ),
]

PLACEHOLDER_MARKERS = (
    "your_",
    "replace_me",
    "placeholder",
    "example",
    "dummy",
    "mock",
    "fake",
    "test_key",
    "xxxx",
    "changeme",
)

IGNORED_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".whl",
    ".pyc",
    ".so",
    ".dylib",
}

IGNORED_PATHS = {
    "scripts/check_secrets.py",
}


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def _scan_text(text: str, source_label: str) -> list[str]:
    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(line):
                matched = match.group(1) if match.lastindex else match.group(0)
                if _is_placeholder(matched):
                    continue
                findings.append(f"{source_label}:{lineno}: [{label}] detected suspicious token")
    return findings


def _git_command(args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _collect_files() -> list[Path]:
    tracked = _git_command(["ls-files"]).splitlines()
    untracked = _git_command(["ls-files", "--others", "--exclude-standard"]).splitlines()
    rel_paths = sorted(set(p for p in (*tracked, *untracked) if p))
    files: list[Path] = []
    for rel in rel_paths:
        if rel in IGNORED_PATHS:
            continue
        p = REPO_ROOT / rel
        if not p.is_file():
            continue
        if p.suffix.lower() in IGNORED_EXTENSIONS:
            continue
        files.append(p)
    return files


def scan_repository() -> list[str]:
    findings: list[str] = []

    # 1. Scan staged diff
    staged_diff = _git_command(["diff", "--cached", "--unified=0"])
    if staged_diff:
        added_lines: list[str] = []
        current_file = "<staged>"
        for raw_line in staged_diff.splitlines():
            if raw_line.startswith("+++ b/"):
                current_file = raw_line[6:]
                continue
            if current_file in IGNORED_PATHS:
                continue
            if raw_line.startswith("+") and not raw_line.startswith("+++"):
                added_lines.append((current_file, raw_line[1:]))
        for fname, content in added_lines:
            findings.extend(_scan_text(content, f"staged:{fname}"))

    # 2. Scan tracked & non-ignored working tree files
    for file_path in _collect_files():
        rel = file_path.relative_to(REPO_ROOT).as_posix()
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(_scan_text(text, rel))

    # Deduplicate preserving order
    return list(dict.fromkeys(findings))


def main() -> int:
    findings = scan_repository()
    if findings:
        print("SECRET SCAN FAILED! Potential secrets detected:", file=sys.stderr)
        for item in findings:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print("Secret scan passed: 0 potential secrets detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
