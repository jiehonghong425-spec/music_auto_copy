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
from urllib.parse import quote

# 修复 Windows 编码
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import aiofiles
import httpx
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
    # 插入默认站点（按 scraper_type 去重）
    existing_types = {s["scraper_type"] for s in db.list_sites()}
    defaults = [
        ("AudioJungle (国外)", "https://audiojungle.net", "audiojungle"),
        ("网易云音乐", "https://music.163.com", "netease"),
        ("QQ音乐", "https://y.qq.com", "qqmusic"),
        ("酷狗音乐", "http://www.kugou.com", "kugou"),
    ]
    for name, url, stype in defaults:
        if stype not in existing_types:
            db.add_site(name, url, stype)
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
        "cookies": {
            "netease": config.cookies.netease[:20] + "..." if len(config.cookies.netease) > 20 else config.cookies.netease,
            "qqmusic": config.cookies.qqmusic[:20] + "..." if len(config.cookies.qqmusic) > 20 else config.cookies.qqmusic,
            "kugou": config.cookies.kugou[:20] + "..." if len(config.cookies.kugou) > 20 else config.cookies.kugou,
        },
    }


@app.post("/api/cookies")
async def save_cookies(netease: str = "", qqmusic: str = "", kugou: str = "", raw: str = ""):
    """保存并验证 VIP Cookie"""
    # 如果粘贴了原始 Cookie 字符串，自动解析
    if raw and not netease:
        pairs = {}
        for part in raw.replace("\n", "").replace("\r", "").split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                pairs[k.strip()] = v.strip()
        if "MUSIC_U" in pairs: netease = pairs["MUSIC_U"]
        if "uin" in pairs: qqmusic = pairs["uin"]
        elif "qqmusic_key" in pairs: qqmusic = pairs["qqmusic_key"]
        if "kg_mid" in pairs: kugou = pairs["kg_mid"]
        elif "kg_mid_v2" in pairs: kugou = pairs["kg_mid_v2"]

    if netease: config.cookies.netease = netease
    if qqmusic: config.cookies.qqmusic = qqmusic
    if kugou: config.cookies.kugou = kugou
    config.save("./config.yaml")

    # 验证 Cookie 是否有效
    valid = {}
    verify_tasks = []
    if netease:
        verify_tasks.append(("netease", _verify_netease_cookie(netease)))
    if qqmusic:
        verify_tasks.append(("qqmusic", _verify_qq_cookie(qqmusic)))
    if kugou:
        verify_tasks.append(("kugou", _verify_kugou_cookie(kugou)))

    for name, task in verify_tasks:
        try:
            valid[name] = await task
        except Exception:
            valid[name] = False

    return {
        "ok": True,
        "parsed": {"netease": bool(netease), "qqmusic": bool(qqmusic), "kugou": bool(kugou)},
        "valid": valid,
    }


async def _verify_netease_cookie(cookie: str) -> bool:
    """验证网易云 Cookie：请求用户信息接口"""
    try:
        async with httpx.AsyncClient(timeout=8) as cli:
            r = await cli.get(
                "https://music.163.com/api/nuser/account/get",
                headers={"Cookie": f"MUSIC_U={cookie}", "User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com"},
            )
            if r.status_code == 200:
                data = r.json()
                return data.get("code") == 200
            return False
    except Exception:
        return False


async def _verify_qq_cookie(cookie: str) -> bool:
    """验证 QQ音乐 Cookie"""
    try:
        async with httpx.AsyncClient(timeout=8) as cli:
            r = await cli.get(
                "https://u.y.qq.com/cgi-bin/musicu.fcg",
                headers={"Cookie": f"uin={cookie}", "User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com"},
                params={"data": '{"req_0":{"module":"userInfo.BaseUserInfoServer","method":"get_user_baseinfo_v2","param":{}}}'},
            )
            if r.status_code == 200:
                data = r.json()
                return data.get("req_0", {}).get("code") == 0
            return False
    except Exception:
        return False


async def _verify_kugou_cookie(cookie: str) -> bool:
    """验证酷狗 Cookie：请求用户信息"""
    try:
        async with httpx.AsyncClient(timeout=8) as cli:
            r = await cli.get(
                "http://kmr.service.kugou.com/v1/user/get_info",
                headers={"Cookie": f"kg_mid={cookie}", "User-Agent": "Mozilla/5.0"},
            )
            if r.status_code == 200:
                data = r.json()
                return data.get("status") == 1 or data.get("error_code") == 0
            return False
    except Exception:
        return False


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
    sites = db.list_sites()
    # 并行检测所有站点连接状态（有缓存时不发起新请求）
    checks = await asyncio.gather(*[_check_site_alive(s["url"]) for s in sites])
    for s, online in zip(sites, checks):
        s["online"] = online
    return sites


_SITE_CACHE: dict[str, tuple[float, bool]] = {}

async def _check_site_alive(url: str) -> bool:
    """检测站点是否可达（60秒缓存，超时3秒）"""
    now = time.time()
    if url in _SITE_CACHE:
        t, result = _SITE_CACHE[url]
        if now - t < 60:
            return result
    try:
        async with httpx.AsyncClient(timeout=3) as cli:
            r = await cli.head(url, headers={"User-Agent": "Mozilla/5.0"})
            result = r.status_code < 500
    except Exception:
        result = False
    _SITE_CACHE[url] = (now, result)
    return result


@app.post("/api/sites/refresh")
async def refresh_sites():
    """强制刷新所有站点连接状态"""
    global _SITE_CACHE
    _SITE_CACHE = {}
    sites = db.list_sites()
    results = {}
    for s in sites:
        results[s["name"]] = await _check_site_alive(s["url"])
    return {"status": results}


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
    """从指定站点搜索音频（支持 AudioJungle + 国内音乐站）"""
    from src.downloader.client import AudioJungleClient
    from src.downloader.chinese_sites import get_scraper
    from src.config import SearchConfig

    site = db.conn.execute("SELECT * FROM sites WHERE id = ?", (body.site_id,)).fetchone()
    if not site:
        raise HTTPException(404, "站点不存在")

    scraper_type = site["scraper_type"]
    keyword = body.tags or ""  # 国内站用 tags 字段做搜索关键词

    # ── 国内音乐站 ──
    if scraper_type in ("netease", "qqmusic", "kugou"):
        # 读取用户的 Cookie 配置
        default_keys = {"netease": "MUSIC_U", "qqmusic": "uin", "kugou": "kg_mid"}
        cookies = {}
        cookie_conf = getattr(config, "cookies", None)
        if cookie_conf:
            raw = getattr(cookie_conf, scraper_type, "")
            if raw:
                if "=" in raw:
                    # key=value 格式
                    for pair in raw.split(";"):
                        if "=" in pair:
                            k, v = pair.strip().split("=", 1)
                            cookies[k.strip()] = v.strip()
                else:
                    # 直接粘贴的值，用默认 key
                    cookies[default_keys.get(scraper_type, "token")] = raw.strip()

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                scraper = get_scraper(scraper_type, client, cookies if cookies else None)
                if not scraper:
                    raise HTTPException(400, f"不支持的站点类型: {scraper_type}")
                items = await asyncio.wait_for(
                    scraper.search(keyword, body.max_items or 20), timeout=30
                )
        except asyncio.TimeoutError:
            raise HTTPException(504, "搜索超时")
        except Exception as e:
            raise HTTPException(502, f"搜索失败: {str(e)[:200]}")

        # 不自动入库，返回结果让用户选择加入待处理队列
        return {"items": items, "count": len(items)}

    # ── AudioJungle ──
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


# ── 待处理队列 ──────────────────────────────────────────

class QueueAddRequest(BaseModel):
    items: list[dict] = []  # [{id, title, author, preview_url, source, ...}]


@app.get("/api/queue")
async def get_queue():
    """获取待处理队列"""
    items = db.get_items_by_status("pending")
    return {"items": items, "count": len(items)}


@app.post("/api/queue/add")
async def add_to_queue(body: QueueAddRequest):
    """添加项目到待处理队列"""
    added = 0
    for item in body.items:
        sid = item.get("id", "")
        # 字符串 ID 映射成整数
        if isinstance(sid, str):
            sid = abs(hash(str(sid))) % (10 ** 9)
        source = item.get("source", "")
        source_id = item.get("source_id", item.get("id", ""))
        try:
            db.conn.execute(
                """INSERT OR IGNORE INTO items (id, title, author, preview_url, status, category, tags)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                (sid, item.get("title", ""), item.get("author", ""),
                 item.get("preview_url", ""), source,
                 json.dumps({"source": source, "source_id": source_id})),
            )
            if db.conn.total_changes > 0:
                added += 1
        except Exception:
            pass
    db.conn.commit()
    return {"ok": True, "added": added}


@app.delete("/api/queue/{item_id}")
async def remove_from_queue(item_id: int):
    """从队列中移除项目"""
    db.conn.execute("DELETE FROM items WHERE id = ? AND status = 'pending'", (item_id,))
    db.conn.commit()
    return {"ok": True}


# ── 下载 ──────────────────────────────────────────────────

@app.post("/api/download")
async def download_items(body: DownloadRequest):
    """下载选中项目的预览音频（支持 AudioJungle + 国内站 + 本地上传）"""
    from src.downloader.client import AudioJungleClient
    from src.downloader.chinese_sites import get_scraper

    items = []
    for iid in body.item_ids:
        row = db.conn.execute("SELECT * FROM items WHERE id = ? AND status = 'pending'", (iid,)).fetchone()
        if row:
            items.append(dict(row))

    if not items:
        raise HTTPException(400, "没有待下载的项目")

    # ── 本地上传文件快通道：已有文件直接标记已下载 ──
    local_items = []
    remote_items = []
    for item in items:
        dp = item.get("download_path", "")
        if dp and Path(dp).exists() and Path(dp).stat().st_size > 1000:
            local_items.append(item)
        else:
            remote_items.append(item)

    for item in local_items:
        db.update_status(item["id"], "downloaded")

    if not remote_items:
        return {"downloaded": len(local_items)}

    items = remote_items

    # 区分来源
    source = ""
    for item in items:
        try:
            tags = json.loads(item.get("tags", "[]"))
            if isinstance(tags, dict):
                source = tags.get("source", "")
        except (json.JSONDecodeError, TypeError):
            pass
        if source:
            break

    # ── 国内站下载 ──
    if source in ("netease", "qqmusic", "kugou"):
        # 读取 Cookie
        default_keys = {"netease": "MUSIC_U", "qqmusic": "uin", "kugou": "kg_mid"}
        cookies = {}
        cookie_conf = getattr(config, "cookies", None)
        if cookie_conf:
            raw = getattr(cookie_conf, source, "")
            if raw:
                if "=" in raw:
                    for pair in raw.split(";"):
                        if "=" in pair:
                            k, v = pair.strip().split("=", 1)
                            cookies[k.strip()] = v.strip()
                else:
                    cookies[default_keys.get(source, "token")] = raw.strip()

        downloaded = 0
        async with httpx.AsyncClient(timeout=60) as client:
            scraper = get_scraper(source, client, cookies if cookies else None)
            if not scraper:
                raise HTTPException(400, f"不支持的站点: {source}")

            output_dir = Path(config.download.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            for item in items:
                iid = item["id"]
                # 从 tags 获取原始 source_id
                try:
                    tags = json.loads(item.get("tags", "{}"))
                    source_id = tags.get("source_id", iid) if isinstance(tags, dict) else iid
                except (json.JSONDecodeError, TypeError):
                    source_id = iid

                db.update_status(iid, "downloading")
                try:
                    dl_url = await scraper.get_download_url(source_id)
                    if not dl_url:
                        db.update_status(iid, "failed", "无法获取下载链接（可能需要会员）")
                        continue

                    # 下载文件
                    safe_title = "".join(c for c in (item.get("title") or f"track_{iid}") if c.isalnum() or c in " _-")[:60]
                    item_dir = output_dir / f"{iid}_{safe_title}"
                    item_dir.mkdir(parents=True, exist_ok=True)
                    dest = item_dir / "preview.mp3"

                    r = await client.get(dl_url, follow_redirects=True)
                    r.raise_for_status()
                    dest.write_bytes(r.content)

                    if dest.exists() and dest.stat().st_size > 1000:
                        db.update_download_path(iid, str(dest))
                        db.update_status(iid, "downloaded")
                        downloaded += 1
                    else:
                        dest.unlink(missing_ok=True)
                        db.update_status(iid, "failed", "下载文件为空")
                except Exception as e:
                    db.update_status(iid, "failed", str(e)[:500])

        return {"downloaded": downloaded + len(local_items)}

    # ── AudioJungle 下载 ──
    config.download.concurrency = body.concurrency
    async with AudioJungleClient(config) as client:
        count = await client.download_items(items, db, config.download)
    return {"downloaded": count + len(local_items)}


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
        to_separate += db.get_items_by_status("failed")
        # 也包含待处理但有本地文件的项目（本地上传）
        pending = db.get_items_by_status("pending")
        for p in pending:
            dp = p.get("download_path", "")
            if dp and Path(dp).exists() and Path(dp).stat().st_size > 1000:
                to_separate.append(p)

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
    """列出所有文件（精简字段，快速加载）"""
    items = db.get_all_items_light(status_filter=status, limit=limit, offset=offset)
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
    """复制文件路径（返回绝对路径供后续操作）"""
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "文件不存在")

    path_key = "instrumental_path" if stem == "instrumental" else "vocals_path"
    file_path = item.get(path_key, "")
    if not file_path or not Path(file_path).exists():
        raise HTTPException(404, f"文件不存在: {stem}")

    return {"path": str(Path(file_path).resolve()), "stem": stem, "title": item["title"]}


@app.get("/api/files/{item_id}/download")
async def download_file(item_id: int, stem: str = "instrumental", format: str = "wav"):
    """直接下载/转换音频文件。format=wav|mp3|flac|ogg"""
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, f"项目 {item_id} 不存在")

    path_map = {"instrumental": "instrumental_path", "vocals": "vocals_path", "download": "download_path"}
    path_key = path_map.get(stem, "instrumental_path")
    file_path = item.get(path_key, "")
    if not file_path:
        raise HTTPException(404, f"无 {stem} 路径记录")

    src = Path(file_path)
    if not src.is_absolute():
        src = Path.cwd() / file_path
    if not src.exists():
        raise HTTPException(404, f"源文件不存在")

    # 如果请求格式与原文件相同，直接返回
    if format == src.suffix.lstrip("."):
        safe = "".join(c for c in (item.get("title") or "audio") if c.isalnum() or c in " _-.")[:40].strip()
        return FileResponse(str(src), filename=f"{safe}_{stem}{src.suffix}", media_type=f"audio/{format}")

    # 格式转换（FFmpeg）
    if format not in ("wav", "mp3", "flac", "ogg"):
        raise HTTPException(400, f"不支持的格式: {format}")

    out_file = src.with_suffix(f".{format}")
    if not out_file.exists():
        try:
            codec_map = {"mp3": "libmp3lame", "flac": "flac", "ogg": "libvorbis", "wav": "pcm_s16le"}
            result = subprocess.run(
                ["ffmpeg", "-i", str(src), "-acodec", codec_map.get(format, "copy"),
                 "-y", str(out_file), "-loglevel", "error"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                raise HTTPException(500, f"转换失败: {result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            raise HTTPException(500, "转换超时")

    safe = "".join(c for c in (item.get("title") or "audio") if c.isalnum() or c in " _-.")[:40].strip()
    media_map = {"mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg", "wav": "audio/wav"}
    return FileResponse(str(out_file), filename=f"{safe}_{stem}.{format}", media_type=media_map.get(format, "audio/wav"))


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
