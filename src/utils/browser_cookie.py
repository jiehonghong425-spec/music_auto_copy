"""浏览器 Cookie 自动读取模块

支持从 Chrome / Edge / Firefox / Brave / Opera 中提取各音乐平台 Cookie。

使用 browser_cookie3 库（纯 Python，跨平台，无需额外驱动）。
安装: pip install browser-cookie3
"""

import json
import sys
from typing import Optional

# 各平台需要提取 Cookie 的域名
PLATFORM_DOMAINS = {
    "netease": [
        "music.163.com",
        ".music.163.com",
        "interface.music.163.com",
        "api.music.163.com",
    ],
    "qqmusic": [
        "y.qq.com",
        ".y.qq.com",
        "u.y.qq.com",
        "c.y.qq.com",
        "i.y.qq.com",
        "aqqmusic.tc.qq.com",
        "qq.com",
    ],
    "kugou": [
        "kugou.com",
        ".kugou.com",
        "www.kugou.com",
        "m.kugou.com",
        "login.user.kugou.com",
        "kmr.service.kugou.com",
        "mobilecdn.kugou.com",
    ],
}

# 各平台的关键 Cookie 名称（用于摘要显示）
KEY_COOKIE_NAMES = {
    "netease": ["MUSIC_U", "MUSIC_A", "__csrf", "NMTID", "os", "appver"],
    "qqmusic": ["uin", "qqmusic_key", "qm_keyst", "p_uin", "p_skey", "skey", "ptui_loginuin"],
    "kugou": ["kg_mid", "kg_mid_v2", "dfid", "userid", "token", "nickname", "userid"],
}


def _get_browser_cookies(domain_filter: list[str] | None = None) -> list[dict]:
    """从所有已安装浏览器中提取指定域名的 Cookie

    Args:
        domain_filter: 要过滤的域名列表，None 表示提取所有

    Returns:
        Cookie 列表，每个元素包含 domain, name, value 等字段
    """
    try:
        import browser_cookie3
    except ImportError:
        return _error_result("请先安装 browser-cookie3: pip install browser-cookie3")

    all_cookies = []
    browsers = []

    # 检测可用的浏览器
    for browser_name, loader in _get_browser_loaders():
        try:
            cj = loader()
            if cj is None:
                continue
            browsers.append(browser_name)
            for cookie in cj:
                domain = getattr(cookie, "domain", "")
                if domain_filter:
                    if not any(d in domain for d in domain_filter):
                        continue
                all_cookies.append({
                    "domain": domain,
                    "name": cookie.name if hasattr(cookie, "name") else "",
                    "value": cookie.value if hasattr(cookie, "value") else "",
                    "path": getattr(cookie, "path", "/"),
                    "secure": getattr(cookie, "secure", False),
                    "expires": getattr(cookie, "expires", None),
                    "browser": browser_name,
                })
        except Exception:
            continue

    return all_cookies


def _get_browser_loaders():
    """返回各浏览器的 Cookie 加载器列表"""
    import browser_cookie3 as bc3

    loaders = []
    # Chrome
    try:
        loaders.append(("Chrome", bc3.chrome))
    except Exception:
        pass
    # Edge
    try:
        loaders.append(("Edge", bc3.edge))
    except Exception:
        pass
    # Firefox
    try:
        loaders.append(("Firefox", bc3.firefox))
    except Exception:
        pass
    # Brave
    try:
        loaders.append(("Brave", bc3.brave))
    except Exception:
        pass
    # Opera
    try:
        loaders.append(("Opera", bc3.opera))
    except Exception:
        pass
    # Opera GX
    try:
        loaders.append(("OperaGX", bc3.opera_gx))
    except Exception:
        pass

    # 对于 Chromium 浏览器（Chrome/Edge/Brave/Opera），在 Windows 上
    # browser_cookie3 默认行为需要匹配。如果以上都失败，试试 chromecookie
    if not loaders:
        try:
            loaders.append(("Chromium", bc3.chromium))
        except Exception:
            pass

    return loaders


def _error_result(msg: str) -> list[dict]:
    return [{"_error": msg}]


def get_platform_cookies(platform: str) -> dict:
    """获取指定平台在当前浏览器中的所有 Cookie

    Args:
        platform: 平台标识 — "netease" | "qqmusic" | "kugou"

    Returns:
        {
            "platform": "netease",
            "cookies": {"MUSIC_U": "xxx", "__csrf": "yyy", ...},
            "summary": {"MUSIC_U": "a1b2...", ...},  # 截断值的摘要
            "browsers_found": ["Chrome", "Edge"],
            "cookie_count": 15,
            "key_cookies_found": ["MUSIC_U", "__csrf"],
            "key_cookies_missing": ["NMTID"],
            "error": None  # 如果有错误
        }
    """
    domains = PLATFORM_DOMAINS.get(platform, [])
    if not domains:
        return {"platform": platform, "cookies": {}, "error": f"未知平台: {platform}"}

    try:
        raw_cookies = _get_browser_cookies(domains)
    except Exception as e:
        return {"platform": platform, "cookies": {}, "error": str(e)}

    if not raw_cookies:
        return {
            "platform": platform,
            "cookies": {},
            "summary": {},
            "browsers_found": [],
            "cookie_count": 0,
            "key_cookies_found": [],
            "key_cookies_missing": KEY_COOKIE_NAMES.get(platform, []),
            "error": None,
            "hint": f"未找到 {platform} 的 Cookie，请先在浏览器中登录对应平台",
        }

    # 去重：同一个 name 取最新的（后遍历的覆盖先遍历的）
    cookies = {}
    browsers = set()
    for c in raw_cookies:
        name = c.get("name", "")
        if name:
            cookies[name] = c.get("value", "")
        browsers.add(c.get("browser", ""))

    # 生成摘要（截断值）
    summary = {}
    for name in KEY_COOKIE_NAMES.get(platform, []):
        if name in cookies:
            v = cookies[name]
            summary[name] = v[:12] + "..." if len(v) > 12 else v

    # 检查关键 Cookie
    key_names = KEY_COOKIE_NAMES.get(platform, [])
    found = [n for n in key_names if n in cookies]
    missing = [n for n in key_names if n not in cookies]

    return {
        "platform": platform,
        "cookies": cookies,
        "summary": summary,
        "browsers_found": sorted(browsers),
        "cookie_count": len(cookies),
        "key_cookies_found": found,
        "key_cookies_missing": missing,
        "error": None,
    }


def get_all_platforms_status() -> dict:
    """获取所有三个平台的 Cookie 状态概览

    Returns:
        {
            "netease": {...},
            "qqmusic": {...},
            "kugou": {...},
            "browser_available": True
        }
    """
    result = {}
    try:
        import browser_cookie3  # noqa: F401
        result["browser_available"] = True
    except ImportError:
        result["browser_available"] = False
        result["error"] = "browser_cookie3 未安装，请执行: pip install browser-cookie3"
        for p in ["netease", "qqmusic", "kugou"]:
            result[p] = {"platform": p, "cookies": {}, "error": "库未安装"}
        return result

    for p in ["netease", "qqmusic", "kugou"]:
        result[p] = get_platform_cookies(p)

    return result


def import_json_cookies(json_str: str) -> dict:
    """解析浏览器扩展（Cookie-Editor / EditThisCookie）导出的 JSON

    支持的格式:
    [
      {
        "domain": ".music.163.com",
        "name": "MUSIC_U",
        "value": "xxx",
        "path": "/",
        ...
      },
      ...
    ]

    Returns:
        {"netease": {...}, "qqmusic": {...}, "kugou": {...}}
    """
    try:
        raw = json.loads(json_str)
    except json.JSONDecodeError as e:
        return {"_error": f"JSON 解析失败: {e}"}

    if not isinstance(raw, list):
        return {"_error": "格式错误：期望 JSON 数组"}

    result = {"netease": {}, "qqmusic": {}, "kugou": {}}

    for item in raw:
        if not isinstance(item, dict):
            continue
        domain = item.get("domain", "")
        name = item.get("name", "")
        value = item.get("value", "")

        if not name:
            continue

        for platform, domains in PLATFORM_DOMAINS.items():
            if any(d in domain for d in domains):
                result[platform][name] = value
                break

    # 统计
    for p in ("netease", "qqmusic", "kugou"):
        if result[p]:
            result[p + "_count"] = len(result[p])

    return result
