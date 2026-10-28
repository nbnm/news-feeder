import asyncio
import ipaddress
import socket
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), ""))


async def public_url(url: str):
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
        raise ValueError("Source URLs must be public HTTP or HTTPS addresses")
    addresses = await asyncio.get_running_loop().getaddrinfo(
        parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM
    )
    if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
        raise ValueError("Source URLs cannot point to private or local network addresses")


class Fetcher:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def get(self, url: str) -> httpx.Response:
        for _ in range(6):
            await public_url(url)
            async with self.client.stream(
                "GET",
                url,
                follow_redirects=False,
                headers={"User-Agent": "NewsFeeder/0.1 (personal news reader)"},
            ) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 3_000_000:
                        raise ValueError("Source response exceeds the 3 MB extraction limit")
                    chunks.append(chunk)
                headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() not in {"content-encoding", "content-length"}
                }
                return httpx.Response(
                    response.status_code,
                    headers=headers,
                    content=b"".join(chunks),
                    request=response.request,
                )
        raise ValueError("Source redirected too many times")
