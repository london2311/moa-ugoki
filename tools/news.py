#!/usr/bin/env python3
"""行政の動き・社会の動きを RSS から集め、分類して日報にまとめる。

使い方:
    python3 tools/news.py                 # 収集して news/ を更新（GitHub Actions が毎朝実行）
    python3 tools/news.py --ai            # さらに「MOAへの示唆」を Claude で付ける（ANTHROPIC_API_KEY が必要）
    python3 tools/news.py --offline a.xml # 手元のRSSファイルで試す（ネットに出ない）
    python3 tools/news.py --show 14       # 直近14日分を分類別に表示（収集はしない）

設定は news/feeds.json。出力:
    news/db.jsonl                 全記事の台帳（重複除去済み・keep_days 日で古いものを削除）
    news/daily/YYYY/YYYY-MM-DD.md その日に新しく入った記事の日報
    news/latest.md                直近14日の分類別一覧（壁打ちのときに読む）

AI 読み解きに送るのは公開ニュースの見出しと knowledge/gyousei・knowledge/moa の要約だけで、
議事録やチーム内の情報は送らない。
"""
from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import KNOWLEDGE, NEWS_DIR, norm  # noqa: E402

JST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; news-digest/1.0)"
GNEWS = "https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"
DB = NEWS_DIR / "db.jsonl"


# ---------- 取得と解析 ----------

def fetch(url: str, timeout: int = 25, tries: int = 3) -> bytes:
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, */*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # ネットワーク・HTTPエラーはまとめて再試行
            last = e
            time.sleep(2 * (k + 1))
    raise RuntimeError(f"{type(last).__name__}: {last}")


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(el: ET.Element, *names: str) -> str:
    for c in el:
        if local(c.tag) in names:
            if local(c.tag) == "link" and c.get("href"):
                return c.get("href")
            return (c.text or "").strip()
    return ""


def parse_date(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        d = None
    if d is None:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    if d and d.tzinfo is None:
        d = d.replace(tzinfo=JST)
    return d.astimezone(JST) if d else None


def parse_feed(data: bytes) -> list[dict]:
    """RSS 2.0 / RSS 1.0(RDF) / Atom をまとめて扱う。"""
    m = re.match(rb'<\?xml[^>]*encoding=["\']([A-Za-z0-9_\-]+)["\']', data.lstrip())
    if m and m.group(1).lower() not in (b"utf-8", b"utf8"):  # Shift_JIS などは utf-8 に直してから読む
        enc = m.group(1).decode()
        text = data.decode(enc, errors="replace")
        text = re.sub(r'encoding=["\'][^"\']+["\']', 'encoding="utf-8"', text, count=1)
        data = text.encode("utf-8")
    root = ET.fromstring(data)
    out = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        title = re.sub(r"\s+", " ", child_text(el, "title"))
        link = child_text(el, "link") or child_text(el, "guid", "id")
        date = parse_date(child_text(el, "pubDate", "date", "updated", "published", "issued"))
        src = child_text(el, "source")
        desc = re.sub(r"<[^>]+>", " ", child_text(el, "description", "summary", "encoded"))
        if title:
            out.append({"title": title, "link": link, "date": date, "source": src,
                        "desc": re.sub(r"\s+", " ", desc).strip()[:300]})
    return out


# ---------- 分類と台帳 ----------

def key_of(title: str) -> str:
    t = re.sub(r"\s[-－|｜]\s[^-－|｜]{1,30}$", "", title)  # 「見出し - 媒体名」の媒体名を外す
    return hashlib.sha1(norm(t).encode()).hexdigest()[:16]


def classify(text: str, cats: dict[str, list[str]]) -> list[str]:
    n = norm(text)
    return [c for c, ws in cats.items() if any(norm(w) in n for w in ws)]


def load_db() -> list[dict]:
    if not DB.exists():
        return []
    rows = []
    for line in DB.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def save_db(rows: list[dict]) -> None:
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r.get("date") or "", r["id"]))
    DB.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def collect(cfg: dict, offline: list[str] | None, days: int) -> tuple[list[dict], list[str]]:
    now = datetime.now(JST)
    cats, excl = cfg["categories"], [norm(w) for w in cfg.get("exclude", [])]
    jobs = []
    if offline:
        jobs = [("手元ファイル", "offline", p, False) for p in offline]
    else:
        jobs += [(f["name"], f.get("level", ""), f["url"], False) for f in cfg.get("feeds", [])]
        jobs += [(f"検索「{s['q']}」", s.get("level", ""), GNEWS.format(q=urllib.parse.quote(s["q"])), True)
                 for s in cfg.get("searches", [])]
        jobs += [(f"議会「{q}」", "議会", GNEWS.format(q=urllib.parse.quote(q)), "議会・議員")
                 for q in cfg.get("assemblies", [])]
        jobs += [(f"議員「{g['name']}」", g.get("role", "議員"),
                  GNEWS.format(q=urllib.parse.quote(f'"{g["name"]}"')), "議会・議員")
                 for g in cfg.get("giin", []) if g.get("name")]
        jobs += [(f["name"], f.get("region", "世界"), f["url"], "世界情勢") for f in cfg.get("world", [])]
    found, errors = {}, []
    for name, level, url, is_search in jobs:
        try:
            data = Path(url).read_bytes() if offline else fetch(url)
            items = parse_feed(data)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        fixed = is_search if isinstance(is_search, str) else None  # 議会・議員／世界情勢
        if fixed == "世界情勢":
            items = items[: cfg.get("world_per_feed", 8)]  # 各紙のトップ記事だけ
        for it in items:
            if it["date"] and it["date"] < now - timedelta(days=days if fixed != "世界情勢" else 3):
                continue
            text = f"{it['title']} {it['desc']}"
            if any(w in norm(text) for w in excl):
                continue
            cs = classify(text, cats)
            if fixed:
                cs = [fixed] + [c for c in cs if fixed != "世界情勢"]
            if not cs and not is_search and not offline:
                continue  # 公式RSSは分類語に当たったものだけ
            k = key_of(it["title"])
            row = found.setdefault(k, {
                "id": k, "title": it["title"], "link": it["link"], "source": it["source"] or name,
                "date": (it["date"] or now).strftime("%Y-%m-%d"), "fetched": now.strftime("%Y-%m-%d"),
                "level": level, "cats": cs or ["その他"], "via": [], "desc": it["desc"][:200],
            })
            if name not in row["via"]:
                row["via"].append(name)
    return list(found.values()), errors


# ---------- 書き出し ----------

def group_md(rows: list[dict], per_cat: int = 30) -> list[str]:
    order = ["浄化療法・健康・医療", "食・自然農法", "美術文化・教育", "まちづくり・地域共生", "広島", "議会・議員",
             "MOA・明主様", "その他", "世界情勢"]
    out = []
    for c in order:
        rs = [r for r in rows if c in r["cats"]]
        if not rs:
            continue
        if c == "世界情勢":  # 地域ごとに、各紙のトップ記事
            out += [f"### 世界情勢（{len(rs)}件）", ""]
            for region in sorted({r.get("level", "") for r in rs}):
                out.append(f"**{region}**")
                for r in sorted([x for x in rs if x.get("level", "") == region], key=lambda r: r["date"], reverse=True)[:per_cat]:
                    out.append(f"- {r['date']} [{r['title']}]({r['link']})（{r['source']}）")
                out.append("")
            continue
        rs.sort(key=lambda r: r["date"], reverse=True)
        out += [f"### {c}（{len(rs)}件）", ""]
        for r in rs[:per_cat]:
            other = "・".join(x for x in r["cats"] if x != c)
            tag = f" 〔{r['level']}〕" if r.get("level") else ""
            out.append(f"- {r['date']}{tag} [{r['title']}]({r['link']})" + (f" ― {other}" if other else ""))
        if len(rs) > per_cat:
            out.append(f"- ……ほか {len(rs) - per_cat} 件（news/db.jsonl）")
        out.append("")
    return out


def write_daily(new: list[dict], errors: list[str], ai_text: str | None) -> Path | None:
    if not new and not ai_text:
        return None
    today = datetime.now(JST).strftime("%Y-%m-%d")
    p = NEWS_DIR / "daily" / today[:4] / f"{today}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    out = [f"# 行政・社会の動き　{today}", "", f"新しく入った記事 {len(new)} 件。", ""]
    if ai_text:
        out += ["## MOAへの示唆（AIによる読み解き・要確認）", "", ai_text.strip(), ""]
    out += ["## 分類別", ""] + group_md(new)
    if errors:
        out += ["<details><summary>取得できなかった配信元</summary>", ""] + [f"- {e}" for e in errors] + ["", "</details>", ""]
    if p.exists():  # 同じ日に2回動いたら追記
        out = ["", "---", f"（{datetime.now(JST):%H:%M} 追加分）", ""] + out[2:]
        p.write_text(p.read_text(encoding="utf-8") + "\n".join(out), encoding="utf-8")
    else:
        p.write_text("\n".join(out), encoding="utf-8")
    return p


def write_latest(rows: list[dict], days: int = 14) -> None:
    since = (datetime.now(JST) - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = [r for r in rows if r["date"] >= since]
    out = [f"# 直近{days}日の行政・社会の動き", "",
           f"<!-- 自動生成: tools/news.py ／ 更新 {datetime.now(JST):%Y-%m-%d %H:%M} JST -->", "",
           f"{since} 以降 {len(recent)} 件。日ごとの読み解きは news/daily/ を参照。", ""]
    (NEWS_DIR / "latest.md").write_text("\n".join(out + group_md(recent, 25)), encoding="utf-8")


# ---------- AI による読み解き（任意） ----------

def ai_commentary(new: list[dict]) -> str | None:
    if not new or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        print("anthropic が未導入のため AI 読み解きを飛ばします（pip install anthropic）", file=sys.stderr)
        return None
    ctx = []
    for f in ("gyousei/行政の願い.md", "moa/理念と事業.md"):
        p = KNOWLEDGE / f
        if p.exists():
            ctx.append(f"<file name='{f}'>\n{p.read_text(encoding='utf-8')[:20000]}\n</file>")
    lines = "\n".join(f"- [{','.join(r['cats'])}] {r['date']} {r['title']}（{r['source']}）" for r in new[:120])
    system = (
        "あなたは、MOA（岡田茂吉の思想にもとづき、浄化療法・自然農法と食・美術文化の三つの事業で"
        "「統合医療の社会モデル」を地域に築く団体）の広島の専従者を支える調査役です。"
        "添付の「行政の願い」「理念と事業」を踏まえて、今日の行政・社会のニュースを読み解いてください。"
        "事実はニュースの見出しから読み取れる範囲にとどめ、推測には「〜の可能性」と書き、見出しにない数字や固有名詞を作らないこと。"
    )
    prompt = (
        "\n\n".join(ctx) + f"\n\n<news>\n{lines}\n</news>\n\n"
        "次の形で、日本語・常体で、全体で1500字以内にまとめてください。\n"
        "1. 今日の要点（3〜5行）\n"
        "2. 行政の願いとの接点：どの記事が、行政のどの願い（地域共生・健康寿命・社会参加・食育など）に関わり、"
        "療院・分院・資格者の営みがどう応えうるか（記事の見出しを括弧で示す）\n"
        "3. 次の一手の候補：議員・社協・地域包括・学校などとの語り合いに使える話題を2〜3個\n"
        "4. 注意して見ておく動き（制度改正・パブリックコメントの締切など）"
    )
    client = anthropic.Anthropic()
    try:
        resp = client.beta.messages.create(
            model="claude-opus-5-5",
            max_tokens=16000,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as e:
        print(f"AI 読み解きに失敗しました（HTTP {e.status_code}）: {e.message}", file=sys.stderr)
        return None
    except anthropic.APIConnectionError as e:
        print(f"AI 読み解きに接続できませんでした: {e}", file=sys.stderr)
        return None
    if resp.stop_reason == "refusal":
        print("AI 読み解きは応答が得られませんでした（refusal）", file=sys.stderr)
        return None
    return "\n".join(b.text for b in resp.content if b.type == "text") or None


# ---------- 入口 ----------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ai", action="store_true", help="Claude で「MOAへの示唆」を付ける")
    ap.add_argument("--offline", nargs="+", metavar="FILE", help="手元のRSS/Atomファイルで試す")
    ap.add_argument("--days", type=int, default=21, help="これより古い記事は取り込まない（日）")
    ap.add_argument("--show", type=int, metavar="DAYS", help="台帳から直近N日分を表示するだけ")
    a = ap.parse_args()
    cfg = json.loads((NEWS_DIR / "feeds.json").read_text(encoding="utf-8"))

    if a.show:
        since = (datetime.now(JST) - timedelta(days=a.show)).strftime("%Y-%m-%d")
        print("\n".join(group_md([r for r in load_db() if r["date"] >= since], 40)))
        return

    rows = load_db()
    known = {r["id"]: r for r in rows}
    got, errors = collect(cfg, a.offline, a.days)
    new = []
    for r in got:
        if r["id"] in known:
            old = known[r["id"]]
            old["via"] = sorted(set(old.get("via", [])) | set(r["via"]))
        else:
            known[r["id"]] = r
            new.append(r)
    keep = (datetime.now(JST) - timedelta(days=cfg.get("keep_days", 400))).strftime("%Y-%m-%d")
    rows = [r for r in known.values() if r["date"] >= keep]
    save_db(rows)
    ai = ai_commentary(new) if a.ai else None
    daily = write_daily(new, errors, ai)
    write_latest(rows)
    print(f"新着 {len(new)} 件 ／ 台帳 {len(rows)} 件 ／ 取得失敗 {len(errors)} 件")
    if daily:
        print(f"日報: {daily.relative_to(NEWS_DIR.parent)}")
    for e in errors[:10]:
        print(f"  !! {e}", file=sys.stderr)
    total = len(cfg.get("feeds", [])) + len(cfg.get("searches", []))
    if not a.offline and errors and len(errors) >= total:
        sys.exit("すべての配信元で取得に失敗しました（ネットワーク制限の可能性）")


if __name__ == "__main__":
    main()
