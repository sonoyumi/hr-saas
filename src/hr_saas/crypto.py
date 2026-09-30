"""Bot tokens are stored encrypted (Fernet: AES-128-CBC + HMAC-SHA256).

A leaked database file alone does not give control of the customers' bots: the key (MASTER_KEY)
lives only in .env on the server.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class CryptoError(Exception):
    pass


def new_key() -> str:
    return Fernet.generate_key().decode()


class Vault:
    def __init__(self, master_key: str) -> None:
        try:
            self._f = Fernet(master_key.encode())
        except (ValueError, TypeError):
            raise CryptoError("MASTER_KEY is not a valid Fernet key") from None

    def encrypt(self, secret: str) -> str:
        return self._f.encrypt(secret.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken:
            raise CryptoError("cannot decrypt: wrong MASTER_KEY or damaged value") from None
