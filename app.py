#!/usr/bin/env python3
"""音乐处理平台 — FastAPI Web 后端"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

# 修复 Windows 编码
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import aiofiles
from fastapi import FastAPI, UploadFile, File, Query, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))

from src.config import load_config, Config, LocalSepConfig
from src.db import Database

# ── 全局状态 ──────────────────────────────────────────────

config: Config = None
db: Database = None
separation_running = False
separation_progress = {"current": 0, "total": 0, "title": "", "eta_seconds": 0, "completed": 0}
progress_listeners: list[asyncio.Queue] = []


def _notify_progress():
    """通知所有 SSE 监听者"""
    data = json.dumps(separation_progress, ensure_ascii=False)
    for q in progress_listeners:
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass


# ── 请求模型 ──────────────────────────────────────────────

class SearchRequest(BaseModel):
    site_id: int = 1
    price_max: int = 19
    sort: str = "date"
    tags: str = ""
    max_items: int = 0


class DownloadRequest(BaseModel):
    item_ids: list[int] = []
    concurrency: int = 5


class SeparateRequest(BaseModel):
    item_ids: list[int] = []
    model: str = "htdemucs"
    jobs: int = 4


class SiteCreate(BaseModel):
    name: str
    url: str
    scraper_type: str = "audiojungle"
    config: dict = {}


class ConfigUpdate(BaseModel):
    model: str = "htdemucs"
    jobs: int = 4
    concurrency: int = 5
    output_dir: str = "./separated"
    device: str = "auto"


# ── 应用生命周期 ──────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, db
    config = load_config("./config.yaml")
    db = Database(config.progress.db_path)
    db.reset_stale()
    # 确保 FFmpeg 在 PATH
    for d in [
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"),
        r"C:\Program Files\FFmpeg\bin",
    ]:
        if Path(d).exists() and d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    # 插入默认站点
    existing = db.list_sites()
    if not existing:
        db.add_site("AudioJungle", "https://audiojungle.net", "audiojungle")
    yield
    db.close()


app = FastAPI(title="音乐处理平台", lifespan=lifespan)

# 静态文件
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)


# ── 配置 API ──────────────────────────────────────────────

@app.get("/api/config")
async def get_config():
    return {
        "model": config.separation.local.model,
        "jobs": config.separation.local.jobs,
        "device": config.separation.local.device,
        "concurrency": config.download.concurrency,
        "output_dir": str(config.separation.local.output_dir),
        "download_dir": str(config.download.output_dir),
        "price_max": config.search.price_max,
        "port": config.server.port,
        "host": config.server.host,
    }


@app.put("/api/config")
async def update_config(body: ConfigUpdate):
    config.separation.local.model = body.model
    config.separation.local.jobs = body.jobs
    config.separation.local.device = body.device  # type: ignore
    config.download.concurrency = body.concurrency
    config.separation.local.output_dir = Path(body.output_dir)
    return {"ok": True}


# ── 站点管理 ──────────────────────────────────────────────

@app.get("/api/sites")
async def list_sites():
    return db.list_sites()


@app.post("/api/sites")
async def add_site(body: SiteCreate):
    sid = db.add_site(body.name, body.url, body.scraper_type, body.config)
    return {"id": sid, "ok": True}


@app.delete("/api/sites/{site_id}")
async def delete_site(site_id: int):
    ok = db.delete_site(site_id)
    if not ok:
        raise HTTPException(404, "站点不存在")
    return {"ok": True}


# ── 搜索 ──────────────────────────────────────────────────

@app.post("/api/search")
async def search_audio(body: SearchRequest):
    """从指定站点搜索音频"""
    from src.downloader.client import AudioJungleClient
    from src.config import SearchConfig

    site = db.conn.execute("SELECT * FROM sites WHERE id = ?", (body.site_id,)).fetchone()
    if not site:
        raise HTTPException(404, "站点不存在")

    sc = SearchConfig(
        price_max=body.price_max,
        sort=body.sort,  # type: ignore
        tags=[t.strip() for t in body.tags.split(",") if t.strip()],
        max_items=body.max_items if body.max_items > 0 else config.search.max_items,
    )

    try:
        async with AudioJungleClient(config) as client:
            items = await asyncio.wait_for(
                client.search_items(sc), timeout=120
            )
    except asyncio.TimeoutError:
        raise HTTPException(504, "搜索超时，请检查网络连接（AudioJungle 可能需要代理）")
    except Exception as e:
        raise HTTPException(502, f"搜索失败: {str(e)[:200]}")

    for item in items:
        db.insert_item({
            "id": item["id"],
            "title": item.get("title", ""),
            "author": item.get("author", ""),
            "preview_url": item.get("preview_url", ""),
            "price_cents": item.get("price_cents", 0),
            "category": item.get("category", ""),
            "tags": [],
        })
    return {"items": items, "count": len(items)}


# ── 下载 ──────────────────────────────────────────────────

@app.post("/api/download")
async def download_items(body: DownloadRequest):
    """下载选中项目的预览音频"""
    from src.downloader.client import AudioJungleClient

    items = []
    for iid in body.item_ids:
        row = db.conn.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
        if row:
            items.append(dict(row))

    if not items:
        raise HTTPException(400, "没有可下载的项目")

    config.download.concurrency = body.concurrency
    async with AudioJungleClient(config) as client:
        count = await client.download_items(items, db, config.download)
    return {"downloaded": count}


# ── 分离 ──────────────────────────────────────────────────

@app.post("/api/separate")
async def start_separation(body: SeparateRequest):
    """启动人声分离任务（后台运行）"""
    global separation_running, separation_progress

    if separation_running:
        raise HTTPException(400, "分离任务正在运行中")

    to_separate = []
    if body.item_ids:
        for iid in body.item_ids:
            row = db.conn.execute(
                "SELECT * FROM items WHERE id = ? AND status IN ('downloaded', 'failed')",
                (iid,),
            ).fetchone()
            if row:
                to_separate.append(dict(row))
    else:
        to_separate = db.get_items_by_status("downloaded")
        # 也包含之前失败的
        to_separate += db.get_items_by_status("failed")

    if not to_separate:
        raise HTTPException(400, "没有待分离的文件")

    # 更新配置
    if body.model:
        config.separation.local.model = body.model
    if body.jobs:
        config.separation.local.jobs = body.jobs

    separation_running = True
    separation_progress = {
        "current": 0, "total": len(to_separate),
        "title": "", "eta_seconds": 0, "completed": 0,
    }
    _notify_progress()

    # 后台执行
    asyncio.create_task(_run_separation(to_separate))
    return {"ok": True, "total": len(to_separate)}


@app.post("/api/separate/stop")
async def stop_separation():
    global separation_running
    separation_running = False
    return {"ok": True}


async def _run_separation(items: list[dict]):
    global separation_running, separation_progress

    from src.separator.local_engine import LocalSeparationEngine

    engine = LocalSeparationEngine(config.separation.local)
    try:
        engine._init_separator()
    except Exception as e:
        separation_running = False
        separation_progress["title"] = f"初始化失败: {e}"
        _notify_progress()
        return

    completed = 0
    total = len(items)
    start_time = time.time()

    for i, item in enumerate(items):
        if not separation_running:
            break

        title = item.get("title", "")[:40]
        separation_progress["current"] = i + 1
        separation_progress["title"] = title
        separation_progress["completed"] = completed

        # 估算剩余时间
        elapsed = time.time() - start_time
        if completed > 0:
            avg = elapsed / completed
            remaining = (total - completed) * avg
            separation_progress["eta_seconds"] = int(remaining)
        _notify_progress()

        ok = await engine._separate_one(item, db)
        if ok:
            completed += 1

    separation_progress["current"] = total
    separation_progress["completed"] = completed
    separation_progress["title"] = "完成"
    separation_progress["eta_seconds"] = 0
    separation_running = False
    _notify_progress()


# ── 进度 SSE ──────────────────────────────────────────────

@app.get("/api/progress/stream")
async def progress_stream():
    """SSE 实时进度推送"""
    async def event_stream():
        q: asyncio.Queue = asyncio.Queue(maxsize=32)
        progress_listeners.append(q)
        try:
            # 发送当前状态
            yield f"data: {json.dumps(separation_progress, ensure_ascii=False)}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            progress_listeners.remove(q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/progress")
async def get_progress():
    """获取当前进度快照"""
    global separation_running
    stats = db.get_stats()
    return {
        **separation_progress,
        "running": separation_running,
        "stats": stats,
    }


# ── 文件管理 ──────────────────────────────────────────────

@app.get("/api/files")
async def list_files(
    status: str = Query("", description="按状态筛选: downloaded, separated, failed"),
    limit: int = 100,
    offset: int = 0,
):
    """列出所有文件"""
    items = db.get_all_items(status_filter=status, limit=limit, offset=offset)
    stats = db.get_stats()
    return {"items": items, "stats": stats}


@app.get("/api/files/{item_id}")
async def get_file(item_id: int):
    """获取单个文件信息"""
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "文件不存在")
    return item


@app.post("/api/files/{item_id}/copy")
async def copy_file(item_id: int, stem: str = "instrumental"):
    """获取文件路径以便复制（返回绝对路径）"""
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "文件不存在")

    path_key = "instrumental_path" if stem == "instrumental" else "vocals_path"
    file_path = item.get(path_key, "")
    if not file_path or not Path(file_path).exists():
        raise HTTPException(404, f"文件不存在: {stem}")

    return {"path": str(Path(file_path).resolve()), "stem": stem, "title": item["title"]}


# ── 音频搜索 ──────────────────────────────────────────────

@app.get("/api/search-audio")
async def audio_search(q: str = Query(..., min_length=1), limit: int = 20):
    """按关键词模糊搜索已分离的音频"""
    results = db.fuzzy_search(q, limit)
    return {"results": results, "query": q, "count": len(results)}


# ── 上传 ──────────────────────────────────────────────────

UPLOAD_DIR = Path("./uploads")
ALLOWED_AUDIO = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma"}
ALLOWED_VIDEO = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv"}
ALLOWED_EXT = ALLOWED_AUDIO | ALLOWED_VIDEO


@app.post("/api/upload")
async def upload_files(files: list[UploadFile] = File(...)):
    """上传本地音频/视频文件"""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    results = []

    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            results.append({"name": f.filename, "ok": False, "error": f"不支持格式 {ext}"})
            continue

        file_type = "video" if ext in ALLOWED_VIDEO else "audio"
        safe_name = f"{int(time.time() * 1000)}_{f.filename}"
        dest_path = UPLOAD_DIR / safe_name

        # 保存文件
        async with aiofiles.open(dest_path, "wb") as out:
            while chunk := await f.read(8 * 1024 * 1024):  # 8MB chunks
                await out.write(chunk)

        size = dest_path.stat().st_size

        # 如果是视频，提取音频
        if file_type == "video":
            audio_path = UPLOAD_DIR / f"{dest_path.stem}_audio.wav"
            try:
                result = subprocess.run(
                    ["ffmpeg", "-i", str(dest_path), "-vn", "-acodec", "pcm_s16le",
                     "-ar", "44100", "-y", str(audio_path)],
                    capture_output=True, text=True, timeout=300,
                )
                if result.returncode != 0:
                    dest_path.unlink(missing_ok=True)
                    results.append({"name": f.filename, "ok": False, "error": "视频音频提取失败"})
                    continue
                # 删视频保留音频
                dest_path.unlink(missing_ok=True)
                dest_path = audio_path
                size = dest_path.stat().st_size
                file_type = "audio"
            except subprocess.TimeoutExpired:
                dest_path.unlink(missing_ok=True)
                results.append({"name": f.filename, "ok": False, "error": "视频处理超时"})
                continue

        # 入库
        title = Path(f.filename).stem
        item_id = db.insert_uploaded_item(title, "本地上传", str(dest_path))
        db.add_uploaded_file(f.filename, str(dest_path), file_type, size, item_id)

        results.append({
            "name": f.filename, "ok": True, "item_id": item_id,
            "size": size, "type": file_type,
        })

    return {"results": results, "count": len(results)}


# ── 统计 ──────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats():
    """获取全局统计"""
    stats = db.get_stats()
    uploaded = db.conn.execute("SELECT COUNT(*) as cnt FROM uploaded_files").fetchone()["cnt"]
    return {**stats, "uploaded": uploaded}


# ── 静态文件 ──────────────────────────────────────────────

@app.get("/")
async def index():
    index_path = static_dir / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>请先创建 static/index.html</h1>")


# ── 启动 ──────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn, webbrowser, threading
    cfg = load_config("./config.yaml")
    url = f"http://localhost:{cfg.server.port}"
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"  浏览器打开: {url}")
    uvicorn.run("app:app", host=cfg.server.host, port=cfg.server.port, reload=True)
