"""Bounded image generation and SSRF-safe image downloads.

The administrator's API endpoint is trusted (including local HTTP endpoints).
Image URLs returned by that API are untrusted and use a separate transport.
Only fixed, non-sensitive messages cross the ImageError boundary.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import ipaddress
import json
import math
import socket
import warnings
from typing import Any

import aiohttp
from aiohttp.abc import AbstractResolver
from PIL import Image, UnidentifiedImageError
from yarl import URL

__all__ = ["ImageError", "ImageGenerator", "validate_image"]

_JSON_LIMIT = 32 * 1024 * 1024
_REDIRECTS = {301, 302, 303, 307, 308}
_MIMES = {"image/png": "PNG", "image/jpeg": "JPEG"}


class ImageError(Exception):
    """A user-safe failure; never includes remote bodies, URLs, or secrets."""


def http_error(status: int, *, download: bool = False) -> ImageError:
    """Use only a status code and fixed guidance, never echo upstream error bodies."""
    if download:
        hint = "图片下载被服务商拒绝或链接已失效，请检查服务商日志"
        stage = "图片下载"
    else:
        stage = "绘图 API"
        hint = {
            400: "请求参数不被接受，请检查模型、尺寸及 images/generations 协议",
            401: "认证失败，请检查配置中的接口密钥是否有效",
            402: "服务商可能要求充值，请检查账户余额或套餐",
            403: "访问被拒绝，请检查模型权限、账户限制或服务商风控",
            404: "接口或模型不存在，请检查 base_url、模型名及接口协议",
            408: "服务商处理超时，请检查服务商请求日志",
            413: "服务商拒绝了过大的请求",
            422: "参数校验失败，请检查模型和图片尺寸",
            429: "请求受限，请检查频率、额度和余额",
        }.get(status, "服务商返回服务器错误，请检查其运行状态" if status >= 500
              else "服务商拒绝请求，请检查服务商请求日志")
        if 300 <= status < 400:
            hint = "接口返回重定向，为保护密钥已拒绝，请填写最终 API 地址"
    return ImageError(f"{stage} HTTP {status}：{hint}；不会自动重试")


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate_image(
    data: bytes, max_bytes: int = 8388608, max_pixels: int = 16777216
) -> bytes:
    """Verify and fully decode a single-frame PNG/JPEG without changing its bytes."""
    if not _positive_int(max_bytes) or not _positive_int(max_pixels):
        raise ImageError("图片限制配置无效")
    if not isinstance(data, bytes) or not data or len(data) > max_bytes:
        raise ImageError("图片为空或超过大小限制")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG"}:
                    raise ImageError("仅支持 PNG/JPEG 图片")
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > max_pixels:
                    raise ImageError("图片超过像素限制")
                if getattr(image, "n_frames", 1) != 1 or getattr(image, "is_animated", False):
                    raise ImageError("不支持动画图片")
                image.verify()
            # verify() checks container structure; load() also checks compressed data.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except ImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageError("图片超过安全像素限制") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError):
        raise ImageError("图片格式无效或文件损坏") from None
    return data


def _public_address(address: str) -> None:
    try:
        # Scoped IPv6 addresses are not valid remote image destinations.
        if "%" in address:
            raise ValueError
        parsed = ipaddress.ip_address(address)
        if not parsed.is_global or parsed.is_multicast or parsed.is_reserved:
            raise ValueError
        if isinstance(parsed, ipaddress.IPv6Address):
            mapped = parsed.ipv4_mapped
            if mapped is not None and (
                not mapped.is_global or mapped.is_multicast or mapped.is_reserved
            ):
                raise ValueError
    except ValueError:
        raise ImageError("图片地址不是允许的公网地址") from None


def _image_url(value: Any) -> URL:
    try:
        if not isinstance(value, str) or not value.strip():
            raise ValueError
        url = URL(value)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError
        if url.user is not None or url.password is not None:
            raise ValueError
        if url.port != (443 if url.scheme == "https" else 80):
            raise ValueError
        host = url.host
        if "%" in host:
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # Non-IP names are checked, with every returned record, by the resolver.
            pass
        else:
            _public_address(host)
        return url.with_fragment(None)
    except ImageError:
        raise
    except (ValueError, TypeError, UnicodeError):
        raise ImageError("图片下载地址无效") from None


class _PublicResolver(AbstractResolver):
    """Validate *all* DNS records before handing numeric addresses to aiohttp.

    The connector receives these exact numeric records, not a hostname to resolve
    again, closing the DNS check/use gap. No proxy or second resolver is involved.
    """

    def __init__(self) -> None:
        self._resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict[str, Any]]:
        try:
            records = await self._resolver.resolve(host, port, family)
            if not records:
                raise ImageError("图片域名未解析到公网地址")
            for record in records:
                _public_address(record["host"])
            return records
        except ImageError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise ImageError("图片域名解析失败") from None

    async def close(self) -> None:
        await self._resolver.close()


async def _read_limited(response: aiohttp.ClientResponse, limit: int) -> bytes:
    if response.content_length is not None and response.content_length > limit:
        raise ImageError("响应超过大小限制")
    parts = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        if len(parts) + len(chunk) > limit:
            raise ImageError("响应超过大小限制")
        parts.extend(chunk)
    return bytes(parts)


class ImageGenerator:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._api_session: aiohttp.ClientSession | None = None
        self._download_session: aiohttp.ClientSession | None = None
        self._download_resolver: _PublicResolver | None = None
        self._closed = False

    def problems(self) -> list[str]:
        """Inspect plugin configuration only, without network I/O or environment lookups."""
        result: list[str] = []
        base = getattr(self.settings, "base_url", None)
        try:
            if not isinstance(base, str) or not base.strip():
                raise ValueError
            url = URL(base)
            if (url.scheme not in {"http", "https"} or not url.host
                    or url.user is not None or url.password is not None
                    or url.query_string or url.fragment):
                raise ValueError
            url.port  # Trigger malformed/out-of-range port validation.
        except (ValueError, TypeError, UnicodeError):
            result.append("绘图 base_url 配置无效")
        key = getattr(self.settings, "api_key", None)
        if (not isinstance(key, str) or not key.strip()
                or any(ord(c) < 32 or ord(c) > 126 for c in key)
                or any(c.isspace() for c in key.strip())):
            result.append("绘图接口密钥缺失或无效，请在插件配置的绘图服务中填写密钥")
        model = getattr(self.settings, "model", None)
        if not isinstance(model, str) or not model.strip():
            result.append("绘图 model 配置无效")
        size = getattr(self.settings, "size", "")
        if size is not None and not isinstance(size, str):
            result.append("绘图 size 配置无效")
        timeout = getattr(self.settings, "timeout_seconds", None)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            result.append("绘图 timeout_seconds 配置无效")
        for field in ("max_image_bytes", "max_pixels"):
            if not _positive_int(getattr(self.settings, field, None)):
                result.append(f"绘图 {field} 配置无效")
        return result

    def _api_client(self) -> aiohttp.ClientSession:
        if self._api_session is None:
            self._api_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True),
                timeout=aiohttp.ClientTimeout(total=self.settings.timeout_seconds),
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        return self._api_session

    def _image_client(self) -> aiohttp.ClientSession:
        if self._download_session is None:
            self._download_resolver = _PublicResolver()
            self._download_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    resolver=self._download_resolver, use_dns_cache=False,
                    family=socket.AF_UNSPEC, force_close=True,
                ),
                timeout=aiohttp.ClientTimeout(total=self.settings.timeout_seconds),
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
            )
            # aiohttp 3.11+ can retry even a fresh GET after a disconnect.
            # There is no public retry switch in the supported 3.x API.
            if hasattr(self._download_session, "_retry_connection"):
                self._download_session._retry_connection = False
        return self._download_session

    async def _download(self, value: Any) -> bytes:
        url = _image_url(value)
        for hop in range(4):
            # No API headers or cookies are ever supplied to this separate client.
            async with self._image_client().get(url, allow_redirects=False) as response:
                if response.status in _REDIRECTS:
                    if hop == 3:
                        raise ImageError("图片下载重定向次数过多")
                    location = response.headers.get("Location")
                    if not location:
                        raise ImageError("图片重定向响应无效")
                    try:
                        url = _image_url(str(url.join(URL(location))))
                    except (ValueError, TypeError, UnicodeError):
                        raise ImageError("图片重定向地址无效") from None
                    continue
                if response.status != 200:
                    raise http_error(response.status, download=True)
                mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if mime not in _MIMES:
                    raise ImageError("图片响应类型不是 PNG/JPEG")
                data = await _read_limited(response, self.settings.max_image_bytes)
                data = validate_image(data, self.settings.max_image_bytes, self.settings.max_pixels)
                with Image.open(io.BytesIO(data)) as image:
                    if image.format != _MIMES[mime]:
                        raise ImageError("图片响应类型与文件格式不符")
                return data
        raise ImageError("图片下载失败")  # Defensive: the loop always returns or raises.

    async def generate(self, prompt: str) -> bytes:
        if self._closed:
            raise ImageError("绘图服务已关闭")
        if self.problems():
            raise ImageError("绘图配置无效或 API 密钥缺失")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ImageError("绘图提示词为空")
        payload = {"model": self.settings.model, "prompt": prompt, "n": 1}
        size = getattr(self.settings, "size", "")
        if size and size.strip():
            payload["size"] = size
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                async with self._api_client().post(
                    self.settings.base_url.rstrip("/") + "/images/generations",
                    json=payload,
                    headers={"Authorization": "Bearer " + self.settings.api_key.strip()},
                    allow_redirects=False,
                ) as response:
                    if not 200 <= response.status < 300:
                        raise http_error(response.status)
                    raw = await _read_limited(response, _JSON_LIMIT)
                try:
                    document = json.loads(raw)
                    if isinstance(document, dict) and document.get("error"):
                        raise ImageError("绘图 API 返回错误对象（HTTP 2xx），请检查服务商请求日志；不会自动重试")
                    items = document["data"]
                    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
                        raise ValueError
                    item = items[0]
                except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
                    raise ImageError("绘图 API 响应格式无效") from None
                if "b64_json" in item:
                    encoded = item["b64_json"]
                    if not isinstance(encoded, str) or not encoded:
                        raise ImageError("绘图 API 图片编码无效")
                    if len(encoded) > 4 * ((self.settings.max_image_bytes + 2) // 3):
                        raise ImageError("图片超过大小限制")
                    try:
                        data = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError):
                        raise ImageError("绘图 API 图片编码无效") from None
                    return validate_image(data, self.settings.max_image_bytes, self.settings.max_pixels)
                if "url" in item:
                    return await self._download(item["url"])
                raise ImageError("绘图 API 未返回图片")
        except ImageError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            raise ImageError("绘图请求超时（包含生成与下载），请检查服务商耗时或调整超时配置；不会自动重试") from None
        except aiohttp.ClientConnectorCertificateError:
            raise ImageError("绘图连接证书校验失败，请检查服务商 HTTPS 证书和本机时间") from None
        except aiohttp.ClientConnectionError:
            raise ImageError("绘图网络连接失败，请检查地址、DNS、网络及服务商状态") from None
        except (aiohttp.ClientError, OSError, ValueError, KeyError, UnicodeError):
            raise ImageError("绘图请求失败") from None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._api_session is not None:
                await self._api_session.close()
        finally:
            try:
                if self._download_session is not None:
                    await self._download_session.close()
            finally:
                if self._download_resolver is not None:
                    await self._download_resolver.close()
