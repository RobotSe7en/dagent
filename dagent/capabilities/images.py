"""Image decoding and validation shared by model inputs and capability results."""

from __future__ import annotations

from io import BytesIO
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dagent.result import RunResult

from PIL import Image, UnidentifiedImageError

from dagent.schemas.images import ImageInputPolicy


class ImageInputError(ValueError):
    """An explicit visual input is invalid or cannot be recovered."""

    result: RunResult | None = None


_MEDIA_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


def inspect_image(data: bytes, policy: ImageInputPolicy, media_type: str | None = None) -> tuple[str, int, int]:
    if not data or len(data) > policy.max_image_bytes:
        raise ImageInputError("Image is empty or exceeds max_image_bytes.")
    try:
        with Image.open(BytesIO(data), formats=list(_MEDIA_TYPES)) as image:
            actual = _MEDIA_TYPES[image.format]
            width, height = image.size
            if width * height > policy.max_image_pixels:
                raise ImageInputError("Image exceeds max_image_pixels.")
            if getattr(image, "is_animated", False):
                raise ImageInputError("Animated images are not supported.")
            if media_type is not None and media_type != actual:
                raise ImageInputError(f"Image media type {media_type!r} does not match {actual!r}.")
            image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError) as exc:
        raise ImageInputError(f"Cannot decode image: {exc}") from exc
    return actual, width, height


_POLICY: ContextVar[ImageInputPolicy | None] = ContextVar("dagent_image_policy", default=None)


def configured_image_policy() -> ImageInputPolicy | None:
    return _POLICY.get()


def current_image_policy() -> ImageInputPolicy:
    return _POLICY.get() or ImageInputPolicy()


@contextmanager
def image_policy_context(policy: ImageInputPolicy | None) -> Iterator[None]:
    token = _POLICY.set(policy)
    try:
        yield
    finally:
        _POLICY.reset(token)
