from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CreateOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str | None = Field(default=None, max_length=30)
    style_hint: str | None = Field(default=None, max_length=500)


class DesignCanvas(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: int = Field(strict=True, ge=320, le=1920)
    height: int = Field(strict=True, ge=320, le=1920)

    @model_validator(mode="after")
    def bounded_area(self):
        if self.width * self.height > 2_073_600:
            raise ValueError("画布面积不能超过 2,073,600 像素")
        return self


class DesignOptions(CreateOptions):
    canvas: DesignCanvas
    page_count: int | None = Field(default=None, strict=True, ge=1, le=20)


class CreatePresentationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_request: str = Field(min_length=1, max_length=20_000)
    options: CreateOptions | DesignOptions = Field(default_factory=CreateOptions)
    entry_mode: Literal["agent", "ppt", "design", "image"] = "ppt"

    @field_validator("user_request")
    @classmethod
    def trim(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("需求不能为空")
        return value

    @model_validator(mode="after")
    def match_options_to_mode(self):
        if self.entry_mode == "design" and not isinstance(self.options, DesignOptions):
            raise ValueError("设计生成需要指定画布宽高")
        if self.entry_mode != "design" and isinstance(self.options, DesignOptions):
            raise ValueError("只有设计生成支持自定义画布和页数")
        return self


class Brief(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    purpose: str = Field(min_length=1, max_length=500)
    audience: str = Field(min_length=1, max_length=200)
    language: str = Field(default="zh-CN", max_length=30)
    tone: str = Field(min_length=1, max_length=200)
    assumptions: list[str] = Field(default_factory=list, max_length=10)


class Colors(BaseModel):
    background: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    surface: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    primary: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    text: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    muted_text: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")


class Typography(BaseModel):
    font_family: str = Field(min_length=1, max_length=200)
    title_size_px: int = Field(ge=18, le=64)
    body_size_px: int = Field(ge=10, le=36)
    caption_size_px: int = Field(ge=8, le=24)


class StyleSpec(BaseModel):
    description: str = Field(min_length=1, max_length=1000)
    colors: Colors
    typography: Typography
    layout_rules: list[str] = Field(min_length=1, max_length=20)


class SlideBrief(BaseModel):
    slide_id: str | None = None
    page_number: int = Field(ge=1, le=20)
    page_type: Literal["cover", "agenda", "section", "content", "ending", "other"] = "content"
    title: str = Field(min_length=1, max_length=100)
    key_message: str = Field(min_length=1, max_length=300)
    content_outline: list[str] = Field(min_length=1, max_length=12)
    design_notes: str = Field(default="", max_length=2000)
    summary: str = Field(min_length=1, max_length=300)


class OutlineDocument(BaseModel):
    schema_version: str = "1.0"
    brief: Brief
    style_spec: StyleSpec
    slides: list[SlideBrief] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_pages(self):
        ordered = sorted(self.slides, key=lambda s: s.page_number)
        if [s.page_number for s in ordered] != list(range(1, len(ordered) + 1)):
            raise ValueError("页面编号必须从 1 连续递增")
        return self


class UpdateOutlineRequest(BaseModel):
    outline_id: str
    content_hash: str
    document: OutlineDocument


class ConfirmRequest(BaseModel):
    outline_id: str
    content_hash: str


class ProviderUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=160)
    api_key: str | None = Field(default=None, max_length=500)
    enabled: bool = True


class GenerationSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reasoning_effort: Literal["auto", "none", "low", "high", "max"]


class RetryRequest(BaseModel):
    page_task_ids: list[str] | None = None
