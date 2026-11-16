"""Chiffrement des jetons d'acces et signature des liens."""

import base64
import hashlib
import hmac
import json
import time

from cryptography.fernet import Fernet, InvalidToken


def _fernet(secret_key: str) -> Fernet:
    # Derive une cle Fernet valide a partir de n'importe quelle chaine secrete.
    digest = hashlib.sha256(secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(secret_key: str, plaintext: str) -> str:
    return _fernet(secret_key).encrypt(plaintext.encode()).decode()


def decrypt(secret_key: str, ciphertext: str) -> str | None:
    try:
        return _fernet(secret_key).decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return None


STATE_PURPOSE = "connect"


def _signature(secret_key: str, payload: str) -> str:
    return hmac.new(secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def sign_payload(secret_key: str, data: dict, ttl_seconds: int, purpose: str) -> str:
    """Cree un jeton signe, a duree de vie limitee, qui transporte `data`.

    `purpose` dit a quoi sert le jeton (connexion, document...) : un jeton
    cree pour un usage est refuse pour tous les autres.
    """
    raw = json.dumps(
        {**data, "p": purpose, "exp": int(time.time()) + ttl_seconds}, separators=(",", ":")
    )
    payload = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    return f"{payload}.{_signature(secret_key, payload)}"


def verify_payload(secret_key: str, token: str, purpose: str) -> dict | None:
    """Renvoie les donnees du jeton s'il est authentique, non expire et cree
    pour cet usage."""
    payload, _, signature = token.partition(".")
    if not payload or not hmac.compare_digest(signature, _signature(secret_key, payload)):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expires = int(data.pop("exp"))
        token_purpose = data.pop("p")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if token_purpose != purpose or expires < time.time():
        return None
    return data


def sign_state(secret_key: str, member_id: int, ttl_seconds: int = 900) -> str:
    """Jeton court qui identifie le membre dans le lien de connexion."""
    return sign_payload(secret_key, {"m": member_id}, ttl_seconds, STATE_PURPOSE)


def verify_state(secret_key: str, state: str) -> int | None:
    data = verify_payload(secret_key, state, STATE_PURPOSE)
    try:
        return int(data["m"]) if data else None
    except (KeyError, TypeError, ValueError):
        return None
