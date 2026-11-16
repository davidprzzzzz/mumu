"""Chiffrement des jetons d'acces et signature des liens de connexion."""

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


def _signature(secret_key: str, payload: str) -> str:
    return hmac.new(secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def sign_state(secret_key: str, member_id: int, ttl_seconds: int = 900) -> str:
    """Cree un jeton court qui identifie le membre dans le lien de connexion."""
    raw = json.dumps({"m": member_id, "exp": int(time.time()) + ttl_seconds})
    payload = base64.urlsafe_b64encode(raw.encode()).decode()
    return f"{payload}.{_signature(secret_key, payload)}"


def verify_state(secret_key: str, state: str) -> int | None:
    """Renvoie l'identifiant du membre si le jeton est authentique et valide."""
    payload, _, signature = state.partition(".")
    if not payload or not hmac.compare_digest(signature, _signature(secret_key, payload)):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        member_id, expires = int(data["m"]), int(data["exp"])
    except (ValueError, KeyError, TypeError):
        return None
    return member_id if expires >= time.time() else None
