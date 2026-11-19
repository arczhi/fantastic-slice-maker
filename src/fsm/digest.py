"""每日总览页：把当天产出的切片汇总成一个可点的 HTML。"""
from __future__ import annotations

import html
import json
from pathlib import Path
from urllib.parse import quote

from .util import atomic_write_text, get_logger

log = get_logger("digest")

_CSS = """
:root{--bg:#0d0d10;--card:#17171c;--line:#2a2a33;--tx:#ececf1;--dim:#8d8d99;--hot:#f0b429}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
 font:14px/1.6 -apple-system,"PingFang SC","Helvetica Neue",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:32px 20px 60px}
h1{font-size:22px;font-weight:600;margin:0 0 6px}
.sub{color:var(--dim);font-size:13px;margin-bottom:26px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;
 transition:border-color .15s}
.card:hover{border-color:#45455a}
.thumb{position:relative;background:#000;aspect-ratio:9/16;max-height:330px;overflow:hidden}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.badge{position:absolute;left:8px;top:8px;background:rgba(0,0,0,.72);color:var(--hot);
 font-size:11px;padding:2px 7px;border-radius:4px;letter-spacing:.02em}
.meta{padding:11px 12px 13px}
.title{font-size:13.5px;font-weight:500;margin:0 0 5px;
 display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.src{color:var(--dim);font-size:11.5px;margin-bottom:7px}
.hook{color:#a9a9b8;font-size:12px;line-height:1.5;
 display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.warn{color:#e2703a;font-size:11px;margin-top:6px}
a{color:inherit;text-decoration:none}
.empty{color:var(--dim);padding:60px 0;text-align:center}
"""


def write_index(day_dir: Path) -> Path | None:
    day_dir = Path(day_dir).resolve()
    if not day_dir.exists():
        return None

    def rel(p: str | None) -> str:
        if not p:
            return ""
        try:
            return quote(Path(p).resolve().relative_to(day_dir).as_posix())
        except ValueError:
            return quote(Path(p).name)

    cards: list[str] = []
    total = 0
    for rep_path in sorted(day_dir.glob("*/report.json")):
        try:
            rep = json.loads(rep_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for r in rep.get("reels") or []:
            if not r.get("ok"):
                continue
            total += 1
            cover_rel = rel(r.get("cover"))
            video_rel = rel(r["path"])
            issues = (r.get("qc") or {}).get("issues") or []
            cards.append(f"""
    <a class="card" href="{html.escape(video_rel)}">
      <div class="thumb">
        <img src="{html.escape(cover_rel)}" alt="" loading="lazy">
        <span class="badge">{r.get('duration', 0):.0f}s</span>
      </div>
      <div class="meta">
        <p class="title">{html.escape(r.get('title') or '(未命名)')}</p>
        <div class="src">{html.escape(Path(rep['video']).stem[:22])} · {html.escape(str(rep.get('strategy', '')))}</div>
        <div class="hook">{html.escape((r.get('hook') or '')[:60])}</div>
        {f'<div class="warn">· {html.escape(issues[0])}</div>' if issues else ''}
      </div>
    </a>""")

    body = (
        "".join(cards) if cards
        else '<div class="empty">今天还没有产出</div>'
    )
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>切片总览 {day_dir.name}</title><style>{_CSS}</style></head>
<body><div class="wrap">
<h1>切片总览 · {day_dir.name}</h1>
<div class="sub">共 {total} 条 · 竖屏 1080×1920 · 点开即看</div>
<div class="grid">{body}
</div></div></body></html>"""

    out = day_dir / "index.html"
    atomic_write_text(out, doc)
    log.info(f"digest: {out} ({total} reels)")
    return out
