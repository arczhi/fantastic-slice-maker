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
    model: str = "paraformer-zh"
    vad_model: str = "fsmn-vad"
    punc_model: str = "ct-punc"
    device: str = "cpu"
    ncpu: int = 6
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
