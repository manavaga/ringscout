"""Cryptographic EIP-712 / EIP-3009 (`TransferWithAuthorization`) signer & verifier for x402 v2 USDC."""

from __future__ import annotations

import secrets
import struct
import time
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed, decode_dss_signature

SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
SECP256K1_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

_KECCAK_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)
_KECCAK_ROT = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)


def _rol64(x: int, n: int) -> int:
    return ((x << n) | (x >> (64 - n))) & 0xFFFFFFFFFFFFFFFF


def keccak256(data: bytes) -> bytes:
    """Ethereum Keccak-256 (original Keccak padding `0x01`, distinct from NIST SHA3-256 `0x06`)."""
    rate = 136
    buf = bytearray(data)
    buf.append(0x01)
    while len(buf) % rate != 0:
        buf.append(0x00)
    buf[-1] |= 0x80
    st = [[0] * 5 for _ in range(5)]
    for off in range(0, len(buf), rate):
        lanes = struct.unpack_from("<17Q", buf, off)
        for idx, val in enumerate(lanes):
            st[idx % 5][idx // 5] ^= val
        for rc in _KECCAK_RC:
            c = [st[x][0] ^ st[x][1] ^ st[x][2] ^ st[x][3] ^ st[x][4] for x in range(5)]
            d = [c[(x - 1) % 5] ^ _rol64(c[(x + 1) % 5], 1) for x in range(5)]
            for x in range(5):
                for y in range(5):
                    st[x][y] ^= d[x]
            b = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    b[y][(2 * x + 3 * y) % 5] = _rol64(st[x][y], _KECCAK_ROT[x][y])
            for x in range(5):
                for y in range(5):
                    st[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
            st[0][0] ^= rc
    out = bytearray()
    for y in range(5):
        for x in range(5):
            out.extend(struct.pack("<Q", st[x][y]))
            if len(out) >= 32:
                return bytes(out[:32])
    return bytes(out[:32])


def eip55_checksum_address(raw_addr: str) -> str:
    """Return EIP-55 mixed-case checksummed Ethereum `0x...` address."""
    clean = raw_addr.strip()
    if clean.startswith(("0x", "0X")):
        clean = clean[2:]
    if len(clean) != 40:
        raise ValueError(f"Invalid 20-byte hex address: {raw_addr}")
    low = clean.lower()
    int(low, 16)
    digest_hex = keccak256(low.encode("ascii")).hex()
    return "0x" + "".join(
        ch.upper() if int(digest_hex[i], 16) >= 8 else ch for i, ch in enumerate(low)
    )


def private_key_hex_to_address(priv_hex: str) -> str:
    clean = priv_hex[2:] if priv_hex.startswith(("0x", "0X")) else priv_hex
    priv_int = int(clean, 16)
    pk = ec.derive_private_key(priv_int, ec.SECP256K1())
    uncompressed = pk.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )[1:]
    return eip55_checksum_address(keccak256(uncompressed)[-20:].hex())


def generate_evm_keypair() -> tuple[str, str]:
    """Generate a fresh `(private_key_hex, checksum_address)` on secp256k1."""
    while True:
        raw = secrets.token_bytes(32)
        val = int.from_bytes(raw, "big")
        if 1 <= val < SECP256K1_N:
            priv_hex = "0x" + raw.hex()
            return priv_hex, private_key_hex_to_address(priv_hex)


def _point_add(
    p1: tuple[int, int] | None, p2: tuple[int, int] | None
) -> tuple[int, int] | None:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % SECP256K1_P == 0:
        return None
    if x1 == x2 and y1 == y2:
        lam = (3 * x1 * x1 * pow(2 * y1, SECP256K1_P - 2, SECP256K1_P)) % SECP256K1_P
    else:
        lam = ((y2 - y1) * pow((x2 - x1) % SECP256K1_P, SECP256K1_P - 2, SECP256K1_P)) % SECP256K1_P
    x3 = (lam * lam - x1 - x2) % SECP256K1_P
    y3 = (lam * (x1 - x3) - y1) % SECP256K1_P
    return (x3, y3)


def _point_mul(k: int, pt: tuple[int, int]) -> tuple[int, int] | None:
    k = k % SECP256K1_N
    if k == 0:
        return None
    res: tuple[int, int] | None = None
    addend: tuple[int, int] | None = pt
    while k:
        if k & 1:
            res = _point_add(res, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return res


def ecrecover_digest(digest32: bytes, sig_hex: str) -> str | None:
    """Pure secp256k1 `ecrecover(digest32, v, r, s)` returning EIP-55 `0x` address or `None`."""
    clean = sig_hex[2:] if sig_hex.startswith(("0x", "0X")) else sig_hex
    if len(clean) != 130 or len(digest32) != 32:
        return None
    try:
        sig_bytes = bytes.fromhex(clean)
    except ValueError:
        return None
    r = int.from_bytes(sig_bytes[:32], "big")
    s = int.from_bytes(sig_bytes[32:64], "big")
    v = sig_bytes[64]
    if v in (27, 28):
        recid = v - 27
    elif v in (0, 1):
        recid = v
    else:
        return None
    if not (1 <= r < SECP256K1_N and 1 <= s <= SECP256K1_N // 2):
        return None
    x = r
    if x >= SECP256K1_P:
        return None
    y_sq = (pow(x, 3, SECP256K1_P) + 7) % SECP256K1_P
    y = pow(y_sq, (SECP256K1_P + 1) // 4, SECP256K1_P)
    if (y * y) % SECP256K1_P != y_sq:
        return None
    if (y & 1) != (recid & 1):
        y = SECP256K1_P - y

    r_inv = pow(r, SECP256K1_N - 2, SECP256K1_N)
    z = int.from_bytes(digest32, "big") % SECP256K1_N
    u1 = (-z * r_inv) % SECP256K1_N
    u2 = (s * r_inv) % SECP256K1_N
    q = _point_add(_point_mul(u1, (SECP256K1_GX, SECP256K1_GY)), _point_mul(u2, (x, y)))
    if q is None:
        return None
    pub_bytes = q[0].to_bytes(32, "big") + q[1].to_bytes(32, "big")
    return eip55_checksum_address(keccak256(pub_bytes)[-20:].hex())


def compute_eip3009_digest(
    *,
    chain_id: int,
    token_contract: str,
    token_name: str,
    token_version: str,
    from_addr: str,
    to_addr: str,
    value_atomic: int,
    valid_after: int,
    valid_before: int,
    nonce_hex: str,
) -> bytes:
    """Compute EIP-712 `TransferWithAuthorization` digest for Circle USDC (EIP-3009)."""
    domain_type = keccak256(
        b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
    )
    contract_bytes = bytes.fromhex(eip55_checksum_address(token_contract)[2:])
    domain_sep = keccak256(
        domain_type
        + keccak256(token_name.encode("utf-8"))
        + keccak256(token_version.encode("utf-8"))
        + chain_id.to_bytes(32, "big")
        + contract_bytes.rjust(32, b"\x00")
    )
    auth_type = keccak256(
        b"TransferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    )
    clean_nonce = nonce_hex[2:] if nonce_hex.startswith(("0x", "0X")) else nonce_hex
    nonce_bytes = bytes.fromhex(clean_nonce)
    if len(nonce_bytes) != 32:
        raise ValueError("EIP-3009 nonce must be 32 bytes")

    struct_hash = keccak256(
        auth_type
        + bytes.fromhex(eip55_checksum_address(from_addr)[2:]).rjust(32, b"\x00")
        + bytes.fromhex(eip55_checksum_address(to_addr)[2:]).rjust(32, b"\x00")
        + int(value_atomic).to_bytes(32, "big")
        + int(valid_after).to_bytes(32, "big")
        + int(valid_before).to_bytes(32, "big")
        + nonce_bytes
    )
    return keccak256(b"\x19\x01" + domain_sep + struct_hash)


def sign_x402_exact_evm_payload(
    *,
    buyer_private_key_hex: str,
    accepted_requirement: dict[str, Any],
    valid_for_sec: int = 600,
) -> dict[str, Any]:
    """Build and cryptographically sign a complete `x402Version: 2` EIP-3009 `paymentPayload`."""
    network = str(accepted_requirement.get("network") or "eip155:84532")
    chain_id = int(network.split(":")[1]) if ":" in network else 84532
    token_contract = str(accepted_requirement["asset"])
    pay_to = eip55_checksum_address(str(accepted_requirement["payTo"]))
    amount_int = int(str(accepted_requirement["amount"]))
    extra = accepted_requirement.get("extra") or {}
    token_name = str(extra.get("name") or "USDC")
    token_version = str(extra.get("version") or "2")

    buyer_addr = private_key_hex_to_address(buyer_private_key_hex)
    nonce_hex = "0x" + secrets.token_bytes(32).hex()
    valid_after = 0
    valid_before = int(time.time()) + valid_for_sec

    digest = compute_eip3009_digest(
        chain_id=chain_id,
        token_contract=token_contract,
        token_name=token_name,
        token_version=token_version,
        from_addr=buyer_addr,
        to_addr=pay_to,
        value_atomic=amount_int,
        valid_after=valid_after,
        valid_before=valid_before,
        nonce_hex=nonce_hex,
    )

    clean_priv = (
        buyer_private_key_hex[2:]
        if buyer_private_key_hex.startswith(("0x", "0X"))
        else buyer_private_key_hex
    )
    pk = ec.derive_private_key(int(clean_priv, 16), ec.SECP256K1())
    der_sig = pk.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
    r_val, s_val = decode_dss_signature(der_sig)
    if s_val > SECP256K1_N // 2:
        s_val = SECP256K1_N - s_val

    sig_hex = ""
    for v_cand in (27, 28):
        cand_hex = (
            "0x"
            + r_val.to_bytes(32, "big").hex()
            + s_val.to_bytes(32, "big").hex()
            + bytes([v_cand]).hex()
        )
        if ecrecover_digest(digest, cand_hex) == buyer_addr:
            sig_hex = cand_hex
            break
    if not sig_hex:
        raise RuntimeError("Failed to recover matching v parity for EIP-3009 signature")

    return {
        "x402Version": 2,
        "accepted": accepted_requirement,
        "payload": {
            "signature": sig_hex,
            "authorization": {
                "from": buyer_addr,
                "to": pay_to,
                "value": str(amount_int),
                "validAfter": str(valid_after),
                "validBefore": str(valid_before),
                "nonce": nonce_hex,
            },
        },
    }


def verify_x402_eip3009_signature_locally(
    payment_payload: dict[str, Any],
) -> tuple[bool, str | None, str]:
    """Deterministically verify EIP-3009 `TransferWithAuthorization` `ecrecover`, field bindings, & expiry locally."""
    try:
        accepted = payment_payload.get("accepted") or {}
        inner = payment_payload.get("payload") or {}
        sig_hex = str(inner.get("signature") or "")
        auth = inner.get("authorization") or {}

        network = str(accepted.get("network") or "")
        chain_id = int(network.split(":")[1]) if ":" in network else 84532
        extra = accepted.get("extra") or {}
        from_addr = eip55_checksum_address(str(auth["from"]))
        to_addr = eip55_checksum_address(str(auth["to"]))
        expected_pay_to = eip55_checksum_address(str(accepted["payTo"]))
        if to_addr.lower() != expected_pay_to.lower():
            return False, None, f"EIP-3009 authorization.to ({to_addr}) != accepted.payTo ({expected_pay_to})"

        value_int = int(str(auth["value"]))
        required_amount = int(str(accepted.get("amount", "0")))
        if value_int < required_amount:
            return False, None, f"EIP-3009 authorization.value ({value_int}) < required ({required_amount})"

        valid_after = int(str(auth.get("validAfter", 0)))
        valid_before = int(str(auth.get("validBefore", 0)))
        nonce_hex = str(auth["nonce"])

        now = int(time.time())
        if valid_before <= now:
            return False, None, "EIP-3009 authorization expired (validBefore <= now)"
        if valid_after > now:
            return False, None, "EIP-3009 authorization not yet active"

        digest = compute_eip3009_digest(
            chain_id=chain_id,
            token_contract=str(accepted["asset"]),
            token_name=str(extra.get("name") or "USDC"),
            token_version=str(extra.get("version") or "2"),
            from_addr=from_addr,
            to_addr=to_addr,
            value_atomic=value_int,
            valid_after=valid_after,
            valid_before=valid_before,
            nonce_hex=nonce_hex,
        )
        recovered = ecrecover_digest(digest, sig_hex)
        if not recovered or recovered.lower() != from_addr.lower():
            return False, recovered, "EIP-3009 signature ecrecover mismatch"
        return True, recovered, "ok"
    except Exception as exc:
        return False, None, f"Malformed EIP-3009 payload: {exc}"
