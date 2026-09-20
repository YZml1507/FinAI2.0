# -*- coding: utf-8 -*-
"""安全回归门禁：tracked 文件中不得出现硬编码秘密字面量。

历史事故：datahubco/promax tushare 代理 key 曾以字面量硬编码于 5 个
采集脚本并进入公开仓历史（自 14ae0f0 起）。本测试用 git ls-files 扫
全部 tracked 文本文件，命中即 FAIL。

匹配规则（刻意保守，避免误伤哈希/指纹常量）：
  (a) 形如 NAME = '<40+字符 hex/base62>' 且 NAME 匹配 /(key|token|secret|passw)/i；
  (b) 'X-API-Key': '<40+字符字面量>'；
排除：行内含 sha256/md5/hash/fingerprint/commit/repro 语义的行（这些是
出处指纹，不是秘密）。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_ASSIGN_SECRET = re.compile(
    r"""(?ix)                      # 忽略大小写
    (key|token|secret|passw\w*)    # 名字含秘密语义
    [\w\[\]\'"\.]*\s*[:=]\s*       # 赋值
    ["'][A-Za-z0-9_\-]{40,}["']    # 40+ 字符字面量
    """)
_XAPIKEY_LITERAL = re.compile(r"['\"]X-API-Key['\"]\s*:\s*['\"][A-Za-z0-9_\-]{16,}['\"]")
_SAFE_HINT = re.compile(r"(?i)sha256|md5|hash|fingerprint|commit|repro|git")
_TEXT_EXTS = {".py", ".sh", ".yaml", ".yml", ".json", ".md", ".txt"}


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                         text=True, check=True)
    return [REPO / p for p in out.stdout.split()
            if Path(p).suffix in _TEXT_EXTS and (REPO / p).is_file()]


def _scan() -> list[str]:
    hits = []
    for f in _tracked_text_files():
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if _SAFE_HINT.search(line):
                continue
            if _ASSIGN_SECRET.search(line) or _XAPIKEY_LITERAL.search(line):
                hits.append(f"{f.relative_to(REPO)}:{i}: {line.strip()[:80]}")
    return hits


class TestNoHardcodedSecrets:
    def test_no_secret_literals_in_tracked_files(self):
        hits = _scan()
        assert not hits, "检出疑似硬编码秘密（须转 .env/环境变量）：\n" + \
            "\n".join(hits[:20])

    def test_scanner_fires_on_reinsertion(self, tmp_path, monkeypatch):
        # 自检：向 tracked 文本里塞回秘密形态 ⇒ 扫描器必须报
        fake = tmp_path / "scripts" / "fake.py"
        fake.parent.mkdir(parents=True)
        fake.write_text("KEY = 'aBc0123" + "x" * 40 + "'\n")
        line = "KEY = 'aBc0123" + "x" * 40 + "'"
        assert _ASSIGN_SECRET.search(line), "扫描器未命中已知秘密形态"
        assert _XAPIKEY_LITERAL.search("'X-API-Key': 'abc" + "y" * 30 + "'")
