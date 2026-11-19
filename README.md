# fantastic-slice-maker

漫剧高光切片全自动流水线。输入一条剧集视频，输出 45-60 秒竖屏切片（含配乐、封面、质检报告），可直接投放抖音/快手/视频号。

四路信号并行提取 → 规则召回候选 → DeepSeek 出剪辑单 → FFmpeg 执行并卡点配乐。全程无交互，一条命令跑完一批。

```bash
./bin/fsm batch ~/Videos/漫剧素材 --reels 2 --workers 3
```

```
视频
 ├─ 四路信号（并行）
 │   ├─ 镜头切分     pyscenedetect AdaptiveDetector（ffmpeg scene 兜底）
 │   ├─ ASR          FunASR Paraformer → 字符级时间戳 → 切句
 │   ├─ 字幕 OCR     PP-OCRv4 → 画面硬字幕 + 时间
 │   └─ 视觉         帧差运动 / 中心度 / 亮度 / 空镜判定
 ├─ 结构化时间轴     多路对齐到 shot，合并成语义单元
 ├─ 规则召回         信息密度 × 情绪词 × 运动 × 台词 → 候选池
 ├─ DeepSeek 剪辑单  hook / setup / rise / climax / cliff，严格 JSON + 校验 + 兜底
 ├─ DeepSeek 选配乐  按情绪从本地曲库选曲 + 起始位置
 └─ FFmpeg 执行      切片 → 竖屏 → 响度归一 → 节拍对齐 → 混音 → 拼接 → 质检
```

## 快速开始

前置：`ffmpeg` / `ffprobe` 在 PATH 里，Python 3.11+。

```bash
git clone https://github.com/arczhi/fantastic-slice-maker.git
cd fantastic-slice-maker

python3 -m venv .venv
.venv/bin/pip install -e .

./bin/fsm doctor                                    # 环境自检，先看这里
export DEEPSEEK_API_KEY=sk-你的key
./bin/fsm run /path/to/episode.mp4 --reels 2
```

成片在 `out/<日期>/<video_id>/`，同目录有 `report.json` 和封面。

`doctor` 里 `FunASR env`、`PP-OCR` 两行显示 missing 也能跑：ASR 缺失时自动用 OCR 字幕替代，OCR 缺失时用 ASR 台词替代，两路都缺失退化为纯视觉选片。想加 ASR 就装 FunASR（需要独立的 py3.9 环境），并用 `FUNASR_PYTHON` 或 `asr.python` 指过去。

不写代码也想调参数，就在仓库根放一个 `config.local.yaml`（已 gitignore），只写差异：

```yaml
planner:
  api_key: "sk-你的key"
```

## 给 Agent 用

这个仓库的对外接口就是 CLI：不读 stdin、不弹交互、不依赖 TTY。任何 agent 都能直接调。

### 三条契约

1. **stdout 只有一份 JSON 摘要**，人读日志走 stderr。直接 `json.loads(stdout)` 即可，不用清洗。
2. **退出码 0 只代表「没有整体崩」**。`batch` 的 `ok` 是 `failed < videos` 的意思——10 部里挂 1 部仍返回 0。判断成败必须读 `succeeded` / `failed` / `results[].ok`。
3. **幂等可续跑**。每个阶段的结果落在 `work/<video_id>/`，重复调用会跳过已完成阶段；`--force` 全重算，`--rerender` 只重跑渲染与质检。

### 最短接入

```bash
# 1. 装环境（一次性）
git clone https://github.com/arczhi/fantastic-slice-maker.git && cd fantastic-slice-maker
python3 -m venv .venv && .venv/bin/pip install -e .

# 2. 自检：任何一项 missing 都会打印原因，不阻断
./bin/fsm doctor

# 3. 注入 key（唯一必需的外部凭证）
export DEEPSEEK_API_KEY=sk-...

# 4. 单条
./bin/fsm run /abs/path/to/ep.mp4 --reels 2

# 5. 批量
./bin/fsm batch /abs/path/to/dir --reels 2 --workers 3
```

### 跑完读什么

| 想知道的 | 读哪里 |
|---|---|
| 这批成了几条、哪条挂了 | `batch` 的 stdout JSON：`succeeded` / `failed` / `results[]` |
| 单条成片路径 | `results[].outputs[]`，或 `report.json` 的 `reels[].path` |
| 剪辑理由（可做文案素材） | `report.json` 的 `reels[].title` / `hook` / `reason` / `beats[]` |
| 质量是否过关 | `report.json` 的 `reels[].qc.ok`，附 `issues[]` |
| 时间花在哪 | `report.json` 的 `elapsed_stages` |
| 失败原因 | `results[].error`，形如 `PermissionError: ...` |

`report.json` 结构（节选）：

```json
{
  "ok": true,
  "video": "/abs/path/ep.mp4",
  "video_id": "ep_a1b2c3d4",
  "duration": 237.74,
  "strategy": "llm",
  "stats": { "shot_count": 65, "unit_count": 24, "sentence_count": 15,
             "subtitle_count": 42, "blank_ratio": 0.0074 },
  "reels": [{
    "index": 1, "ok": true,
    "title": "叛徒已擒！挡我者死",
    "hook": "叛徒已擒挡我者",
    "reason": "用擒叛冲突开场，接敌方轻敌与不安，再以放话对峙推高潮。",
    "path": "/abs/out/2026-11-19/ep_a1b2c3d4/01_叛徒已擒！挡我者死.mp4",
    "cover": "...jpg", "duration": 43.72,
    "beats": [{ "role": "hook", "src_start": 120.0, "src_end": 129.9,
                "dur": 9.87, "text": "哼新息机可还未归，岂能高枕无忧？" }],
    "bgm": { "id": "epic_movement_proposition", "bpm": 83.4,
             "aligned": true, "attribution": "..." },
    "qc": { "ok": true, "issues": [], "resolution": "1080x1920",
            "mean_volume_db": -15.1, "black_segments": 0 }
  }],
  "elapsed_stages": { "asr": { "elapsed": 120.2 }, "ocr": { "elapsed": 62.7 } }
}
```

`strategy` 有三种状态：`llm`（DeepSeek 正常出单）、`fallback`（模型不可用或输出不合规，走规则兜底）、`llm` 但 `plan_error` 非空（首次失败后重试成功）。`fallback` 的切片只保证「片段好看」，不保证「剧情连贯」。

### 给 Agent 的建议

- **不要一上来 `--force`**。默认的断点续跑能省掉大部分时间；只有改了 `config.yaml` 里的信号参数才需要 `--force`。
- **批量并发别超过 3**。ASR 服务内部是串行的，`--workers` 再高只会让各阶段互相抢核，实测反而更慢。
- **别用 `watch` 做一次性任务**。它是常驻进程，agent 调用会永久阻塞。
- **长素材先估时间**再加超时，见下面的「性能与容量」。
- 仓库内附了一个 skill（`skills/fsm-slicer/`），把整个流程封装成可被 agent 直接加载的操作手册，见该目录的 `SKILL.md`。

## 命令

```bash
./bin/fsm doctor                                    # 环境自检
./bin/fsm run <video> [--reels N]                   # 单条
./bin/fsm batch <dir|files...> [--workers N] [--limit N]
./bin/fsm watch <dir> [--interval 60]               # 常驻监听新片
./bin/fsm bgm                                       # 构建/更新本地 BGM 曲库
```

通用参数：

| 参数 | 作用 |
|---|---|
| `--config PATH` | 额外配置层（默认已叠加 `config.yaml` + `config.local.yaml`） |
| `--reels N` | 每部剧出几条切片，默认 2 |
| `--force` | 忽略 `work/` 中间产物，全部重算 |
| `--rerender` | 只重跑渲染与质检，复用信号与剪辑单 |
| `--limit N` | 批量时只跑前 N 部（调试用） |
| `--no-llm` | 跳过 DeepSeek，纯规则排序出片 |
| `--no-asr` / `--no-ocr` / `--no-bgm` | 关掉对应环节 |

## 设计决策

**竖屏用模糊背景而不是裁切**
原片的中文字幕是烧死在画面里的，任何放大裁切都会切掉字幕。所以取原比例居中、上下用自身模糊填充，字幕 100% 保留。

**OCR 用 PP-OCRv4 而不是 Unlimited-OCR**
Unlimited-OCR 是 3B MoE 文档模型，最佳部署是 vLLM + CUDA。按 0.5 秒一帧处理一天几十小时素材是小时级开销。字幕只是白字黑边的台词条，PP-OCRv4 det+rec ONNX 单帧约 90ms。另外两个实测结论：裁字幕带反而更慢（det 会把小图放大到固定边长），所以送整帧再按框位置过滤；相邻帧字幕不变时复用上次结果，实际引擎调用量约为总帧数的 20-40%。

**ASR 走常驻服务**
`AutoModel` 单次加载约 73 秒，推理 4 分钟音频只要 13 秒。每部剧各加载一次会白烧半小时。服务（`scripts/asr_server.py`）只加载一次，batch 期间所有剧共用。

**切点吸附镜头边界**
ASR 给的是「这句话 12.4-15.8s」，但观众感知的是镜头。切在句子边界但镜头中间，会切在人脸或动作一半。`render.snap_to_shots` 只往更靠内吸附，只会让片段略长，不会切掉台词。

**hook 必须带台词**
实测模型爱挑「画面最炸但一句台词都没有」的段落开场。抖音前 3 秒需要一句话让观众知道发生了什么，所以 `planner._enforce_hook` 会强制换成有台词的段。

**镜头切分不用 ffmpeg 单一 scene 阈值**
在这批漫剧上标定过：真硬切约 150 个，而 `scene>0.28` 只检出 48 个（召回 17%-56%）。烧死字幕、火光、快速挥刀都会拉高帧差，单阈值只能取折中值。改用 `AdaptiveDetector` 后《辛弃疾1》从 48 个镜头涨到 65 个。

**BGM 按响度归一而不是固定衰减**
各首曲子出厂响度差很远（实测 -42dB 到 -16dB），固定 -14dB 会让安静的那几首几乎听不见。用 `loudnorm` 把 BGM 归到 -20 LUFS（人声 -14），再叠加上去，总响度只增 0.5-0.7 LU。

**混音放在拼接之后**
每个 clip 单独混会各自触发一次 fade-in，听感是「一段一段」的；拼接后混一次，才是「一条连续配乐」。

**节拍对齐只延长不缩短**
缩短片段会切掉台词尾部，这在漫剧里是致命的。所以只把片段尾部延长到下一个鼓点，上限 0.45 秒，且延长区间内必须是有效画面。节拍不稳定的曲子（`beat_cv > 0.22`）直接跳过对齐。

## 性能与容量

实测环境：Apple M5 / 10 核 / 32GB，`--workers 3`，素材 1280×720。

| 项目 | 数值 |
|---|---|
| 10 部短剧（素材共 549s）冷启动整批 | 694s（含 ASR 模型加载 74.7s） |
| 单部 4 分钟剧，冷启动 | 约 160s |
| 单部 1 分钟剧，冷启动 | 约 90s |
| 阶段中位耗时 | ASR 58.6s / OCR 91.6s / 镜头+运动 20.9s / 出单 2.6s / 渲染 29.7s / 混音 4.3s |
| 渲染速度 | 1080×1920 veryfast，约 0.7 倍成片时长 |

**吞吐大致等于 0.8-1.3 倍素材总时长**，短素材偏高（固定开销摊不开），长素材偏低。10 小时满载约能处理 400-500 分钟素材。

瓶颈顺序：OCR ≈ ASR > 渲染 > 镜头切分 > DeepSeek。DeepSeek 只占单部 2-6 秒，加机器没用；想提速先动 OCR 抽帧率（`ocr.fps` 从 2 降到 1.5 省 25%）和 ASR 环境（换 GPU 或 funasr-onnx）。

## 适用范围与限制

**这套流水线擅长**：已经分好集的短剧（每集 1-4 分钟），批量出 30-60 秒切片。这个尺度上「一集」约等于「一个完整高光弧」，规则召回足够覆盖。

**不适合直接喂 40 分钟以上的整集**。原因是召回阶段的采样密度会随时长衰减：`recall.py` 的非极大抑制间隔是 `max(6, duration/16)`，45 分钟的片子等于每 169 秒才有一个候选点，全片只剩 16 个候选，真正的高光（可能就 20 秒）会被挤掉。要用长视频必须先改成分块召回（3 分钟一块，块内取 top-k，再跨块比选），当前版本没做。

其他限制：

- 文本质量取决于 ASR/OCR 准确度。配乐重、口音重、画质差的素材会掉字，此时退化为视觉选片。
- 叙事连贯性靠模型判断，不保证逻辑严密。`strategy=fallback` 时只保证片段好看。
- 节拍对齐只把片段尾部延长到鼓点，没有做严格的「片段时长 = 鼓点整数倍」卡点（那要求按 BPM 反推选片时长，会牺牲台词完整性）。
- 只做硬切，没有转场特效。
- 人脸检测没接入。YuNet 在这批漫剧上检出率只有 23%-31%，噪声大于收益。
- BGM 曲库是通用曲库，没有中国风/古筝/二胡专项。

## 目录

```
src/fsm/
  config.py        配置（dataclass + config.yaml + config.local.yaml + 环境变量）
  ffmpeg.py        所有 ffmpeg 调用（探测/场景/帧流/切片/拼接/黑场/响度）
  signals/         四路信号：shots / asr / ocr / vision
  timeline.py      多路对齐 + 语义单元合并
  recall.py        规则召回打分
  planner.py       DeepSeek 剪辑单（校验、重试、兜底）
  bgm.py           配乐：选曲 / 节拍对齐 / ducking 混音
  render.py        切点吸附、黑场修剪、并行渲染
  qc.py            成片质检
  pipeline.py      单剧编排（阶段落盘 + 断点续跑）
  batch.py         进程池批量 + 目录监听
  digest.py        每日总览页
  cli.py           命令行
scripts/
  asr_server.py    FunASR 常驻服务（用 FunASR 自己的 venv 跑）
  asr_worker.py    一次性转写（服务不可用时回退）
  build_bgm_lib.py 构建本地 BGM 库（下载 + BPM/beat 分析 + 索引）
skills/
  fsm-slicer/      Agent skill：把整条流水线封装成可直接加载的操作手册
assets/
  bgm_index.json   曲目索引：标签 / BPM / beat 时间轴 / 署名串
  bgm/             按情绪分目录的 mp3（不入库，本地构建）
work/<video_id>/   中间产物（可断点续跑，删掉即重算）
out/<日期>/        成片 + report.json + 总览页 index.html
```

## BGM 与授权

曲库不入库（体积大且授权独立）。首次使用自行构建，约 15 分钟、25 首：

```bash
.venv/bin/pip install -e ".[bgm]"
./bin/fsm bgm
./bin/fsm doctor        # 确认 BGM library 一行有数字
```

曲源是 [incompetech.com](https://incompetech.com/music/royalty-free/) 上 Kevin MacLeod 的作品，**CC BY 4.0：商用必须署名**。署名串写在 `bgm_index.json` 的 `attribution` 字段，渲染时会带进 `切片说明.md` 和 `report.json` 的 `bgm.attribution`。投放时请放进视频简介或片尾。

## 配置

改 `config.yaml` 即可。环境变量优先级最高（`DEEPSEEK_API_KEY`、`FSM_WORKERS`、`FUNASR_PYTHON`）。

常用项：

| 配置 | 默认 | 说明 |
|---|---|---|
| `planner.target_seconds` | 52 | 目标成片时长 |
| `recall.candidates` | 32 | 候选数，调大给模型更多选择但费 token |
| `render.preset` | veryfast | 编码速度，追求质量改 `medium` |
| `ocr.fps` | 2.0 | 抽帧频率，降到 1.5 省 25% |
| `bgm.enabled` | true | 关掉等于 `--no-bgm` |
| `workers` | 3 | 同时处理的剧数 |

## License

[Apache License 2.0](LICENSE)，附专利授权条款。分发时请保留 [NOTICE](NOTICE)、并在修改过的文件里注明改动。

BGM 曲目不在本协议范围内：它们由 Kevin MacLeod 以 CC BY 4.0 授权，不入库，商用需署名。
