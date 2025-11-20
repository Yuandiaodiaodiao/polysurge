#!/usr/bin/env python3
"""
PolySurge 本地开发服务器 (异步版本)
- 提供静态文件
- 代理 Polymarket API（解决 CORS 问题）
- 使用 aiohttp 实现异步请求和连接池
"""

import asyncio
import aiohttp
from aiohttp import web
import os
import time
import logging
from urllib.parse import urlparse
from collections import OrderedDict
from pathlib import Path

PORT = 8080
API_BASE = "https://data-api.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"

# LRU 缓存配置
MAX_CACHE_SIZE = 1000  # 最多缓存 1000 个请求

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class LRUCache:
    def __init__(self, max_size):
        self.cache = OrderedDict()
        self.max_size = max_size
        self.lock = asyncio.Lock()

    async def get(self, key):
        async with self.lock:
            if key not in self.cache:
                return None
            # 移到末尾表示最近使用
            self.cache.move_to_end(key)
            return self.cache[key]

    async def put(self, key, value):
        async with self.lock:
            if key in self.cache:
                # 更新并移到末尾
                self.cache.move_to_end(key)
            self.cache[key] = value
            # 超过最大容量,删除最久未使用的
            if len(self.cache) > self.max_size:
                self.cache.popitem(last=False)

# 缓存实例: {url: (data, expire_timestamp)}
cache = LRUCache(MAX_CACHE_SIZE)


async def proxy_api(request, session):
    """代理 API 请求"""
    path = request.path[4:]  # 去掉 /api 前缀
    query_string = request.query_string

    # 确定目标 URL
    if path.startswith('/gamma/'):
        url = f"{GAMMA_BASE}{path[6:]}"
    else:
        url = f"{API_BASE}{path}"

    if query_string:
        url += f"?{query_string}"

    # 计算当前 UTC 时间对齐到 30 秒的过期时间
    now = time.time()
    expire_time = ((int(now) // 30) + 1) * 30

    # 检查缓存
    cached = await cache.get(url)
    if cached:
        cached_data, cached_expire = cached
        if time.time() < cached_expire:
            logger.info(f"Cache HIT: {url}")
            return web.Response(
                body=cached_data,
                content_type='application/json',
                headers={
                    'Access-Control-Allow-Origin': '*',
                    'X-Cache': 'HIT'
                }
            )

    # 缓存未命中或已过期，请求 API
    logger.info(f"Cache MISS: {url}")
    try:
        async with session.get(
            url,
            headers={'User-Agent': 'PolySurge/1.0'},
            timeout=aiohttp.ClientTimeout(total=30)  # 30 秒超时
        ) as resp:
            data = await resp.read()

            # 存入缓存
            await cache.put(url, (data, expire_time))

            return web.Response(
                body=data,
                content_type='application/json',
                headers={
                    'Access-Control-Allow-Origin': '*',
                    'X-Cache': 'MISS'
                }
            )

    except asyncio.TimeoutError:
        logger.error(f"Timeout: {url}")
        return web.Response(status=504, text="Gateway Timeout")
    except aiohttp.ClientError as e:
        logger.error(f"Client error for {url}: {e}")
        return web.Response(status=502, text=f"Bad Gateway: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error for {url}: {e}")
        return web.Response(status=500, text=f"Internal Server Error: {str(e)}")


async def handle_request(request):
    """处理所有请求"""
    session = request.app['http_session']

    # API 代理
    if request.path.startswith('/api/'):
        return await proxy_api(request, session)

    # 静态文件
    file_path = request.path.lstrip('/')
    if not file_path:
        file_path = 'index.html'

    full_path = Path(file_path)

    if full_path.exists() and full_path.is_file():
        return web.FileResponse(full_path)
    else:
        return web.Response(status=404, text="Not Found")


async def on_startup(app):
    """应用启动时创建连接池"""
    connector = aiohttp.TCPConnector(
        limit=1000,  # 最大连接数
        limit_per_host=100,  # 每个主机最大连接数
        ttl_dns_cache=300,  # DNS 缓存 5 分钟
    )
    timeout = aiohttp.ClientTimeout(total=30)
    app['http_session'] = aiohttp.ClientSession(
        connector=connector,
        timeout=timeout
    )
    logger.info("HTTP session and connection pool created")


async def on_cleanup(app):
    """应用关闭时清理连接池"""
    await app['http_session'].close()
    logger.info("HTTP session closed")


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    app = web.Application()
    app.router.add_get('/{tail:.*}', handle_request)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    print(f"""
╔════════════════════════════════════════════╗
║     PolySurge - 异常信号雷达 (异步)        ║
╚════════════════════════════════════════════╝

服务器已启动: http://localhost:{PORT}

API 代理:
  - /api/trades    -> data-api.polymarket.com/trades
  - /api/gamma/... -> gamma-api.polymarket.com/...

特性:
  ✓ 异步处理
  ✓ 连接池 (1000 连接)
  ✓ 30 秒超时
  ✓ LRU 缓存 (30 秒过期)

按 Ctrl+C 停止服务器
""")

    web.run_app(app, host='0.0.0.0', port=PORT)


if __name__ == "__main__":
    main()
