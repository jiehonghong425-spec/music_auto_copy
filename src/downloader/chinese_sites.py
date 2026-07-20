"""国内音乐站点爬虫 — 网易云/QQ/酷狗"""

import asyncio
import httpx
from typing import Optional


BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}


# ═══════════════════════════════════════════════════════════
# 网易云音乐
# ═══════════════════════════════════════════════════════════

class NeteaseScraper:
    """网易云音乐爬虫 — 搜索 + 试听下载"""

    SEARCH_API = "https://music.163.com/api/search/get"
    SONG_URL_API = "https://music.163.com/api/song/enhance/player/url"
    OUTER_URL = "https://music.163.com/song/media/outer/url?id={}.mp3"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def search(self, keyword: str, limit: int = 20) -> list[dict]:
        """搜索歌曲"""
        params = {"s": keyword, "type": 1, "limit": limit, "offset": 0}
        headers = {**BASE_HEADERS, "Referer": "https://music.163.com"}
        try:
            r = await self.client.get(self.SEARCH_API, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            data = r.json()
            songs = data.get("result", {}).get("songs", [])
        except Exception:
            return []

        results = []
        for s in songs:
            artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
            album = s.get("album", {}).get("name", "")
            results.append({
                "id": s["id"],
                "title": s.get("name", ""),
                "author": artists,
                "album": album,
                "duration": s.get("duration", 0) // 1000,
                "source": "netease",
                "preview_url": self.OUTER_URL.format(s["id"]),
            })
        return results

    async def get_download_url(self, song_id: int, br: int = 128000) -> Optional[str]:
        """获取可下载的音频 URL"""
        params = {"id": song_id, "ids": f"[{song_id}]", "br": br}
        headers = {**BASE_HEADERS, "Referer": "https://music.163.com"}
        try:
            r = await self.client.get(self.SONG_URL_API, params=params, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()
            for d in data.get("data", []):
                url = d.get("url")
                if url:
                    return url
        except Exception:
            pass
        return None


# ═══════════════════════════════════════════════════════════
# QQ音乐
# ═══════════════════════════════════════════════════════════

class QQMusicScraper:
    """QQ音乐爬虫"""

    SEARCH_API = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
    SONG_URL_API = "https://u.y.qq.com/cgi-bin/musicu.fcg"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def search(self, keyword: str, limit: int = 20) -> list[dict]:
        """搜索歌曲"""
        params = {"w": keyword, "n": limit, "format": "json", "p": 1}
        headers = {**BASE_HEADERS, "Referer": "https://y.qq.com"}
        try:
            r = await self.client.get(self.SEARCH_API, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            data = r.json()
            songs = data.get("data", {}).get("song", {}).get("list", [])
        except Exception:
            return []

        results = []
        for s in songs:
            singers = ", ".join(si.get("name", "") for si in s.get("singer", []))
            songmid = s.get("songmid", "")
            media_mid = s.get("media_mid", "") or songmid
            results.append({
                "id": songmid,
                "title": s.get("songname", ""),
                "author": singers,
                "album": s.get("albumname", ""),
                "duration": s.get("interval", 0),
                "source": "qqmusic",
                "preview_url": f"https://y.qq.com/n/ryqq/songDetail/{media_mid}",
            })
        return results

    async def get_download_url(self, song_mid: str) -> Optional[str]:
        """获取可下载的音频 URL"""
        payload = {
            "req_1": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "guid": "0",
                    "songmid": [song_mid],
                    "songtype": [0],
                    "uin": "0",
                    "loginflag": 1,
                    "platform": "20",
                },
            }
        }
        headers = {**BASE_HEADERS, "Referer": "https://y.qq.com"}
        try:
            r = await self.client.post(
                self.SONG_URL_API, json=payload, headers=headers, timeout=10
            )
            r.raise_for_status()
            data = r.json()
            midurlinfo = data.get("req_1", {}).get("data", {}).get("midurlinfo", [])
            if midurlinfo:
                purl = midurlinfo[0].get("purl", "")
                if purl:
                    # 拼接完整 URL
                    return f"http://ws.stream.qqmusic.qq.com/{purl}"
        except Exception:
            pass
        return None


# ═══════════════════════════════════════════════════════════
# 酷狗
# ═══════════════════════════════════════════════════════════

class KugouScraper:
    """酷狗音乐爬虫"""

    SEARCH_API = "http://mobilecdn.kugou.com/api/v3/search/song"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def search(self, keyword: str, limit: int = 20) -> list[dict]:
        """搜索歌曲"""
        params = {"keyword": keyword, "page": 1, "pagesize": limit}
        headers = {**BASE_HEADERS}
        try:
            r = await self.client.get(self.SEARCH_API, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            data = r.json()
            songs = data.get("data", {}).get("info", [])
        except Exception:
            return []

        results = []
        for s in songs:
            results.append({
                "id": s.get("hash", ""),
                "title": s.get("songname", ""),
                "author": s.get("singername", ""),
                "album": s.get("album_name", ""),
                "duration": s.get("duration", 0),
                "source": "kugou",
                "preview_url": s.get("share_url", ""),
            })
        return results

    async def get_download_url(self, file_hash: str) -> Optional[str]:
        """获取可下载的音频 URL"""
        # 酷狗需要先获取 key，再拼接 URL
        if not file_hash:
            return None
        try:
            key_url = f"http://m.kugou.com/app/i/getSongInfo.php?hash={file_hash}&cmd=playInfo"
            r = await self.client.get(key_url, headers=BASE_HEADERS, timeout=10)
            r.raise_for_status()
            data = r.json()
            url = data.get("url", "")
            if url:
                return url
        except Exception:
            pass
        return None


# ═══════════════════════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════════════════════

SCRAPER_MAP = {
    "netease": NeteaseScraper,
    "qqmusic": QQMusicScraper,
    "kugou": KugouScraper,
    "audiojungle": None,  # 用原有的 AudioJungleClient
}


def get_scraper(scraper_type: str, client: httpx.AsyncClient):
    cls = SCRAPER_MAP.get(scraper_type)
    if cls is None:
        return None
    return cls(client)
