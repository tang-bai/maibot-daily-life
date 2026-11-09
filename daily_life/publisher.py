"""Publish exactly one illustrated post through the existing SnowLuma adapter API."""
from __future__ import annotations

import asyncio
import base64
from typing import Any

QZONE_API = "maibot-team.snowluma-adapter.adapter.napcat.qzone.send_qzone_msg"
VERSION_API = "maibot-team.snowluma-adapter.adapter.napcat.system.get_version_info"


class PublishError(Exception):
    """A definite preflight/rejection error with no raw upstream message."""


class PublishUncertain(PublishError):
    """Submission may have succeeded; never automatically resend."""


def receipt(response: Any) -> str:
    if not isinstance(response, dict):
        raise PublishUncertain("发布回执格式不明，请检查真实空间；不会重发")
    data = response.get("data")
    code = response.get("retcode")
    if response.get("status") == "ok" and type(code) is int and code == 0:
        tid = data.get("tid") if isinstance(data, dict) else None
        if isinstance(tid, str) and tid.strip() and len(tid) <= 300:
            return tid.strip()
    elif response.get("status") == "failed" and type(code) is int and code != 0:
        raise PublishError("SnowLuma 明确拒绝发布，请检查登录、空间权限或账号风控")
    raise PublishUncertain("未取得明确成功的 tid，请检查真实空间；不会重发")


class Publisher:
    def __init__(self, ctx: Any, settings: Any) -> None:
        self.ctx, self.settings = ctx, settings

    async def check(self) -> None:
        try:
            async with asyncio.timeout(15):
                info = await self.ctx.api.get(QZONE_API, version="1")
                if not info:
                    raise PublishError("SnowLuma 图文空间 API 未注册")
                response = await self.ctx.api.call(VERSION_API, version="1", params={})
        except PublishError:
            raise
        except Exception as exc:
            raise PublishError("SnowLuma API 未就绪或版本检查失败") from exc
        data = response.get("data") if isinstance(response, dict) else None
        if (not isinstance(data, dict) or response.get("status") != "ok"
                or type(response.get("retcode")) is not int or response["retcode"] != 0
                or "snowluma" not in str(data.get("app_name", "")).casefold()):
            raise PublishError("无法确认实际客户端为 SnowLuma；不会生成或发布")

    async def publish(self, caption: str, image: bytes) -> str:
        params = {
            "content": caption,
            "images": ["base64://" + base64.b64encode(image).decode("ascii")],
            "ugc_right": self.settings.visibility,
        }
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                response = await self.ctx.api.call(QZONE_API, version="1", params=params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise PublishUncertain("发布调用异常或超时，结果不明；请检查真实空间，不会重发") from exc
        return receipt(response)
