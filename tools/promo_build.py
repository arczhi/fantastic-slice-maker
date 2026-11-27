"""星火计划推广片：原片高光切片 + TTS 解说 + 原声 ducking。

用法：python tools/promo_build.py --spec config/promo/dongbei-wuxian.json [--out /tmp/test.mp4]

发布仍在快手 App 的任务详情页「发布作品去赚钱」；本工具只产出成片 + publish.json。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"


def _run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit((p.stderr or p.stdout)[-800:])


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT, size)


def caption_png(lines: list[str], path: Path, size: tuple[int, int],
                *, center: bool = False) -> None:
    w, h = size
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if center:
        f1, f2 = _font(88), _font(48)
        y = int(h * 0.40)
        for i, line in enumerate(lines):
            font = f1 if i == 0 else f2
            lw = d.textlength(line, font=font)
            d.text(((w - lw) / 2, y + i * (118 if i == 0 else 74)), line,
                   font=font, fill=(245, 240, 230))
    else:
        f = _font(54)
        y = int(h * 0.69)
        d.rectangle([0, y - 40, w, y + len(lines) * 88 + 30], fill=(0, 0, 0, 150))
        for i, line in enumerate(lines):
            d.text((70, y + i * 88), line, font=f, fill=(245, 240, 230))
    im.save(path)


def title_png(title: str, watermark: str, path: Path, size: tuple[int, int]) -> None:
    w, _ = size
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.text((70, 120), title, font=_font(56), fill=(255, 255, 255, 235))
    d.rectangle([70, 190, 70 + 330, 196], fill=(210, 170, 70, 255))
    d.text((w - 250, 70), watermark, font=_font(34), fill=(255, 255, 255, 150))
    im.save(path)


def render_beat(spec: dict, beat: dict, idx: int, work: Path, mats: list[Path]) -> Path:
    w, h = spec["size"]
    cap = work / f"cap{idx}.png"
    caption_png(beat["cap"], cap, (w, h), center=bool(beat.get("center")))
    dst = work / f"beat{idx}.mp4"
    title = work / "title.png"
    dur = float(beat["dur"])
    if beat.get("src") is None:
        _run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
              "-i", f"color=c=0x0d0d0c:s={w}x{h}:d={dur}",
              "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
              "-loop", "1", "-i", str(cap), "-loop", "1", "-i", str(title),
              "-filter_complex", "[0:v][2:v]overlay=0:0[v1];[v1][3:v]overlay=0:0[v]",
              "-map", "[v]", "-map", "1:a", "-t", str(dur), "-c:v", "libx264",
              "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(dst)])
        return dst
    src = mats[int(beat["src"])]
    vf = (
        f"[0:v]crop=iw:trunc(ih*0.84/2)*2:0:trunc(ih*0.08/2)*2,split=2[bg][fg];"
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
        f"gblur=sigma=42,eq=brightness=-0.05:saturation=0.75[bg2];"
        f"[fg]scale={w}:-2[fg2];[bg2][fg2]overlay=(W-w)/2:{spec.get('fg_y', 560)}[v1];"
    )
    if beat.get("dark"):
        vf += f"[v1]drawbox=x=0:y=0:w={w}:h={h}:color=black@{beat['dark']}:t=fill[v1b];"
        vf += "[v1b][1:v]overlay=0:0[v2];"
    else:
        vf += "[v1][1:v]overlay=0:0[v2];"
    vf += "[v2][2:v]overlay=0:0[v];[0:a]volume=1.0[a]"
    _run(["ffmpeg", "-v", "error", "-y", "-ss", str(beat["start"]), "-t", str(dur),
          "-i", str(src), "-loop", "1", "-i", str(cap), "-loop", "1", "-i", str(title),
          "-filter_complex", vf, "-map", "[v]", "-map", "[a]", "-t", str(dur),
          "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
          "-c:a", "aac", str(dst)])
    return dst


def synth_line(text: str, path: Path, voice: dict, depth: int = 0) -> list[Path]:
    import sys
    src = Path(os.environ.get("SHORTS_STUDIO_SRC", Path.home() / "coding/shorts-studio/src"))
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from shorts_studio import tts
    try:
        tts.synth(text, path, voice=voice["voice"], instruct=voice["instruct"],
                  temperature=voice.get("temperature", 0.6), backend="qwen3",
                  target_cps=0, pitch=voice.get("pitch", 1.0))
        return [path]
    except Exception:
        if depth >= 2:
            raise
        mid = text.rfind("，", 0, len(text) // 2 + 4)
        if mid < 4:
            mid = len(text) // 2
        a, b = text[:mid + 1], text[mid + 1:]
        return (synth_line(a, path.with_name(path.stem + "_a.wav"), voice, depth + 1)
                + synth_line(b, path.with_name(path.stem + "_b.wav"), voice, depth + 1))


def concat_wavs(paths: list[Path], out: Path, gap: float = 0.04) -> None:
    params, frames = None, b""
    for p in paths:
        with wave.open(str(p)) as f:
            if params is None:
                params = f.getparams()
            frames += f.readframes(f.getnframes()) + b"\x00" * int(f.getframerate() * gap) * f.getnchannels() * 2
    with wave.open(str(out), "wb") as o:
        o.setparams(params)
        o.writeframes(frames)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    os.environ["SHORTS_TTS_FALLBACK"] = "0"

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    mats_dir = Path(spec["materials"]).expanduser()
    mats = sorted(mats_dir.glob("*.MP4")) or sorted(mats_dir.glob("*.mp4"))
    if not mats:
        raise SystemExit(f"素材目录没有视频：{mats_dir}")
    out = Path(args.out).expanduser() if args.out else (mats_dir / spec["output"])
    out.parent.mkdir(parents=True, exist_ok=True)
    work = out.parent / ".promo-work"
    work.mkdir(exist_ok=True)

    title_png(spec["title"], spec.get("watermark", ""), work / "title.png", tuple(spec["size"]))
    segs = [render_beat(spec, b, i, work, mats) for i, b in enumerate(spec["beats"])]
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{s}'\n" for s in segs), encoding="utf-8")
    base = work / "base.mp4"
    _run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(base)])

    total = sum(float(b["dur"]) for b in spec["beats"])
    wavs, inputs, filt = [], [], []
    for i, n in enumerate(spec["narration"]):
        parts = synth_line(n["text"], work / f"n{i}.wav", spec["voice"])
        p = work / f"n{i}.wav"
        if len(parts) > 1:
            concat_wavs(parts, p)
        wavs.append(p)
        inputs += ["-i", str(p)]
        filt.append(f"[{i}:a]adelay={int(float(n['start']) * 1000)}|{int(float(n['start']) * 1000)}[a{i}]")
    mix = "".join(f"[a{i}]" for i in range(len(wavs))) + f"amix=inputs={len(wavs)}:duration=longest:normalize=0[na]"
    nar = work / "nar.m4a"
    _run(["ffmpeg", "-v", "error", "-y"] + inputs + ["-filter_complex",
          ";".join(filt) + ";" + mix + f";[na]apad=pad_dur={total},atrim=0:{total}[nar]",
          "-map", "[nar]", "-c:a", "aac", str(nar)])

    bgm = spec.get("bgm")
    if bgm and Path(bgm).exists():
        _run(["ffmpeg", "-v", "error", "-y", "-i", str(base), "-i", str(nar), "-i", str(bgm),
              "-filter_complex",
              "[0:a]volume=1.0[orig];[1:a]asplit=2[nar][sc];"
              "[orig][sc]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=300[duck];"
              "[duck][nar]amix=inputs=2:duration=first:normalize=0[voice];"
              "[2:a]volume=0.08[bg];[voice][bg]amix=inputs=2:duration=first:normalize=0[a]",
              "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
              "-t", str(total), str(out)])
    else:
        _run(["ffmpeg", "-v", "error", "-y", "-i", str(base), "-i", str(nar),
              "-filter_complex", "[0:a]volume=1.0[o];[1:a]asplit=2[n][sc];"
              "[o][sc]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=300[d];"
              "[d][n]amix=inputs=2:duration=first:normalize=0[a]",
              "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
              "-t", str(total), str(out)])

    pub = dict(spec.get("publish") or {})
    pub.update({"task": spec["task"], "video": out.name, "duration_s": total})
    (out.parent / f"{out.stem}.publish.json").write_text(
        json.dumps(pub, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"video": str(out), "duration": total, "publish": str(out.parent / f"{out.stem}.publish.json")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
