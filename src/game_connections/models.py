"""Private credentials and library membership never enter the shared catalogue."""

import uuid

from django.conf import settings
from django.db import models


class GameConnection(models.Model):
    class Provider(models.TextChoices):
        STEAM = "steam", "Steam"
        ITCH = "itch", "itch.io"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    provider = models.CharField(max_length=16, choices=Provider.choices)
    external_id = models.CharField(max_length=32)
    credential = models.TextField(editable=False)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_attempt = models.DateTimeField(null=True)
    last_success = models.DateTimeField(null=True)
    status = models.CharField(max_length=200, default="Ready to sync")
    generation = models.UUIDField(default=uuid.uuid4, editable=False)
    lease = models.UUIDField(null=True, editable=False)
    busy_until = models.DateTimeField(null=True, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "provider"], name="one_game_connection_per_user_service"
            ),
            models.UniqueConstraint(
                fields=["provider", "external_id"], name="one_game_account_owner"
            ),
        ]

    def __str__(self):
        return f"{self.get_provider_display()} connection {self.pk}"


class LibraryGame(models.Model):
    connection = models.ForeignKey(
        GameConnection, on_delete=models.CASCADE, related_name="library"
    )
    external_id = models.CharField(max_length=32)
    title = models.CharField(max_length=500)
    minutes = models.PositiveIntegerField(null=True)
    item = models.ForeignKey("app.Item", null=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "external_id"], name="unique_connection_game"
            )
        ]
        ordering = ["title"]
