# 找剧与下载

流水线的前半段。目标：**拿到带音轨、480p、前 700 秒**的素材，文件名带剧名。
这一段最容易出错，出错的表现往往不是下载失败，而是「下成功了但没声音」。

## 1. 找剧

用 WebSearch，抓最近一周的热榜。查询词直接抄：

```
2026年11月 短剧 热度榜
抖音短剧 本周 排行
dataeye 短剧 榜 周榜
红果短剧 热播榜
```

挑片标准：

- **近期上线/在榜**，不要拿几年前的老剧。
- **剧情冲突明确**：复仇、逆袭、豪门、斗法、扮猪吃虎。纯甜宠缺少可剪的对峙，切片撑不住 40 秒。
- **单集合适**：2-10 分钟最好。超过 10 分钟要先分段（见 SKILL.md 步骤 9）。
- **平台可下**：YouTube 上大量短剧频道会把整季合成长视频，最适合；国内平台基本下不动。

给用户一张候选表，带一句看点说明，再确认数量。不要闷头下 20 部。

## 2. 工具

```bash
# 用 yt-dlp，不要用 yoinks（Ink TUI，只能手点，无法脚本化）
command -v yt-dlp || brew install yt-dlp
yt-dlp --version          # 2025 年之后的版本才稳，太旧会报 format not available
```

如果系统里同时有多个 yt-dlp，明确用绝对路径（例如 `~/.local/bin/yt-dlp`），
避免 PATH 里那个陈旧版本被调用。

## 3. 先探再下

```bash
yt-dlp "ytsearch5:十八岁太奶奶驾到 短剧" --flat-playlist \
  --print "%(title)s | %(id)s | %(duration)s"
```

确认标题、时长、是不是合集。时长 600-3000 秒的多半是整季合集，正合用。

## 4. 下载（视频流 + 音频流分开，再合并）

**为什么不能一条命令搞定**：`-f "bv*[height<=480]/b"` 会挑到「纯视频流」
（`bv*` 的优先级高于合成流），下完没有音轨。更糟的是某些频道的 480p 只有
video-only 的 DASH 流，`/b` 兜底也不一定命中带音轨的版本。于是成片「只有配乐没人声」，
流水线判 `has_audio=false`、QC 全灭。

正确做法是分两条流各下一次，再用 ffmpeg `-c copy` 合并（不重编码，几秒钟）：

```bash
D=~/Downloads/热门短剧素材
NAME="某剧名"
mkdir -p "$D"

# 1) 视频流（135 = 480p mp4 video only）
yt-dlp -f 135 --download-sections "*0-700" --no-part \
  -o "$D/$NAME.v.mp4" "<URL>"

# 2) 音频流（140 = m4a）
yt-dlp -f 140 --download-sections "*0-700" --no-part \
  -o "$D/$NAME.a.m4a" "<URL>"

# 3) 合并（视频取 0:v，音频取 1:a）
ffmpeg -v error -y -i "$D/$NAME.v.mp4" -i "$D/$NAME.a.m4a" \
  -map 0:v -map 1:a -c copy "$D/$NAME.mp4"
rm -f "$D/$NAME.v.mp4" "$D/$NAME.a.m4a"
```

要点：

- `--download-sections "*0-700"` 只取前 700 秒，够出 3-6 条切片，别下整季。
- `--no-part` 关掉分片临时文件，避免沙箱里改名被拦。
- **不要用 yt-dlp 的自动合并**（`--merge-output-format` / `+ba`）：它需要把临时文件
  改名，宿主策略拒绝时会整体失败，留下半个文件。
- 拿不到 135 时降级：`-f 18` 是 360p 的合成流（自带音轨），一条命令即可，画质差一档。
- 480p 都没有就只有 240p（`-f 134` 之类），画质明显糊，能换源就换源。

## 5. 必须验

```bash
for f in "$D"/*.mp4; do
  echo "== $(basename "$f")"
  ffprobe -v error -select_streams a -show_entries stream=codec_name -of csv=p=0 "$f"
  ffprobe -v error -show_entries format=duration -of csv=p=0 "$f"
  ffprobe -v error -select_streams v -show_entries stream=width,height -of csv=p=0 "$f"
done
```

判据：

- 音频行有输出（`aac` / `opus`）→ 有音轨。**空白 = 没有音轨，必须重下。**
- 时长 ≈ 700 秒（`700.033` 这种是正常的容器对齐误差）。
- 分辨率 ≥ 640×360；240p 的糊到字都看不清，字幕 OCR 会失效。

再抽一帧听不出来的问题靠音量判：

```bash
ffmpeg -v error -i "$D/某剧.mp4" -map 0:a -af volumedetect -f null - 2>&1 | grep mean_volume
# 期望 -30dB ~ -10dB；接近 -91dB 说明音轨是静音的
```

## 6. 下载失败的常见原因

| 现象 | 处理 |
|---|---|
| `Requested format is not available` | yt-dlp 太旧，或该视频没有该清晰度。换 `-f 18` / `-f best` |
| `HTTP Error 403` | 直链过期。改用 `--no-part` 重试，或加 `--extractor-args "youtube:player_client=web"` |
| 下到 0 字节 / 几 MB 就停 | 触发风控。降速 `--limit-rate 2M`，或隔几分钟重试 |
| `Brokered host rename refused` | 宿主沙箱拦改名。加 `--no-part`，或直接输出成 `.part` 不管它 |
| 有画面没声音 | 走第 4 节的分流下载 + `-c copy` 合并，别指望 yt-dlp 自动合并 |
| 视频能放但流水线 `probe failed` | 容器头损坏。`ffmpeg -c copy` 重新封装一次即可 |

## 7. 素材合规

下载的网络剧集**只能用于验证流程**，不能商用投放。批量产出的成片如果要对外发布，
必须换成有授权的素材。这条要写进交付说明里，不要含糊。
