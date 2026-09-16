"""Ethereum address validation and normalization."""


class AddressError(ValueError):
    """Raised when an address fails format validation."""


def normalize_address(address: str) -> str:
    """Validate and return a lowercase hex address."""
    if not address or not isinstance(address, str):
        raise AddressError("Address must be a non-empty string")

    normalized = address.strip().lower()

    if not normalized.startswith("0x") or len(normalized) != 42:
        raise AddressError("Address must start with 0x and be 42 characters long")

    try:
        int(normalized[2:], 16)
    except ValueError as exc:
        raise AddressError("Address contains invalid hexadecimal characters") from exc

    return normalized


def is_valid_evm_address(address: str) -> bool:
    """Strict EVM check aligned with wallet-transactional-current-batch edge function."""
    if not address or not isinstance(address, str):
        return False
    return address.startswith("0x") and len(address) == 42
