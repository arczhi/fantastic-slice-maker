# fantastic-slice-maker

漫剧高光切片全自动流水线。输入一条剧集视频，输出 45-60 秒竖屏切片（含配乐、封面、质检报告），可直接投放抖音/快手/视频号。

四路信号并行提取 → 规则召回候选 → DeepSeek 出剪辑单 → FFmpeg 执行并卡点配乐。全程无交互，一条命令跑完一批。

```bash
./bin/fsm batch ~/Videos/漫剧素材 --total 3 --workers 2
```

同一部剧可以出多套互不重复的方案：`--total 6` 会拆成两套、跨套不复用 hook，得到 6 条真正不同的切片。

```
视频
 ├─ 四路信号（并行）
 │   ├─ 镜头切分     pyscenedetect AdaptiveDetector（ffmpeg scene 兜底）
 │   ├─ ASR          SenseVoice-Small → 字符级时间戳 → 切句（附情感/音频事件）
 │   ├─ 字幕 OCR     PP-OCRv4 → 画面硬字幕 + 时间
 │   └─ 视觉         帧差运动 / 中心度 / 亮度 / 空镜判定
 ├─ 结构化时间轴     多路对齐到 shot，合并成语义单元
 ├─ 规则召回         信息密度 × 情绪词 × 运动 × 台词 → 候选池
 ├─ DeepSeek 剪辑单  hook / setup / rise / climax / cliff，严格 JSON + 校验 + 兜底
 ├─ DeepSeek 选配乐  按情绪从本地曲库选曲 + 起始位置
 └─ FFmpeg 执行      切片 → 竖屏 → 响度归一 → 节拍对齐 → 混音 → 拼接 → 质检
```

## 模式

- `--mode clip`：整段原片切片（原声 + 原硬字幕）。
- `--mode commentary`：解说形态——原创文字卡口播 + ≤5 秒引用片段，音轨重做。
- `--mode overlay`：解说字幕叠原片——画面全程原片（配 `render.dewatermark` 去水印），
  口播 TTS 压在压低的原声上，解说字幕按口播时长切成字幕条叠加在原画面底边，
  没有独立的文字卡画面。拼接合集的多处台标用 `render.dewatermark.boxes` 多框 delogo。


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

`doctor` 里 `FunASR env`、`PP-OCR` 两行显示 missing 也能跑：ASR 缺失时自动用 OCR 字幕替代，OCR 缺失时用 ASR 台词替代，两路都缺失退化为纯视觉选片。想加 ASR 就装 FunASR（需要独立的 py3.9 环境），并用 `FUNASR_PYTHON` 或 `asr.python` 指过去。默认引擎 SenseVoice 需要先拉一次权重（约 900MB）：

```bash
~/coding/FunASR/.venv/bin/python -c "from modelscope import snapshot_download; snapshot_download('iic/SenseVoiceSmall')"
```

没拉也能跑——会自动回退到 `paraformer`，只是慢一倍、没有情感标签。

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
- **并发交给流水线自己算**。默认按「核数 / 可用内存」推算安全并发（10 核 32GB 上落在 2-3），每完成一部才补下一部，负载或内存越线会暂停派活，子进程统一降 nice。要更保守用 `--workers 1 --max-load 0.8 --min-free-mem 6`，不要靠 `--workers 8` 抢时间（ASR 服务内部串行，多开只会互相抢核）。
- **别用 `watch` 做一次性任务**。它是常驻进程，agent 调用会永久阻塞。
- **长素材先估时间**再加超时，见下面的「性能与容量」。
- 仓库内附了一个 skill（`skills/fsm-slicer/`），把「找剧 → 下载 → 切片 → 交付」整个流程封装成可被 agent 直接加载的操作手册，见该目录的 `SKILL.md`。

## 命令

```bash
./bin/fsm doctor                                    # 环境自检（含本机安全并发数）
./bin/fsm run <video> [--total N]                   # 单条，N 条自动拆套
./bin/fsm batch <dir|files...> [--workers N] [--limit N]
./bin/fsm watch <dir> [--interval 60]               # 常驻监听新片
./bin/fsm bgm                                       # 构建/更新本地 BGM 曲库
```

通用参数：

| 参数 | 作用 |
|---|---|
| `--config PATH` | 额外配置层（默认已叠加 `config.yaml` + `config.local.yaml`） |
| `--total N` | 直接要 N 条，自动拆成多套方案（6 → 3+3，7 → 3+2+2） |
| `--variants V` | 出 V 套方案，每套 `--reels` 条 |
| `--reels N` | 每套几条，默认取 `output.reels`（2） |
| `--max-load X` | 单核负载上限，超过暂停派活（默认 1.2） |
| `--min-free-mem G` | 可用内存下限 GB，低于此值暂停派活（默认 3） |
| `--nice N` | 子进程降优先级幅度，默认 5 |
| `--force` | 忽略 `work/` 中间产物，全部重算 |
| `--rerender` | 只重跑渲染与质检，复用信号与剪辑单 |
| `--limit N` | 批量时只跑前 N 部（调试用） |
| `--no-llm` | 跳过 DeepSeek，纯规则排序出片 |
| `--no-asr` / `--no-ocr` / `--no-bgm` | 关掉对应环节 |
| `--mode commentary` | 出「解说形态」：原创文字卡口播 + ≤5 秒引用片段（见下节） |
| `--no-tts` | 解说模式不合成配音，只出口播脚本与引用片段 |
| `--quote-max S` | 单条引用最长秒数，默认 5 |
| `--quote-ratio R` | 引用总时长占成片比例上限，默认 0.30 |

## 解说模式：引用片段 + 口播

```bash
./bin/fsm run <video> --mode commentary --reels 1 --no-asr
```

**为什么需要它**：抖音把「原片连续画面 + 原声 + 原硬字幕」判为「以片段拼接/拆条
形式呈现」，B 站判为「网络剧片」。这两条都不是水印问题——去水印、镜像、变速、
换封面改的是画面指纹那一层，而判定走的是形态与资质。平台自己给的出路只有
「本人出镜口播解说、剧情观点梳理」这类原创表达。

所以这个模式把产出从「一段原片」改成「口播主体 + 短引用」：

```
口播卡 → 引用1 → 口播卡 → 引用2 → … → 口播卡（收尾）
```

三条硬约束由程序保证，不交给模型：

| 约束 | 默认 | 为什么 |
|---|---|---|
| 单条引用时长 | ≤ 5 秒 | 版权判定里最硬的一个数；再长就构成「拆条」 |
| 引用总占比 | ≤ 30% | 主体必须是原创表达，不是原片 |
| 音轨 | 口播 TTS + 引用原声 -18dB | 原声仍为主体 = 形态没改 |

结构固定交替，模型只写口播词、不排时间，所以它再离谱也产不出「引用占 80%」
的片子。引用段独立导出、画面内缩到 78% 并垫深色底——视觉上就在说「这是引用」，
也让它没法被当成独立成片直接发。

**产出**（`out/<date>/<video_id>/`）：

```
v01-01_<标题>.mp4          成片（1080x1920，口播音轨）
v01-01_<标题>.jpg          封面
script/v01-01_<标题>.json  分镜 + 时间轴 + 引用区间（程序读）
script/v01-01_<标题>.md    口播词逐句 + 引用清单（人改）
quotes/v01-01/q01_hook.mp4 引用片段，已去水印/内缩/原声压低，可单独当素材
```

`script.md` 可以直接拿去改：换真人出镜就把对应时长的文字卡段替换掉，
口播词和顺序不用动；换配音就重跑同一份词，时间轴按新音频重算。

口播词的硬规则写在提示词里：讲观众看不出来的动机与关系、不复述引用里的台词、
不出现「本片/片段/素材/剪辑/二创/搬运/点赞/关注/平台名」、不涉钱不煽动对立。
命中禁用词会要求重写一次，两次都不行就退化成规则版并在脚本里标 `degraded`——
那种必须人工重写后再发。没有 ASR 时台词取 OCR 硬字幕，所以 `--no-asr` 也能用。

## 设计决策

**竖屏用模糊背景而不是裁切**
原片的中文字幕是烧死在画面里的，任何放大裁切都会切掉字幕。所以取原比例居中、上下用自身模糊填充，字幕 100% 保留。

**模糊层按需降级**
两层优化，改完单条切片的 CPU 从 58.7 秒降到 25.9-37.0 秒（等效核 5.9 → 3.5-4.3），墙钟略快：

1. 竖屏源（480×854、360×640）按宽度铺满后高度正好等于或超过画布，模糊层被前景整块遮住、一个像素都看不到。这种情况直接不建模糊层，只做等比缩放 + 居中裁切。
2. 需要模糊时，把 `gblur` 放到 1/4 分辨率（270×480）上做、`sigma` 同步除以 4，再放大到 1080×1920。模糊本身就把细节抹掉了，这个等价变换肉眼分辨不出——抽同一帧做左右对比确认过。全分辨率 gblur 一项就占单条切片 39% 的 CPU（52.8 → 32.1 CPU 秒中那 20.7 秒）。

`render.blur_div` 控制这个降级倍数（默认 4），设 1 即回到老的全分辨率模糊。

**同一部剧出多套方案，而不是把一套方案重复渲染**
`--total 6` 不会把一份剪辑单渲染六次。它拆成两套独立规划：第二套拿到的是「第一套已用过的 hook 和候选」这一份排除清单，撞 hook 直接驳回，候选重合率 ≥60% 也驳回。代价是多几次模型调用，换来的是六条能同时投出去、不会被判重复内容的片子。单套最多 3 条是实测出来的：一次要 4 条以上，模型后半段就开始凑数了。

**跑批按设备资源调度，而不是按任务数量**
并发上限 = min(核数/3, 可用内存/2.5GB)，再被 `--workers` 压一层。每完成一部才补下一部，补之前检查实时负载与可用内存，越线就暂停（最长等 180 秒后放行，避免死等）。子进程 `nice 5`，单段 x264 限 3 线程。目标是「跑批时机器还能正常用」，不是「最快跑完」。

**编码器分两个池，不是一个池**
硬编（VideoToolbox）走媒体引擎、几乎不占 CPU，软编（x264）吃 CPU，两者抢的不是同一种资源，可以同时开：一条切片的多个片段按共享队列动态取活，GPU 池和 CPU 池各自消费，谁空出来谁拿下一段。实测混合能到 23.6 条/分钟，纯软编 3 路 21.7。另有两点反直觉：硬编的媒体引擎是**串行**的（1 路 11.6、6 路 11.9 条/分钟，加并发不涨），所以 `gpu_slots` 开大没意义；硬编画质其实更好（同一素材 SSIM 0.9947 / 3.2 Mbps vs x264 ultrafast 0.9925 / 5.2 Mbps），因为它判断内容不需要那么高码率时会主动省。

阈值是 `max_load_per_core`（每核平均负载）而不是核数：默认 1.2，10 核机器上相当于 load1 ≈ 12 才拦。之所以卡在 1.2，是因为渲染本身就吃核——单条 45 秒切片实测等效 5.9 核（`-threads 3` 只限 x264，滤镜图默认按核数开线程），3 个 worker 并行就是 17.7 核的需求压在 10 核上，不拦就会一路 swap 到卡死。降低渲染的核占用比调阈值更有效，见下面的「模糊层按需降级」。

**信号层的三路并发也要分配核数**
ASR 子进程、OCR 的 ONNX、视觉帧差三路是**并行**跑的，如果都按核数开线程，加起来远超物理核数，结果是全部变慢。实测 OCR 单独跑 42 秒、进流水线后变 170-210 秒，差的 3 倍全耗在抢核上。现在按配额走：ASR 4 核 + OCR 3 核 + 视觉 2 核 ≈ 9 核，留一核给主进程和 IO。

**源片没字幕时自动补**
部分短剧不带内嵌字幕（实测《少夫人来自东北3》OCR 覆盖率只有 3.3%，有字幕的片子都在 20% 以上）。补不补按**覆盖率**判定而不是条数：无字幕片里 OCR 仍会零星抓到台标、集数标题和「恭喜少夫人」这类画面文字，按条数判断会误以为是字幕。

字幕文字取 ASR 句子，按时长比例分配到词；烧在**原画面**的底边而不是画布底边——否则横屏素材的字幕会飘到模糊背景上，看着像别人的台词。

没有用 ffmpeg 的 `subtitles` / `drawtext`：Homebrew 的 ffmpeg 8.1 是精简构建（`--enable-libass` / `--enable-libfreetype` 都没有），这两个滤镜根本不存在。改成 PIL 渲染透明 PNG + `overlay` 链，与切片同一次编码完成（不二次压缩），时间轴用**段内相对时间**，不受多段拼接的偏移累积影响。

**OCR 用 PP-OCRv4 而不是 Unlimited-OCR**
Unlimited-OCR 是 3B MoE 文档模型，最佳部署是 vLLM + CUDA。按 0.5 秒一帧处理一天几十小时素材是小时级开销。字幕只是白字黑边的台词条，PP-OCRv4 det+rec ONNX 单帧约 45-65ms。另外两个实测结论：裁字幕带反而更慢（det 会把小图放大到固定边长），所以送整帧再按框位置过滤；相邻帧字幕不变时复用上次结果。

**但别指望这个复用能省多少**：短剧字幕在 2fps 下几乎每帧都在变，实测门控命中率 96%（922/960 次引擎调用），基本形同虚设。OCR 真正的浪费不在门控，而在**线程**：ONNX 默认按核数开 intra-op 线程会占到 5.8 个核，而它实际有效并行度没那么高——限到 `threads: 3` 只慢 3%，却降回 3.5 个核。三路信号是并行的，这个配额必须给，否则 OCR 会把 ASR 的核抢走。

**ASR 走常驻服务，默认 SenseVoice**
`AutoModel` 单次加载约 73 秒，推理 4 分钟音频只要 13 秒。每部剧各加载一次会白烧半小时。服务（`scripts/asr_server.py`）只加载一次，batch 期间所有剧共用。

默认引擎是 `sensevoice`（SenseVoice-Small，230M 非自回归）。480 秒素材实测 68 秒出结果，比 `paraformer`（SeACo-Paraformer large，106–148 秒）快一倍多，代价是它只吐一整段文本、要自己按标点切句。换来的东西更值钱：**它同时给出情感与音频事件标签**（`<|ANGRY|>` / `<|HAPPY|>` / `<|BGM|>`），召回打分直接拿这个找冲突点，比纯词表准。要回到原生句级时间戳就设 `asr.engine: paraformer`。

两个容易踩的坑：
- **必须配 VAD**。不配时整片被当成一段、只转写开头几秒（实测 26 字 vs 1457 字）。
- **`ncpu` 不是越大越好**：`ncpu=4`（=性能核数）68 秒，`ncpu=8` 反而 155 秒——多出来的线程被调度到能效核上互相等。默认 `ncpu: 0` 会自动取性能核数。

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
| 阶段耗时（480s 素材，改引擎后实测） | ASR 125s（服务端转写 96s）/ OCR 68s / 镜头+运动 17s / 出单 4.3s / 渲染 10.2s / 混音 5.7s |
| 渲染速度 | 1080×1920 ultrafast，单条 45s 成片约 3-4s（0.07-0.09 倍成片时长） |
| 单条切片 CPU 占用 | 25.9-37.0 CPU 秒（等效 3.5-4.3 核），内存峰值 430MB |

**吞吐大致等于 0.8-1.3 倍素材总时长**，短素材偏高（固定开销摊不开），长素材偏低。10 小时满载约能处理 400-500 分钟素材。

瓶颈顺序：ASR > OCR > 渲染 > 镜头切分 > DeepSeek。改引擎后的实测收益：OCR 210s → 68s（线程限核），ASR 149s → 125s（SenseVoice + 性能核数），渲染 33-79s → 16s（ultrafast + 硬软双编码器池）。

导出侧还有一条没走完的路：硬编（VideoToolbox）画质其实**更好**且体积更小——同一段素材 SSIM 0.9947 / 3.2 Mbps，而 x264 ultrafast crf22 是 0.9925 / 5.2 Mbps。但硬编的媒体引擎是串行的（实测 1 路 11.6 条/分钟、6 路 11.9 条/分钟，加并发不涨），所以 `gpu_slots` 开大没有意义，它只适合当第二个资源池和 CPU 并行用。

## 适用范围与限制

**这套流水线擅长**：已经分好集的短剧（每集 1-4 分钟），批量出 30-60 秒切片。这个尺度上「一集」约等于「一个完整高光弧」，规则召回足够覆盖。

**不适合直接喂 40 分钟以上的整集**。召回密度已经从「`duration/16`」改成「固定 6 秒间隔 + 按时长放开候选数」，长片不会再只剩十几个点；但候选上限是 96 个，45 分钟等于每 28 秒才摸一次样本，而且 96 条候选塞进 prompt 后模型也没法逐条比较。真要做长视频得改成分块 map-reduce（3 分钟一块，块内取 top-k，块级判「有没有料」后再跨块比选），当前版本没做。另外 ASR 是最长关键路径，45 分钟素材约 22 分钟转写，且服务端推理串行。

其他限制：

- **素材必须有音轨。** 源片没有音轨时 ASR 拿不到输入，剪辑单只能靠画面和 OCR 猜，成片也不可避免地只剩配乐。这种情况 `report.has_audio` 为 `false`、`warnings` 会写明原因、每条 `reels[].qc.ok` 一律判 false，不会静默交付。下载素材时**视频流与音频流分开取再合并**（`yt-dlp -f 135` + `-f 140`，然后 `ffmpeg -map 0:v -map 1:a -c copy`）：`-f "bv*[height<=480]/b"` 这类写法很容易只命中纯视频流，下完就是一部哑片。具体命令见 `skills/fsm-slicer/references/download.md`。
- 文本质量取决于 ASR/OCR 准确度。配乐重、口音重、画质差的素材会掉字，此时退化为视觉选片。自动补字幕的字幕文字也来自 ASR，专有名词和英文会错（实测 `royal college of art` 被识别成 `arart`），介意的话用 `--subtitle off` 关掉。
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
  commentary.py    解说模式：口播词 → TTS → 引用片段 → 文字卡 → 组装
  qc.py            成片质检
  pipeline.py      单剧编排（阶段落盘 + 断点续跑）
  batch.py         进程池批量 + 目录监听
  digest.py        每日总览页
  cli.py           命令行
scripts/
  asr_server.py    FunASR 常驻服务（用 FunASR 自己的 venv 跑），支持双引擎
  asr_worker.py    一次性转写（服务不可用时回退），`--engine sensevoice|paraformer`
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
| `planner.min_seconds` | 32 | 成片时长**硬下限**，低于它模型输出会被退回重试 |
| `planner.max_seconds` | 95 | 时长上限，超过按角色顺序裁段 |
| `recall.candidates` | 32 | 候选数下限（短片也保底这么多） |
| `recall.candidates_max` | 96 | 候选数上限，防止给模型的 prompt 过长 |
| `recall.min_gap` | 6.0 | 候选之间的最小间隔（秒），略粗于一个语义单元 |
| `recall.cand_step` | 10.0 | 每这么多秒素材允许多一个候选，长片靠它维持密度 |
| `subtitle.mode` | auto | 自动补字幕：`auto` 按覆盖率判断 / `on` 强制 / `off` 关闭 |
| `subtitle.coverage_threshold` | 0.12 | OCR 字幕覆盖率低于此值视为源片无字幕 |
| `subtitle.max_chars` | 15 | 单行最多字数（中文按 1、ASCII 按 0.5 计宽） |
| `subtitle.size` | 58 | 字号（1080 宽画布） |
| `render.preset` | ultrafast | 批量出片优先速度；要画质改 veryfast/superfast（crf 各自加 2） |
| `render.crf` | 22 | 与 preset 配套，ultrafast 下 22 约等于 veryfast 的 20 |
| `render.encoder` | auto | `auto` = 硬编+软编双池 / `libx264` / `h264_videotoolbox` |
| `render.cpu_slots` | 2 | 软编（x264）并发槽位，吃 CPU |
| `render.gpu_slots` | 2 | 硬编并发槽位，走媒体引擎不占 CPU |
| `render.vt_bitrate` | 6M | 硬编目标码率（硬编只能定码率；实测常打不满，体积反而更小） |
| `render.threads` | 3 | 单段 x264 线程数，与并发数相乘别超核数 |
| `render.blur_div` | 4 | 模糊层在 1/N 分辨率上做再放大，1 = 全分辨率（更耗 CPU） |
| `output.per_plan` | 3 | 指定总条数时单套最多几条（一次要 4 条以上模型会开始凑数） |
| `output.max_total` | 12 | 单部剧总条数上限 |
| `output.overlap` | 0.6 | 跨套候选重合率超过此值即驳回重出 |
| `resource.cpu_per_worker` | 3 | 每个 worker 期望占用核数，用于推算安全并发 |
| `resource.max_load_per_core` | 1.2 | 负载红线，越线暂停派活 |
| `resource.min_free_mem_gb` | 3.0 | 可用内存红线 |
| `ocr.fps` | 2.0 | 抽帧频率，降到 1.5 省 25% |
| `ocr.threads` | 3 | ONNX 线程配额。不限会占 5.8 核；限到 3 只慢 3% 却省 40% CPU |
| `asr.engine` | sensevoice | `sensevoice` 快一倍且带情感标签；`paraformer` 给原生句级时间戳 |
| `asr.ncpu` | 0 | 0 = 自动取性能核数。SenseVoice 超过性能核数反而变慢 |
| `bgm.enabled` | true | 关掉等于 `--no-bgm` |
| `workers` | 3 | 同时处理的剧数 |

## License

[Apache License 2.0](LICENSE)，附专利授权条款。分发时请保留 [NOTICE](NOTICE)、并在修改过的文件里注明改动。

BGM 曲目不在本协议范围内：它们由 Kevin MacLeod 以 CC BY 4.0 授权，不入库，商用需署名。
