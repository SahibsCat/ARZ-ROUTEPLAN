"""Reversible copy of each driver's password so the admin can see it on the
Drivers page. Login still checks only the salted hash on Driver
(driver_auth.verify_password); this vault is never used to authenticate.

Encryption is stdlib only: an HMAC-SHA256 keystream (counter mode) with a
random 16-byte nonce, plus an HMAC-SHA256 tag over nonce+ciphertext.
The key comes from DRIVER_PASSWORD_KEY when set, otherwise it is derived
from DATABASE_URL. If the key changes, old entries just stop decrypting
(shown as "not saved") instead of erroring."""

import base64
import hashlib
import hmac
import os
import secrets
from typing import Optional

_NONCE = 16
_TAG = 32


def _key() -> bytes:
    secret = os.getenv("DRIVER_PASSWORD_KEY") or os.getenv("DATABASE_URL") or "rootplan-local"
    return hashlib.sha256(b"rootplan-driver-vault:" + secret.encode("utf-8")).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(key, b"ks" + nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def encrypt(plaintext: str) -> str:
    key = _key()
    nonce = secrets.token_bytes(_NONCE)
    data = plaintext.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))
    tag = hmac.new(key, b"tag" + nonce + ct, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + tag + ct).decode("ascii")


def decrypt(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
    except Exception:
        return None
    if len(raw) < _NONCE + _TAG:
        return None
    key = _key()
    nonce, tag, ct = raw[:_NONCE], raw[_NONCE:_NONCE + _TAG], raw[_NONCE + _TAG:]
    if not hmac.compare_digest(tag, hmac.new(key, b"tag" + nonce + ct, hashlib.sha256).digest()):
        return None
    try:
        return bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct)))).decode("utf-8")
    except UnicodeDecodeError:
        return None
