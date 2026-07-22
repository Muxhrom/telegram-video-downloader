from __future__ import annotations

import keyring


SERVICE_NAME = "TelegramVideoDownloader"
API_HASH_USER = "telegram_api_hash"
OPENLIST_PASSWORD_USER = "openlist_admin_password"


def save_api_hash(value: str) -> None:
    keyring.set_password(SERVICE_NAME, API_HASH_USER, value)


def load_api_hash() -> str | None:
    return keyring.get_password(SERVICE_NAME, API_HASH_USER)


def delete_api_hash() -> None:
    try:
        keyring.delete_password(SERVICE_NAME, API_HASH_USER)
    except keyring.errors.PasswordDeleteError:
        pass


def save_openlist_password(value: str) -> None:
    keyring.set_password(SERVICE_NAME, OPENLIST_PASSWORD_USER, value)


def load_openlist_password() -> str | None:
    return keyring.get_password(SERVICE_NAME, OPENLIST_PASSWORD_USER)
