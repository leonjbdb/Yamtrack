import uuid
from datetime import timedelta
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from app.models import Game, Item, MediaTypes, Sources, Status

from . import openid
from .credentials import decrypt, encrypt
from .models import GameConnection, LibraryGame
from .providers import (
    ConnectionFailure,
    OwnedGame,
    api_get,
    itch_identity,
    itch_library,
    steam_library,
    steam_wishlist,
)
from .sync import sync_connection

STEAM_ID = "76561198000000000"
KEY = "a" * 32


class ConnectionFixtures:
    def setUp(self):
        cache.clear()
        wishlist_patch = patch("game_connections.sync.steam_wishlist", return_value=[])
        self.wishlist = wishlist_patch.start()
        self.addCleanup(wishlist_patch.stop)
        self.user = get_user_model().objects.create_user(
            username="owner", password="test"
        )
        self.other = get_user_model().objects.create_user(
            username="other", password="test"
        )
        self.client.force_login(self.user)

    def connection(self, **kwargs):
        values = dict(user=self.user, provider="steam", external_id=STEAM_ID)
        values.update(kwargs)
        obj = GameConnection(**values)
        obj.credential = encrypt(obj, KEY)
        obj.save()
        return obj

    def verified(self):
        session = self.client.session
        session["verified_steam"] = {
            "id": STEAM_ID,
            "expires": timezone.now().timestamp() + 600,
        }
        session.save()


class ConnectionTests(ConnectionFixtures, TestCase):
    def test_ciphertext_bound_to_owner_and_never_shown(self):
        connection = self.connection()
        self.assertNotIn(KEY, connection.credential)
        self.assertEqual(decrypt(connection), KEY)
        response = self.client.get("/settings/game-connections")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, KEY)
        self.assertNotContains(response, connection.credential)
        self.assertIn("no-store", response["Cache-Control"])
        connection.user = self.other
        with self.assertRaises(ValueError):
            decrypt(connection)

    def test_uniqueness_both_directions(self):
        self.connection()
        for changes in ({"external_id": "76561198000000001"}, {"user": self.other}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                self.connection(**changes)

    def test_other_user_cannot_view_sync_or_disconnect(self):
        connection = self.connection()
        LibraryGame.objects.create(
            connection=connection, external_id="1", title="Private title"
        )
        self.client.force_login(self.other)
        response = self.client.get("/settings/game-connections")
        self.assertNotContains(response, STEAM_ID)
        self.assertNotContains(response, "Private title")
        for action in ["sync", "toggle", "disconnect"]:
            self.assertEqual(
                self.client.post(
                    "/connections/steam/action", {"action": action}
                ).status_code,
                404,
            )
        self.assertTrue(GameConnection.objects.filter(pk=connection.pk).exists())

    def test_csrf_and_login_required(self):
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        for url in [
            "/connections/steam/connect",
            "/connections/steam/action",
            "/connections/steam/start",
        ]:
            self.assertEqual(csrf.post(url, {"api_key": KEY}).status_code, 403)
            self.assertEqual(self.client.get(url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.get("/settings/game-connections").status_code, 302)

    @patch("game_connections.views.steam_library", return_value=[])
    def test_steam_requires_verification_and_never_accepts_posted_identity(
        self, library
    ):
        self.client.post(
            "/connections/steam/connect", {"api_key": KEY, "steam_id": STEAM_ID}
        )
        self.assertFalse(GameConnection.objects.exists())
        library.assert_not_called()
        cache.clear()
        self.verified()
        self.client.post(
            "/connections/steam/connect",
            {"api_key": KEY, "steam_id": "76561198000000099"},
        )
        obj = GameConnection.objects.get()
        self.assertEqual(obj.external_id, STEAM_ID)
        self.assertEqual(decrypt(obj), KEY)
        library.assert_called_once_with(KEY, STEAM_ID)

    @patch("game_connections.views.itch_identity", return_value="123")
    @patch("game_connections.views.itch_library", return_value=[])
    def test_itch_identity_comes_from_key(self, library, identity):
        self.client.post(
            "/connections/itch/connect", {"api_key": KEY, "external_id": "999"}
        )
        self.assertEqual(GameConnection.objects.get().external_id, "123")
        library.assert_called_once_with(KEY)

    @patch(
        "game_connections.views.steam_library",
        side_effect=ConnectionFailure("Access denied."),
    )
    def test_failed_library_check_never_saves_or_echoes_key(self, _):
        self.verified()
        response = self.client.post(
            "/connections/steam/connect", {"api_key": KEY}, follow=True
        )
        self.assertFalse(GameConnection.objects.exists())
        self.assertNotContains(response, KEY)
        self.assertContains(response, "Access denied.")

    @patch("game_connections.views.sync_game_connection.delay")
    def test_job_only_contains_database_id(self, queue):
        obj = self.connection()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post("/connections/steam/action", {"action": "sync"})
        queue.assert_called_once_with(obj.pk)

    def test_disconnect_removes_secret_and_private_library(self):
        obj = self.connection()
        LibraryGame.objects.create(connection=obj, external_id="1", title="Private")
        self.client.post("/connections/steam/action", {"action": "disconnect"})
        self.assertFalse(GameConnection.objects.exists())
        self.assertFalse(LibraryGame.objects.exists())

    def test_old_import_cannot_use_arbitrary_id(self):
        response = self.client.post("/import/steam", {"user": "76561198000000099"})
        self.assertRedirects(response, "/settings/game-connections")

    @patch("game_connections.sync.resolve_game", return_value=None)
    @patch(
        "game_connections.sync.steam_library",
        return_value=[OwnedGame("10", "Private unmatched game", 120)],
    )
    def test_unmatched_private_games_retained_and_success_recorded(
        self, library, resolve
    ):
        obj = self.connection()
        sync_connection(obj.pk)
        obj.refresh_from_db()
        self.assertIsNotNone(obj.last_success)
        self.assertEqual(obj.library.get().title, "Private unmatched game")
        self.assertIn("1 unmatched", obj.status)
        library.assert_called_once_with(KEY, STEAM_ID)

    @patch("game_connections.sync.resolve_game", return_value=None)
    def test_disconnect_during_fetch_prevents_stale_writes(self, resolve):
        obj = self.connection()

        def remove(*args):
            obj.delete()
            return [OwnedGame("10", "Secret")]

        with patch("game_connections.sync.steam_library", side_effect=remove):
            sync_connection(obj.pk)
        self.assertFalse(LibraryGame.objects.exists())

    @patch("game_connections.sync.resolve_game", return_value=None)
    def test_pause_or_key_generation_change_cancels_stale_job(self, resolve):
        obj = self.connection()

        def replace(*args):
            GameConnection.objects.filter(pk=obj.pk).update(generation=uuid.uuid4())
            return [OwnedGame("10", "Secret")]

        with patch("game_connections.sync.steam_library", side_effect=replace):
            sync_connection(obj.pk)
        self.assertFalse(LibraryGame.objects.exists())

    def test_sync_error_does_not_publish_secret_exception(self):
        obj = self.connection()
        with patch(
            "game_connections.sync.steam_library", side_effect=RuntimeError(KEY)
        ):
            sync_connection(obj.pk)
        obj.refresh_from_db()
        self.assertNotIn(KEY, obj.status)
        self.assertIsNone(obj.last_success)
        self.assertIsNone(obj.lease)

    @patch("game_connections.sync.steam_library")
    def test_busy_paused_and_inactive_connections_are_not_read(self, library):
        obj = self.connection(busy_until=timezone.now() + timedelta(minutes=10))
        sync_connection(obj.pk)
        GameConnection.objects.filter(pk=obj.pk).update(busy_until=None, enabled=False)
        sync_connection(obj.pk)
        GameConnection.objects.filter(pk=obj.pk).update(enabled=True)
        self.user.is_active = False
        self.user.save()
        sync_connection(obj.pk)
        library.assert_not_called()

    @patch(
        "game_connections.sync.resolve_game",
        return_value={
            "media_id": "42",
            "title": "A game",
            "image": "https://example.com/a.jpg",
        },
    )
    @patch(
        "game_connections.sync.steam_library",
        return_value=[OwnedGame("10", "A game", 120, 30)],
    )
    def test_sync_preserves_manual_fields_and_does_not_delete_tracked_games(
        self, library, resolve
    ):
        obj = self.connection()
        sync_connection(obj.pk)
        game = Game.objects.get(user=self.user)
        for status in ("Played", Status.DROPPED):
            with self.subTest(status=status):
                Game.objects.filter(pk=game.pk).update(
                    status=status, notes="My notes", score=9, progress=200
                )
                sync_connection(obj.pk)
                game.refresh_from_db()
                self.assertEqual(
                    (game.status, game.notes, game.score, game.progress),
                    (
                        Status.IN_PROGRESS if status == "Played" else status,
                        "My notes",
                        9,
                        200,
                    ),
                )
        library.return_value = []
        sync_connection(obj.pk)
        game.refresh_from_db()
        self.assertEqual(game.status, Status.DROPPED)
        self.assertFalse(obj.library.exists())
        library.return_value = [OwnedGame("10", "A game", 240, 30)]
        sync_connection(obj.pk)
        self.client.post("/connections/steam/action", {"action": "disconnect"})
        game.refresh_from_db()
        self.assertEqual(game.status, Status.DROPPED)
        self.assertEqual(game.notes, "My notes")
        self.assertEqual(Game.objects.filter(user=self.user, item=game.item).count(), 1)


class ProviderTests(TestCase):
    @patch("game_connections.providers.api_get")
    def test_steam_inaccessible_is_not_empty(self, get):
        get.return_value = {"response": {}}
        with self.assertRaises(ConnectionFailure):
            steam_library(KEY, STEAM_ID)
        get.return_value = {"response": {"game_count": 0}}
        self.assertEqual(steam_library(KEY, STEAM_ID), [])
        self.assertEqual(get.call_args.args[1], {"x-webapi-key": KEY})
        self.assertNotIn(KEY, str(get.call_args.kwargs))

    @patch("game_connections.providers.api_get")
    def test_steam_private_owner_response_and_incomplete_response(self, get):
        get.return_value = {
            "response": {
                "game_count": 1,
                "games": [
                    {"appid": 10, "name": "Private game", "playtime_forever": 15}
                ],
            }
        }
        self.assertEqual(steam_library(KEY, STEAM_ID)[0].minutes, 15)
        get.return_value = {"response": {"game_count": 2, "games": []}}
        with self.assertRaises(ConnectionFailure):
            steam_library(KEY, STEAM_ID)

    @patch("game_connections.providers.api_get")
    def test_itch_pagination_deduplication_and_download_keys_not_retained(self, get):
        row = {
            "key": "private-download-secret",
            "game": {"id": 5, "title": "Owned", "classification": "game"},
        }
        get.side_effect = [
            {"page": 1, "per_page": 1, "owned_keys": [row]},
            {"page": 2, "per_page": 1, "owned_keys": [row]},
            {"page": 3, "per_page": 1, "owned_keys": []},
        ]
        games = itch_library(KEY)
        self.assertEqual(len(games), 1)
        self.assertNotIn("private-download-secret", str(games))
        self.assertEqual(get.call_count, 3)

    @patch("game_connections.providers.requests.Session")
    def test_provider_redirects_and_error_bodies_not_exposed(self, session):
        response = Mock(status_code=302, text=KEY)
        session.return_value.__enter__.return_value.get.return_value = response
        with self.assertRaises(ConnectionFailure) as error:
            api_get("https://api.itch.io/profile", {"Authorization": "Bearer " + KEY})
        self.assertNotIn(KEY, str(error.exception))
        self.assertFalse(
            session.return_value.__enter__.return_value.get.call_args.kwargs[
                "allow_redirects"
            ]
        )


class OpenIDTests(ConnectionFixtures, TestCase):
    def callback_params(self):
        response = self.client.post("/connections/steam/start")
        params = parse_qs(urlparse(response.url).query)
        callback = params["openid.return_to"][0]
        nonce = timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ") + "nonce"
        return {
            "state": parse_qs(urlparse(callback).query)["state"][0],
            "openid.return_to": callback,
            "openid.ns": openid.NS,
            "openid.mode": "id_res",
            "openid.op_endpoint": openid.ENDPOINT,
            "openid.claimed_id": "https://steamcommunity.com/openid/id/" + STEAM_ID,
            "openid.identity": "https://steamcommunity.com/openid/id/" + STEAM_ID,
            "openid.response_nonce": nonce,
            "openid.assoc_handle": "association",
            "openid.signed": "op_endpoint,claimed_id,identity,return_to,response_nonce,assoc_handle",
            "openid.sig": "signature",
        }

    @patch(
        "game_connections.openid.requests.post",
        return_value=Mock(status_code=200, text="ns:x\nis_valid:true\n"),
    )
    def test_verified_callback_and_replay(self, post):
        params = self.callback_params()
        self.client.get("/connections/steam/callback", params)
        self.assertEqual(self.client.session["verified_steam"]["id"], STEAM_ID)
        session = self.client.session
        session.pop("verified_steam")
        session.save()
        self.client.get("/connections/steam/callback", params)
        self.assertNotIn("verified_steam", self.client.session)
        post.assert_called_once()

    @patch("game_connections.openid.requests.post")
    def test_forged_state_provider_identity_and_unsigned_fields_rejected(self, post):
        for key, value in [
            ("state", "forged"),
            ("openid.op_endpoint", "https://evil.example"),
            ("openid.identity", "https://evil.example"),
            ("openid.signed", "identity"),
        ]:
            params = self.callback_params()
            params[key] = value
            self.client.get("/connections/steam/callback", params)
            self.assertNotIn("verified_steam", self.client.session)
        post.assert_not_called()


class OwnershipSyncTests(ConnectionFixtures, TestCase):
    @patch("game_connections.sync.resolve_game")
    @patch("game_connections.sync.steam_library")
    def test_purchase_wishlist_removal_and_dropped_preservation(self, library, resolve):
        resolve.side_effect = lambda provider, entry: {
            "media_id": entry.external_id,
            "title": "Game " + entry.external_id,
            "image": "",
        }
        library.return_value = [OwnedGame("1", "Owned game", 0)]
        self.wishlist.return_value = [
            OwnedGame("2", "Wishlist game", owned=False),
            OwnedGame("1", "Also wished", owned=False),
        ]
        connection = self.connection()
        sync_connection(connection.pk)
        self.assertEqual(
            dict(Game.objects.values_list("item__media_id", "status")),
            {"1": "Owned", "2": "Planned"},
        )
        self.assertEqual(connection.library.filter(owned=True).count(), 1)
        self.assertEqual(connection.library.filter(owned=False).count(), 1)
        wished = Game.objects.get(item__media_id="2")
        Game.objects.filter(pk=wished.pk).update(notes="Keep", score=8)
        library.return_value.append(OwnedGame("2", "Purchased", 0))
        sync_connection(connection.pk)
        wished.refresh_from_db()
        self.assertEqual(
            (wished.status, wished.notes, wished.score), ("Owned", "Keep", 8)
        )
        Game.objects.filter(pk=wished.pk).update(status="Dropped")
        library.return_value = []
        self.wishlist.return_value = []
        sync_connection(connection.pk)
        wished.refresh_from_db()
        self.assertEqual(wished.status, "Dropped")
        self.assertEqual(Game.objects.count(), 2)
        self.assertFalse(connection.library.exists())
        library.return_value = [OwnedGame("2", "Purchased", 120, 30)]
        sync_connection(connection.pk)
        wished.refresh_from_db()
        self.assertEqual((wished.status, wished.progress), ("Dropped", 120))

    @patch("game_connections.sync.resolve_game")
    @patch("game_connections.sync.steam_library")
    def test_wishlist_failure_keeps_membership_and_owned_sync_succeeds(
        self, library, resolve
    ):
        connection = self.connection()
        LibraryGame.objects.create(
            connection=connection, external_id="9", title="Old wishlist", owned=False
        )
        self.wishlist.side_effect = ConnectionFailure("Unavailable")
        library.return_value = [OwnedGame("1", "New purchase", 0)]
        resolve.return_value = {"media_id": "1", "title": "New purchase", "image": ""}
        sync_connection(connection.pk)
        connection.refresh_from_db()
        self.assertEqual(Game.objects.get().status, "Owned")
        self.assertTrue(
            connection.library.filter(external_id="9", owned=False).exists()
        )
        self.assertIn("unavailable", connection.wishlist_status)
        self.assertIsNotNone(connection.last_success)
        self.assertIsNone(connection.last_wishlist_success)

    @patch("game_connections.sync.resolve_game")
    @patch("game_connections.sync.itch_library")
    @patch("game_connections.sync.itch_identity", return_value="123")
    def test_itch_owned_promotes_planned_without_erasing_manual_activity(
        self, identity, library, resolve
    ):
        connection = self.connection(provider="itch", external_id="123")
        library.return_value = [OwnedGame("1", "An itch game")]
        resolve.return_value = {"media_id": "1", "title": "An itch game", "image": ""}
        sync_connection(connection.pk)
        game = Game.objects.get()
        self.assertEqual(game.status, "Owned")
        for status, expected in [
            ("Planned", "Owned"),
            ("Played", "Played"),
            ("In progress", "In progress"),
            ("Dropped", "Dropped"),
        ]:
            Game.objects.filter(pk=game.pk).update(status=status, progress=60)
            sync_connection(connection.pk)
            game.refresh_from_db()
            self.assertEqual((game.status, game.progress), (expected, 60))

    @patch("game_connections.providers.api_get")
    def test_wishlist_requires_explicit_valid_items_and_uses_private_header(self, get):
        get.return_value = {"response": {"items": [{"appid": 42, "priority": 0}]}}
        rows = steam_wishlist(KEY, STEAM_ID)
        self.assertEqual((rows[0].external_id, rows[0].owned), ("42", False))
        self.assertEqual(get.call_args.args[1], {"x-webapi-key": KEY})
        self.assertNotIn(KEY, str(get.call_args.args[2]))
        get.return_value = {"response": {"items": []}}
        self.assertEqual(steam_wishlist(KEY, STEAM_ID), [])
        for payload in [
            {},
            {"items": None},
            {"items": [{"appid": -1}]},
            {"items": [{"appid": 1}, {"appid": 1}]},
        ]:
            get.return_value = {"response": payload}
            with self.assertRaises(ConnectionFailure):
                steam_wishlist(KEY, STEAM_ID)
