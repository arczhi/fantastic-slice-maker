"""横屏短剧解说（西瓜向）：16:9、3-5 分钟，竖屏居左 + 右侧解说栏。

第一版只做验证片：
- 台词取自已缓存 timeline.json（ASR sentences）；
- DeepSeek 写解说稿 + 3 个章节标题；
- Qwen3-TTS 男声（默认 ryan）逐句合成后拼接；
- 原片压到 0.15、BGM 0.10，解说是主体；
- 左侧原片竖屏缩到 608x1080，右侧 1300px 解说栏。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import ffmpeg, planner
from .config import load
from .subtitle import _font_path

TTS_PY = Path.home() / "coding/qwen3-tts-mlx/.venv/bin/python"
TTS_CLI = Path.home() / "coding/shorts-studio/tools/qwen3_tts_cli.py"
VOICE = "ryan"
INSTRUCT = ("用清楚、稳的中年男声讲短剧剧情，语速正常偏快，句尾收住，"
            "不要播音腔，不要压低嗓子，不要拖长音。")

SYSTEM = "你是短剧解说编剧，为西瓜视频写横屏中视频解说。只输出 JSON。"
TEMPLATE = """下面是短剧《{title}》第 {a:.0f}-{b:.0f} 秒的台词。

请写一条 1000-1200 字的横屏解说稿（约 4 分钟），要求：
1. 开头 3 秒用冲突/悬念钩子，不要寒暄；
2. 按时间顺序讲清人物关系、冲突和反转，口语、短句；
3. 不要照抄台词，不要编造台词里没有的剧情；
4. 结尾留一个问题或一句判断；
5. 同时给 3 个 4-8 字的章节标题。

台词：
{lines}

只输出 JSON：
{{"script":"...","chapters":["...","...","..."]}}"""


def _sentences(work: Path) -> list[dict]:
    tl = json.loads((work / "timeline.json").read_text(encoding="utf-8"))
    return tl.get("sentences") or []


def _lines_in(sentences: list[dict], a: float, b: float) -> str:
    out = []
    for s in sentences:
        if float(s.get("end") or 0) > a and float(s.get("start") or 0) < b:
            t = str(s.get("text") or "").strip()
            if t:
                out.append(t)
    return "".join(out)[:6000]


def _panel(title: str, chapters: list[str], dst: Path) -> Path:
    W, H = 1300, 1080
    img = Image.new("RGB", (W, H), (12, 12, 12))
    d = ImageDraw.Draw(img)
    fp = _font_path()
    big = ImageFont.truetype(fp, 66)
    mid = ImageFont.truetype(fp, 42)
    small = ImageFont.truetype(fp, 30)
    d.text((64, 90), title, font=big, fill=(245, 245, 245))
    d.line([(64, 210), (1236, 210)], fill=(200, 160, 60), width=3)
    y = 280
    for i, c in enumerate(chapters[:3], start=1):
        d.text((64, y), f"0{i}", font=mid, fill=(200, 160, 60))
        d.text((140, y + 2), c, font=mid, fill=(235, 235, 235))
        y += 150
    d.text((64, H - 330), "分集剧情 · 一次讲完", font=small, fill=(150, 150, 150))
    d.text((64, H - 90), "短剧小当家 · 解说", font=small, fill=(120, 120, 120))
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst)
    return dst


def _tts(script: str, out_wav: Path, work: Path) -> float:
    parts = [p.strip() for p in script.replace("\n", "").split("。") if p.strip()]
    chunks, cur = [], ""
    for p in parts:
        if len(cur) + len(p) > 70 and cur:
            chunks.append(cur + "。")
            cur = p
        else:
            cur = (cur + "。" if cur else "") + p
    if cur:
        chunks.append(cur + "。")
    wavs = []
    for i, ch in enumerate(chunks):
        # 连续标点（？。、！。）会让 TTS 偶尔报错，统一成一个句号
        text = re.sub(r"[？?。！!]+", "。", ch).strip()
        w = work / f"tts_{i:02d}.wav"
        cmd = [str(TTS_PY), str(TTS_CLI), "--text", text, "--out", str(w),
               "--voice", VOICE, "--instruct", INSTRUCT,
               "--target-cps", "4.2", "--pitch", "1.0", "--json"]
        for attempt in range(2):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if r.returncode == 0:
                break
            if attempt == 1:
                print(r.stderr[-600:], file=sys.stderr)
                raise SystemExit(f"TTS 失败：chunk {i} {text[:30]}")
        wavs.append(w)
    lst = work / "tts_list.txt"
    lst.write_text("".join(f"file '{w.resolve()}'\n" for w in wavs), encoding="utf-8")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(out_wav)], check=True, timeout=600)
    return ffmpeg.duration_of(str(out_wav))


def run(src: str, work: Path, out: Path, title: str, a: float, b: float,
        reuse: bool = False, segmented: bool = False,
        vertical: bool = True) -> dict:
    cfg = load()
    work.mkdir(parents=True, exist_ok=True)
    narr = work / "narration.wav"
    if reuse and (work / "script.txt").exists() and narr.exists():
        script = (work / "script.txt").read_text(encoding="utf-8")
        chapters = json.loads((work / "chapters.json").read_text(encoding="utf-8"))
        ndur = ffmpeg.duration_of(str(narr))
        print(f"复用解说 {len(script)} 字 / {ndur:.1f}s")
    else:
        sentences = _sentences(work)
        lines = _lines_in(sentences, a, b)
        prompt = TEMPLATE.format(title=title, a=a, b=b, lines=lines)
        raw = planner._post(cfg.planner, [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt}], 0.6)
        data = planner._parse(raw)
        script = str(data.get("script") or "").strip()
        chapters = [str(x) for x in (data.get("chapters") or [])][:3]
        if len(script) < 200:
            raise SystemExit(f"解说稿太短：{script[:80]}")
        (work / "script.txt").write_text(script, encoding="utf-8")
        (work / "chapters.json").write_text(json.dumps(chapters, ensure_ascii=False), encoding="utf-8")
        ndur = _tts(script, narr, work)
        print(f"解说 {len(script)} 字 / {ndur:.1f}s")

    panel = _panel(title, chapters, work / "panel.png")
    dur = max(b - a, ndur + 2.0)

    # 分镜文字描述：ASR 分段 → DeepSeek 每段一句，压在左画面原字幕上方。
    caps: list[tuple[Path, float, float]] = []
    if segmented:
        segs = _segments(_sentences(work), a, b)
        descs = _describe(segs, title, cfg) if segs else []
        for i, (sg, d) in enumerate(zip(segs, descs)):
            if not d:
                continue
            caps.append((_cap_png(d, work / f"seg_{i:02d}.png",
                                  width=1000 if vertical else 600,
                                  size=48 if vertical else 34), sg["t0"], sg["t1"]))
        print(f"分镜描述 {len(caps)} 条")
    bgm = Path("assets/bgm/mixkit/871.mp3")

    if vertical:
        vf = (
            f"[0:v]crop=iw:trunc(ih*0.92/2)*2:0:trunc(ih*0.08/2)*2,scale=1080:1920,setsar=1[v0];"
        )
        cap_y = 1030
    else:
        vf = (
            f"color=c=0x0b0b0b:s=1920x1080:d={dur:.2f}[bg];"
            f"[0:v]scale=-2:1080,setsar=1[left];"
            f"[bg][left]overlay=0:0[b1];"
            f"[3:v]format=rgba[panel];[b1][panel]overlay=620:0[v0];"
        )
        cap_y = 610
    vf += (
        f"[0:a]volume=0.15[oa];"
        f"[1:a]adelay=800|800,volume=1.0[na];"
        f"[2:a]volume=0.10,afade=t=in:st=0:d=1.0[ba];"
        f"[oa][na][ba]amix=inputs=3:duration=longest:normalize=0[a]"
    )
    last = "v0"
    base = 3 if vertical else 4
    for i, (_, c0, c1) in enumerate(caps):
        vf += (f";[{base+i}:v]format=rgba[c{i}];"
               f"[{last}][c{i}]overlay=x=({1080 if vertical else 608}-w)/2:y={cap_y}"
               f":enable='between(t,{c0:.3f},{c1:.3f})'[v{i+1}]")
        last = f"v{i+1}"
    vf += f";[{last}]null[v]"
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-ss", f"{a:.2f}", "-t", f"{dur:.2f}", "-i", src,
           "-i", str(narr), "-i", str(bgm),
           ]
    if not vertical:
        cmd += ["-loop", "1", "-i", str(panel)]
    for png, _, _ in caps:
        cmd += ["-loop", "1", "-i", str(png)]
    cmd += ["-filter_complex", vf, "-map", "[v]", "-map", "[a]",
           "-t", f"{dur:.2f}", "-r", "30", "-c:v", "libx264", "-crf", "19",
           "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    ffmpeg.run(cmd, timeout=1800)
    info = ffmpeg.probe(str(out))
    print(f"成片 {out.name} {info.duration:.1f}s {info.width}x{info.height}")
    return {"out": str(out), "duration": info.duration, "narration": ndur,
            "script": str(work / "script.txt")}




def _segments(sentences: list[dict], a: float, b: float, max_dur: float = 12.0) -> list[dict]:
    """把 ASR 按时间聚成 8-12 秒一段，段内台词拼起来。"""
    segs: list[dict] = []
    for s in sentences:
        e, st = float(s.get("end") or 0), float(s.get("start") or 0)
        if e <= a or st >= b:
            continue
        t0, t1 = max(a, st) - a, min(b, e) - a
        txt = str(s.get("text") or "").strip()
        if not txt:
            continue
        if not segs or t1 - segs[-1]["t0"] > max_dur:
            segs.append({"t0": round(t0, 2), "t1": round(t1, 2), "text": txt})
        else:
            segs[-1]["t1"] = round(t1, 2)
            segs[-1]["text"] += txt
    return segs


SEG_SYSTEM = "你是短剧解说编剧，为横屏中视频写分镜文字描述。只输出 JSON。"
SEG_TEMPLATE = """下面是短剧《{title}》的 ASR 分段（时间相对切片开头）。

请为每一段写一句 6-12 字的剧情描述，要求：
1. 说清这一段在发生什么，不要照抄台词；
2. 不剧透后面的反转，不写“镜头/画面”；
3. 一句话，不要标点结尾。

分段：
{rows}

只输出 JSON：{{"segments":[{{"i":1,"desc":"..."}}]}}"""


def _describe(segs: list[dict], title: str, cfg) -> list[str]:
    rows = [f'{i+1}. {s["t0"]:.1f}-{s["t1"]:.1f}s｜{s["text"][:80]}'
            for i, s in enumerate(segs)]
    raw = planner._post(cfg.planner, [
        {"role": "system", "content": SEG_SYSTEM},
        {"role": "user", "content": SEG_TEMPLATE.format(title=title, rows="\n".join(rows))},
    ], 0.5)
    data = planner._parse(raw)
    got = data.get("segments") if isinstance(data, dict) else None
    by = {int(x.get("i") or 0): str(x.get("desc") or "").strip()
          for x in (got or []) if isinstance(x, dict)}
    return [by.get(i + 1, "")[:14] for i in range(len(segs))]


def _cap_png(text: str, dst: Path, width: int = 600, size: int = 34) -> Path:
    fp = _font_path()
    font = ImageFont.truetype(fp, size)
    dummy = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(dummy)
    lines, cur = [], ""
    for ch in text:
        if d.textlength(cur + ch, font=font) > width - 36:
            lines.append(cur); cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    text_w = max(int(d.textlength(ln, font=font)) for ln in lines) + 40
    width = max(200, min(width, text_w))
    h = size * len(lines) + 26
    img = Image.new("RGBA", (width, h), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle([0, 0, width - 1, h - 1], radius=10, fill=(0, 0, 0, 150))
    for i, ln in enumerate(lines):
        w = d.textlength(ln, font=font)
        dr.text(((width - w) / 2, 10 + i * size), ln, font=font, fill=(255, 255, 255, 245))
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst)
    return dst


def make_cover(video: str, title: str, dst: Path) -> Path:
    """从成片抽一帧做 1920x1080 横版封面；版面里已有标题，不再叠字。"""
    dur = ffmpeg.duration_of(video)
    frame = dst.with_suffix(".frame.jpg")
    subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{dur * 0.22:.2f}",
                    "-i", video, "-frames:v", "1", "-y", str(frame)], check=True)
    img = Image.open(frame).convert("RGB").resize((1920, 1080))
    img.save(dst, quality=92)
    frame.unlink(missing_ok=True)
    return dst


def package(video: str, title: str, desc: str, tags: list[str], out_dir: Path) -> dict:
    stem = Path(video).stem
    cover = make_cover(video, title, out_dir / f"{stem}.cover.jpg")
    meta = {"video": str(Path(video).resolve()), "title": title, "desc": desc,
            "tags": tags, "cover": str(cover.resolve()),
            "platform": "xigua", "note": "西瓜网页创作平台已并入抖音，当前走西瓜 App 手动发布"}
    p = out_dir / f"{stem}.publish.json"
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"meta": str(p), "cover": str(cover)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="fsm recap", description="横屏短剧解说（西瓜半自动）")
    ap.add_argument("src")
    ap.add_argument("work", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--title", required=True)
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--desc", default="")
    ap.add_argument("--tag", action="append", default=[])
    ap.add_argument("--reuse", action="store_true")
    ap.add_argument("--segmented", action="store_true",
                    help="按 ASR 分段叠分镜文字描述（在原字幕上方）")
    ap.add_argument("--horizontal", action="store_true",
                    help="16:9 横屏+右侧解说框（默认是 9:16 竖屏）")
    ns = ap.parse_args(argv)
    res = run(ns.src, ns.work, ns.out, ns.title, ns.start, ns.end,
              reuse=ns.reuse, segmented=ns.segmented,
              vertical=not ns.horizontal)
    tags = ns.tag or ["短剧", "解说", "家庭", "悬疑"]
    pkg = package(res["out"], ns.title, ns.desc or ns.title, tags, ns.out.parent)
    res.update(pkg)
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
