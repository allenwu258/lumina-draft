from __future__ import annotations

import keyring

SERVICE = "lumina-draft"


def save_secret(ref: str, value: str) -> None:
    keyring.set_password(SERVICE, ref, value)


def read_secret(ref: str) -> str | None:
    return keyring.get_password(SERVICE, ref)
