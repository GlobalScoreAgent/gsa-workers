"""Goldsky GraphQL client for Virtual ACP satellite entities by job."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("on_demand_backfill")

DEFAULT_GOLDSKY_URL = (
    "https://api.goldsky.com/api/public/project_cmma0eekxnc4e01vt9klkbya9"
    "/subgraphs/virtual-acp-base/prod/gn"
)

PAGE_SIZE = 1000

# entity_key -> (root_query, field selection)
ENTITY_QUERIES: dict[str, tuple[str, str]] = {
    "payments": (
        "virtualAcpPayments",
        """
        id eventType jobId account amount chainId
        blockNumber blockTimestamp txHash logIndex
        """,
    ),
    "budgets": (
        "virtualAcpBudgets",
        """
        id jobId budget chainId
        blockNumber blockTimestamp txHash logIndex
        """,
    ),
    "deliveries": (
        "virtualAcpDeliveries",
        """
        id jobId provider deliverable chainId
        blockNumber blockTimestamp txHash logIndex
        """,
    ),
    "job_statuses": (
        "virtualAcpJobStatuses",
        """
        id jobId statusType actor reason chainId
        blockNumber blockTimestamp txHash logIndex
        """,
    ),
}


def _job_where(job_id: int, chain_id: str) -> str:
    return f'jobId: "{job_id}", chainId: "{chain_id}"'


async def _fetch_pages(
    client: httpx.AsyncClient,
    *,
    url: str,
    root: str,
    where: str,
    fields: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    skip = 0
    while True:
        query = f"""
        query {{
          {root}(
            first: {PAGE_SIZE}
            skip: {skip}
            where: {{ {where} }}
            orderBy: id
            orderDirection: asc
          ) {{
            {fields}
          }}
        }}
        """
        resp = await client.post(url, json={"query": query})
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"Goldsky GraphQL errors for {root}: {payload['errors']}")
        data = (payload.get("data") or {}).get(root)
        if not isinstance(data, list):
            raise RuntimeError(f"unexpected Goldsky payload for {root}: {type(data)}")
        out.extend(data)
        if len(data) < PAGE_SIZE:
            break
        skip += PAGE_SIZE
    return out


async def fetch_satellites_for_job(
    client: httpx.AsyncClient,
    *,
    url: str,
    job_id: int,
    chain_id: str,
) -> dict[str, list[dict[str, Any]]]:
    where = _job_where(job_id, chain_id)
    result: dict[str, list[dict[str, Any]]] = {}
    for entity, (root, fields) in ENTITY_QUERIES.items():
        rows = await _fetch_pages(client, url=url, root=root, where=where, fields=fields)
        result[entity] = rows
        logger.info(
            "Goldsky virtual_acp entity=%s job_id=%s rows=%s",
            entity,
            job_id,
            len(rows),
        )
    return result
