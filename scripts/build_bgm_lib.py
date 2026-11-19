"""构建本地 BGM 库：从 incompetech 的公开曲库筛选、下载、分析节拍，生成索引。

为什么不用 Pixabay / FreePD：
- Pixabay 的 CDN 对非浏览器 UA 返回 403，抓取不稳定；
- FreePD 已关站（首页是 Site Closed 通告）。
incompetech 提供机器可读的 pieces.json（含 bpm / feel / genre / 时长），
且 mp3 直链稳定，是当前可自动化程度最高的来源。

授权：Kevin MacLeod 的作品为 CC BY 4.0，商用需署名。署名串由本脚本写进
索引，渲染时会带上，不要删。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "assets" / "bgm"
CACHE = ROOT / "assets" / "_catalog"
BASE = "https://incompetech.com/music/royalty-free"
MP3 = BASE + "/mp3-royaltyfree/"

BUCKETS = {
    "epic": {"Epic", "Action", "Heroic", "Triumphant"},
    "tense": {"Suspenseful", "Intense", "Dark", "Unnerving", "Eerie"},
    "somber": {"Somber", "Sad", "Melancholy"},
    "mystical": {"Mystical", "Mysterious", "Ancient"},
    "uplifting": {"Uplifting", "Inspiring", "Triumphant"},
}
PER_BUCKET = 5

# 漫剧（古装/武侠/爽文）的配乐禁区：
# Bouncy/Humorous/Bright 这类轻快卡通音色跟刀光剑影完全打架；
# Holiday 是圣诞颂歌，第一轮筛选就混进了 "We Wish you a Merry Christmas"，
# 必须按 genre 直接排除而不是靠 feel 猜。
_HOLIDAY_GENRE = 9
_REJECT_FEELS = {"Bouncy", "Humorous", "Happy", "Funny", "Silly", "Playful", "Whimsical",
                 "Cartoon", "Christmas", "Holiday"}
_HEAVY_FEELS = {"Epic", "Dark", "Intense", "Suspenseful", "Aggressive", "Somber",
                "Mystical", "Mysterious", "Eerie", "Unnerving", "Action", "Driving"}

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def fetch(url: str, dst: Path, timeout: int = 120) -> bool:
    if dst.exists() and dst.stat().st_size > 100_000:
        return True
    req = urllib.request.Request(url, headers=UA)
    tmp = dst.with_suffix(dst.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 16):
                f.write(chunk)
        tmp.replace(dst)
        return True
    except Exception as e:
        print(f"    download failed: {type(e).__name__}: {e}", flush=True)
        tmp.unlink(missing_ok=True)
        return False


def dur_sec(s: str | None) -> int:
    try:
        h, m, sec = (int(x) for x in (s or "").split(":"))
        return h * 3600 + m * 60 + sec
    except (ValueError, TypeError):
        return 0


def load_catalog() -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / "pieces.json"
    if not fetch(f"{BASE}/pieces.json", p, timeout=60):
        raise SystemExit("拿不到曲库目录")
    return json.loads(p.read_text(encoding="utf-8"))


def pick(pieces: list[dict]) -> list[tuple[str, dict]]:
    """按情绪分桶挑选，避免 25 首都同一个味道。"""
    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for bucket, tags in BUCKETS.items():
        rows = []
        for it in pieces:
            fn = it.get("filename")
            if not fn or fn in seen:
                continue
            try:
                if int(str(it.get("genre", "")).split(" ")[0]) == _HOLIDAY_GENRE:
                    continue
            except ValueError:
                pass
            feels = {f.strip() for f in (it.get("feel") or "").split(",") if f.strip()}
            if not (feels & tags):
                continue
            # 轻快卡通类一律不进池，除非同时具备厚重情绪
            if (feels & _REJECT_FEELS) and not (feels & _HEAVY_FEELS):
                continue
            secs = dur_sec(it.get("length"))
            # 切片 30-120 秒，曲子太短没法留出剪辑余量，太长浪费磁盘
            if not (120 <= secs <= 420):
                continue
            try:
                bpm = int(str(it.get("bpm", "0")).split(" ")[0])
            except ValueError:
                continue
            if not (70 <= bpm <= 172):
                continue
            # 标签越贴合越优先，其次选时长较短的（省磁盘）
            score = len(feels & tags) - 0.3 * len(feels & _REJECT_FEELS)
            rows.append((-score, secs, it))
        rows.sort(key=lambda x: (x[0], x[1]))
        for _, secs, it in rows[:PER_BUCKET]:
            seen.add(it["filename"])
            it["_bucket"] = bucket
            it["_secs"] = secs
            out.append((bucket, it))
    return out


def analyze(path: Path) -> dict:
    """用 librosa 提取 BPM 与 beat 时间轴。"""
    import librosa
    import numpy as np

    y, sr = librosa.load(str(path), sr=22050, mono=True)
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="time")
    bpm = float(np.atleast_1d(tempo)[0])
    beats = [round(float(b), 3) for b in np.atleast_1d(beat_frames)]

    # 节拍稳定性：把 beat 间隔的变异系数作为指标，越小越适合卡点
    if len(beats) > 4:
        gaps = np.diff(beats)
        cv = float(gaps.std() / max(1e-6, gaps.mean()))
    else:
        cv = 1.0

    rms = librosa.feature.rms(y=y)[0]
    energy = float(np.mean(rms)) / max(1e-6, float(np.percentile(rms, 99)))
    return {
        "bpm": round(bpm, 1),
        "beats": beats,
        "beat_cv": round(cv, 3),
        "energy": round(min(1.0, energy), 3),
        "duration": round(float(len(y) / sr), 2),
    }


def main() -> int:
    pieces = load_catalog()
    print(f"曲库 {len(pieces)} 首，开始筛选", flush=True)
    chosen = pick(pieces)
    print(f"选中 {len(chosen)} 首", flush=True)

    # 复用旧索引里已分析过的曲子：librosa 分析一首要 3-5 秒，
    # 重建时没必要对没变的文件重跑。
    out_path = ROOT / "assets" / "bgm_index.json"
    cached: dict[str, dict] = {}
    if out_path.exists():
        try:
            for t in json.loads(out_path.read_text(encoding="utf-8")).get("tracks") or []:
                if t.get("file") and t.get("beats"):
                    cached[t["file"]] = t
        except (OSError, ValueError):
            pass

    tracks = []
    for i, (bucket, it) in enumerate(chosen, 1):
        d = LIB / bucket
        d.mkdir(parents=True, exist_ok=True)
        fn = it["filename"]
        dst = d / fn
        rel = str(dst.relative_to(ROOT))
        url = MP3 + urllib.parse.quote(fn)
        print(f"[{i}/{len(chosen)}] {bucket:9s} {it['title'][:30]:30s} bpm={it.get('bpm')}", flush=True)
        if not fetch(url, dst, timeout=180):
            continue

        old = cached.get(rel)
        if old:
            a = {k: old[k] for k in ("duration", "bpm", "beat_cv", "energy", "beats")}
            print(f"    复用分析结果 {a['duration']:.0f}s bpm={a['bpm']}", flush=True)
        else:
            try:
                a = analyze(dst)
            except Exception as e:
                print(f"    分析失败，跳过: {type(e).__name__}: {e}", flush=True)
                dst.unlink(missing_ok=True)
                continue
            print(f"    ok {a['duration']:.0f}s bpm={a['bpm']} beat_cv={a['beat_cv']}", flush=True)

        tracks.append({
            "id": f"{bucket}_{Path(fn).stem.lower().replace(' ', '_')}",
            "title": it["title"],
            "bucket": bucket,
            "tags": [f.strip() for f in (it.get("feel") or "").split(",") if f.strip()],
            "file": rel,
            "duration": a["duration"],
            "bpm": a["bpm"],
            "beat_cv": a["beat_cv"],
            "energy": a["energy"],
            "beats": a["beats"],
            "desc": (it.get("description") or "")[:120],
            "attribution": f"{it['title']} — Kevin MacLeod (incompetech.com), CC BY 4.0",
        })

    index = {
        "version": 2,
        "source": "incompetech.com",
        "license": "CC BY 4.0（商用需署名，attribution 字段即署名串）",
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tracks": tracks,
    }
    out_path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(t["duration"] for t in tracks)
    print(f"\n完成：{len(tracks)} 首，共 {total/60:.1f} 分钟，索引 {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
