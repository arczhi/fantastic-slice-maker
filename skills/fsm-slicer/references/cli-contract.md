# CLI 与数据契约

给 agent 用的完整接口说明。日常操作看 `../SKILL.md`，需要精确字段时看这里。

## 命令

```
fsm [--config PATH] [--verbose] <子命令>

fsm run    <video>            [--total N | --variants V | --reels N] [--force] [--rerender] [--no-asr] [--no-ocr] [--no-llm] [--no-bgm] [--subtitle auto|on|off] [--max-load X] [--min-free-mem G] [--nice N]
fsm batch  <inputs...>        [--workers N] [--limit N] [同上全部开关]
fsm watch  <inputs...>        [--interval 60] [同上全部开关]
fsm doctor
fsm bgm
```

`inputs` 可以是目录（递归收集 `.mp4/.mov/.mkv/.avi/.flv/.webm/.ts/.m4v`）或文件列表。

### 出片数量三选一

| 参数 | 语义 | 例 |
|---|---|---|
| `--total N` | 直接要 N 条，自动拆套（单套 ≤ `output.per_plan`） | 6 → `[3, 3]`；7 → `[3, 2, 2]` |
| `--variants V` | V 套方案，每套 `--reels` 条 | `--variants 3 --reels 2` → 6 条 |
| `--reels N` | 1 套 N 条 | `--reels 3` → 3 条 |

优先级 `--total` > `--variants` > `--reels` > 配置默认（`output.reels` × `output.variants`）。
套与套之间强制去重：hook 段不复用，候选重合率 ≥ `output.overlap`（默认 0.6）驳回重出。


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
  "variants": 2,
  "reels_per_variant": [3, 3],
  "reels": [
    { "variant": 1, "index": 1, "title": "...", "path": "/abs/out/.../v01-01_xxx.mp4",
      "duration": 43.72, "qc": true }
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
  "workers": 2,
  "gate_wait_seconds": 0.0,
  "resource": { "cpu_cores": 10, "load1": 2.58, "load_per_core": 0.26, "free_mem_gb": 6.6 },
  "reels_plan": { "total": 6 },
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
  "has_audio": true,                   // false = 源片无音轨，此时 ASR 无输入、成片只有配乐
  "warnings": [],                      // 非空要原样转告用户，例如「源视频无音轨」
  "strategy": "llm",                   // llm | fallback
  "plan_error": null,                  // 非空表示首次调用失败后重试成功
  "stats": {
    "shot_count": 65, "unit_count": 24, "sentence_count": 15,
    "subtitle_count": 42, "blank_ratio": 0.0074, "motion_p95": 38.38
  },
  "candidate_count": 11,               // 进入大模型的候选数
  "autosub": {                          // 自动补字幕的判定结果
    "needed": false,                    // 源片自带字幕 → false，成片保持原样
    "coverage": 0.358,                  // OCR 字幕覆盖率（有字幕片 20%-78%，无字幕片 <5%）
    "reason": "源片自带字幕（覆盖率 35.8%）"
  },
  "reel_count": 2,
  "variants": 2,
  "reels_per_variant": [3, 3],
  "reels": [{
    "index": 1,
    "variant": 1,
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
    "subtitle": { "auto": false, "count": 0 },   // 该条是否补了字幕、补了几条
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
| `plan_v01.json` … | 每套方案的剪辑单（含指纹；候选/条数/已用 hook 变化即失效） |
| `v01/reel01/` | 该套该条成片的切片与渲染中间文件 |

删掉即可重算。`--force` 忽略缓存全部重算，`--rerender` 只重跑 `reel*` 之后的部分。

## 配置字段

`config.yaml`（入库，默认值）→ `config.local.yaml`（本地私有，gitignore）→ `--config` 指定的文件，后者覆盖前者。环境变量优先级最高。

```yaml
workers: 3

asr:
  enabled: true
  python: ""              # FunASR 解释器；留空自动探测，或用 FUNASR_PYTHON
  engine: sensevoice      # sensevoice（快一倍+情感标签）| paraformer（原生句级时间戳）
  model: paraformer-zh
  device: cpu
  ncpu: 0                 # 0=自动取性能核数。SenseVoice 超过性能核数反而变慢
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
  det_limit_side_len: 480 # 检测输入长边；736→480 快一倍且字幕仍准
  threads: 3              # ONNX 线程配额。不限会占 5.8 核；限 3 只慢 3% 却省 40%

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
  crf: 22
  preset: ultrafast       # 批量优先速度；要画质改 veryfast/superfast（crf 各加 2）
  loudness_i: -14
  blur_sigma: 24
  audio_bitrate: 192k
  threads: 3              # 单段 x264 线程数，与 workers 相乘别超核数
  blur_div: 4             # 模糊层在 1/N 分辨率上做再放大；1=全分辨率（更耗 CPU）
  encoder: auto           # auto=硬编+软编双池 | libx264 | h264_videotoolbox
  cpu_slots: 2            # 软编并发槽位（吃 CPU）
  gpu_slots: 2            # 硬编并发槽位（走媒体引擎，不占 CPU）
  vt_bitrate: 6M          # 硬编目标码率；实测常打不满，体积反而更小

output:                   # 出片数量与跨套去重
  reels: 2
  variants: 1
  per_plan: 3             # 指定总条数时单套最多几条
  max_total: 12
  dedupe_hook: true
  overlap: 0.6

resource:                 # 跑批资源闸门
  max_workers: 0          # 0 = 按核数与内存自动推算
  cpu_per_worker: 3
  mem_per_worker_gb: 2.5
  min_free_mem_gb: 3.0
  max_load_per_core: 1.2
  nice: 5
  gate_max_wait: 180

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
