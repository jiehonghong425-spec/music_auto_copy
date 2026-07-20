"""AudioJungle 数据模型"""

from pydantic import BaseModel


class AudioItem(BaseModel):
    """单个音乐项目"""
    id: int
    title: str
    author: str = ""
    preview_url: str = ""
    price: float = 0.0
    price_cents: int = 0
    category: str = ""
    tags: list[str] = []
    length: str = ""  # "2:35"
    sales: int = 0
    rating: float = 0.0
    url: str = ""
    thumbnail: str = ""

    @classmethod
    def from_api_item(cls, data: dict) -> "AudioItem":
        """从 Edge API 返回的 item 创建"""
        info = data.get("item_info", data)
        tags_raw = info.get("tags", "")
        if isinstance(tags_raw, str) and tags_raw:
            tags = [t.strip() for t in tags_raw.split(",")]
        else:
            tags = tags_raw if isinstance(tags_raw, list) else []

        cost_str = info.get("cost", "0")
        try:
            price = float(cost_str)
            price_cents = int(price * 100)
        except (ValueError, TypeError):
            price = 0.0
            price_cents = 0

        return cls(
            id=int(info.get("id", data.get("id", 0))),
            title=info.get("item", data.get("description", "")),
            author=info.get("user", ""),
            preview_url=info.get("preview_url", ""),
            price=price,
            price_cents=price_cents,
            category=info.get("category", ""),
            tags=tags,
            length=info.get("length", ""),
            sales=int(info.get("sales", 0)),
            rating=float(info.get("rating", 0)),
            url=info.get("url", data.get("url", "")),
            thumbnail=info.get("thumbnail", info.get("live_preview_url", "")),
        )
