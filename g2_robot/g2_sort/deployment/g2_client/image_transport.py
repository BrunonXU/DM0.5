"""Validated compressed camera frames for zero-copy policy transport."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import ContractError


@dataclass(frozen=True)
class EncodedImage:
    """One compressed RGB camera frame without an eager pixel decode."""

    data: bytes
    media_type: str
    width: int
    height: int

    def __post_init__(self) -> None:
        payload = bytes(self.data)
        if self.media_type not in {"image/jpeg", "image/png"}:
            raise ContractError(f"unsupported encoded image type {self.media_type!r}")
        # Some compressed GDK messages don't promise populated dimensions;
        # zero means "infer while decoding on the server".
        if self.width < 0 or self.height < 0:
            raise ContractError("encoded image dimensions cannot be negative")
        if not payload:
            raise ContractError("encoded image payload cannot be empty")
        if self.media_type == "image/jpeg" and not payload.startswith(b"\xff\xd8"):
            raise ContractError("JPEG payload is missing its SOI marker")
        if self.media_type == "image/png" and not payload.startswith(
            b"\x89PNG\r\n\x1a\n"
        ):
            raise ContractError("PNG payload has an invalid signature")
        object.__setattr__(self, "data", payload)
