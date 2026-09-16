"""Cliente de Supabase Storage para el bucket privado humi-reasons."""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger("humi_reason_publisher")

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class StorageError(RuntimeError):
    pass


class StorageClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        service_role_key: str,
        bucket: str,
        max_attempts: int = 3,
        retry_base_seconds: float = 1.0,
    ):
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._bucket = bucket
        self._max_attempts = max(int(max_attempts), 1)
        self._retry_base_seconds = retry_base_seconds
        self._headers = {
            "Authorization": f"Bearer {service_role_key}",
            "apikey": service_role_key,
            "Content-Type": "application/json",
            "x-upsert": "true",
            "Cache-Control": "no-cache",
        }

    async def upload(self, path: str, payload: bytes) -> None:
        url = f"{self._base_url}/storage/v1/object/{self._bucket}/{path}"
        last_detail = ""
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.post(url, content=payload, headers=self._headers)
            except httpx.HTTPError as exc:
                last_detail = f"{exc.__class__.__name__}: {exc}"
            else:
                if response.status_code < 300:
                    return
                last_detail = f"HTTP {response.status_code}: {response.text[:300]}"
                if response.status_code not in RETRYABLE_STATUS:
                    raise StorageError(f"upload {path} failed ({last_detail})")

            if attempt >= self._max_attempts:
                break
            await asyncio.sleep(self._retry_base_seconds * attempt)

        raise StorageError(f"upload {path} failed after {self._max_attempts} attempts ({last_detail})")
