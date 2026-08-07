"""Map Goldsky GraphQL items → ethos.* upsert row dicts (normalize-compatible)."""

from __future__ import annotations

from typing import Any


def _s(val: Any) -> str | None:
    if val is None:
        return None
    text = str(val).strip()
    return text if text else None


def _lower(val: Any) -> str | None:
    text = _s(val)
    return text.lower() if text else None


def _i(val: Any, default: int = 0) -> int:
    if val is None or val == "":
        return default
    return int(val)


def _i_opt(val: Any) -> int | None:
    if val is None or val == "":
        return None
    return int(val)


def _f(val: Any, default: float = 0.0) -> float:
    if val is None or val == "":
        return default
    return float(val)


def _b(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).lower() in ("true", "1", "yes")


def _profile_id(obj: Any) -> int | None:
    if not isinstance(obj, dict):
        return None
    return _i_opt(obj.get("profileId")) or _i_opt(obj.get("id"))


def map_attestations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for it in items:
        graph_id = _s(it.get("id"))
        if not graph_id:
            continue
        pid = _profile_id(it.get("profile"))
        if pid is None:
            continue
        rows.append(
            {
                "graph_id": graph_id,
                "attestation_id": _i(it.get("attestationId")),
                "profile_id": pid,
                "service": _s(it.get("service")) or "",
                "account": _s(it.get("account")) or "",
                "evidence": _s(it.get("evidence")),
                "created_at": _i(it.get("createdAt")),
                "archived": _b(it.get("archived")),
            }
        )
    return rows


def map_reviews(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for it in items:
        graph_id = _s(it.get("id"))
        if not graph_id:
            continue
        rows.append(
            {
                "graph_id": graph_id,
                "review_id": _i(it.get("reviewId")),
                "score": _s(it.get("score")),
                "author_address": _lower(it.get("author")),
                "subject_address": _lower(it.get("subject")),
                "attestation_hash": _s(it.get("attestationHash")),
                "comment": _s(it.get("comment")),
                "metadata": _s(it.get("metadata")),
                "created_at": _i(it.get("createdAt")),
                "archived": _b(it.get("archived")),
                "author_profile_id": _profile_id(it.get("authorProfile")),
                "subject_profile_id": _profile_id(it.get("subjectProfile")),
            }
        )
    return rows


def map_vouches(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for it in items:
        graph_id = _s(it.get("id"))
        author = _profile_id(it.get("authorProfile"))
        subject = _profile_id(it.get("subjectProfile"))
        if not graph_id or author is None or subject is None:
            continue
        rows.append(
            {
                "graph_id": graph_id,
                "vouch_id": _i(it.get("vouchId")),
                "balance": _f(it.get("balance")),
                "archived": _b(it.get("archived")),
                "unhealthy": _b(it.get("unhealthy")),
                "vouched_at": _i(it.get("vouchedAt")),
                "unvouched_at": _i_opt(it.get("unvouchedAt")),
                "comment": _s(it.get("comment")),
                "metadata": _s(it.get("metadata")),
                "author_profile_id": author,
                "subject_profile_id": subject,
            }
        )
    return rows


def map_slashes(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for it in items:
        graph_id = _s(it.get("id"))
        author = _profile_id(it.get("authorProfile"))
        if not graph_id or author is None:
            continue
        rows.append(
            {
                "graph_id": graph_id,
                "slash_id": _i(it.get("slashId")),
                "amount": _f(it.get("amount")),
                "created_at": _i(it.get("createdAt")),
                "archived": _b(it.get("archived")),
                "slash_type": _i_opt(it.get("slashType")),
                "comment": _s(it.get("comment")),
                "metadata": _s(it.get("metadata")),
                "subject_address": _lower(it.get("subject")),
                "attestation_hash": _s(it.get("attestationHash")),
                "author_profile_id": author,
                "subject_profile_id": _profile_id(it.get("subjectProfile")),
            }
        )
    return rows


def map_reputation_markets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for it in items:
        graph_id = _s(it.get("id"))
        pid = _i_opt(it.get("profileId")) or _profile_id(it.get("profile"))
        if not graph_id or pid is None:
            continue
        rows.append(
            {
                "graph_id": graph_id,
                "profile_id": pid,
                "graduated": _b(it.get("graduated")),
                "vote_trust": _f(it.get("voteTrust")),
                "vote_distrust": _f(it.get("voteDistrust")),
                "trust_price": _f(it.get("trustPrice")),
                "distrust_price": _f(it.get("distrustPrice")),
                "liquidity": _f(it.get("liquidity")),
                "base_price": _f(it.get("basePrice")),
                "created_at": _i(it.get("createdAt")),
                "updated_at": _i(it.get("updatedAt")),
            }
        )
    return rows


def map_market_trades(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for it in items:
        graph_id = _s(it.get("id"))
        pid = _i_opt(it.get("profileId"))
        if pid is None and isinstance(it.get("market"), dict):
            pid = _i_opt(it["market"].get("profileId"))
        if not graph_id or pid is None:
            continue
        rows.append(
            {
                "graph_id": graph_id,
                "profile_id": pid,
                "trader_address": _lower(it.get("trader")),
                "is_positive": _b(it.get("isPositive")),
                "is_buy": _b(it.get("isBuy")),
                "amount": _f(it.get("amount")),
                "funds": _f(it.get("funds")),
                "traded_at": _i(it.get("timestamp")),
                "tx_hash": _s(it.get("txHash")),
            }
        )
    return rows


def map_broker_posts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for it in items:
        graph_id = _s(it.get("id"))
        author = _profile_id(it.get("authorProfile")) or _i_opt(it.get("authorProfileId"))
        if not graph_id or author is None:
            continue
        rows.append(
            {
                "graph_id": graph_id,
                "post_id": _i(it.get("postId")),
                "author_profile_id": author,
                "post_type": _s(it.get("type")),
                "title": _s(it.get("title")),
                "description": _s(it.get("description")),
                "cost": _s(it.get("cost")),
                "tags": _s(it.get("tags")),
                "level": _i_opt(it.get("level")),
                "created_at": _i(it.get("createdAt")),
                "updated_at": _i_opt(it.get("updatedAt")),
                "tx_hash": _s(it.get("txHash")),
            }
        )
    return rows


def map_projects(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for it in items:
        graph_id = _s(it.get("id"))
        owner = _profile_id(it.get("ownerProfile"))
        if not graph_id or owner is None:
            continue
        rows.append(
            {
                "graph_id": graph_id,
                "project_id": _i(it.get("projectId")),
                "userkey": _s(it.get("userkey")),
                "status": _s(it.get("status")),
                "name": _s(it.get("name")),
                "description": _s(it.get("description")),
                "created_at": _i(it.get("createdAt")),
                "updated_at": _i_opt(it.get("updatedAt")),
                "owner_profile_id": owner,
            }
        )
    return rows


def map_bonds(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for it in items:
        graph_id = _s(it.get("id"))
        author = _profile_id(it.get("authorProfile"))
        if not graph_id or author is None:
            continue
        rows.append(
            {
                "graph_id": graph_id,
                "bond_id": _i(it.get("bondId")),
                "amount": _s(it.get("amount")),
                "bond_type": _s(it.get("bondType")),
                "amount_type": _s(it.get("amountType")),
                "status": _s(it.get("status")),
                "created_at": _i(it.get("createdAt")),
                "released_at": _i_opt(it.get("releasedAt")),
                "author_profile_id": author,
            }
        )
    return rows


MAPPERS = {
    "attestations": map_attestations,
    "reviews": map_reviews,
    "vouches": map_vouches,
    "slashes": map_slashes,
    "reputation_markets": map_reputation_markets,
    "market_trades": map_market_trades,
    "broker_posts": map_broker_posts,
    "projects": map_projects,
    "bonds": map_bonds,
}


def map_all(raw: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for entity, items in raw.items():
        mapper = MAPPERS.get(entity)
        if mapper is None:
            raise ValueError(f"no mapper for entity={entity}")
        out[entity] = mapper(items)
    return out
