"""Run-scoped image policy, snapshot ownership, and model projection."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from pathlib import Path
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dagent.providers.model_io import ModelImage

from dagent.capabilities.images import ImageInputError, inspect_image, current_image_policy
from dagent.capabilities.tools.runtime_paths import RuntimePaths
from dagent.capabilities.workspace import current_workspace_root
from dagent.schemas.images import ImageData
from dagent.schemas.conversation import ContentReference, ImageReference, ConversationItem, item_images


def save_image(image: ImageData | ImageReference, workspace: str | Path) -> ImageReference:
    from dagent.harness_runtime.conversation_changes import note_resource

    if isinstance(image, ImageReference):
        read_image_snapshot(image, workspace)
        reference = image
    else:
        if len(image.data) > 4 * ((current_image_policy().max_image_bytes + 2) // 3):
            raise ImageInputError("Image exceeds max_image_bytes.")
        try:
            data = base64.b64decode(image.data, validate=True)
        except ValueError as exc:
            raise ImageInputError("Image contains invalid base64 data.") from exc
        policy = current_image_policy()
        media_type, width, height = inspect_image(data, policy, image.media_type)
        digest = hashlib.sha256(data).hexdigest()
        paths = RuntimePaths.current(workspace)
        root = (paths.root / "images").resolve()
        root.relative_to(paths.boundary)
        root.mkdir(parents=True, exist_ok=True)
        destination = root / digest
        if destination.is_symlink():
            raise ImageInputError("Image snapshot may not be a symbolic link.")
        if destination.exists():
            if destination.read_bytes() != data:
                raise ImageInputError("Existing image snapshot does not match its digest.")
        else:
            handle, temporary = tempfile.mkstemp(dir=root)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        reference = ImageReference(
            reference=ContentReference(path=paths.relative(destination), media_type=media_type,
                                       byte_length=len(data), sha256=digest),
            width=width, height=height,
        )
    paths = RuntimePaths.current(workspace)
    note_resource(reference.reference, workspace, paths.directory)
    return reference


def read_image_snapshot(image: ImageReference, workspace: str | Path) -> bytes:
    reference = image.reference
    try:
        path = RuntimePaths.current(workspace).resolve(reference.path)
        with path.open("rb") as stream:
            data = stream.read(current_image_policy().max_image_bytes + 1)
    except (OSError, ValueError) as exc:
        raise ImageInputError(f"Cannot read image snapshot {reference.path}: {exc}") from exc
    if len(data) != reference.byte_length or hashlib.sha256(data).hexdigest() != reference.sha256:
        raise ImageInputError(f"Image snapshot identity mismatch: {reference.path}.")
    _, width, height = inspect_image(data, current_image_policy(), reference.media_type)
    if (width, height) != (image.width, image.height):
        raise ImageInputError("Image snapshot dimensions do not match the reference.")
    return data


def project_images(images: Sequence[ImageReference], workspace: str | Path | None = None) -> tuple[ModelImage, ...]:
    from dagent.providers.model_io import ModelImage
    from dagent.harness_runtime.conversation_changes import note_resource

    workspace = workspace or current_workspace_root(".")
    policy = current_image_policy()
    projected = []
    for image in images:
        data = read_image_snapshot(image, workspace)
        note_resource(image.reference, workspace, RuntimePaths.current(workspace).directory)
        projected.append(ModelImage(
            data_url=f"data:{image.reference.media_type};base64,{base64.b64encode(data).decode('ascii')}",
            width=image.width, height=image.height,
            estimated_tokens=policy.estimated_tokens_per_image,
        ))
    return tuple(projected)


def source_images(items: Sequence[ConversationItem]) -> tuple[ImageReference, ...]:
    return tuple(image for item in items for image in item_images(item))


def merge_image_references(*groups: Sequence[ImageReference]) -> tuple[ImageReference, ...]:
    """Keep archived visual evidence across repeated summary replacements."""
    return tuple({(image.reference.path, image.reference.sha256): image
                  for group in groups for image in group}.values())
