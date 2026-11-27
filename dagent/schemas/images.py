"""Explicit image inputs and immutable model-visible image references."""

from __future__ import annotations

import base64
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ImageInputPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_image_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    max_image_pixels: int = Field(default=40_000_000, gt=0)
    max_images_per_request: int = Field(default=16, gt=0)
    max_total_image_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    estimated_tokens_per_image: int = Field(default=8192, gt=0)


class ImageData(BaseModel):
    """Base64 image returned explicitly by a Python capability or MCP tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["image_data"] = "image_data"
    data: str
    media_type: str | None = None

    @classmethod
    def from_bytes(cls, data: bytes, *, media_type: str | None = None) -> ImageData:
        return cls(data=base64.b64encode(data).decode("ascii"), media_type=media_type)



__all__ = ["ImageData", "ImageInputPolicy"]
