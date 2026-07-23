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
        # 并行检测所有歌曲的下载可用性
        dl_checks = await asyncio.gather(
            *[self._check_available(s["id"]) for s in songs],
            return_exceptions=True,
        )
        for s, can_dl in zip(songs, dl_checks):
            artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
            can_dl = can_dl if isinstance(can_dl, bool) else False
            sid = s["id"]
            results.append({
                "id": sid,
                "title": s.get("name", ""),
                "author": artists,
                "album": s.get("album", {}).get("name", ""),
                "duration": s.get("duration", 0) // 1000,
                "source": "netease",
                "preview_url": self.OUTER_URL.format(sid),
                "can_download": can_dl,
                "dl_note": "可下载" if can_dl else "需登录",
            })
        return results

    async def _check_available(self, song_id: int) -> bool:
        """快速检测歌曲是否可免费下载"""
        try:
            outer = self.OUTER_URL.format(song_id)
            r = await self.client.head(outer, headers=BASE_HEADERS, timeout=5, follow_redirects=True)
            ct = r.headers.get("content-type", "")
            return r.status_code == 200 and "audio" in ct
        except Exception:
            return False

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
        # 检查是否有完整的登录 Cookie（不止一个 key）
        has_cookie = bool(self.cookies) and len(self.cookies) > 0
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
                "can_download": has_cookie,
                "dl_note": "可下载" if has_cookie else "需登录",
            })
        return results

    async def get_download_url(self, song_mid: str) -> Optional[str]:
        """获取下载链接 — 多策略尝试

        策略 1: 官方 CgiGetVkey API（VIP Cookie 可获取高音质）
        策略 2: testurl 兜底（免费低音质片段）
        """
        uin = self.cookies.get("uin", "") or self.cookies.get("p_uin", "0")
        # 使用完整的 Cookie 上下文
        headers = _merge_cookies(
            {**BASE_HEADERS, "Referer": "https://y.qq.com",
             "Origin": "https://y.qq.com"},
            self.cookies,
        )

        # 策略 1: 官方 API — 用完整 Cookie 请求高音质
        for guid in [uin, "0"]:
            payload = {
                "req_1": {
                    "module": "vkey.GetVkeyServer",
                    "method": "CgiGetVkey",
                    "param": {
                        "guid": str(guid),
                        "songmid": [song_mid],
                        "songtype": [0],
                        "uin": str(uin),
                        "loginflag": 1 if self.cookies else 0,
                        "platform": "20",
                    },
                }
            }
            try:
                r = await self.client.post(
                    self.SONG_URL_API, json=payload, headers=headers, timeout=10
                )
                r.raise_for_status()
                data = r.json()
                midurlinfo = data.get("req_1", {}).get("data", {}).get("midurlinfo", [])
                if midurlinfo:
                    purl = midurlinfo[0].get("purl", "")
                    if purl and purl.strip():
                        # 尝试获取最高可用域名
                        sip = data.get("req_1", {}).get("data", {}).get("sip", [])
                        if sip:
                            return f"{sip[0]}{purl}"
                        return f"http://ws.stream.qqmusic.qq.com/{purl}"

                # 兜底 testurl
                testurl = data.get("req_1", {}).get("data", {}).get("testurl", "")
                if testurl:
                    sip = data.get("req_1", {}).get("data", {}).get("sip", [])
                    if sip and testurl:
                        return f"{sip[0]}{testurl}"
            except Exception:
                continue

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

        has_cookie = bool(self.cookies) and len(self.cookies) > 0
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
                "can_download": has_cookie,
                "dl_note": "可下载" if has_cookie else "需登录",
            })
        return results

    async def get_download_url(self, file_hash: str) -> Optional[str]:
        """获取下载链接 — 多策略尝试

        策略 1: 官方 playInfo API（VIP Cookie 可获取完整链接+高音质）
        策略 2: 128kbps hash 备选
        策略 3: 免费试听 CDN（60秒片段，需 Cookie）
        """
        if not file_hash:
            return None

        headers = _merge_cookies(
            {**BASE_HEADERS, "Referer": "https://www.kugou.com",
             "Origin": "https://www.kugou.com"},
            self.cookies,
        )

        # 策略 1: 官方 API — 完整 Cookie 请求高音质
        try:
            key_url = f"{self.SONG_INFO_API}?hash={file_hash}&cmd=playInfo"
            r = await self.client.get(key_url, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()

            # 直接 URL（VIP 用户可获得完整歌曲）
            url = data.get("url", "")
            if url and url.startswith("http"):
                # 检查是否是完整歌曲（非试听片段）
                if "try_listen" not in url and len(url) > 20:
                    return url

            # backup_url（VIP 备选线路）
            backup = data.get("backup_url", {})
            if isinstance(backup, dict):
                for k in sorted(backup.keys()):
                    if backup[k] and str(backup[k]).startswith("http"):
                        bu = str(backup[k])
                        if "try_listen" not in bu:
                            return bu

            # 如果只有试听片段 URL，也返回（兜底）
            if url and url.startswith("http"):
                return url

            # 128kbps hash 备选
            extra = data.get("extra", {})
            hash_128 = extra.get("128hash", "")
            if hash_128 and hash_128 != file_hash:
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
