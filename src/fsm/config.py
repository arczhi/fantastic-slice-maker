"""配置：默认值 + config.yaml 覆盖 + 环境变量。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def _project_root() -> Path:
    """仓库根目录：从本文件向上找第一个含 pyproject.toml 的目录。

    所有默认路径都基于它，因此仓库 clone 到任何位置都能直接跑，
    不依赖开发机上的绝对路径。装成 site-packages 时退化为当前工作目录。
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


PROJECT_ROOT = _project_root()


def _guess_funasr_python() -> str:
    """探测装了 funasr 的独立解释器。FunASR 依赖 py3.9，通常不在主环境里。"""
    candidates = [
        os.environ.get("FUNASR_PYTHON", ""),
        str(Path.home() / "coding" / "FunASR" / ".venv" / "bin" / "python"),
        str(Path.home() / "funasr" / ".venv" / "bin" / "python"),
    ]
    for c in candidates:
        if c and Path(c).expanduser().exists():
            return c
    return ""


@dataclass
class ASRCfg:
    enabled: bool = True
    python: str = ""                 # 留空自动探测，或用 FUNASR_PYTHON 指定
    worker: str = ""                 # 由 config.load 填默认路径
    server: str = ""                 # 常驻服务脚本，由 config.load 填
    server_port: int = 8788
    use_server: bool = True          # 常驻服务可用时优先走 HTTP
    # sensevoice：230M 非自回归，480s 素材 68s，额外产出情感/音频事件（默认）
    # paraformer：SeACo-Paraformer large，106-148s，原生句级时间戳
    engine: str = "sensevoice"
    model: str = "paraformer-zh"
    vad_model: str = "fsmn-vad"
    punc_model: str = "ct-punc"
    device: str = "cpu"
    # 0 = 自动取性能核数。SenseVoice 实测 4 线程 68s、8 线程 155s——
    # 超过性能核数后线程被调度到能效核，反而互相拖慢
    ncpu: int = 0
    batch_size_s: int = 300
    timeout: int = 1800


@dataclass
class OCRCfg:
    enabled: bool = True
    backend: str = "ppocr"           # ppocr | unlimited | none
    det_model: str = str(Path.home() / "coding/video-multimodal-feature/models/rapidocr/ch_PP-OCRv4_det_infer.onnx")
    rec_model: str = str(Path.home() / "coding/video-multimodal-feature/models/rapidocr/ch_PP-OCRv4_rec_infer.onnx")
    cls_model: str = str(Path.home() / "coding/video-multimodal-feature/models/rapidocr/ch_ppocr_mobile_v2.0_cls_infer.onnx")
    fps: float = 2.0                 # 抽帧频率
    band_top: float = 0.55           # 字幕带起始（画面高度比例）
    band_bottom: float = 1.0
    change_threshold: float = 2.5    # 字幕带平均像素差阈值，超过才 OCR
    min_confidence: float = 0.55
    frame_width: int = 960           # 送 OCR 前的帧宽
    det_limit_side_len: int = 480    # 检测输入长边；736→480 快一倍，字幕仍认得准
    # ONNX 默认按核数开 intra-op 线程，实测占到 5.8 个核；限到 3 只慢 3%
    # 却降回 3.5 个核——超出有效并行度的线程基本在自旋空转。
    # 三路信号（ASR/OCR/视觉）是并行的，这里必须给配额，否则互相抢核。
    threads: int = 3
    timeout: int = 900


@dataclass
class VisionCfg:
    fps: float = 4.0
    width: int = 160
    height: int = 90
    use_pyscene: bool = True         # 优先用 pyscenedetect 自适应检测
    adaptive_threshold: float = 3.0  # AdaptiveDetector 的窗口倍数，越大越保守
    scene_threshold: float = 0.28    # 仅 ffmpeg 兜底时使用
    min_shot: float = 0.7            # 最短镜头时长（秒）
    max_shot: float = 12.0           # 超长镜头强制切分
    blank_std: float = 6.0           # 灰度标准差低于此视为空镜


@dataclass
class BGMCfg:
    enabled: bool = True
    index: str = ""                  # 由 config.load 填默认路径
    gain_db: float = 0.0             # 响度归一后的微调偏移
    bgm_lufs: float = -20.0          # BGM 归一目标（人声 -14，低 6dB 起）
    duck_threshold: float = 0.15     # sidechain 触发门限
    duck_ratio: float = 2.5          # 人声出现时 BGM 被压的比例（漫剧原声自带配乐，不能压太狠）
    fade_in: float = 0.4
    fade_out: float = 1.2
    target_loudness: float = -14.0   # 仅用于校验参考，混音不再二次归一
    # 节拍对齐只允许「延长」到下一个鼓点，缩短会切掉台词
    max_stretch: float = 0.45
    min_beat_cv: float = 0.22        # 节拍稳定性低于此（越稳定越小）才用于卡点


@dataclass
class RecallCfg:
    candidates: int = 32             # 候选数下限（短片也用满，NMS 自然收敛）
    candidates_max: int = 96         # 上限，防止 prompt 过长
    min_gap: float = 6.0             # 候选之间的最小间隔（秒），略粗于一个语义单元
    cand_step: float = 10.0          # 每这么长素材就允许再多一个候选
    min_beat: float = 2.0
    max_beat: float = 8.0
    head_skip: float = 0.02          # 片头跳过比例
    tail_skip: float = 0.06          # 片尾跳过比例
    w_density: float = 0.32
    w_emotion: float = 0.22
    w_motion: float = 0.20
    w_subtitle: float = 0.14
    w_center: float = 0.12


@dataclass
class PlannerCfg:
    enabled: bool = True
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    api_key_env: str = "DEEPSEEK_API_KEY"
    api_key: str = ""
    temperature: float = 0.7
    max_tokens: int = 2000
    timeout: int = 180
    retries: int = 3
    target_seconds: float = 52.0
    min_seconds: float = 32.0
    max_seconds: float = 95.0
    # 题材策略文件（由外部运营数据生成，如 fsm-ops 的 strategy.yaml）。
    # 留空则用内置的通用剪辑规则；给了就按文件里的题材偏好改写选段要求。
    strategy_file: str = ""


@dataclass
class DewatermarkCfg:
    """源片自带搬运水印时的处理。

    实测（720x1280 源、45 秒切片，含 loudnorm 音频）：off 4.67s / crop 4.47s /
    delogo 4.56s，差异都在噪声范围内，开去水印基本不额外花时间。

    - crop：顶部裁一条 + 左右各裁一点补回 9:16，再直接缩放到画布。效果最干净。
      代价是画面放大约 crop_ratio 倍、左右各损失一点。
    - delogo：在区域上做插值修复，画面尺寸不变。文字类水印会留下明显灰斑，
      背景越简单越干净；只在确实不能裁画面时用。

    水印位置随源而变：有的在右上（台标），有的在左上且横向拉得很长
    （账号名 + 剧名），所以 crop_ratio 要按源调；delogo 必须给源分辨率
    下的精确 bbox。默认 off，不影响原有流程。
    """

    mode: str = "off"             # off | crop | delogo
    crop_ratio: float = 0.06      # crop 模式：裁掉顶部占源高的比例
    x: int = 0                    # delogo 区域，源分辨率坐标
    y: int = 0
    w: int = 0
    h: int = 0


@dataclass
class RenderCfg:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    crf: int = 20
    preset: str = "medium"
    loudness_i: float = -14.0
    blur_sigma: int = 24
    audio_bitrate: str = "192k"
    timeout: int = 900
    threads: int = 3                 # 单段裁切的 x264 线程数，与 workers 相乘别超过核数
    blur_div: int = 4                # 模糊层在 1/blur_div 分辨率上做再放大（4≈肉眼看不出差别）
    encoder: str = "auto"            # auto | libx264 | h264_videotoolbox
    cpu_slots: int = 2               # 软编（x264）并发槽位
    gpu_slots: int = 2               # 硬编（VideoToolbox）并发槽位，与 CPU 槽位互不争抢
    vt_bitrate: str = "6M"           # 硬编码率，硬编只能定码率
    dewatermark: DewatermarkCfg = field(default_factory=DewatermarkCfg)
    # auto：横屏源走「上下模糊 + 原画面居中」，画面完整但非满屏（默认，老行为）
    # crop：横屏源按 9:16 居中裁切再缩放，成片满屏，代价是左右切掉较多
    fit: str = "auto"


@dataclass
class SubtitleCfg:
    auto_fill: bool = True            # 源片无字幕时自动补
    mode: str = "auto"                # auto | on | off
    coverage_threshold: float = 0.12  # 字幕覆盖率低于此值视为「源片无字幕」
    font: str = ""                    # 留空自动探测系统中文字体
    size: int = 58
    max_chars: int = 15               # 单行最多字数（中文按 1、ASCII 按 0.5 计宽）
    bottom_pct: float = 0.06          # 字幕底边距原画面底部比例
    stroke: int = 3
    min_cue: float = 0.55


@dataclass
class CommentaryCfg:
    """解说模式：原片只做「引用片段」，成片主体是自己的口播与原创新画面。

    为什么要有这个模式：抖音把「原片连续画面 + 原声 + 原硬字幕」判为
    「片段拼接/拆条」，B 站判为「网络剧片」。这两条都不是水印问题，
    后期处理改不了。唯一能过的形态是平台自己写的「口播解说 / 观点梳理」：
    原创音轨占主体、原片只作短暂引用。
    """
    enabled: bool = False            # 默认仍是整段切片，避免影响现有链路
    # 单条引用的最长秒数。这是版权判定里最硬的一个数——引用越短越安全，
    # 但也越容易看不懂，5 秒是「能交代一句台词」与「不构成拆条」的交点。
    quote_max: float = 5.0
    quote_min: float = 2.2
    max_quotes: int = 4              # 引用条数上限：再多就成流水账，占比也压不住
    # 引用总时长 / 成片时长上限，超过就继续压短引用
    quote_ratio_max: float = 0.30
    # 成片目标时长。抖音 60-90 秒的完播率明显好于 2 分钟
    target_seconds: float = 88.0
    # 引用片段的原声压低多少 dB。保留一点环境声能看出是原片，
    # 但主体必须是解说音轨，否则等于没改形态。
    quote_audio_gain_db: float = -18.0
    # 引用画面占画布宽度的比例。内缩 + 深色底 = 视觉上就在说「这是引用」，
    # 顺手也让它没法被当成一条独立成片直接发出去。
    quote_inset: float = 0.78
    pad_color: str = "0x14161C"
    # 配音
    tts: bool = True
    voice: str = "Tingting"          # macOS say 的中文语音，没有则退化为静音
    rate: int = 190                  # say 语速（词/分）
    lead_pad: float = 0.35           # 每段口播尾留白，避免两段粘连
    # 文字卡
    card_bg: str = "#2A3342"         # 必须亮于 10% 亮度，否则 QC 判黑场
    card_accent: str = "#E0A458"
    card_footer: str = "剧情评述 · 片段引用"
    # 口播词长度约束（字）。35-70 字约等于 9-17 秒，五段正好压在 90 秒内
    talk_min_chars: int = 35
    talk_max_chars: int = 70
    llm: bool = True
    bgm: bool = False


@dataclass
class ResourceCfg:
    """跑批的资源上限。默认值按「10 核 / 32GB 上跑得舒服」标定。"""
    max_workers: int = 0             # 0 = 按核数与内存自动推算
    cpu_per_worker: int = 3          # 每个 worker 期望占用的核数
    mem_per_worker_gb: float = 2.5   # 每个 worker 峰值内存（700s 素材实测约 2GB）
    min_free_mem_gb: float = 3.0     # 可用内存低于此值就暂停派活
    max_load_per_core: float = 1.2   # 单核负载超过此值就暂停派活
    nice: int = 5                    # 子进程降优先级，保住前台交互
    gate_max_wait: float = 180.0     # 闸门最长等待，超时放行


@dataclass
class OutputCfg:
    """出片数量：同一部剧可以出多套互不重复的方案（组合式批量出片）。"""
    reels: int = 2                   # 每套方案出几条
    variants: int = 1                # 出几套方案
    per_plan: int = 3                # 指定总条数时，单次规划最多给几条（>3 质量会掉）
    max_total: int = 12              # 单部剧总条数上限，防手滑
    dedupe_hook: bool = True         # 跨方案不复用同一段 hook
    overlap: float = 0.6             # 与前几套的候选重合率超过此值即驳回


@dataclass
class PathsCfg:
    work: str = str(PROJECT_ROOT / "work")
    out: str = str(PROJECT_ROOT / "out")
    log: str = str(PROJECT_ROOT / "logs")


@dataclass
class Config:
    asr: ASRCfg = field(default_factory=ASRCfg)
    ocr: OCRCfg = field(default_factory=OCRCfg)
    vision: VisionCfg = field(default_factory=VisionCfg)
    recall: RecallCfg = field(default_factory=RecallCfg)
    planner: PlannerCfg = field(default_factory=PlannerCfg)
    render: RenderCfg = field(default_factory=RenderCfg)
    bgm: BGMCfg = field(default_factory=BGMCfg)
    subtitle: SubtitleCfg = field(default_factory=SubtitleCfg)
    commentary: CommentaryCfg = field(default_factory=CommentaryCfg)
    output: OutputCfg = field(default_factory=OutputCfg)
    resource: ResourceCfg = field(default_factory=ResourceCfg)
    paths: PathsCfg = field(default_factory=PathsCfg)
    workers: int = 3                 # 同时处理的剧数
    force: bool = False
    dry_run: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _apply(obj: Any, data: dict) -> None:
    for k, v in (data or {}).items():
        if not hasattr(obj, k):
            continue
        cur = getattr(obj, k)
        if isinstance(v, dict) and hasattr(cur, "__dataclass_fields__"):
            _apply(cur, v)
        elif v is not None:
            setattr(obj, k, v)


def load(path: str | Path | None = None, overrides: dict | None = None) -> Config:
    cfg = Config()

    base = PROJECT_ROOT / "config.yaml"
    # 先把基础配置读进来，再依次用 config.local.yaml（本机私有，已 gitignore）
    # 和显式传入的 path 覆盖。这样不带 --config 也能读到本地的 api_key / 模型路径。
    targets = [base] if base.exists() else []
    local = PROJECT_ROOT / "config.local.yaml"
    if local.exists():
        targets.append(local)
    if path and Path(path).resolve() != local.resolve():
        targets.append(Path(path))
    for target in targets:
        if target.exists() and yaml is not None:
            _apply(cfg, yaml.safe_load(target.read_text(encoding="utf-8")) or {})

    if overrides:
        _apply(cfg, overrides)

    # 环境变量优先级最高：方便 docker / crontab 注入
    if not cfg.planner.api_key:
        cfg.planner.api_key = os.environ.get(cfg.planner.api_key_env, "")
    if os.environ.get("FSM_WORKERS"):
        cfg.workers = int(os.environ["FSM_WORKERS"])

    # 默认路径补齐
    root = PROJECT_ROOT
    if not cfg.asr.python:
        cfg.asr.python = _guess_funasr_python()
    if not cfg.asr.worker:
        cfg.asr.worker = str(root / "scripts" / "asr_worker.py")
    if not cfg.asr.server:
        cfg.asr.server = str(root / "scripts" / "asr_server.py")
    if not cfg.bgm.index:
        cfg.bgm.index = str(root / "assets" / "bgm_index.json")
    cfg.paths.work = os.path.expanduser(cfg.paths.work)
    cfg.paths.out = os.path.expanduser(cfg.paths.out)
    cfg.paths.log = os.path.expanduser(cfg.paths.log)
    return cfg
