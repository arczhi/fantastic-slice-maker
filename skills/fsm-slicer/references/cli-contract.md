# CLI 与数据契约

给 agent 用的完整接口说明。日常操作看 `../SKILL.md`，需要精确字段时看这里。

## 命令

```
fsm [--config PATH] [--verbose] <子命令>

fsm run    <video>            [--reels N] [--force] [--rerender] [--no-asr] [--no-ocr] [--no-llm] [--no-bgm]
fsm batch  <inputs...>        [--workers N] [--limit N] [--reels N] [同上开关]
fsm watch  <inputs...>        [--interval 60] [--reels N]
fsm doctor
fsm bgm
```

`inputs` 可以是目录（递归收集 `.mp4/.mov/.mkv/.avi/.flv/.webm/.ts/.m4v`）或文件列表。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 正常结束。`run`：`report.ok` 为真。`batch`：至少一部没崩（**不代表全部成功**） |
| 1 | 有失败。`run`：该剧失败或 0 条成片。`batch`：全部失败 |

成败判定读 JSON 里的 `ok` / `succeeded` / `failed`，不要只看退出码。

## stdout

### `fsm run`

```json
{
  "ok": true,
  "video": "/abs/path/ep.mp4",
  "error": null,
  "strategy": "llm",
  "reels": [
    { "title": "...", "path": "/abs/out/.../01_xxx.mp4", "duration": 43.72, "qc": true }
  ]
}
```

### `fsm batch`

```json
{
  "ok": true,
  "finished_at": "2026-11-19T09:40:24+08:00",
  "videos": 10,
  "succeeded": 10,
  "failed": 0,
  "reels": 10,
  "elapsed_seconds": 549.4,
  "avg_seconds_per_video": 54.9,
  "workers": 3,
  "report_path": "out/batches/batch_20261119-094024.json",
  "results": [
    {
      "video": "/abs/path/a.mp4",
      "ok": true,
      "reels": 1,
      "strategy": "llm",
      "error": null,
      "outputs": ["/abs/out/2026-11-19/a_xxx/01_yyy.mp4"]
    }
  ]
}
```

失败条目：`ok:false`，`error` 形如 `PermissionError: ...` / `planner produced no reel`，`outputs` 为空数组。

每次批量还会在 `out/batches/batch_<时间戳>.json` 落一份同样的完整记录。

## report.json

位置：`out/<日期>/<video_id>/report.json`。同日另有 `out/<日期>/index.html` 总览页。

```jsonc
{
  "ok": true,
  "video": "/abs/path/ep.mp4",
  "video_id": "ep_a1b2c3d4",          // 原名 + 路径 hash 前 16 位
  "duration": 237.74,
  "resolution": "1280x720",
  "strategy": "llm",                   // llm | fallback
  "plan_error": null,                  // 非空表示首次调用失败后重试成功
  "stats": {
    "shot_count": 65, "unit_count": 24, "sentence_count": 15,
    "subtitle_count": 42, "blank_ratio": 0.0074, "motion_p95": 38.38
  },
  "candidate_count": 11,               // 进入大模型的候选数
  "reel_count": 1,
  "reels": [{
    "index": 1,
    "ok": true,
    "title": "叛徒已擒！挡我者死",
    "hook": "叛徒已擒挡我者",
    "reason": "用擒叛冲突开场，接敌方轻敌与不安，再以放话对峙推高潮，结尾留撤退悬念。",
    "path": "/abs/out/2026-11-19/ep_a1b2c3d4/01_叛徒已擒！挡我者死.mp4",
    "cover": "/abs/.../01_叛徒已擒！挡我者死.jpg",
    "duration": 43.72,
    "strategy": "llm",
    "beats": [
      { "role": "hook",   "src_start": 120.0, "src_end": 129.872, "dur": 9.872, "text": "..." },
      { "role": "rise",   "src_start": 138.067, "src_end": 147.099, "dur": 9.032, "text": "..." },
      { "role": "climax", "src_start": 186.567, "src_end": 191.870, "dur": 5.303, "text": "..." },
      { "role": "cliff",  "src_start": 211.1,   "src_end": 220.0,   "dur": 8.900, "text": "..." }
    ],
    "bgm": {
      "id": "epic_movement_proposition", "title": "Movement Proposition",
      "start": 21.15, "bpm": 83.4, "mixed": true,
      "attribution": "Movement Proposition — Kevin MacLeod (incompetech.com), CC BY 4.0"
    },
    "beat_align": {
      "aligned": true, "beats_total": 5, "beats_on_grid": 3,
      "stretch_seconds": 0.68, "max_stretch_used": 0.33,
      "mean_residual": 0.37, "bpm": 83.4, "beat_cv": 0.061
    },
    "qc": {
      "ok": true, "issues": [],
      "duration": 43.74, "resolution": "1080x1920",
      "mean_volume_db": -15.1, "black_segments": 0
    }
  }],
  "elapsed_stages": {
    "probe":  { "duration": 237.735, "size": "1280x720", "fps": 30.0, "status": "ok" },
    "shots":  { "status": "ok", "elapsed": 6.85, "updated_at": "..." },
    "vision": { "status": "ok", "elapsed": 7.07 },
    "ocr":    { "status": "ok", "elapsed": 62.67 },
    "asr":    { "status": "ok", "elapsed": 120.23 },
    "timeline": { "status": "ok", "elapsed": 0.04 },
    "recall":   { "status": "ok", "elapsed": 0.04 },
    "plan":     { "status": "ok", "elapsed": 3.2 },
    "render_1": { "status": "ok", "elapsed": 29.8 },
    "mix_1":    { "status": "ok", "elapsed": 1.6 }
  }
}
```

`beats[].role` 取值：`hook` / `setup` / `rise` / `climax` / `cliff`。`src_start` / `src_end` 是**原片时间轴**，不是成片时间轴。

`qc.issues` 可能值：`黑场 N 处，最长 Xs`、`太短: Xs`、`响度过低`。

## 中间产物

`work/<video_id>/` 下按阶段落盘，是断点续跑的依据：

| 文件 | 内容 |
|---|---|
| `status.json` | 各阶段状态与耗时 |
| `shots.json` | 镜头边界 |
| `vision.json` | 采样帧的运动/亮度/空镜统计 |
| `asr.json` / `asr_raw.json` | 切句结果 / 原始转写 |
| `ocr.json` | 字幕条（文本 + 时间） |
| `timeline.json` | 语义单元 |
| `recall.json` | 候选与打分 |
| `plan.json` | DeepSeek 原始剪辑单 |
| `reel01/` | 单条成片的切片与渲染中间文件 |

删掉即可重算。`--force` 忽略缓存全部重算，`--rerender` 只重跑 `reel*` 之后的部分。

## 配置字段

`config.yaml`（入库，默认值）→ `config.local.yaml`（本地私有，gitignore）→ `--config` 指定的文件，后者覆盖前者。环境变量优先级最高。

```yaml
workers: 3

asr:
  enabled: true
  python: ""              # FunASR 解释器；留空自动探测，或用 FUNASR_PYTHON
  model: paraformer-zh
  device: cpu
  batch_size_s: 120
  timeout: 1800
  server_port: 8788
  use_server: true

ocr:
  enabled: true
  backend: ppocr
  det_model: ""           # 留空用 rapidocr 自带 PP-OCRv4
  rec_model: ""
  cls_model: ""
  fps: 2.0                # 抽帧频率，降到 1.5 省 25%
  band_top: 0.42          # 字幕带起始位置（占帧高比例）
  change_threshold: 5.0   # 相邻帧差异小于此值则复用上次识别
  min_confidence: 0.55

vision:
  fps: 4.0
  width: 160
  height: 90
  use_pyscene: true
  adaptive_threshold: 3.0
  scene_threshold: 0.28   # 仅 ffmpeg 兜底时使用
  min_shot: 0.7
  max_shot: 12.0
  blank_std: 6.0

recall:
  candidates: 32          # 进模型的候选上限
  min_beat: 2.0
  max_beat: 8.0
  head_skip: 0.02         # 跳过片头比例
  tail_skip: 0.06         # 跳过片尾比例

planner:
  enabled: true
  base_url: "https://api.deepseek.com/v1"
  model: deepseek-chat
  api_key_env: DEEPSEEK_API_KEY
  temperature: 0.7
  max_tokens: 2400
  timeout: 180
  retries: 3
  target_seconds: 52
  min_seconds: 32
  max_seconds: 95

render:
  width: 1080
  height: 1920
  fps: 30
  crf: 20
  preset: veryfast
  loudness_i: -14
  blur_sigma: 24
  audio_bitrate: 192k

bgm:
  enabled: true
  bgm_lufs: -20.0
  duck_threshold: 0.15
  duck_ratio: 2.5
  fade_in: 0.4
  fade_out: 1.2
  max_stretch: 0.45        # 节拍对齐最多延长多少秒
  min_beat_cv: 0.22        # 节拍不稳定的曲子跳过对齐

paths:
  work: "work"
  out: "out"
  log: "logs"
```

## 环境变量

| 变量 | 作用 |
|---|---|
| `DEEPSEEK_API_KEY` | 必填，模型凭证 |
| `FSM_WORKERS` | 覆盖并发数 |
| `FUNASR_PYTHON` | FunASR 解释器路径 |

## 日志

`logs/fsm.jsonl` 是结构化日志（每行一个 JSON：`ts` / `level` / `logger` / `msg`），`logs/asr-server.log` 是 ASR 服务输出。排查问题时先看这两份。
