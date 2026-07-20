"""本地人声分离引擎 — 基于 Meta Demucs v4"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
)

from src.config import LocalSepConfig
from src.db import Database

console = Console()


def _find_ffmpeg() -> Optional[Path]:
    """查找 FFmpeg 可执行文件路径"""
    # 常见的 FFmpeg 安装位置
    candidates = [
        "ffmpeg", "ffmpeg.exe",
    ]
    extra_dirs = [
        r"C:\Program Files\FFmpeg\bin",
        r"C:\Program Files (x86)\FFmpeg\bin",
        r"C:\ffmpeg\bin",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Local\Microsoft\WinGet\Links"),
    ]
    for d in extra_dirs:
        p = Path(d) / "ffmpeg.exe"
        if p.exists():
            candidates.insert(0, str(p))

    for c in candidates:
        try:
            result = subprocess.run(
                [c, "-version"], capture_output=True, timeout=10
            )
            if result.returncode == 0:
                # 确保其目录在 PATH 中
                parent = str(Path(c).parent) if Path(c).parent != Path(".") else None
                if parent and parent not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = parent + os.pathsep + os.environ.get("PATH", "")
                return Path(c)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def _detect_device() -> str:
    """自动检测最佳可用设备"""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except ImportError:
        pass

    import platform
    if platform.system() == "Windows":
        try:
            import torch_directml
            return "directml"
        except ImportError:
            pass

    return "cpu"


class LocalSeparationEngine:
    """本地人声分离引擎 — 使用 Demucs CLI"""

    def __init__(self, config: LocalSepConfig):
        self.config = config
        self._ready = False

    def _init_separator(self):
        """验证 demucs 和依赖可用"""
        if self._ready:
            return

        # 1. 检查 FFmpeg（Demucs 解码部分 MP3 需要）
        ffmpeg_path = _find_ffmpeg()
        if ffmpeg_path:
            console.print(f"  [dim]FFmpeg: {ffmpeg_path}[/]")
        else:
            console.print(
                "[red]未找到 FFmpeg！Demucs 需要 FFmpeg 解码音频文件。[/]\n"
                "  安装方法:\n"
                "    Windows: winget install Gyan.FFmpeg\n"
                "    或下载: https://ffmpeg.org/download.html"
            )
            raise RuntimeError("FFmpeg 未安装")

        # 2. 验证 demucs
        try:
            result = subprocess.run(
                [sys.executable, "-m", "demucs", "--help"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                raise RuntimeError("demucs 命令执行失败")
        except FileNotFoundError:
            console.print(
                "[red]未安装 demucs。请运行:[/]\n"
                "  pip install demucs"
            )
            raise
        except Exception as e:
            console.print(f"[red]demucs 检测失败: {e}[/]")
            raise

        device = self.config.device
        if device == "auto":
            device = _detect_device()

        console.print(
            f"  [dim]引擎: Demucs v4 | "
            f"模型: {self.config.model} | "
            f"设备: {device}[/]"
        )
        self._device = device
        self._ready = True

    async def process_batch(
        self, items: list[dict], db: Database
    ) -> int:
        """批量处理人声分离"""
        if not items:
            return 0

        self._init_separator()
        output_base = Path(self.config.output_dir)
        output_base.mkdir(parents=True, exist_ok=True)

        success_count = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("[dim]{task.fields[detail]}[/]"),
        ) as progress:
            task = progress.add_task(
                "[magenta]人声分离中",
                total=len(items),
                detail="",
            )

            for item in items:
                title = item.get("title", "")[:40]
                progress.update(
                    task,
                    description=f"[magenta]分离: {title}",
                    detail=f"✓{success_count}/{len(items)}",
                )

                ok = await self._separate_one(item, db)
                if ok:
                    success_count += 1
                progress.update(task, advance=1)

            progress.update(
                task,
                description="[green]✓ 分离完成",
                detail=f"成功 {success_count}/{len(items)}",
            )

        return success_count

    async def _separate_one(self, item: dict, db: Database) -> bool:
        """分离单个文件"""
        item_id = item["id"]
        download_path = item.get("download_path", "")

        if not download_path or not Path(download_path).exists():
            db.update_status(item_id, "failed", "下载文件不存在")
            return False

        input_file = Path(download_path)
        safe_title = item.get("title", f"track_{item_id}")
        safe_title = "".join(
            c for c in safe_title if c.isalnum() or c in " _-"
        )[:60]

        # Demucs 输出临时目录和最终目录
        tmp_output = Path(self.config.output_dir) / "_tmp"
        tmp_output.mkdir(parents=True, exist_ok=True)
        final_dir = Path(self.config.output_dir) / f"{item_id}_{safe_title}"
        final_dir.mkdir(parents=True, exist_ok=True)

        db.update_status(item_id, "separating")

        try:
            loop = asyncio.get_event_loop()
            success, error_msg = await loop.run_in_executor(
                None,
                self._run_demucs,
                str(input_file),
                str(tmp_output),
            )

            if not success:
                db.update_status(
                    item_id, "failed",
                    f"Demucs 失败: {error_msg}" if error_msg else "Demucs 处理失败"
                )
                console.print(f"  [red]✗ {safe_title}: {error_msg}[/]")
                # 清理残留的临时文件
                shutil.rmtree(tmp_output, ignore_errors=True)
                tmp_output.mkdir(parents=True, exist_ok=True)
                return False

            # 查找 Demucs 输出并移动到最终目录
            instrumental_path, vocals_path = self._collect_outputs(
                tmp_output, final_dir
            )

            if instrumental_path:
                db.update_separation_paths(
                    item_id,
                    str(instrumental_path),
                    str(vocals_path) if vocals_path else "",
                )
                db.update_status(item_id, "separated")
                console.print(
                    f"  [green]✓ {safe_title}[/] "
                    f"[dim](伴奏: {instrumental_path.stat().st_size // 1024}KB, "
                    f"人声: {vocals_path.stat().st_size // 1024 if vocals_path else 0}KB)[/]"
                )
                return True
            else:
                db.update_status(item_id, "failed", "未找到 Demucs 输出文件")
                console.print(f"  [red]✗ {safe_title}: 未找到输出文件[/]")
                return False

        except Exception as e:
            db.update_status(item_id, "failed", str(e))
            console.print(f"  [red]✗ {safe_title}: {e}[/]")
            # 清理残留
            shutil.rmtree(tmp_output, ignore_errors=True)
            return False

    def _run_demucs(self, input_path: str, output_dir: str) -> tuple[bool, str]:
        """同步执行 Demucs CLI，返回 (成功, 错误信息)"""
        cmd = [
            sys.executable, "-m", "demucs",
            "--two-stems", "vocals",   # 仅分离人声+伴奏（不用 = 号，兼容性更好）
            "-n", self.config.model,   # 模型: htdemucs_ft
            "-o", output_dir,          # 输出目录
        ]

        # 设备选择
        if self._device == "cpu":
            cmd.extend(["-d", "cpu"])
            # CPU 模式下利用多核
            cmd.extend(["-j", "4"])
        # cuda 是默认值，无需指定

        cmd.append(input_path)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,  # 60 分钟超时
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                # 提取关键错误行
                error_lines = []
                for line in result.stderr.splitlines():
                    lower = line.lower()
                    if any(kw in lower for kw in ("error", "fail", "traceback", "cannot", "unable")):
                        error_lines.append(line.strip())
                if not error_lines:
                    # 取最后几行
                    error_lines = result.stderr.splitlines()[-5:]
                error_msg = "; ".join(error_lines[:3]) if error_lines else f"返回码 {result.returncode}"
                return False, error_msg
            return True, ""
        except subprocess.TimeoutExpired:
            return False, f"处理超时 (>60分钟): {Path(input_path).name}"
        except Exception as e:
            return False, f"执行异常: {e}"

    def _collect_outputs(
        self, tmp_dir: Path, final_dir: Path
    ) -> tuple[Optional[Path], Optional[Path]]:
        """从 Demucs 临时输出目录收集结果到最终目录

        Demucs 输出结构: tmp_dir/{model_name}/{input_stem}/vocals.wav, no_vocals.wav
        """
        instrumental = None
        vocals = None

        model_name = self.config.model

        # 直接在模型目录下查找
        model_dir = tmp_dir / model_name
        if not model_dir.exists():
            # 尝试查找其他可能的模型目录名
            for child in tmp_dir.iterdir():
                if child.is_dir():
                    model_dir = child
                    break

        if model_dir.exists():
            for sub_dir in model_dir.iterdir():
                if not sub_dir.is_dir():
                    continue
                for f in sub_dir.iterdir():
                    if not f.is_file():
                        continue
                    if f.suffix.lower() not in (".wav", ".mp3", ".flac"):
                        continue

                    name_lower = f.name.lower()
                    # "no_vocals.wav" = 伴奏, "vocals.wav" = 人声
                    # 注意: "no_vocals" 包含 "vocals"，必须优先匹配 no_vocals
                    if name_lower.startswith("no_vocals") or "no_vocals" in name_lower:
                        dest = final_dir / ("instrumental" + f.suffix)
                        shutil.copy2(str(f), str(dest))
                        instrumental = dest
                    elif "vocals" in name_lower:
                        dest = final_dir / ("vocals" + f.suffix)
                        shutil.copy2(str(f), str(dest))
                        vocals = dest

        # 清理临时目录
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        # 保存元数据
        meta_path = final_dir / "metadata.json"
        meta_path.write_text(
            json.dumps(
                {
                    "model": self.config.model,
                    "two_stems": True,
                    "device": self._device,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        return instrumental, vocals
