"""URI resolve dispatcher with uri_documents dedupe + nested/DID."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from documents import extract_cid
from handlers.http import fetch_http, looks_like_http
from handlers.inline import try_data_uri, try_hex, try_raw_json
from handlers.ipfs import fetch_ipfs, looks_like_ipfs
from nested import (
    extract_did_uri,
    extract_nested_uri,
    inject_did_json,
    replace_with_nested,
)
from result import ResolveResult

logger = logging.getLogger("agent_uri_resolve.resolve")


class UriResolver:
    def __init__(
        self,
        db: Any,
        http: httpx.AsyncClient,
        pinata_token: str = "",
        scrape_do_token: str = "",
    ):
        self.db = db
        self.http = http
        self.pinata_token = pinata_token
        self.scrape_do_token = scrape_do_token
        self._mem: dict[str, ResolveResult] = {}

    async def resolve(
        self,
        uri: str,
        *,
        force_refresh: bool = False,
        _depth: int = 0,
    ) -> ResolveResult:
        uri = (uri or "").strip()
        if not uri:
            return ResolveResult(ok=False, error="empty_uri")
        if _depth > 5:
            return ResolveResult(ok=False, error="nested_depth_exceeded")

        if not force_refresh and uri in self._mem:
            return self._mem[uri]

        if not force_refresh:
            cached = self.db.lookup_document(uri)
            if cached is not None:
                result = ResolveResult(
                    ok=True,
                    document=cached["document"],
                    used_gateway=cached.get("source_gateway") or "uri_documents",
                    from_cache=True,
                    uri_document_id=cached["id"],
                )
                self.db.touch_document(cached["id"])
                self._mem[uri] = result
                # Still resolve nested/DID if pointers present but missing inject
                return await self._follow_nested(uri, result, _depth=_depth)

        raw = await self._resolve_raw(uri)
        if not raw.ok or raw.document is None:
            return raw

        doc_id = self.db.upsert_document(
            uri=uri,
            document=raw.document,
            source_gateway=raw.used_gateway or "resolved",
            cid=extract_cid(uri),
        )
        raw.uri_document_id = doc_id
        self._mem[uri] = raw
        return await self._follow_nested(uri, raw, _depth=_depth)

    async def _follow_nested(
        self,
        uri: str,
        result: ResolveResult,
        *,
        _depth: int,
    ) -> ResolveResult:
        document = result.document
        nested_uri = extract_nested_uri(document)
        if nested_uri:
            result.nested_uris.append(nested_uri)
            nested = await self.resolve(
                nested_uri, force_refresh=False, _depth=_depth + 1
            )
            if nested.ok and nested.document is not None:
                document = replace_with_nested(document, nested.document)
                result.document = document
                result.used_gateway = (result.used_gateway or "") + "+nested"
                # Parent document updated with nested body — persist
                result.uri_document_id = self.db.upsert_document(
                    uri=uri,
                    document=document,
                    source_gateway=result.used_gateway or "nested",
                    cid=extract_cid(uri),
                )

        did_uri = extract_did_uri(document)
        if did_uri:
            result.nested_uris.append(did_uri)
            did = await self.resolve(did_uri, force_refresh=False, _depth=_depth + 1)
            if did.ok and did.document is not None:
                document = inject_did_json(document, did.document)
                result.document = document
                result.used_gateway = (result.used_gateway or "") + "+did"
                result.uri_document_id = self.db.upsert_document(
                    uri=uri,
                    document=document,
                    source_gateway=result.used_gateway or "with_did",
                    cid=extract_cid(uri),
                )
                if did.uri_document_id is None and did.document is not None:
                    did.uri_document_id = self.db.upsert_document(
                        uri=did_uri,
                        document=did.document,
                        source_gateway=did.used_gateway or "did",
                        cid=extract_cid(did_uri),
                    )

        self._mem[uri] = result
        return result

    async def _resolve_raw(self, uri: str) -> ResolveResult:
        for fn in (try_hex, try_raw_json, try_data_uri):
            hit = fn(uri)
            if hit is not None:
                return hit

        if looks_like_ipfs(uri):
            return await fetch_ipfs(uri, self.http, self.pinata_token)

        if looks_like_http(uri):
            return await fetch_http(uri, self.http, self.scrape_do_token)

        return ResolveResult(ok=False, error="unsupported_uri_scheme")
