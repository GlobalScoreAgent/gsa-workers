from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ResolveResult:
    ok: bool
    document: dict[str, Any] | list[Any] | None = None
    used_gateway: str | None = None
    error: str | None = None
    from_cache: bool = False
    uri_document_id: int | None = None
    nested_uris: list[str] = field(default_factory=list)
