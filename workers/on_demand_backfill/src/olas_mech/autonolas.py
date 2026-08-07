"""Autonolas GraphQL client for Olas Mech satellite entities by mech address."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("on_demand_backfill")

DEFAULT_BASE_URL = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-base"
DEFAULT_GNOSIS_URL = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis"

PAGE_SIZE = 1000

REQUEST_FIELDS = """
  id
  sender { id }
  priorityMech
  mech
  deliveredByMech
  isDelivered
  feeRaw
  feeUnit
  feeUSD
  finalFeeUSD
  service { serviceId }
  blockNumber
  blockTimestamp
  transactionHash
""".strip()

DELIVERY_FIELDS = """
  id
  deliveryMech
  blockTimestamp
  blockNumber
  transactionHash
  numDeliveries
  requestIds
  deliveredRequests
""".strip()


def url_for_chain(
    chain_id: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    gnosis_url: str = DEFAULT_GNOSIS_URL,
) -> str:
    chain = (chain_id or "").strip().lower()
    if chain == "base":
        return base_url
    if chain == "gnosis":
        return gnosis_url
    raise ValueError(f"unsupported olas_mech chain_id: {chain_id!r}")


def _request_where(address: str, cursor_id: str | None) -> str:
    addr = address.lower()
    or_clause = (
        f'{{ mech: "{addr}" }}, '
        f'{{ priorityMech: "{addr}" }}, '
        f'{{ sender_: {{ id: "{addr}" }} }}'
    )
    parts = [f"or: [ {or_clause} ]"]
    if cursor_id:
        parts.append(f'id_gt: "{cursor_id}"')
    return ", ".join(parts)


def _delivery_where(address: str, cursor_id: str | None) -> str:
    addr = address.lower()
    parts = [f'deliveryMech: "{addr}"']
    if cursor_id:
        parts.append(f'id_gt: "{cursor_id}"')
    return ", ".join(parts)


async def _fetch_pages(
    client: httpx.AsyncClient,
    *,
    url: str,
    root: str,
    where_fn,
    fields: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor_id: str | None = None
    while True:
        where = where_fn(cursor_id)
        query = f"""
        query {{
          {root}(
            first: {PAGE_SIZE}
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
            raise RuntimeError(f"Autonolas GraphQL errors for {root}: {payload['errors']}")
        data = (payload.get("data") or {}).get(root)
        if not isinstance(data, list):
            raise RuntimeError(f"unexpected Autonolas payload for {root}: {type(data)}")
        out.extend(data)
        if len(data) < PAGE_SIZE:
            break
        last_id = data[-1].get("id") if isinstance(data[-1], dict) else None
        if not last_id:
            break
        cursor_id = str(last_id)
    return out


async def fetch_satellites_for_mech(
    client: httpx.AsyncClient,
    *,
    url: str,
    address: str,
    chain_id: str,
) -> dict[str, list[dict[str, Any]]]:
    addr = address.lower().strip()
    if not addr:
        raise ValueError("mech address is required")

    requests = await _fetch_pages(
        client,
        url=url,
        root="requests",
        where_fn=lambda cursor: _request_where(addr, cursor),
        fields=REQUEST_FIELDS,
    )
    deliveries = await _fetch_pages(
        client,
        url=url,
        root="marketplaceDeliveries",
        where_fn=lambda cursor: _delivery_where(addr, cursor),
        fields=DELIVERY_FIELDS,
    )
    logger.info(
        "Autonolas olas_mech chain=%s address=%s requests=%s deliveries=%s",
        chain_id,
        addr,
        len(requests),
        len(deliveries),
    )
    return {"requests": requests, "deliveries": deliveries}
