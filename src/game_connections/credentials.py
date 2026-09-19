"""Encrypt credentials with an independent deployment key and bind their owner."""

import json

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.views.decorators.debug import sensitive_variables


def cipher():
    key = settings.GAME_CONNECTIONS_KEY
    if not key:
        raise ImproperlyConfigured("GAME_CONNECTIONS_KEY must be configured")
    return Fernet(key.encode())


@sensitive_variables()
def encrypt(connection, api_key):
    payload = [connection.user_id, connection.provider, connection.external_id, api_key]
    return cipher().encrypt(json.dumps(payload).encode()).decode()


@sensitive_variables()
def decrypt(connection):
    try:
        owner, provider, external_id, api_key = json.loads(
            cipher().decrypt(connection.credential.encode())
        )
        if [owner, provider, external_id] != [
            connection.user_id,
            connection.provider,
            connection.external_id,
        ]:
            raise ValueError
        return api_key
    except (InvalidToken, ValueError, TypeError):
        raise ValueError(
            "Connection credential is unavailable; reconnect the account"
        ) from None
