"""Map Goldsky ERC-8183 satellite items → bsc_erc_8183 upsert row dicts."""

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


def _n_opt(val: Any) -> float | None:
    if val is None or val == "":
        return None
    return float(val)


def _job_ref(chain_id: str, contract: str, job_id: int) -> str:
    return f"{chain_id}-{contract}-{job_id}"


def _common(item: dict[str, Any]) -> dict[str, Any]:
    contract = _lower(item.get("contractAddress")) or ""
    chain = _s(item.get("chainId")) or ""
    job_id = _i(item.get("jobId"), 0)
    ts = _i_opt(item.get("blockTimestamp"))
    return {
        "id": _s(item.get("id")) or "",
        "job_id": job_id,
        "contract_address": contract,
        "chain_id": chain,
        "block_number": _i_opt(item.get("blockNumber")),
        "block_timestamp": ts,
        "tx_hash": _lower(item.get("txHash")),
        "log_index": _i_opt(item.get("logIndex")),
        "job_ref_id": _job_ref(chain, contract, job_id) if chain and contract else None,
    }


def map_payment(item: dict[str, Any]) -> dict[str, Any]:
    row = _common(item)
    row["event_type"] = _s(item.get("eventType")) or ""
    row["account"] = _lower(item.get("account"))
    row["amount"] = _n_opt(item.get("amount"))
    return row


def map_budget(item: dict[str, Any]) -> dict[str, Any]:
    row = _common(item)
    row["budget"] = _n_opt(item.get("budget"))
    return row


def map_delivery(item: dict[str, Any]) -> dict[str, Any]:
    row = _common(item)
    row["provider"] = _lower(item.get("provider"))
    row["deliverable"] = _s(item.get("deliverable"))
    return row


def map_job_status(item: dict[str, Any]) -> dict[str, Any]:
    row = _common(item)
    row["status_type"] = _s(item.get("statusType")) or ""
    row["actor"] = _lower(item.get("actor"))
    row["reason"] = _s(item.get("reason"))
    return row


MAPPERS = {
    "payments": map_payment,
    "budgets": map_budget,
    "deliveries": map_delivery,
    "job_statuses": map_job_status,
}


def map_all(raw: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for entity, items in raw.items():
        mapper = MAPPERS[entity]
        rows = []
        for item in items:
            if not isinstance(item, dict):
                continue
            row = mapper(item)
            if not row.get("id"):
                continue
            rows.append(row)
        out[entity] = rows
    return out
