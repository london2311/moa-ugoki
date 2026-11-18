"""共通の設定とユーティリティ（標準ライブラリのみ）。"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = ROOT / "knowledge"  # 公開リポジトリには置かない（AI 読み解きを使うときだけ）
NEWS_DIR = ROOT / "news"


def norm(s: str) -> str:
    """検索用の正規化：全角英数→半角、大文字→小文字、空白の除去。"""
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", "", s).lower()
