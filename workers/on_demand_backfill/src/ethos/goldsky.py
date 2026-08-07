"""Goldsky GraphQL client for Ethos signal history by profileId."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("on_demand_backfill")

DEFAULT_GOLDSKY_URL = (
    "https://api.goldsky.com/api/public/project_cmma0eekxnc4e01vt9klkbya9"
    "/subgraphs/ethos-network-base/prod/gn"
)

PAGE_SIZE = 1000

# entity_key -> (root_query, where_clause_template with {pid}, field selection)
# Filters match normalize link criteria (author and/or subject / profile / owner).
ENTITY_QUERIES: dict[str, tuple[str, str, str]] = {
    "attestations": (
        "ethosAttestations",
        'profile_: {{ profileId: "{pid}" }}',
        """
        id attestationId service account evidence createdAt archived
        profile { id profileId }
        """,
    ),
    "reviews": (
        "ethosReviews",
        'or: [{{ authorProfile_: {{ profileId: "{pid}" }} }}, {{ subjectProfile_: {{ profileId: "{pid}" }} }}]',
        """
        id reviewId score author subject attestationHash comment metadata createdAt archived
        authorProfile { id profileId }
        subjectProfile { id profileId }
        """,
    ),
    "vouches": (
        "ethosVouches",
        'or: [{{ authorProfile_: {{ profileId: "{pid}" }} }}, {{ subjectProfile_: {{ profileId: "{pid}" }} }}]',
        """
        id vouchId balance archived unhealthy vouchedAt unvouchedAt comment metadata
        authorProfile { id profileId }
        subjectProfile { id profileId }
        """,
    ),
    "slashes": (
        "ethosSlashes",
        'or: [{{ authorProfile_: {{ profileId: "{pid}" }} }}, {{ subjectProfile_: {{ profileId: "{pid}" }} }}]',
        """
        id slashId amount createdAt archived slashType comment metadata subject attestationHash
        authorProfile { id profileId }
        subjectProfile { id profileId }
        """,
    ),
    "reputation_markets": (
        "ethosReputationMarkets",
        'profileId: "{pid}"',
        """
        id profileId graduated voteTrust voteDistrust trustPrice distrustPrice
        liquidity basePrice createdAt updatedAt
        profile { id profileId }
        """,
    ),
    "market_trades": (
        "ethosMarketTrades",
        'profileId: "{pid}"',
        """
        id profileId trader isPositive isBuy amount funds timestamp txHash
        market { id profileId }
        """,
    ),
    "broker_posts": (
        "ethosBrokerPosts",
        'authorProfile_: {{ profileId: "{pid}" }}',
        """
        id postId authorProfileId type title description cost tags level
        createdAt updatedAt txHash
        authorProfile { id profileId }
        """,
    ),
    "projects": (
        "ethosProjects",
        'ownerProfile_: {{ profileId: "{pid}" }}',
        """
        id projectId userkey status name description createdAt updatedAt
        ownerProfile { id profileId }
        """,
    ),
    "bonds": (
        "ethosBonds",
        'authorProfile_: {{ profileId: "{pid}" }}',
        """
        id bondId amount bondType amountType status createdAt releasedAt
        authorProfile { id profileId }
        """,
    ),
}


def _build_query(root: str, where: str, fields: str, skip: int) -> str:
    return f"""
query EthosHistory {{
  {root}(where: {{ {where} }}, first: {PAGE_SIZE}, skip: {skip}, orderBy: id, orderDirection: asc) {{
    {fields}
  }}
}}
""".strip()


async def fetch_entity_pages(
    client: httpx.AsyncClient,
    *,
    url: str,
    entity: str,
    profile_id: int,
) -> list[dict[str, Any]]:
    spec = ENTITY_QUERIES.get(entity)
    if spec is None:
        raise ValueError(f"unknown entity: {entity}")
    root, where_tmpl, fields = spec
    where = where_tmpl.format(pid=str(profile_id))
    out: list[dict[str, Any]] = []
    skip = 0
    while True:
        query = _build_query(root, where, fields, skip)
        resp = await client.post(url, json={"query": query})
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"Goldsky GraphQL errors for {entity}: {payload['errors']}")
        batch = (payload.get("data") or {}).get(root) or []
        if not isinstance(batch, list):
            raise RuntimeError(f"unexpected Goldsky data for {entity}: {type(batch)}")
        out.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        skip += PAGE_SIZE
        logger.info(
            "Goldsky paginate entity=%s profile_id=%s skip=%s total=%s",
            entity,
            profile_id,
            skip,
            len(out),
        )
    return out


async def fetch_all_signals(
    client: httpx.AsyncClient,
    *,
    url: str,
    profile_id: int,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for entity in ENTITY_QUERIES:
        rows = await fetch_entity_pages(
            client, url=url, entity=entity, profile_id=profile_id
        )
        result[entity] = rows
        logger.info(
            "Goldsky entity=%s profile_id=%s rows=%s",
            entity,
            profile_id,
            len(rows),
        )
    return result
