
import os
import json
import base64
import hashlib
import hmac

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _load_keys():
    raw = os.environ.get("SP_ENCRYPTION_KEYS")
    active_id = os.environ.get("SP_ACTIVE_ENCRYPTION_KEY")

    if not raw or not active_id:
        raise RuntimeError("Encryption key configuration is missing")

    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("SP_ENCRYPTION_KEYS must be valid JSON") from exc

    if active_id not in configured:
        raise RuntimeError("Active encryption key ID was not found")

    keys = {}
    for key_id, encoded in configured.items():
        try:
            key = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise RuntimeError(
                f"Invalid base64 encryption key: {key_id}"
            ) from exc

        if len(key) != 32:
            raise RuntimeError(
                f"Encryption key {key_id} must be exactly 32 bytes"
            )

        keys[key_id] = key

    return keys, active_id


def _hmac_key():
    encoded = os.environ.get("SP_HMAC_KEY")
    if not encoded:
        raise RuntimeError("SP_HMAC_KEY is missing")

    try:
        key = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise RuntimeError("SP_HMAC_KEY must be valid base64") from exc

    if len(key) < 32:
        raise RuntimeError("HMAC key must be at least 32 bytes")

    return key


def encrypt_text(value):
    if value is None:
        return None

    if not isinstance(value, str):
        raise TypeError("encrypt_text expects a string")

    keys, active_id = _load_keys()
    nonce = os.urandom(12)

    aad = f"smart-parking:v1:{active_id}".encode()
    ciphertext = AESGCM(keys[active_id]).encrypt(
        nonce,
        value.encode("utf-8"),
        aad,
    )

    payload = base64.b64encode(nonce + ciphertext).decode("ascii")
    return f"enc:v1:{active_id}:{payload}"


def decrypt_text(value):
    if value is None:
        return None

    if not isinstance(value, str):
        raise TypeError("decrypt_text expects a string")

    if not value.startswith("enc:v1:"):
        raise ValueError("Value is not an encrypted v1 record")

    try:
        _, version, key_id, payload = value.split(":", 3)
        keys, _ = _load_keys()

        if key_id not in keys:
            raise ValueError(f"Encryption key unavailable: {key_id}")

        raw = base64.b64decode(payload, validate=True)
        nonce, ciphertext = raw[:12], raw[12:]

        if len(nonce) != 12 or not ciphertext:
            raise ValueError("Invalid encrypted payload")

        aad = f"smart-parking:{version}:{key_id}".encode()

        plaintext = AESGCM(keys[key_id]).decrypt(
            nonce,
            ciphertext,
            aad,
        )

        return plaintext.decode("utf-8")

    except Exception as exc:
        raise ValueError("Decryption or integrity verification failed") from exc


def plate_hmac(plate):
    if plate is None:
        return None

    normalized = "".join(str(plate).upper().split())

    return hmac.new(
        _hmac_key(),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def json_hmac(data):
    canonical = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    return hmac.new(
        _hmac_key(),
        canonical,
        hashlib.sha256,
    ).hexdigest()


def verify_json_hmac(data, signature):
    if not signature:
        return False

    expected = json_hmac(data)

    return hmac.compare_digest(expected, signature)

def encrypt_bytes(value):
    """Encrypt binary evidence using AES-256-GCM."""
    if value is None:
        return None
    if not isinstance(value, bytes):
        raise TypeError("encrypt_bytes expects bytes")

    keys, active_id = _load_keys()
    nonce = os.urandom(12)
    aad = f"smart-parking:evidence:v1:{active_id}".encode("utf-8")
    ciphertext = AESGCM(keys[active_id]).encrypt(nonce, value, aad)

    key_id = active_id.encode("utf-8")
    if len(key_id) > 255:
        raise ValueError("Encryption key ID is too long")

    return b"SPENC1" + bytes([len(key_id)]) + key_id + nonce + ciphertext


def decrypt_bytes(value):
    """Authenticate and decrypt an encrypted evidence image."""
    if not isinstance(value, bytes):
        raise TypeError("decrypt_bytes expects bytes")
    if not value.startswith(b"SPENC1") or len(value) < 35:
        raise ValueError("Invalid encrypted evidence format")

    try:
        key_len = value[6]
        key_start = 7
        nonce_start = key_start + key_len
        nonce_end = nonce_start + 12

        if key_len == 0 or len(value) <= nonce_end:
            raise ValueError("Invalid encrypted evidence payload")

        key_id = value[key_start:nonce_start].decode("utf-8")
        nonce = value[nonce_start:nonce_end]
        ciphertext = value[nonce_end:]

        keys, _ = _load_keys()
        if key_id not in keys:
            raise ValueError("Evidence encryption key unavailable")

        aad = f"smart-parking:evidence:v1:{key_id}".encode("utf-8")
        return AESGCM(keys[key_id]).decrypt(nonce, ciphertext, aad)

    except Exception as exc:
        raise ValueError("Evidence decryption or integrity verification failed") from exc
