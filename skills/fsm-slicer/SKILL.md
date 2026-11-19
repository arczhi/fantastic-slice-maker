---
name: fsm-slicer
description: 把剧集视频批量切成可直接投放的 45-60 秒竖屏切片，含 AI 选段、配乐卡点、封面与质检报告。当用户提出「把这部剧切一下」「批量出短视频切片」「从剧里挑精彩片段投放」「生成抖音/快手切片」「这条片子能剪出什么」时使用。需要 ffmpeg 与 DeepSeek API key，FunASR 和 PP-OCRv4 可选。
agent_created: true
---

# 视频高光切片流水线

把一条剧集视频拆成多条竖屏切片。流程是四路信号提取（镜头 / ASR / OCR / 运动）→ 规则召回候选 → DeepSeek 出剪辑单 → FFmpeg 切片、配乐、卡点、拼接、质检。

本 skill 只做一件事：**给定视频文件或目录，跑出成片并把结果交付给用户**。

## 步骤 1：定位项目

需要找到含 `bin/fsm` 与 `pyproject.toml` 的目录。

- 用户给了路径就用它。
- 否则从当前工作目录往上找，或用 `find . -maxdepth 3 -name fsm -path "*/bin/*"`。
- 都没有就先克隆：

```bash
git clone https://github.com/arczhi/fantastic-slice-maker.git ~/fantastic-slice-maker
```

后文用 `$FSM` 表示这个目录。

## 步骤 2：确认环境

```bash
$FSM/bin/fsm doctor
```

看输出，**不要因为 missing 就停下**：

| 结果 | 处理 |
|---|---|
| `ffmpeg` / `ffprobe` missing | 必须先装（`brew install ffmpeg` / `apt install ffmpeg`），否则流程跑不起来 |
| `FunASR env` missing | 可以继续，台词改从 OCR 字幕取。跑批时加 `--no-asr` 更快 |
| `PP-OCR` missing | 可以继续，会自动装 rapidocr 的默认模型 |
| `DeepSeek key` missing | 必须解决，见步骤 3 |
| `BGM library` 0 首 | 可以继续，加 `--no-bgm`，或先跑 `$FSM/bin/fsm bgm`（约 15 分钟） |

依赖没装过就先装：

```bash
cd $FSM && python3 -m venv .venv && .venv/bin/pip install -e .
```

## 步骤 3：注入 API key

只有这一项外部凭证。优先用环境变量，**不要写进任何会被提交的文件**：

```bash
export DEEPSEEK_API_KEY=sk-...
```

用户只给了 key 没给环境变量时，可以写进 `$FSM/config.local.yaml`（已被 gitignore）：

```yaml
planner:
  api_key: "sk-..."
```

禁止把 key 写进 `config.yaml`、README、脚本或任何提交物。

## 步骤 4：跑

单条：

```bash
$FSM/bin/fsm run /abs/path/ep.mp4 --reels 2
```

批量：

```bash
$FSM/bin/fsm batch /abs/path/to/dir --reels 2 --workers 3
```

规则：

- **`--workers` 不超过 3**。ASR 服务内部串行，再高只会抢核变慢。
- **预留超时**。按素材总时长的 1.5 倍估算，再加 120 秒模型加载。4 分钟素材给 600 秒，45 分钟素材给 4500 秒。
- **不要用 `watch`**，它是常驻进程，会一直阻塞。
- **不要默认加 `--force`**。断点续跑会跳过已完成阶段，能省大量时间。

常用变体：

| 需求 | 命令 |
|---|---|
| 先跑 1 部试水 | `batch <dir> --limit 1` |
| 只换配乐/重渲染 | `--rerender` |
| 没装 FunASR | `--no-asr` |
| 不要配乐 | `--no-bgm` |
| 不想调模型（省钱/离线） | `--no-llm` |

## 步骤 5：读结果

`run` 的 stdout 是一份 JSON；`batch` 的 stdout 是摘要 JSON，失败详情逐条打到 stderr。

**关键：退出码 0 不代表全部成功**。`batch` 的 `ok` 只表示「至少有一部没崩」，必须读 `succeeded` 与 `failed`。

```bash
$FSM/bin/fsm batch /path/to/dir --reels 2 > /tmp/batch.json 2>/tmp/batch.log
python3 -c "
import json
d=json.load(open('/tmp/batch.json'))
print('成功', d['succeeded'], '失败', d['failed'], '总耗时', round(d['elapsed_seconds']), 's')
for r in d['results']:
    print('OK ' if r['ok'] else 'FAIL', r['video'].split('/')[-1], r.get('error') or '')
    for o in r.get('outputs') or []: print('   ->', o)
"
```

每部剧的详细结果在同目录的 `report.json`：

- `reels[].path` 成片路径，`reels[].cover` 封面
- `reels[].title` / `hook` / `reason` / `beats[]` 剪辑理由，可直接做文案
- `reels[].qc.ok` 质检是否通过，`issues[]` 说明问题
- `reels[].bgm.attribution` BGM 署名，**交付时要一并给用户**（CC BY 4.0 商用需署名）
- `strategy` 为 `fallback` 时说明模型没出单、走了规则兜底，切片只保证好看不保证连贯

完整字段见 `references/cli-contract.md`。

## 步骤 6：交付

1. 列出每条成片的绝对路径与时长、标题。
2. 用 `present_files` 把成片和封面呈现给用户；`out/<日期>/index.html` 是当日总览页，也一并给出。
3. 附上 BGM 署名串，提醒用户投放时放进简介。
4. 如果 `qc.ok` 为 false，明确说明是哪一项没过（时长/黑场/响度），不要隐瞒。
5. 如果整批都失败，先看 `results[].error`：`PermissionError` 多半是输出目录权限或跨设备重命名，`no videos found` 是路径不对，`planner produced no reel` 是候选太少（素材过短）。

## 故障速查

| 现象 | 原因与处理 |
|---|---|
| `planner produced no reel` | 素材太短，候选凑不齐 4 段。换更长的素材，或 `--no-llm` 走规则兜底 |
| `QC 未通过: 太短` | 成片不足 32 秒。素材本身就没有足够可剪内容，属正常拒绝 |
| ASR 阶段 20s~150s 抖动 | ASR 服务串行，多部并发在排队。正常现象，不是卡死 |
| 渲染很慢 | 检查是否同时跑了多批。`--workers` 降到 2 |
| `Brokered host rename overwrite refused` | 输出目录被宿主沙箱拦截，换一个可写目录或放宽权限 |
| 全片黑场误判 | 片尾字幕卡会被 blackdetect 抓到，已被 `_trim_blank` 处理；仍有问题看 `qc.issues` |

## 不要做

- 不要把 `work/`、`out/`、`logs/`、`assets/bgm/`、`config.local.yaml` 提交到仓库或放进交付物。
- 不要修改 `recall.py` 的 NMS 间隔来处理长视频——那是设计问题，需要分块召回，改参数只会让候选全部挤在一起。
- 不要承诺「一定能剪出爆款」。这套流程决定的是「片段好看」，账号能不能起量取决于素材本身。
