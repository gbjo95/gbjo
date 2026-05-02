import asyncio
from typing import Optional, Any, Dict

import httpx
from bot.core.config import API_BASE_URL, API_KEY

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 522, 524}
RETRYABLE_ERRORS = (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.RemoteProtocolError, httpx.WriteError, httpx.TransportError)


class HTTPClient:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()
        self._timeout = httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=5.0)
        self._limits = httpx.Limits(max_keepalive_connections=20, max_connections=100, keepalive_expiry=30.0)
        self._base_headers = {'X-API-Key': API_KEY, 'Connection': 'keep-alive'}

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(base_url=API_BASE_URL, headers=self._base_headers, limits=self._limits, timeout=self._timeout)
        return self._client

    async def reset(self):
        async with self._lock:
            if self._client is not None:
                try:
                    await self._client.aclose()
                finally:
                    self._client = None

    async def request(self, method: str, path: str, *, json: Dict[str, Any] | None = None, params: Dict[str, Any] | None = None, headers: Dict[str, str] | None = None, max_attempts: int = 1):
        delay = 0.2
        attempt = 1
        request_headers = dict(self._base_headers)
        if headers:
            request_headers.update(headers)
        while True:
            client = await self._ensure_client()
            try:
                resp = await client.request(method, path, json=json, params=params, headers=request_headers)
                if resp.status_code in RETRYABLE_STATUS and attempt < max_attempts:
                    await asyncio.sleep(delay)
                    delay *= 2
                    attempt += 1
                    continue
                return resp
            except RETRYABLE_ERRORS:
                if attempt >= max_attempts:
                    raise
                await self.reset()
                await asyncio.sleep(delay)
                delay *= 2
                attempt += 1

    async def get(self, path: str, **kwargs):
        return await self.request('GET', path, **kwargs)

    async def post(self, path: str, **kwargs):
        return await self.request('POST', path, **kwargs)

    async def patch(self, path: str, **kwargs):
        return await self.request('PATCH', path, **kwargs)

    async def delete(self, path: str, **kwargs):
        return await self.request('DELETE', path, **kwargs)


http_client = HTTPClient()
