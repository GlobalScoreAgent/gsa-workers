"""Minimal JSON-RPC eth_call + Multicall3 helpers (Alchemy HTTP)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from networks import MULTICALL3

logger = logging.getLogger("wallet_lp_positions_discovery")


def alchemy_url(subdomain: str, api_key: str) -> str:
    return f"https://{subdomain}.g.alchemy.com/v2/{api_key}"


def _selector(sig: str) -> bytes:
    return function_signature_to_4byte_selector(sig)


def encode_call(sig: str, types: list[str], args: list[Any]) -> str:
    data = _selector(sig) + (encode(types, args) if types else b"")
    return "0x" + data.hex()


def decode_result(types: list[str], data_hex: str) -> tuple[Any, ...]:
    raw = data_hex[2:] if data_hex.startswith("0x") else data_hex
    if not raw:
        raise ValueError("empty eth_call return")
    return decode(types, bytes.fromhex(raw))


async def eth_call(
    client: httpx.AsyncClient,
    url: str,
    to: str,
    data: str,
    *,
    block: str = "latest",
) -> str | None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": to_checksum_address(to), "data": data}, block],
    }
    response = await client.post(url, json=payload, timeout=45.0)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError("Invalid eth_call body")
    if body.get("error"):
        raise RuntimeError(f"eth_call error: {body['error']}")
    result = body.get("result")
    if result is None or result in ("0x", "0x0"):
        return None
    return str(result)


async def multicall3(
    client: httpx.AsyncClient,
    url: str,
    calls: list[tuple[str, str]],
    *,
    allow_failure: bool = True,
) -> list[tuple[bool, bytes]]:
    """calls: list of (target, calldata_hex). Returns (success, returndata_bytes)."""
    if not calls:
        return []

    # aggregate3((address target, bool allowFailure, bytes callData)[])
    typed = [
        (
            to_checksum_address(target),
            allow_failure,
            bytes.fromhex(data[2:] if data.startswith("0x") else data),
        )
        for target, data in calls
    ]
    calldata = encode_call(
        "aggregate3((address,bool,bytes)[])",
        ["(address,bool,bytes)[]"],
        [typed],
    )
    result = await eth_call(client, url, MULTICALL3, calldata)
    if not result:
        return [(False, b"") for _ in calls]

    decoded = decode_result(["(bool,bytes)[]"], result)
    out: list[tuple[bool, bytes]] = []
    for success, ret in decoded[0]:
        out.append((bool(success), bytes(ret)))
    return out


def parse_uint(data: bytes) -> int:
    if not data:
        return 0
    return int.from_bytes(data, byteorder="big")


def parse_address(data: bytes) -> str:
    if len(data) < 32:
        return ""
    return "0x" + data[-20:].hex()
