# Private game connections

This fork is based on Yamtrack 0.26.3. Account login remains independent of game
connections, so an installation can continue to require Authentik OIDC.

Settings → Game connections supports Steam and itch.io. Each Yamtrack user may
connect one account per service. Each service identity may belong to only one
Yamtrack user. Database constraints enforce both limits, including concurrent
requests. Disconnect before changing accounts or replacing a credential.

Steam ownership is verified through OpenID, with session-bound return state,
signed claims, direct verification against Steam and nonce replay protection.
The user then supplies that same account's personal Web API key. OpenID does
not grant library access. Steam's GetOwnedGames endpoint is checked using the
personal key before saving. Missing access is an error, never an empty library.
A successful read proves current access; the API does not expose a general
key-owner introspection endpoint, so users must supply their own key to retain
access when their library is private.

itch.io identifies the owner from the API credential itself. Its authenticated
profile/owned-keys endpoint lists purchased and claimed games without requiring
a public library. Only games are imported, not assets or books. Unclaimed
bundle entries are not promised by this endpoint. itch.io supplies no playtime.

## Credential boundary

Set GAME_CONNECTIONS_KEY to a Fernet key generated with
`cryptography.fernet.Fernet.generate_key()`. Supply it through the deployment's
secret manager, independently of the Django SECRET. There is no generated or
shared-key fallback. A missing key fails credential operations. Preserve this
key with encrypted backups; replacing it without re-encrypting the rows requires
users to reconnect.

Each saved credential is authenticated ciphertext bound to the local user,
provider and external identity. Keys never appear in rendered pages, URLs,
Celery task arguments/results or application error messages. Outbound credentials
use HTTPS headers against fixed provider endpoints; redirects are rejected.
Private responses are not sent through the shared catalogue cache. No credential
model is registered in Django admin. Database dumps contain ciphertext, while the
runtime and operators holding the encryption key can decrypt it. This protects
against accidental disclosure and database-only exposure, not a compromised
application server. Disconnect deletes the live credential; historical encrypted
backups retain their contents until retention expiry. Users can revoke keys at
the provider to invalidate any historical copy.

## Sync behavior

New connections are enabled for daily sync. The scheduler checks hourly; Sync
now is available. Per-connection leases prevent overlapping jobs, and generation
checks discard results after disconnect or pause/reconnect. Tasks carry only the
connection database ID. Inactive users are excluded. Remote failures preserve
the last successful library and show a safe error to its owner.

Steam playtime updates do not reduce manually recorded time. Ratings, notes,
and completed/dropped status are preserved. Missing store games never delete
tracked Yamtrack games. IGDB external IDs resolve catalogue entries; unmatched
games remain visible only to their owner in the connection page and are retried
at the next sync. Provider response validation and full fetch happen before
library writes; catalogue failures do not commit a partially updated library.

The old arbitrary Steam ID/shared-key form redirects to Game connections. Old
scheduled Steam tasks dispatch only the user's verified connection, if present.
No new Steam login provider is enabled for Yamtrack authentication.

## Validation

Run `DJANGO_SETTINGS_MODULE=game_connections.test_settings uv run src/manage.py
test game_connections --noinput`. The suite uses an isolated database and cache,
synthetic credentials and mocked provider responses. It covers tenant isolation,
encryption binding, uniqueness, CSRF, identity tampering, OpenID replay checks,
private/inaccessible library responses, pagination, safe failures, stale jobs,
and preservation of tracking data. It does not establish live private-library
access: acceptance requires a real owner's key and private account for each
service, entered through the connection page, followed by a successful sync.

## Provider references and limits

- Steam API key headers: https://partner.steamgames.com/doc/webapi_overview/auth
- Steam private-account key requirement: https://github.com/FuzzyGrim/Yamtrack/discussions/999
- itch.io personal keys: https://itch.io/docs/api/serverside
- itch.io profile:owned scope: https://itch.io/docs/api/oauth
- itch.io official client and pagination: https://github.com/itchio/go-itchio/blob/master/endpoints_profile.go

Epic, GOG, Xbox, PlayStation, Nintendo, EA and Ubisoft are not implemented.
A public profile feed or publisher-only API does not satisfy private library
access. Additional providers require an authenticated personal-library API and
an owner-account acceptance test; browser cookies/password scraping is not part
of this implementation.
