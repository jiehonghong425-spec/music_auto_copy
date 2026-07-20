"""搜索过滤器 — URL 构建 + 本地过滤"""

from src.config import SearchConfig


def build_category_url(config: SearchConfig, page: int = 1) -> str:
    """构建 AudioJungle 分类页面 URL

    格式: https://audiojungle.net/category/music?date=this-year&price_max=19&sort=date&page=1
    """
    params = []
    if config.date and config.date != "all":
        params.append(f"date={config.date}")
    if config.price_max and config.price_max > 0:
        params.append(f"price_max={config.price_max}")
    if config.price_min and config.price_min > 0:
        params.append(f"price_min={config.price_min}")
    if config.sort:
        params.append(f"sort={config.sort}")
    if config.tags:
        params.append(f"tags={','.join(config.tags)}")
    if config.category:
        category = config.category
    else:
        category = "music"
    params.append(f"page={page}")
    query = "&".join(params)
    return f"https://audiojungle.net/category/{category}?{query}"


def filter_items(items: list[dict], config: SearchConfig) -> list[dict]:
    """对已解析的结果进行本地二次过滤"""
    result = []
    for item in items:
        # 价格过滤
        price = item.get("price", 0)
        if config.price_min and price < config.price_min:
            continue
        if config.price_max and price > config.price_max:
            continue

        # 必须有预览 URL
        if not item.get("preview_url"):
            continue

        result.append(item)

        # 达到上限
        if config.max_items > 0 and len(result) >= config.max_items:
            break

    return result
