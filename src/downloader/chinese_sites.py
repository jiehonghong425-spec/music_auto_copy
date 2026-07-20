"""国内音乐站点爬虫 — 网易云/QQ/酷狗

支持的下载策略:
  1. 免费试听 (无需登录) — 30~90 秒片段
  2. Cookie 注入 (用户填自己的 VIP Cookie) — 可下完整歌曲
  3. 外链播放器 (outer URL) — 部分歌曲免费完整版
"""

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


def _merge_cookies(headers: dict, cookies: dict | None) -> dict:
    """将 cookies 字典注入到请求头"""
    if not cookies:
        return headers
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return {**headers, "Cookie": cookie_str}


# ═══════════════════════════════════════════════════════════
# 网易云音乐
# ═══════════════════════════════════════════════════════════

class NeteaseScraper:
    """网易云音乐爬虫 — 多策略试听下载"""

    SEARCH_API = "https://music.163.com/api/search/get"
    SONG_URL_API = "https://music.163.com/api/song/enhance/player/url"
    OUTER_URL = "https://music.163.com/song/media/outer/url?id={}.mp3"

    def __init__(self, client: httpx.AsyncClient, cookies: dict | None = None):
        self.client = client
        self.cookies = cookies or {}

    async def search(self, keyword: str, limit: int = 20) -> list[dict]:
        """搜索歌曲"""
        params = {"s": keyword, "type": 1, "limit": limit, "offset": 0}
        headers = _merge_cookies(
            {**BASE_HEADERS, "Referer": "https://music.163.com"}, self.cookies
        )
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
            results.append({
                "id": s["id"],
                "title": s.get("name", ""),
                "author": artists,
                "album": s.get("album", {}).get("name", ""),
                "duration": s.get("duration", 0) // 1000,
                "source": "netease",
                "preview_url": self.OUTER_URL.format(s["id"]),
                "has_free": True,  # outer URL 通常有免费片段
            })
        return results

    async def get_download_url(self, song_id: int, br: int = 128000) -> Optional[str]:
        """获取下载链接 — 多策略尝试

        策略 1: API 获取（有 Cookie 可下完整版，无 Cookie 可能返回片段）
        策略 2: outer URL（无需登录，返回免费试听片段）
        """
        headers = _merge_cookies(
            {**BASE_HEADERS, "Referer": "https://music.163.com"}, self.cookies
        )

        # 策略 1: 官方 API
        try:
            params = {"id": song_id, "ids": f"[{song_id}]", "br": br}
            r = await self.client.get(self.SONG_URL_API, params=params, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()
            for d in data.get("data", []):
                url = d.get("url")
                if url:
                    return url
        except Exception:
            pass

        # 策略 2: outer URL 兜底（免费试听）
        try:
            outer = self.OUTER_URL.format(song_id)
            r = await self.client.head(outer, headers=headers, timeout=10, follow_redirects=True)
            if r.status_code == 200 and "audio" in r.headers.get("content-type", ""):
                return str(r.url)  # 返回重定向后的真实 URL
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

    def __init__(self, client: httpx.AsyncClient, cookies: dict | None = None):
        self.client = client
        self.cookies = cookies or {}

    async def search(self, keyword: str, limit: int = 20) -> list[dict]:
        """搜索歌曲"""
        params = {"w": keyword, "n": limit, "format": "json", "p": 1}
        headers = _merge_cookies(
            {**BASE_HEADERS, "Referer": "https://y.qq.com"}, self.cookies
        )
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
            results.append({
                "id": songmid,
                "title": s.get("songname", ""),
                "author": singers,
                "album": s.get("albumname", ""),
                "duration": s.get("interval", 0),
                "source": "qqmusic",
                "preview_url": f"https://y.qq.com/n/ryqq/songDetail/{songmid}",
                "has_free": False,  # QQ 免费试听基本需要登录
            })
        return results

    async def get_download_url(self, song_mid: str) -> Optional[str]:
        """获取下载链接"""
        payload = {
            "req_1": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "guid": "0",
                    "songmid": [song_mid],
                    "songtype": [0],
                    "uin": self.cookies.get("uin", "0"),
                    "loginflag": 1 if self.cookies else 0,
                    "platform": "20",
                },
            }
        }
        headers = _merge_cookies(
            {**BASE_HEADERS, "Referer": "https://y.qq.com"}, self.cookies
        )
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
                    return f"http://ws.stream.qqmusic.qq.com/{purl}"

            # 兜底：用免费试听 URL
            sip = data.get("req_1", {}).get("data", {}).get("sip", [])
            testurl = data.get("req_1", {}).get("data", {}).get("testurl", "")
            if testurl and sip:
                return f"{sip[0]}{testurl}"
        except Exception:
            pass
        return None


# ═══════════════════════════════════════════════════════════
# 酷狗
# ═══════════════════════════════════════════════════════════

class KugouScraper:
    """酷狗音乐爬虫 — 多策略试听下载"""

    SEARCH_API = "http://mobilecdn.kugou.com/api/v3/search/song"
    SONG_INFO_API = "http://m.kugou.com/app/i/getSongInfo.php"
    # 免费试听 CDN（无需登录，~60秒片段）
    TRY_PLAY_CDN = "http://fs.open.kugou.com"

    def __init__(self, client: httpx.AsyncClient, cookies: dict | None = None):
        self.client = client
        self.cookies = cookies or {}

    async def search(self, keyword: str, limit: int = 20) -> list[dict]:
        """搜索歌曲"""
        params = {"keyword": keyword, "page": 1, "pagesize": limit}
        headers = _merge_cookies({**BASE_HEADERS}, self.cookies)
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
                "has_free": False,  # 酷狗现在大部分需要付费
            })
        return results

    async def get_download_url(self, file_hash: str) -> Optional[str]:
        """获取下载链接 — 多策略尝试

        策略 1: 官方 playInfo API（VIP Cookie 可能返回完整链接）
        策略 2: 免费试听 CDN（60秒片段，无需登录）
        """
        if not file_hash:
            return None
        headers = _merge_cookies({**BASE_HEADERS}, self.cookies)

        # 策略 1: 官方 API（无 Cookie = 可能空，有 Cookie = 可能完整）
        try:
            key_url = f"{self.SONG_INFO_API}?hash={file_hash}&cmd=playInfo"
            r = await self.client.get(key_url, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()

            # 直接 URL
            url = data.get("url", "")
            if url and url.startswith("http"):
                return url

            # backup_url
            backup = data.get("backup_url", {})
            if isinstance(backup, dict):
                for k in backup:
                    if backup[k] and str(backup[k]).startswith("http"):
                        return str(backup[k])

            # hash_offset 免费片段（酷狗 CDN 已封锁外部访问，此路不通）
            # trans = data.get("trans_param", {})
            # offset = trans.get("hash_offset", {})
            # 酷狗免费试听 CDN 返回 502/403，无 Cookie 基本无法下载

            # 使用 128kbps hash 作为备选
            extra = data.get("extra", {})
            hash_128 = extra.get("128hash", "")
            if hash_128 and hash_128 != file_hash:
                # 重试用 128kbps hash
                try:
                    r2 = await self.client.get(
                        f"{self.SONG_INFO_API}?hash={hash_128}&cmd=playInfo",
                        headers=headers, timeout=10,
                    )
                    r2.raise_for_status()
                    d2 = r2.json()
                    u2 = d2.get("url", "")
                    if u2 and u2.startswith("http"):
                        return u2
                except Exception:
                    pass

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
    "audiojungle": None,
}


def get_scraper(scraper_type: str, client: httpx.AsyncClient, cookies: dict | None = None):
    cls = SCRAPER_MAP.get(scraper_type)
    if cls is None:
        return None
    return cls(client, cookies=cookies)
