"""成片叠加层：放大裁切去水印 + 分镜文字描述 + 旋转水印。

为什么放在拼接之后、混音之前：
- 放大裁切必须对整条成片做，逐段做会在段与段之间出现缩放跳变；
- 分镜描述是按成片时间轴（beats 的 dur 累加）铺的，拼接后才有稳定时间；
- BGM 混音最后做，保证人声 ducking 拿到的是最终音轨。

三个功能都在一次 ffmpeg 里完成，避免多次重编码掉画质。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import ffmpeg
from .config import OverlayCfg, RenderCfg
from .subtitle import _font_path
from .util import ensure_dir, get_logger

log = get_logger("overlay")


def _hex_rgba(color: str, alpha: int) -> tuple[int, int, int, int]:
    v = color.lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16), alpha)


def _text_png(text: str, *, size: int, color: str, stroke: int, opacity: float,
              out: Path, pad: int = 18, bg: bool = False) -> Path:
    font = ImageFont.truetype(_font_path(), size)
    dummy = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(dummy)
    bbox = d.textbbox((0, 0), text, font=font, stroke_width=stroke)
    w = bbox[2] - bbox[0] + pad * 2
    h = bbox[3] - bbox[1] + pad * 2
    img = Image.new("RGBA", (max(1, w), max(1, h)), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    if bg:
        dr.rounded_rectangle([0, 0, w - 1, h - 1], radius=max(8, h // 4),
                             fill=(0, 0, 0, 150))
    fill = _hex_rgba(color, int(255 * opacity))
    dr.text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=fill,
            stroke_width=stroke, stroke_fill=(0, 0, 0, int(210 * opacity)))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def _watermark_png(text: str, cfg: OverlayCfg, out: Path) -> Path:
    """正方形画布 + 居中文字：旋转时四角不会把字切掉。"""
    size = cfg.watermark_size
    font = ImageFont.truetype(_font_path(), size)
    dummy = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(dummy)
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    side = int(max(tw, th) * 1.55) + 24
    img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    fill = _hex_rgba("#F2C14E", int(255 * cfg.watermark_opacity))
    dr.text(((side - tw) / 2 - bbox[0], (side - th) / 2 - bbox[1]), text,
            font=font, fill=fill, stroke_width=2,
            stroke_fill=(0, 0, 0, int(180 * cfg.watermark_opacity)))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def apply(video: str | Path, out: str | Path, beats: list[dict],
          cfg: RenderCfg, work_dir: str | Path) -> Path:
    """放大裁切 + 分镜描述 + 旋转水印，一次重编码完成。"""
    ov = cfg.overlay
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not ov.enabled:
        ffmpeg.run(["ffmpeg", "-v", "error", "-y", "-i", str(video),
                    "-c", "copy", "-movflags", "+faststart", str(out)], timeout=cfg.timeout)
        return out

    work = ensure_dir(Path(work_dir) / "overlay")
    caps: list[tuple[Path, float, float]] = []
    t = 0.0
    for i, b in enumerate(beats):
        dur = float(b.get("dur") or 0)
        cap = (b.get("caption") or "").strip()
        if ov.caption_enabled and cap:
            png = _text_png(cap, size=ov.caption_size, color=ov.caption_color,
                            stroke=ov.caption_stroke, opacity=1.0,
                            out=work / f"cap_{i:02d}.png", bg=True)
            caps.append((png, t, t + dur))
        t += dur

    wm = _watermark_png(ov.watermark_text, ov, work / "watermark.png") if ov.watermark_enabled else None

    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(video)]
    for png, _, _ in caps:
        cmd += ["-loop", "1", "-i", str(png)]
    if wm:
        cmd += ["-loop", "1", "-i", str(wm)]

    W, H = cfg.width, cfg.height
    z = max(1.0, float(ov.zoom))
    parts = [f"[0:v]scale=trunc(iw*{z:.4f}/2)*2:trunc(ih*{z:.4f}/2)*2,"
             f"crop={W}:{H},setsar=1[base]"]
    last = "base"
    for i, (_, a, b) in enumerate(caps, start=1):
        parts.append(f"[{i}:v]format=rgba[c{i}]")
        parts.append(f"[{last}][c{i}]overlay=x=(W-w)/2:y={ov.caption_top}"
                     f":enable='between(t,{a:.3f},{b:.3f})'[v{i}]")
        last = f"v{i}"
    if wm:
        wi = len(caps) + 1
        sec = max(2.0, float(ov.watermark_rotate_seconds))
        parts.append(f"[{wi}:v]format=rgba,rotate=2*PI*t/{sec:.3f}:c=none:ow=iw:oh=ih[wmr]")
        parts.append(f"[{last}][wmr]overlay=x=W-w-{ov.watermark_right}:y={ov.watermark_top}"
                     f":format=auto[vout]")
        last = "vout"

    cmd += ["-filter_complex", ";".join(parts), "-map", f"[{last}]", "-map", "0:a?",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart", "-shortest", str(out)]
    ffmpeg.run(cmd, timeout=cfg.timeout)
    log.info(f"  overlay: zoom={z:.2f} caps={len(caps)} wm={bool(wm)} -> {out.name}")
    return out
