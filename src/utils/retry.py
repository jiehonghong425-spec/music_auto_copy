"""工具函数 — 重试、文件系统、日志"""

import asyncio
import functools
import hashlib
import re
import time
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


def sanitize_filename(name: str, max_length: int = 80) -> str:
    """清理文件名，移除非法字符并限制长度"""
    # 移除 Windows 非法字符
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    # 替换多个空格/下划线
    name = re.sub(r"[_\s]+", "_", name).strip("_")
    # 截断
    if len(name) > max_length:
        name = name[:max_length].rstrip("_")
    return name


def compute_sha256(file_path: Path) -> str:
    """计算文件 SHA256"""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


async def retry_async(
    coro_factory: Callable[[], T],
    max_retries: int = 3,
    base_delay: float = 5.0,
    should_retry: Callable[[Exception], bool] | None = None,
) -> T:
    """对异步操作进行指数退避重试

    Args:
        coro_factory: 返回可等待对象的工厂函数
        max_retries: 最大重试次数
        base_delay: 基础延迟秒数 (延迟 = base * 2^attempt)
        should_retry: 判断异常是否应重试, 默认全部重试
    """
    last_exception = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except Exception as e:
            last_exception = e
            if should_retry and not should_retry(e):
                raise
            if attempt < max_retries:
                delay = base_delay * (2**attempt)
                await asyncio.sleep(delay)
    raise last_exception  # type: ignore


class RateLimiter:
    """简单的异步速率限制器"""

    def __init__(self, calls_per_second: float = 2.0):
        self.min_interval = 1.0 / calls_per_second
        self._last_call = 0.0

    async def wait(self):
        """等待直到可以发起下一次调用"""
        now = time.monotonic()
        elapsed = now - self._last_call
        if elapsed < self.min_interval:
            await asyncio.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()
