"""SDK-generated configuration; drawing credentials are stored in plugin config only."""
from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator


class PluginSettings(PluginConfigBase):
    """控制插件启用状态与管理员权限。"""

    __ui_label__ = "基本设置"
    __ui_order__ = 0

    config_version: str = Field(default="1.0.0", description="配置版本", json_schema_extra={"label":"配置格式版本"})
    enabled: bool = Field(default=True, description="启用插件；关闭会阻止生成和发布", json_schema_extra={"label":"启用插件"})
    admin_qqs: list[str] = Field(default_factory=list, description="允许执行命令的管理员 QQ 白名单", json_schema_extra={"label":"管理员 QQ 白名单"})

    @field_validator("admin_qqs")
    @classmethod
    def valid_admins(cls, values: list[str]) -> list[str]:
        if any(not value.isascii() or not value.isdigit() for value in values):
            raise ValueError("管理员 QQ 必须为数字字符串")
        return list(dict.fromkeys(values))


class CharacterSettings(PluginConfigBase):
    """设置固定角色形象、插画风格和配文语气。"""

    __ui_label__ = "角色与画风"
    __ui_order__ = 1

    appearance: str = Field(default="", max_length=2000, description="必填：固定外貌、发型和服装，不含私人信息", json_schema_extra={"label":"角色外貌与服装","placeholder":"例如：成年角色、黑色短发、棕色眼睛、浅色休闲服"})
    art_style: str = Field(default="温暖的日常插画，自然光线，画面中不出现文字", max_length=1000, description="固定画风", json_schema_extra={"label":"插画风格"})
    voice: str = Field(default="自然、轻松，像给朋友分享自己的小日常；不要提及系统或状态数值", max_length=2000, description="第一人称配文的人设和语气", json_schema_extra={"label":"配文人设与语气"})


class TextSettings(PluginConfigBase):
    """使用 MaiBot 已配置的文本模型生成配文与绘图提示词。"""

    __ui_label__ = "配文生成"
    __ui_order__ = 2

    task_name: str = Field(default="utils", min_length=1, max_length=100, description="MaiBot 文本模型任务", json_schema_extra={"label":"文本模型任务"})
    model_name: str = Field(default="", max_length=200, description="可选：指定 MaiBot 已配置模型", json_schema_extra={"label":"指定文本模型（可选）"})
    timeout_seconds: int = Field(default=90, ge=5, le=300, description="文本生成超时（秒），不自动重试", json_schema_extra={"label":"文本生成超时（秒）"})


class ImageSettings(PluginConfigBase):
    """配置第三方 OpenAI 兼容绘图接口；直接在本页填写接口密钥。"""

    __ui_label__ = "绘图服务"
    __ui_order__ = 3

    base_url: str = Field(default="", max_length=1000, description="OpenAI 兼容根地址，如 https://example.com/v1", json_schema_extra={"label":"绘图接口地址","placeholder":"https://你的绘图服务/v1"})
    api_key: str = Field(default="", max_length=8192, repr=False, description="必填：第三方绘图接口密钥，直接填写在插件配置中，不读取环境变量", json_schema_extra={"label":"绘图接口密钥","input_type":"password","hint":"直接填写密钥本身；保存于插件配置，请勿截图、分享或提交到仓库。"})
    model: str = Field(default="", max_length=200, description="必填：服务支持的绘图模型名称", json_schema_extra={"label":"绘图模型名称"})
    size: str = Field(default="1024x1024", max_length=50, description="图片尺寸；不支持该参数时设为空字符串", json_schema_extra={"label":"生成图片尺寸"})
    timeout_seconds: int = Field(default=180, ge=5, le=600, description="绘图及下载总超时（秒）", json_schema_extra={"label":"绘图与下载超时（秒）"})
    max_image_bytes: int = Field(default=8388608, ge=1024, le=16777216, description="单图字节上限；插件自设，不是 QQ 限制", json_schema_extra={"label":"图片大小上限（字节）"})
    max_pixels: int = Field(default=16777216, ge=1024, le=33554432, description="单图最大像素数", json_schema_extra={"label":"图片像素数上限"})


class TriggerSettings(PluginConfigBase):
    """设置活动检查、稳定等待、冷却和每日额度。"""

    __ui_label__ = "自动发布规则"
    __ui_order__ = 4

    auto_enabled: bool = Field(default=False, description="显式开启才会自动发布 QQ 空间", json_schema_extra={"label":"开启自动发布"})
    poll_seconds: int = Field(default=60, ge=10, le=3600, description="状态检查间隔（秒）", json_schema_extra={"label":"状态检查间隔（秒）"})
    stable_seconds: int = Field(default=120, ge=0, le=3600, description="新活动连续稳定时长（秒）", json_schema_extra={"label":"活动稳定等待（秒）"})
    cooldown_seconds: int = Field(default=7200, ge=0, le=86400, description="仅自动任务检查的生成间隔；管理员手动命令跳过，所有生成尝试仍留痕", json_schema_extra={"label":"自动生成冷却时间（秒）"})
    daily_limit: int = Field(default=3, ge=1, le=24, description="仅自动任务检查的每日上限；管理员手动命令跳过限制", json_schema_extra={"label":"自动任务每日上限（次）","hint":"所有尝试均记录并参与自动任务限流；手动命令不受此数值限制，每次生成可能收费。"})
    timezone: str = Field(default="Asia/Shanghai", description="每日额度时区", json_schema_extra={"label":"每日额度统计时区"})

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("无效 IANA 时区；Windows 请安装 tzdata") from exc
        return value


class PublishSettings(PluginConfigBase):
    """设置 QQ 空间可见范围、发布超时与草稿有效期。"""

    __ui_label__ = "空间发布"
    __ui_order__ = 5

    visibility: Literal[1, 4, 64] = Field(default=4, description="1 公开；4 好友可见；64 仅自己", json_schema_extra={"label":"空间可见范围","hint":"1：公开；4：好友可见；64：仅自己可见。"})
    timeout_seconds: int = Field(default=180, ge=5, le=600, description="发布等待超时（秒），超时结果不明且不重发", json_schema_extra={"label":"空间发布超时（秒）"})
    draft_ttl_seconds: int = Field(default=900, ge=60, le=7200, description="预览草稿有效期，活动结束时提前失效", json_schema_extra={"label":"草稿有效期（秒）"})


class StorageSettings(PluginConfigBase):
    """设置本地图片缓存与完成任务记录的保留时间。"""

    __ui_label__ = "缓存与记录"
    __ui_order__ = 6

    image_retention_days: int = Field(default=7, ge=1, le=90, description="图片缓存保留天数", json_schema_extra={"label":"图片缓存保留（天）"})
    log_retention_days: int = Field(default=90, ge=7, le=365, description="完成任务记录保留天数，结果不明记录不清理", json_schema_extra={"label":"任务记录保留（天）"})


class Settings(PluginConfigBase):
    plugin: PluginSettings = Field(default_factory=PluginSettings)
    character: CharacterSettings = Field(default_factory=CharacterSettings)
    text: TextSettings = Field(default_factory=TextSettings)
    image: ImageSettings = Field(default_factory=ImageSettings)
    trigger: TriggerSettings = Field(default_factory=TriggerSettings)
    publish: PublishSettings = Field(default_factory=PublishSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
