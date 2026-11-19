"""自动补字幕：源片没有内嵌字幕时，用 ASR 句子生成字幕条烧进成片。

判定依据是 OCR 字幕覆盖率而不是「条数」——无字幕片里 OCR 仍会零星抓到
台标、集数标题、画面招牌，按条数判断会误以为有字幕。

为什么不用 ffmpeg 的 subtitles / drawtext 滤镜：
本机 Homebrew ffmpeg 8.1 是精简构建（编译参数里没有 --enable-libass /
--enable-libfreetype），这两个滤镜根本不存在。所以改成「PIL 渲染透明 PNG
+ overlay 链」：一次编码完成，不损失画质，样式也完全可控（白字黑边、
自动换行、按字数分配时长）。

时间轴按「段内相对时间」算，而不是成片绝对时间。因为字幕是逐 clip 烧的，
每段切片都是从 0 开始，不用担心多段拼接后的偏移累积误差。
"""
from __future__ import annotations

import math
import unicodedata
from functools import lru_cache
from pathlib import Path

from .config import SubtitleCfg
from .util import ensure_dir, get_logger

log = get_logger("subtitle")

# 句末标点在字幕里不好看，只保留问号感叹号（有情绪价值）
_TAIL_STRIP = "，。、；：,.;:"
_BREAK = "，。！？；：、,.!?;:…—"

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:/Windows/Fonts/msyh.ttc",
)


# ---------------------------------------------------------------- 判定


def coverage(strips: list[dict], start: float = 0.0, end: float = 0.0) -> float:
    """字幕覆盖率 = 有字幕的总时长 / 区间时长。"""
    if not strips:
        return 0.0
    if end <= start:
        lo = min(float(s["start"]) for s in strips)
        hi = max(float(s["end"]) for s in strips)
        end = max(hi, lo + 1.0)
        start = 0.0
    span = max(end - start, 1.0)
    total = 0.0
    for s in strips:
        a = max(float(s["start"]), start)
        b = min(float(s["end"]), end)
        if b > a:
            total += b - a
    return round(min(1.0, total / span), 4)


def decide(strips: list[dict], duration: float, cfg: SubtitleCfg,
           force_mode: str | None = None) -> dict:
    """判断这部剧要不要补字幕。"""
    mode = (force_mode or cfg.mode or "auto").lower()
    cov = coverage(strips, 0.0, duration)
    if mode == "off":
        return {"needed": False, "mode": mode, "coverage": cov, "reason": "已关闭"}
    if mode == "on":
        return {"needed": True, "mode": mode, "coverage": cov, "reason": "强制开启"}
    if not cfg.auto_fill:
        return {"needed": False, "mode": mode, "coverage": cov, "reason": "auto_fill 关闭"}
    needed = cov < cfg.coverage_threshold
    why = (f"源片字幕覆盖率 {cov:.1%} < {cfg.coverage_threshold:.0%}"
           if needed else f"源片自带字幕（覆盖率 {cov:.1%}）")
    return {"needed": needed, "mode": mode, "coverage": cov, "reason": why}


# ---------------------------------------------------------------- 文本切分


def _wide(ch: str) -> bool:
    """中日韩字符按 1 个宽度算，ASCII 按半个——否则「毕业于 royal college」
    会被当成 20 个字切碎，而它实际只占 10 个中文字的版面。"""
    return unicodedata.east_asian_width(ch) in ("W", "F")


def _width(s: str) -> float:
    return sum(1.0 if _wide(ch) else 0.5 for ch in s)


def _fit(s: str, limit: float) -> int:
    """返回不超过 limit 宽度、且不切断英文单词的最大前缀长度。"""
    w = 0.0
    for i, ch in enumerate(s):
        cw = 1.0 if _wide(ch) else 0.5
        if w + cw > limit:
            if i == 0:
                return 1
            # 切点落在英文单词内部时退到词首，最多退 10 个字符
            if not _wide(ch) and not _wide(s[i - 1]) and s[i - 1] != " " and ch != " ":
                sp = s.rfind(" ", max(0, i - 10), i)
                if sp > 0:
                    return sp + 1
            return i
        w += cw
    return len(s)


def split_text(text: str, max_chars: int) -> list[str]:
    """把一句台词切成适合单行显示的短句。

    先按标点断，再按显示宽度切（不切断英文单词），最后把过短的碎片并回
    前一句——否则会出现「他」「你说什么」这种一闪而过的两条。
    """
    text = (text or "").strip()
    if not text:
        return []
    limit = float(max_chars)
    if _width(text) <= limit:
        return [text.rstrip(_TAIL_STRIP) or text]

    chunks: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _BREAK and _width(buf) >= max(4.0, limit * 0.5):
            chunks.append(buf)
            buf = ""
    if buf.strip():
        chunks.append(buf)

    pieces: list[str] = []
    for c in chunks:
        c = c.strip()
        while _width(c) > limit:
            cut = _fit(c, limit)
            pieces.append(c[:cut])
            c = c[cut:]
        if c:
            pieces.append(c)

    merged: list[str] = []
    for p in pieces:
        if merged and _width(merged[-1]) + _width(p) <= limit:
            merged[-1] += p
        else:
            merged.append(p)
    return [m.rstrip(_TAIL_STRIP) or m for m in merged if m.strip()]


# ---------------------------------------------------------------- 时间轴


def cues_for_beat(sentences: list[dict], start: float, end: float,
                  cfg: SubtitleCfg) -> list[dict]:
    """把 ASR 句子映射到某个 beat 的段内相对时间。

    beat 常常从一句话中间切入，所以交集不足整句时按字数比例截取文字——
    直接显示整句会出现「字幕比声音多一截」。
    """
    out: list[dict] = []
    for s in sentences:
        try:
            s0, s1 = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = (s.get("text") or "").strip()
        if not text:
            continue
        a, b = max(s0, start), min(s1, end)
        if b - a < cfg.min_cue * 0.75:
            continue

        span = max(s1 - s0, 0.01)
        if a > s0 + 0.01 or b < s1 - 0.01:
            i0 = int(len(text) * (a - s0) / span)
            i1 = max(i0 + 1, math.ceil(len(text) * (b - s0) / span))
            # 只截掉结尾一小段时保留整句——「你还妄」这种半句话比
            # 「字幕比声音多 0.3 秒」难看得多。
            head_cut = i0 > 0.12 * len(text)
            if head_cut or i1 / max(len(text), 1) < 0.7:
                text = text[i0:i1].strip()
        if not text:
            continue

        parts = split_text(text, cfg.max_chars)
        if not parts:
            continue
        weights = [max(1, len(p)) for p in parts]
        wsum = sum(weights)
        total = b - a
        t = a
        for p, w in zip(parts, weights):
            d = max(cfg.min_cue, total * w / wsum)
            out.append({"text": p, "start": t, "end": min(t + d, end)})
            t += d

    # 裁到 beat 内、丢掉过短的、去掉重叠
    cleaned: list[dict] = []
    for c in out:
        s = max(c["start"], start)
        e = min(c["end"], end)
        if e - s >= 0.3:
            cleaned.append({"text": c["text"], "start": round(s - start, 3),
                            "end": round(e - start, 3)})
    cleaned.sort(key=lambda x: x["start"])
    for i in range(len(cleaned) - 1):
        limit = cleaned[i + 1]["start"] - 0.02
        if cleaned[i]["end"] > limit and limit - cleaned[i]["start"] >= 0.3:
            cleaned[i]["end"] = round(limit, 3)
    return cleaned


def layout_of(src_w: int, src_h: int, width: int, height: int,
              cfg: SubtitleCfg) -> dict:
    """算出字幕该落在哪。

    成片是「模糊背景铺满 + 原画面按比例居中」：竖屏源会铺满 1080x1920，
    横屏源只占中间一条。字幕必须贴着**原画面**的底边，而不是画布底边，
    否则横屏素材的字幕会飘到模糊背景上，看着像另一个人的台词。
    """
    src_w = max(int(src_w or 0), 1)
    src_h = max(int(src_h or 0), 1)
    fg_h = min(height, int(round(width * src_h / src_w / 2) * 2))
    fg_top = (height - fg_h) // 2
    bottom = fg_top + fg_h - int(fg_h * cfg.bottom_pct)
    return {"w": width, "h": height, "fg_h": fg_h, "fg_top": fg_top,
            "bottom": bottom}


# ---------------------------------------------------------------- 渲染 PNG


@lru_cache(maxsize=8)
def _font_path(explicit: str = "") -> str:
    if explicit:
        if Path(explicit).expanduser().exists():
            return str(Path(explicit).expanduser())
        log.warning(f"字幕字体不存在，回退系统字体: {explicit}")
    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return ""


def render_png(lines: list[str], lay: dict, cfg: SubtitleCfg, dst: Path) -> int:
    """把一行或多行文字渲染成透明 PNG，返回图片高度（overlay 要用）。"""
    from PIL import Image, ImageDraw, ImageFont

    fp = _font_path(cfg.font)
    if not fp:
        raise RuntimeError("找不到可用的中文字体，请在 subtitle.font 指定路径")
    font = ImageFont.truetype(fp, cfg.size)

    line_h = int(cfg.size * 1.34)
    pad_y = int(cfg.size * 0.30)
    pad_x = int(cfg.size * 0.6)
    img_h = line_h * len(lines) + pad_y * 2

    img = Image.new("RGBA", (lay["w"], img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for i, text in enumerate(lines):
        bbox = draw.textbbox((0, 0), text, font=font,
                             stroke_width=cfg.stroke)
        tw = bbox[2] - bbox[0]
        # 单行超宽时左右各留一点，避免贴边
        x = max(pad_x, (lay["w"] - tw) // 2 - bbox[0])
        y = pad_y + i * line_h - bbox[1]
        draw.text((x, y), text, font=font, fill=(255, 255, 255, 255),
                  stroke_width=cfg.stroke, stroke_fill=(0, 0, 0, 240))

    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst)
    return img_h


# ---------------------------------------------------------------- 对外入口


def build(
    sentences: list[dict], beats: list[dict], info, rcfg,
    cfg: SubtitleCfg, out_dir: Path,
) -> list[list[dict]]:
    """给每个 beat 生成 overlay 参数，供 ffmpeg.cut 使用。

    返回与 beats 一一对应的列表；某项为空表示该段没有可显示的台词。
    """
    lay = layout_of(info.width, info.height, rcfg.width, rcfg.height, cfg)
    ensure_dir(out_dir)
    # 重跑时字幕条数会变，先清掉上一轮的 PNG，避免残留被误引用
    for old in out_dir.glob("*.png"):
        old.unlink(missing_ok=True)
    per_beat: list[list[dict]] = []

    for bi, b in enumerate(beats):
        cues = cues_for_beat(sentences, float(b["start"]), float(b["end"]), cfg)
        subs: list[dict] = []
        for ci, cue in enumerate(cues):
            lines = split_text(cue["text"], cfg.max_chars)
            if not lines:
                continue
            if len(lines) > 2:       # 兜底：最多两行
                lines = lines[:2]
            png = out_dir / f"b{bi:02d}_{ci:02d}.png"
            try:
                h = render_png(lines, lay, cfg, png)
            except Exception as e:
                log.warning(f"字幕渲染失败 {cue['text'][:12]}: {e}")
                continue
            subs.append({
                "png": png,
                "start": cue["start"],
                "end": cue["end"],
                "y": max(0, lay["bottom"] - h),
                "text": cue["text"],
            })
        if subs:
            log.info(f"  beat{bi} {b['start']:.0f}-{b['end']:.0f}s: {len(subs)} 条字幕")
        per_beat.append(subs)
    return per_beat
