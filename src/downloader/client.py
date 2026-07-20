"""AudioJungle 客户端 — HTML 解析 + 下载"""

import asyncio
import json
import re
import html as html_mod
from pathlib import Path
from typing import Optional

import httpx
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeRemainingColumn,
)

from src.config import Config, SearchConfig, DownloadConfig
from src.db import Database
from src.utils.retry import sanitize_filename

# AudioJungle 基础 URL
BASE_URL = "https://audiojungle.net"


class AudioJungleClient:
    """AudioJungle HTTP 客户端（HTML 解析模式）"""

    def __init__(self, config: Config):
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None
        self._semaphore: Optional[asyncio.Semaphore] = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "no-cache",
                "Sec-Ch-Ua": (
                    '"Google Chrome";v="125", "Chromium";v="125", '
                    '"Not.A/Brand";v="24"'
                ),
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            }
            limits = httpx.Limits(
                max_keepalive_connections=self.config.download.concurrency + 5,
                max_connections=self.config.download.concurrency + 10,
            )
            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(60.0, connect=15.0),
                limits=limits,
                follow_redirects=True,
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ── 搜索 ─────────────────────────────────────────────

    async def search_items(self, search_config: SearchConfig) -> list[dict]:
        """搜索音乐项目并返回列表，支持自动翻页"""
        all_items: list[dict] = []
        seen_ids: set[int] = set()
        page = 1
        max_pages = search_config.max_pages or 9999

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TextColumn("[dim]{task.fields[status]}[/]"),
        ) as progress:
            task = progress.add_task(
                "[cyan]搜索中...", total=None, status=""
            )

            next_url = self._build_category_url(search_config, page)

            while next_url and page <= max_pages:
                progress.update(
                    task,
                    description=f"[cyan]搜索第 {page} 页...",
                    status=f"已找到 {len(all_items)} 首",
                )

                html_text, next_page_url = await self._fetch_html(next_url)
                if not html_text:
                    break

                items = self._parse_items(html_text)
                new_items = [i for i in items if i["id"] not in seen_ids]

                if not new_items:
                    break  # 没有新内容

                for item in new_items:
                    seen_ids.add(item["id"])
                    all_items.append(item)

                # 达到最大数量
                if (
                    search_config.max_items > 0
                    and len(all_items) >= search_config.max_items
                ):
                    all_items = all_items[: search_config.max_items]
                    break

                next_url = next_page_url
                page += 1

                if next_url:
                    await asyncio.sleep(self.config.audiojungle.page_delay)

            progress.update(
                task,
                description="[green]✓ 搜索完成",
                status=f"共 {len(all_items)} 首",
            )

        return all_items

    def _build_category_url(self, search_config: SearchConfig, page: int) -> str:
        """构建分类页面 URL"""
        params = []
        if search_config.date and search_config.date != "all":
            params.append(f"date={search_config.date}")
        if search_config.price_max:
            params.append(f"price_max={search_config.price_max}")
        if search_config.price_min:
            params.append(f"price_min={search_config.price_min}")
        if search_config.sort:
            params.append(f"sort={search_config.sort}")
        if search_config.tags:
            params.append(f"tags={','.join(search_config.tags)}")
        params.append(f"page={page}")

        query = "&".join(params)
        return f"{BASE_URL}/category/{search_config.category}?{query}"

    async def _fetch_html(
        self, url: str, max_retries: int = 3
    ) -> tuple[Optional[str], Optional[str]]:
        """获取页面 HTML 并提取下一页链接

        Returns:
            (html_text, next_page_url) — next_page_url 为 None 表示没有下一页
        """
        html_text = None
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                response = await self.client.get(url)
                response.raise_for_status()
                html_text = response.text
                last_error = None
                break
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    await asyncio.sleep(2.0 * (attempt + 1))

        if html_text is None:
            from rich.console import Console
            Console().print(
                f"[yellow]Warning: page fetch failed after "
                f"{max_retries} retries: {last_error}[/]"
            )
            return None, None

        # 提取下一页链接
        next_url = None
        next_match = re.search(
            r'<link\s+rel="next"\s+href="([^"]+)"', html_text
        )
        if next_match:
            next_path = next_match.group(1)
            if next_path.startswith("/"):
                next_url = f"{BASE_URL}{next_path}"
            elif next_path.startswith("http"):
                next_url = next_path

        return html_text, next_url

    def _parse_items(self, html_text: str) -> list[dict]:
        """从 HTML 中解析音乐项目数据"""
        items_map: dict[int, dict] = {}

        # 1. 从 data-analytics-click-payload 提取项目信息
        pattern = r'data-analytics-click-payload="([^"]*)"'
        for match in re.finditer(pattern, html_text):
            try:
                payload_str = html_mod.unescape(match.group(1))
                data = json.loads(payload_str)
                ecom_items = data.get("ecommerce", {}).get("items", [])
                for ecom_item in ecom_items:
                    item_id = int(ecom_item.get("itemId", 0))
                    if item_id and item_id not in items_map:
                        items_map[item_id] = {
                            "id": item_id,
                            "title": ecom_item.get("itemName", ""),
                            "author": ecom_item.get("itemBrand", ""),
                            "price": float(ecom_item.get("price", 0)),
                            "price_cents": int(
                                float(ecom_item.get("price", 0)) * 100
                            ),
                            "category": ecom_item.get("itemCategory2", ""),
                            "preview_url": "",
                            "url": f"{BASE_URL}/item/slug/{item_id}",
                        }
            except (json.JSONDecodeError, KeyError, ValueError):
                continue

        # 2. 从 preview-downloads 链接提取 file_id 并与 item 匹配
        # URL 格式: .../files/{file_id}/preview.mp3?...filename={item_id}_{slug}_by_{author}_preview.mp3
        preview_pattern = (
            r'https://preview-downloads\.customer\.envatousercontent\.com/'
            r'files/(\d+)/preview\.mp3\?[^"]*'
            r'filename=(\d+)_[^"]*_preview\.mp3'
        )
        for match in re.finditer(preview_pattern, html_text, re.IGNORECASE):
            try:
                file_id = match.group(1)
                item_id = int(match.group(2))
                # 使用 unsigned URL（更稳定，不需要签名）
                unsigned_url = (
                    f"https://previews.customer.envatousercontent.com/"
                    f"files/{file_id}/preview.mp3"
                )
                if item_id in items_map:
                    items_map[item_id]["preview_url"] = unsigned_url
            except (ValueError, IndexError):
                continue

        return list(items_map.values())

    # ── 下载 ─────────────────────────────────────────────

    async def download_items(
        self,
        items: list[dict],
        db: Database,
        download_config: DownloadConfig,
    ) -> int:
        """批量下载预览音频"""
        if not items:
            return 0

        # 先确保所有项目已入库
        for item in items:
            db.insert_item(
                {
                    "id": item["id"],
                    "title": item.get("title", ""),
                    "author": item.get("author", ""),
                    "preview_url": item.get("preview_url", ""),
                    "price_cents": item.get("price_cents", 0),
                    "category": item.get("category", ""),
                    "tags": item.get("tags", []),
                }
            )

        # 重新查询 pending 项目
        pending = [
            p
            for p in db.get_pending_downloads()
            if p["preview_url"]
        ]

        if self.config.search.max_items > 0:
            pending = pending[: self.config.search.max_items]

        if not pending:
            return 0

        self._semaphore = asyncio.Semaphore(download_config.concurrency)
        output_dir = Path(download_config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        success_count = 0
        failed_count = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("[dim]{task.fields[detail]}[/]"),
            TimeRemainingColumn(),
        ) as progress:
            task = progress.add_task(
                "[cyan]下载中",
                total=len(pending),
                detail="",
            )

            async def download_one(pending_item: dict) -> bool:
                nonlocal success_count, failed_count
                async with self._semaphore:  # type: ignore
                    ok = await self._download_single(
                        pending_item, db, download_config
                    )
                    if ok:
                        success_count += 1
                    else:
                        failed_count += 1
                    progress.update(
                        task,
                        advance=1,
                        detail=f"OK:{success_count} FAIL:{failed_count}",
                    )
                    return ok

            tasks_list = [download_one(p) for p in pending]
            await asyncio.gather(*tasks_list)

        return success_count

    async def _download_single(
        self,
        item: dict,
        db: Database,
        download_config: DownloadConfig,
    ) -> bool:
        """下载单个预览音频"""
        item_id = item["id"]
        preview_url = item["preview_url"]

        if not preview_url:
            db.update_status(item_id, "failed", "无预览链接")
            return False

        # 准备工作目录
        safe_title = sanitize_filename(item.get("title", f"track_{item_id}"))
        dir_name = f"{item_id}_{safe_title}"
        item_dir = Path(download_config.output_dir) / dir_name
        item_dir.mkdir(parents=True, exist_ok=True)

        dest_path = item_dir / "preview.mp3"

        # 检查是否已存在
        if dest_path.exists() and dest_path.stat().st_size > 0:
            if not download_config.overwrite:
                db.update_download_path(item_id, str(dest_path))
                db.update_status(item_id, "downloaded")
                return True

        # 下载
        db.update_status(item_id, "downloading")
        max_retries = download_config.max_retries

        for attempt in range(max_retries + 1):
            try:
                async with self.client.stream("GET", preview_url) as resp:
                    resp.raise_for_status()
                    with open(dest_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(65536):
                            f.write(chunk)

                if dest_path.exists() and dest_path.stat().st_size > 0:
                    db.update_download_path(item_id, str(dest_path))
                    db.update_status(item_id, "downloaded")
                    return True
                else:
                    raise ValueError("下载文件为空")

            except Exception as e:
                if attempt < max_retries:
                    delay = download_config.retry_delay * (2**attempt)
                    await asyncio.sleep(delay)
                else:
                    db.increment_retry(item_id)
                    db.update_status(item_id, "failed", str(e))
                    if dest_path.exists() and dest_path.stat().st_size == 0:
                        dest_path.unlink(missing_ok=True)
                    return False

        return False
