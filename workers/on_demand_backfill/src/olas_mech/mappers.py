"""Map Autonolas Olas Mech satellite items → olas_mech upsert row dicts."""

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


def _i_opt(val: Any) -> int | None:
    if val is None or val == "":
        return None
    return int(val)


def _n_opt(val: Any) -> float | None:
    if val is None or val == "":
        return None
    return float(val)


def _bool_opt(val: Any) -> bool | None:
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        return val
    text = str(val).strip().lower()
    if text in ("true", "1"):
        return True
    if text in ("false", "0"):
        return False
    return None


def _sender_id(item: dict[str, Any]) -> str | None:
    sender = item.get("sender")
    if isinstance(sender, dict):
        return _lower(sender.get("id"))
    return _lower(sender)


def _service_id(item: dict[str, Any]) -> str | None:
    service = item.get("service")
    if isinstance(service, dict):
        return _s(service.get("serviceId"))
    return _s(item.get("serviceId"))


def map_request(item: dict[str, Any], *, chain_id: str) -> dict[str, Any] | None:
    gql_id = _s(item.get("id"))
    if not gql_id:
        return None
    chain = chain_id.strip().lower()
    return {
        "id": f"{chain}-{gql_id}",
        "sender": _sender_id(item),
        "priority_mech": _lower(item.get("priorityMech")),
        "mech": _lower(item.get("mech")),
        "delivered_by_mech": _lower(item.get("deliveredByMech")),
        "is_delivered": _bool_opt(item.get("isDelivered")),
        "fee_raw": _n_opt(item.get("feeRaw")),
        "fee_unit": _s(item.get("feeUnit")),
        "fee_usd": _n_opt(item.get("feeUSD")),
        "final_fee_usd": _n_opt(item.get("finalFeeUSD")),
        "service_id": _service_id(item),
        "chain_id": chain,
        "block_number": _i_opt(item.get("blockNumber")),
        "block_timestamp": _i_opt(item.get("blockTimestamp")),
        "tx_hash": _lower(item.get("transactionHash")),
    }


def map_delivery_rows(item: dict[str, Any], *, chain_id: str) -> list[dict[str, Any]]:
    delivery_id = _s(item.get("id"))
    if not delivery_id:
        return []
    chain = chain_id.strip().lower()
    delivery_mech = _lower(item.get("deliveryMech"))
    block_number = _i_opt(item.get("blockNumber"))
    block_timestamp = _i_opt(item.get("blockTimestamp"))
    tx_hash = _lower(item.get("transactionHash"))
    num_deliveries = _n_opt(item.get("numDeliveries"))

    request_ids = item.get("requestIds") or []
    delivered_flags = item.get("deliveredRequests") or []
    if not isinstance(request_ids, list):
        request_ids = []
    if not isinstance(delivered_flags, list):
        delivered_flags = []

    if not request_ids:
        return [
            {
                "id": f"{chain}-{delivery_id}",
                "request_id": None,
                "delivery_mech": delivery_mech,
                "delivered": None,
                "num_deliveries": num_deliveries,
                "chain_id": chain,
                "block_number": block_number,
                "block_timestamp": block_timestamp,
                "tx_hash": tx_hash,
            }
        ]

    rows: list[dict[str, Any]] = []
    for idx, raw_req in enumerate(request_ids):
        req_id = _lower(raw_req)
        if not req_id:
            continue
        delivered = None
        if idx < len(delivered_flags):
            delivered = _bool_opt(delivered_flags[idx])
        rows.append(
            {
                "id": f"{chain}-{delivery_id}-{req_id}",
                "request_id": req_id,
                "delivery_mech": delivery_mech,
                "delivered": delivered,
                "num_deliveries": num_deliveries,
                "chain_id": chain,
                "block_number": block_number,
                "block_timestamp": block_timestamp,
                "tx_hash": tx_hash,
            }
        )
    return rows


def map_all(
    raw: dict[str, list[dict[str, Any]]],
    *,
    chain_id: str,
) -> dict[str, list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []
    for item in raw.get("requests") or []:
        if not isinstance(item, dict):
            continue
        row = map_request(item, chain_id=chain_id)
        if row is not None:
            requests.append(row)

    deliveries: list[dict[str, Any]] = []
    for item in raw.get("deliveries") or []:
        if not isinstance(item, dict):
            continue
        deliveries.extend(map_delivery_rows(item, chain_id=chain_id))

    return {"requests": requests, "deliveries": deliveries}
