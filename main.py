#!/usr/bin/env python3
"""AudioJungle 预览音频批量下载 + 人声分离 自动化工具

用法:
    python main.py run --preset small       # 完整流程 (小批量)
    python main.py search --price-max 19    # 仅搜索预览
    python main.py download                 # 仅下载
    python main.py separate                 # 仅分离
    python main.py resume                   # 断点续传
    python main.py status                   # 查看进度
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

# 修复 Windows GBK 终端编码问题
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import typer
from rich.console import Console
from rich.table import Table

# 确保项目根在 sys.path
sys.path.insert(0, str(Path(__file__).parent))

from src.config import load_config, Config, SearchConfig
from src.db import Database

app = typer.Typer(
    name="audiojungle-pipeline",
    help="AudioJungle 预览音频下载 + 人声分离工具",
    add_completion=False,
)

console = Console()


# ── 共享选项 ──────────────────────────────────────────────

def _config_option(config_path: Optional[str] = None) -> Config:
    """共享的 --config 选项处理"""
    return load_config(config_path)


# ── run: 完整流水线 ────────────────────────────────────────

@app.command()
def run(
    preset: str = typer.Option(
        None, "--preset", "-p", help="下载量预设: small | medium | large"
    ),
    max_items: int = typer.Option(
        0, "--max-items", "-n", help="最大下载数量 (覆盖预设)"
    ),
    concurrency: int = typer.Option(
        0, "--concurrency", "-c", help="并发下载数 (覆盖预设)"
    ),
    price_max: int = typer.Option(
        0, "--price-max", help="最高价格(美元) (覆盖配置文件)"
    ),
    config_path: str = typer.Option(
        "./config.yaml", "--config", help="配置文件路径"
    ),
):
    """运行完整流水线: 搜索 → 下载 → 人声分离"""
    config = load_config(config_path)
    config.merge_preset(preset)

    # CLI 覆盖
    if max_items > 0:
        config.search.max_items = max_items
    if concurrency > 0:
        config.download.concurrency = concurrency
    if price_max > 0:
        config.search.price_max = price_max

    console.print("[bold cyan]╔══════════════════════════════════════════╗[/]")
    console.print("[bold cyan]║  AudioJungle 下载 + 人声分离 流水线     ║[/]")
    console.print("[bold cyan]╚══════════════════════════════════════════╝[/]")
    console.print(f"  价格上限: ${config.search.price_max}")
    console.print(f"  排序方式: {config.search.sort}")
    console.print(f"  最大数量: {config.search.max_items or '不限'}")
    console.print(f"  并发下载: {config.download.concurrency}")
    console.print(f"  分离引擎: {config.separation.engine}")
    console.print()

    asyncio.run(_run_pipeline(config))


async def _run_pipeline(config: Config):
    """异步执行完整流水线"""
    db = Database(config.progress.db_path)
    db.reset_stale()

    try:
        # Phase 1: 搜索
        console.print("[bold yellow][1/3] 搜索 AudioJungle...[/]")
        from src.downloader.client import AudioJungleClient

        async with AudioJungleClient(config) as client:
            items = await client.search_items(config.search)
            console.print(f"  ✓ 发现 {len(items)} 首音乐")

            if not items:
                console.print("[yellow]没有找到符合条件的音乐，请调整搜索条件[/]")
                return

            # Phase 2: 下载
            console.print(f"\n[bold yellow][2/3] 下载预览音频...[/]")
            downloaded = await client.download_items(items, db, config.download)
            console.print(f"  ✓ 下载完成: {downloaded} 首")

            # Phase 3: 分离
            console.print(f"\n[bold yellow][3/3] 人声分离...[/]")
            from src.separator.local_engine import LocalSeparationEngine

            engine = LocalSeparationEngine(config.separation.local)
            to_separate = db.get_items_by_status("downloaded")
            separated = await engine.process_batch(to_separate, db)
            console.print(f"  ✓ 分离完成: {separated} 首")

        # 最终统计
        stats = db.get_stats()
        _print_stats(stats)

    finally:
        db.close()


# ── search: 仅搜索 ────────────────────────────────────────

@app.command()
def search(
    price_max: int = typer.Option(19, "--price-max", help="最高价格(美元)"),
    sort: str = typer.Option("date", "--sort", help="排序: date|sales|rating|price"),
    tags: str = typer.Option("", "--tags", help="标签, 逗号分隔"),
    max_pages: int = typer.Option(3, "--max-pages", help="最大翻页数"),
    config_path: str = typer.Option("./config.yaml", "--config"),
):
    """搜索 AudioJungle 音乐（不下载）"""
    config = load_config(config_path)
    config.search.price_max = price_max
    config.search.sort = sort  # type: ignore
    config.search.max_pages = max_pages
    if tags:
        config.search.tags = [t.strip() for t in tags.split(",")]

    console.print("[bold cyan]搜索 AudioJungle...[/]")

    async def _search():
        from src.downloader.client import AudioJungleClient

        async with AudioJungleClient(config) as client:
            items = await client.search_items(config.search)

            table = Table(title=f"搜索结果 (价格 ≤ ${price_max})")
            table.add_column("ID", style="dim")
            table.add_column("标题")
            table.add_column("作者")
            table.add_column("价格", justify="right")
            table.add_column("时长")

            for item in items:
                table.add_row(
                    str(item.get("id", "")),
                    item.get("title", "")[:50],
                    item.get("author", ""),
                    f"${item.get('price', 0):.2f}",
                    item.get("length", ""),
                )

            console.print(table)
            console.print(f"\n共 {len(items)} 首音乐")

    asyncio.run(_search())


# ── download: 仅下载 ──────────────────────────────────────

@app.command()
def download(
    max_items: int = typer.Option(0, "--max-items", "-n"),
    concurrency: int = typer.Option(5, "--concurrency", "-c"),
    config_path: str = typer.Option("./config.yaml", "--config"),
):
    """仅下载预览音频（从数据库中的 pending 项目）"""
    config = load_config(config_path)
    if max_items > 0:
        config.search.max_items = max_items
    if concurrency > 0:
        config.download.concurrency = concurrency

    console.print("[bold cyan]下载预览音频...[/]")

    async def _download():
        db = Database(config.progress.db_path)

        # 先搜索（如果数据库为空）
        pending = db.get_pending_downloads()
        if not pending:
            console.print("  数据库为空, 先搜索...")
            from src.downloader.client import AudioJungleClient

            async with AudioJungleClient(config) as client:
                items = await client.search_items(config.search)
                console.print(f"  发现 {len(items)} 首")

        # 下载
        from src.downloader.client import AudioJungleClient

        async with AudioJungleClient(config) as client:
            pending = db.get_pending_downloads(
                config.search.max_items
            )
            console.print(f"  待下载: {len(pending)} 首")
            downloaded = await client.download_items(
                pending, db, config.download
            )
            console.print(f"  ✓ 下载完成: {downloaded} 首")

        db.close()

    asyncio.run(_download())


# ── separate: 仅分离 ──────────────────────────────────────

@app.command()
def separate(
    model: str = typer.Option("htdemucs_ft", "--model", "-m"),
    device: str = typer.Option("auto", "--device", "-d"),
    config_path: str = typer.Option("./config.yaml", "--config"),
):
    """仅执行人声分离（对已下载的文件）"""
    config = load_config(config_path)
    config.separation.local.model = model
    config.separation.local.device = device  # type: ignore

    console.print("[bold cyan]人声分离中...[/]")

    async def _separate():
        db = Database(config.progress.db_path)
        to_separate = db.get_items_by_status("downloaded")
        if not to_separate:
            console.print("[yellow]没有待分离的文件[/]")
            db.close()
            return

        console.print(f"  待分离: {len(to_separate)} 首")
        console.print(f"  模型: {config.separation.local.model}")
        console.print(f"  设备: {config.separation.local.device}")

        from src.separator.local_engine import LocalSeparationEngine

        engine = LocalSeparationEngine(config.separation.local)
        separated = await engine.process_batch(to_separate, db)

        console.print(f"  ✓ 分离完成: {separated} 首")
        db.close()

    asyncio.run(_separate())


# ── resume: 断点续传 ─────────────────────────────────────

@app.command()
def resume(
    config_path: str = typer.Option("./config.yaml", "--config"),
):
    """从上次中断处继续"""
    config = load_config(config_path)
    console.print("[bold cyan]断点续传...[/]")

    asyncio.run(_run_pipeline(config))


# ── status: 查看进度 ──────────────────────────────────────

@app.command()
def status(
    config_path: str = typer.Option("./config.yaml", "--config"),
):
    """查看当前进度"""
    config = load_config(config_path)
    db = Database(config.progress.db_path)
    stats = db.get_stats()
    _print_stats(stats)

    # 显示失败项目
    failed = db.get_items_by_status("failed")
    if failed:
        console.print("\n[red]失败项目:[/]")
        for item in failed:
            console.print(
                f"  [{item['id']}] {item['title'][:50]} — {item.get('error_message', '未知错误')}"
            )

    db.close()


def _print_stats(stats: dict):
    """打印统计表格"""
    table = Table(title="进度统计")
    table.add_column("状态", style="bold")
    table.add_column("数量", justify="right")

    labels = {
        "pending": ("[..] 待处理", "yellow"),
        "downloaded": ("[OK] 已下载", "blue"),
        "separated": ("[OK] 已分离", "green"),
        "failed": ("[XX] 失败", "red"),
    }

    for key, (label, color) in labels.items():
        table.add_row(f"[{color}]{label}[/]", str(stats.get(key, 0)))

    table.add_row("[bold]总计[/]", f"[bold]{stats.get('total', 0)}[/]")
    console.print(table)


# ── 入口 ──────────────────────────────────────────────────

if __name__ == "__main__":
    app()
