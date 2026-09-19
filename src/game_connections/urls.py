from django.urls import path
from . import views

app_name = "game_connections"
urlpatterns = [
    path("settings/game-connections", views.index, name="index"),
    path("connections/steam/start", views.steam_start, name="steam_start"),
    path("connections/steam/callback", views.steam_callback, name="steam_callback"),
    path("connections/<str:provider>/connect", views.connect, name="connect"),
    path("connections/<str:provider>/action", views.action, name="action"),
]
