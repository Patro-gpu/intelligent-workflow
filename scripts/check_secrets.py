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

PLACEHOLDER = re.compile(r"your_[a-z]+_api_key_here|sk-mock-dummy-key-1234567890")

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

def _is_placeholder(value: str) -> bool:
    return PLACEHOLDER.fullmatch(value) is not None


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
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return proc.stdout


def _scan_blob(data: bytes, label: str) -> list[str]:
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
    return _scan_text(data.decode(encoding, errors="replace"), label)


def scan_repository() -> list[str]:
    findings: list[str] = []

    # Read the index, even if the staged content differs from the worktree.
    staged = _git_command(["diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z"])
    for rel in filter(None, staged.split("\0")):
        if Path(rel).suffix.lower() not in IGNORED_EXTENSIONS:
            data = subprocess.check_output(["git", "show", f":{rel}"], cwd=REPO_ROOT)
            findings.extend(_scan_blob(data, f"staged:{rel}"))

    paths = _git_command(["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    for rel in filter(None, paths.split("\0")):
        file_path = REPO_ROOT / rel
        if (file_path.name.startswith(".env") and file_path.name != ".env.example") or (
            rel.startswith("workspace/inputs/") and file_path.name != ".gitkeep"
        ):
            findings.append(f"{rel}: private runtime input must not be committed")
        if file_path.is_file() and not file_path.is_symlink() and file_path.suffix.lower() not in IGNORED_EXTENSIONS:
            findings.extend(_scan_blob(file_path.read_bytes(), rel))

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
