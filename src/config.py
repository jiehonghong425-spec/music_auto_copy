"""配置管理模块 — 使用 Pydantic 加载和验证 config.yaml"""

from pathlib import Path
from typing import Literal, Optional
import yaml
from pydantic import BaseModel, Field


class AudioJungleConfig(BaseModel):
    """AudioJungle API 相关配置"""
    api_strategy: Literal["edge", "scrape", "official"] = "edge"
    api_token: Optional[str] = None
    proxy: Optional[str] = None
    page_delay: float = 1.0  # 翻页间隔（秒）


class SearchConfig(BaseModel):
    """搜索过滤参数"""
    category: str = "music"
    sort: Literal["date", "sales", "rating", "price"] = "date"
    date: Literal["all", "today", "this-week", "this-month", "this-year"] = "this-year"
    price_min: Optional[int] = None
    price_max: int = 19
    tags: list[str] = Field(default_factory=list)
    max_pages: int = 0  # 0 = 不限制
    max_items: int = 0  # 0 = 不限制


class PresetConfig(BaseModel):
    """下载量预设"""
    max_items: int = 50
    concurrency: int = 5


class DownloadConfig(BaseModel):
    """下载相关配置"""
    concurrency: int = 5
    output_dir: Path = Path("./downloads/previews")
    overwrite: bool = False
    verify_hash: bool = True
    max_retries: int = 3
    retry_delay: float = 5.0
    presets: dict[str, PresetConfig] = Field(default_factory=lambda: {
        "small": PresetConfig(max_items=20, concurrency=3),
        "medium": PresetConfig(max_items=100, concurrency=5),
        "large": PresetConfig(max_items=500, concurrency=8),
    })


class LocalSepConfig(BaseModel):
    """本地人声分离配置"""
    model: str = "htdemucs"  # htdemucs(快) | htdemucs_ft(慢) | mdx_extra
    device: Literal["auto", "cpu", "cuda", "directml"] = "auto"
    two_stems: bool = True  # True = 仅人声+伴奏; False = 鼓/贝斯等全部分离
    jobs: int = 4  # 并行线程数 (1-8)
    model_dir: Path = Path("./models")
    output_dir: Path = Path("./separated")


class SeparationConfig(BaseModel):
    """人声分离总配置"""
    engine: Literal["local"] = "local"
    local: LocalSepConfig = Field(default_factory=LocalSepConfig)


class ProgressConfig(BaseModel):
    """进度跟踪配置"""
    db_path: Path = Path("./progress.db")
    resume: bool = True


class Config(BaseModel):
    """应用总配置"""
    audiojungle: AudioJungleConfig = Field(default_factory=AudioJungleConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    separation: SeparationConfig = Field(default_factory=SeparationConfig)
    progress: ProgressConfig = Field(default_factory=ProgressConfig)

    @classmethod
    def load(cls, path: Path | str = "./config.yaml") -> "Config":
        """从 YAML 文件加载配置"""
        path = Path(path)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
        return cls.model_validate(data)

    def save(self, path: Path | str = "./config.yaml") -> None:
        """保存配置到 YAML 文件"""
        path = Path(path)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(
                self.model_dump(exclude_none=True),
                f,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )

    def merge_preset(self, preset_name: str | None):
        """将预设合并到搜索和下载配置"""
        if preset_name and preset_name in self.download.presets:
            preset = self.download.presets[preset_name]
            if self.search.max_items == 0:
                self.search.max_items = preset.max_items
            if self.download.concurrency == 5:  # 仍是默认值
                self.download.concurrency = preset.concurrency

    @classmethod
    def create_default(cls) -> "Config":
        """创建并保存默认配置"""
        config = cls()
        config.save()
        return config


def load_config(config_path: str | None = None) -> Config:
    """便捷函数：加载配置，不存在则创建默认"""
    path = Path(config_path) if config_path else Path("./config.yaml")
    if not path.exists():
        return Config.create_default()
    return Config.load(path)
