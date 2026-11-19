---
name: fsm-slicer
description: 从「找剧」到「出片」的全流程：搜近期热门短剧 → yt-dlp 下载（带音轨）→ 自动选段切片 → 配乐卡点/自动补字幕 → 质检交付。支持同一部剧出多套互不重复的方案（--total / --variants），跑批自带资源闸门不会把机器压满。当用户说「找几部热门短剧切片」「批量出短视频切片」「这部剧能剪出几条」「一条素材多出几条不重复的」「跑一批投放素材」时使用。需要 ffmpeg 与 DeepSeek API key，FunASR / PP-OCRv4 可选。
agent_created: true
---

# 热门短剧 → 竖屏切片 全流程

一条命令式的产线，四段：**选题 → 下载 → 切片 → 交付**。中间任何一段失败都要如实报告，不要静默跳过。

- 单部素材大约 50 秒出一条 45-60 秒竖屏切片（信号阶段有缓存，重跑只算渲染）。
- 同一部剧可以出多套方案：`--total 6` 出 6 条互不撞车的切片（自动拆成 3+3 两套）。
- 跑批有资源闸门：并发按核数与内存推算，负载越线自动暂停派活。

## 步骤 1：定位项目

找含 `bin/fsm` 与 `pyproject.toml` 的目录。用户给了路径就用；否则：

```bash
ls ~/coding/fantastic-slice-maker/bin/fsm 2>/dev/null || find . -maxdepth 3 -name fsm -path "*/bin/*"
```

都没有就克隆：

```bash
git clone https://github.com/arczhi/fantastic-slice-maker.git ~/coding/fantastic-slice-maker
```

后文用 `$FSM` 指这个目录。

## 步骤 2：环境自检

```bash
$FSM/bin/fsm doctor
```

**不要因为 missing 就停下**，按表处理：

| 结果 | 处理 |
|---|---|
| `ffmpeg` / `ffprobe` missing | 必须先装（`brew install ffmpeg`），否则跑不起来 |
| `FunASR env` missing | 继续。台词改从 OCR 字幕取；跑批加 `--no-asr` 更快 |
| `PP-OCR` missing | 继续，会自动用 rapidocr 自带模型 |
| `DeepSeek key` missing | 必须解决，见步骤 3 |
| `BGM library` 0 首 | 加 `--no-bgm`，或先 `$FSM/bin/fsm bgm`（约 15 分钟） |
| `并发上限` N 部 | 这是本机安全并发数，跑批时心里有数，别硬压 |

依赖没装过：

```bash
cd $FSM && python3 -m venv .venv && .venv/bin/pip install -e .
```

## 步骤 3：注入 API key

唯一的外部凭证，**不要写进任何会被提交的文件**：

```bash
export DEEPSEEK_API_KEY=sk-...
```

只给了 key 时写进 `$FSM/config.local.yaml`（已 gitignore）：

```yaml
planner:
  api_key: "sk-..."
```

## 步骤 4：选题（找近期热门短剧）

用 WebSearch 搜最近一周的热门短剧，查询词参考：

```
2026年11月 短剧 热榜
抖音短剧 热度榜 本周
dataeye 短剧 榜
红果短剧 热播榜
```

筛选标准（按顺序）：**近期（一周内）** → **有明确冲突/反转的剧情片**（古装、复仇、逆袭、豪门都行）→ **单集 2-10 分钟**（长片要分块，见步骤 9）→ 标题里带季/集数说明是连载。

把候选整理成表给用户看，**每部都要能一句话说清看点**：

| 剧名 | 看点 | 平台 |
|---|---|---|
| 十八岁太奶奶驾到 5 | 反差设定 + 家族斗法 | YouTube 短剧频道 |

不要一次性铺 20 部。用户没指定数量时默认 6 部 × 每部 3 条 = 18 条，先跑 1 部试水再铺开。

## 步骤 5：下载（必须带音轨）

细节命令与踩坑见 `references/download.md`，核心只有三条：

1. **用 `yt-dlp`，不要用 `yoinks`**。yoinks 是 Ink TUI，只能人手动点，没法脚本化。
2. **视频流和音频流分开下再合并**。`-f "bv*[height<=480]/b"` 这种写法容易只拿到视频流，成片会「只有配乐没有人声」——流水线会判 `has_audio=false` 并让 QC 全部不通过。
3. **下完必须验**：

```bash
ffprobe -v error -select_streams a -show_entries stream=codec_name -of csv=p=0 素材.mp4  # 有输出才算有音轨
ffprobe -v error -show_entries format=duration -of csv=p=0 素材.mp4
```

只取前 700 秒做切片（`--download-sections "*0-700"`），省流量也够 3 条切片的取材量。

## 步骤 6：切片

单部试水（推荐先跑 1 部）：

```bash
$FSM/bin/fsm run /abs/path/ep.mp4 --total 3
```

批量（目录即批次）：

```bash
$FSM/bin/fsm batch /abs/path/素材目录 --total 3 --workers 2
```

### 出片数量怎么指定

| 需求 | 命令 | 结果 |
|---|---|---|
| 每部出 3 条（自动拆套） | `--total 3` | 1 套 × 3 条 |
| 每部出 6 条互不重复 | `--total 6` | 2 套 × 3 条，跨套不复用 hook |
| 每部 7 条 | `--total 7` | 3 套，3+2+2 |
| 分套出，每套 2 条 | `--variants 3` | 3 套 × 2 条 |
| 一套 3 条，不拆套 | `--reels 3` | 1 套 × 3 条 |

要点：

- **`--total` 是最省心的写法**。单套最多 3 条（`output.per_plan`）：一次让模型交 4 条以上，它会开始凑数，后面几条 hook 越来越弱、段落开始重复。
- **跨套去重是强制的**：前一套用过的 hook 段不允许再用，候选重合率超过 60% 直接驳回重出。所以「同一部剧出 6 条」得到的是 6 条不同的片子，不是 6 个近似版。
- 上限 `output.max_total: 12`，防手滑。真要更多，先扩素材时长，否则候选池撑不住。

### 资源调度（不许把机器压满）

跑批默认就是「安全模式」，不需要额外参数：

- 并发数 = min(核数/3, 可用内存/2.5GB, `--workers`)，10 核 32GB 机器上通常落在 2-3。
- 每完成一部才补下一部，补之前检查负载：单核负载 > 1.2 或可用内存 < 3GB 就暂停派活（最长等 180 秒后放行）。
- 子进程统一 `nice 5`，前台办公、看视频不受影响。
- 单段裁切 x264 限 3 线程（`render.threads`），和 worker 数相乘不会超核。

需要更保守时：

```bash
# 只许开 1 部并发，负载线压到 0.8 核，可用内存低于 6GB 就等
$FSM/bin/fsm batch /path/素材 --total 3 --workers 1 --max-load 0.8 --min-free-mem 6

# 机器正在被人用（开会 / 直播），把优先级降到最低
$FSM/bin/fsm batch /path/素材 --total 3 --workers 1 --nice 10
```

**不要**靠 `--workers 8` 抢时间：ASR 服务内部是全局串行的，多开的进程只会互相抢核，总时长反而变长。

### 超时预算

按素材总时长的 1.5 倍 + 120 秒模型加载估算。700 秒素材单部约 3-4 分钟；6 部两并发约 8-12 分钟。命令跑在后台，别阻塞对话。

其他常用开关：`--no-asr`（没装 FunASR）、`--no-bgm`（不要配乐）、`--rerender`（只换配乐/重渲染）、`--subtitle on|off`（强制补/不补字幕）。**不要习惯性加 `--force`**，断点续跑能省掉全部信号计算。

## 步骤 7：验收

**退出码 0 不代表全部成功。** `batch` 的 `ok` 只表示「至少有一部没崩」，必须读 `succeeded` / `failed`：

```bash
python3 - <<'PY'
import json
d = json.load(open('/tmp/batch.json'))
print('成功', d['succeeded'], '失败', d['failed'], '条数', d['reels'],
      '耗时', round(d['elapsed_seconds']), 's', '并发', d['workers'])
for r in d['results']:
    print('OK ' if r['ok'] else 'FAIL', r['video'].split('/')[-1], r.get('error') or '')
PY
```

逐部再核这几项（都在 `out/<日期>/<video_id>/report.json`）：

| 字段 | 判据 | 不通过怎么办 |
|---|---|---|
| `has_audio` | 必须 `true` | 源片无音轨，重下（步骤 5），别交付 |
| `reels[].qc.ok` | 全部 `true` | 看 `qc.issues`：`太短`=素材没内容，`黑场`=片尾字幕卡 |
| `reels[].duration` | 32-95 秒 | 明显偏短说明候选池太小 |
| `reels[].subtitle.auto` | 与 `autosub.needed` 一致 | 源片有字幕却被补了第二层，用 `--subtitle off` |
| `variants` / `reels_per_variant` | 与 `--total` 对得上 | 对不上说明缓存指纹没失效，加 `--force` |
| `strategy` | `llm` 优先 | `fallback` = 模型没出单，只保证好看不保证连贯 |

跨套重复度自查（够 2 套以上才需要）：

```bash
python3 - <<'PY'
import json, glob, itertools
rep = json.load(open(glob.glob('$FSM/out/*/*/report.json')[-1]))
sets = [(r['variant'], r['index'], {b['src_start'] for b in r['beats']})
        for r in rep['reels'] if r.get('ok')]
for (v1,i1,a), (v2,i2,b) in itertools.combinations(sets, 2):
    ov = len(a & b) / min(len(a), len(b))
    if ov > 0.5:
        print(f'重合 {ov:.0%}: v{v1}-{i1} vs v{v2}-{i2}')
PY
```

## 步骤 8：交付

1. 每条成片的绝对路径 + 时长 + 标题，按套分组列出。
2. `present_files` 呈上成片与封面；`out/<日期>/index.html` 是当日总览页，一并给出。
3. 附 BGM 署名串（`reels[].bgm.attribution`，CC BY 4.0 商用必须署名）。
4. 有 `warnings` 或 `qc.ok=false` 就直说哪一项没过，不要美化。
5. 素材来源要交代清楚：网络下载的非授权素材只能用于验证流程，不能商用投放。

## 步骤 9：长片怎么办

当前召回是单次全局打分，**适合 1-4 分钟的短剧集**。超过 10 分钟的长片：

- 先用 `ffmpeg -ss A -t 300 -c copy` 切段，再对每段跑一次 `fsm run`，最后人工挑。
- 不要靠调 `recall.min_gap` 硬凑：候选会全挤在开头，后面整段没有候选。

## 故障速查

| 现象 | 原因与处理 |
|---|---|
| 成片只有配乐、没人声 | 源片无音轨。`ffprobe -select_streams a` 确认后按步骤 5 分开下视频/音频再 `-c copy` 合并 |
| `planner produced no reel` | 素材太短凑不出 4 段。换长一点的素材，或 `--no-llm` 走规则兜底 |
| `no candidates` | 全片黑场或解码失败。`ffprobe` 看是不是坏文件 |
| 某套方案条数变少 | 该套被驳回多次（撞 hook/重合度过高），日志里有 `reel rejected: ...`。候选池不够大时属正常，`--total` 调小 |
| 改了 `--total` 但出的条数没变 | 剪辑单缓存指纹未命中。`--force` 一次，或删 `work/<video_id>/plan_v*.json` |
| `Brokered ... rename` / `PermissionError` | 宿主沙箱拦 rename。写入已自动降级为直接覆盖；渲染产物也会退回复制，不影响成片 |
| 渲染很慢 / 风扇狂转 | 并发过高。`--workers 2`，并确认没有同时跑两批 |
| 补字幕压到模糊背景上 | 调 `subtitle.bottom_pct`（调大更贴底边）或 `subtitle.size` |
| 有字幕的片子被补了第二层 | 该片 OCR 覆盖率异常低（水印多/图片字幕），`--subtitle off` |
| `yt-dlp: Requested format is not available` | 版本太旧。用 2025 之后的版本，或换 `-f 18`（360p 合成流，含音轨） |

## 不要做

- 不要把 `work/`、`out/`、`logs/`、`assets/bgm/`、`config.local.yaml`、`models/*.onnx` 提交到仓库。
- 不要用 `yoinks` 之类 TUI 工具做自动化下载，它不能脚本化。
- 不要为了刷数量把 `--total` 拉到 12 以上：候选池复用、成片互相撞车，投出去会被判重复内容。
- 不要承诺「一定能出爆款」。这套流程保证的是「片段好看、结构完整」，起量取决于素材本身。
