"""
PII Protection utilities: deterministic HMAC-SHA256 hashing, masking, and AES-based encryption.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Optional

from cryptography.fernet import Fernet

from solution.config import FERNET_KEY, PEPPER_KEY

_fernet_instance: Optional[Fernet] = None


def get_fernet() -> Fernet:
    global _fernet_instance
    if _fernet_instance is None:
        _fernet_instance = Fernet(FERNET_KEY)
    return _fernet_instance


def hash_phone(canonical_phone: str) -> str:
    """
    Computes deterministic HMAC-SHA256 hash of canonical phone using pepper key.
    Provides fast O(1) indexed lookups without storing plaintext phone numbers.
    """
    if not canonical_phone:
        return ""
    return hmac.new(PEPPER_KEY, canonical_phone.strip().encode("utf-8"), hashlib.sha256).hexdigest()


def mask_phone(canonical_phone: str) -> str:
    """
    Masks canonical phone according to spec: '+91******' followed by the last 4 digits.
    Example: '+919876543210' -> '+91******3210'
    """
    clean = canonical_phone.strip()
    digits = clean[-4:] if len(clean) >= 4 else clean
    return f"+91******{digits}"


def encrypt_pii(plaintext: str) -> str:
    """
    Encrypts PII text using Fernet (AES-128 in CBC mode with PKCS7 padding and HMAC-SHA256 authentication).
    """
    if plaintext is None:
        return ""
    f = get_fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_pii(ciphertext: str) -> str:
    """
    Decrypts encrypted PII ciphertext back to plaintext.
    """
    if not ciphertext:
        return ""
    f = get_fernet()
    return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
