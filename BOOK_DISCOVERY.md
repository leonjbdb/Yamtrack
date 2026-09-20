# Book discovery

## Free catalogues

New book searches default to Open Library. Wikidata supplies explicit series
membership and ordering. Neither integration requires an API key, subscription,
trial, billing account or payment method. The HTTP client admits only the two
public catalogue hosts. Existing Hardcover records and links remain available;
no migration rewrites tracking, ratings, notes, dates, progress or custom lists.
Hardcover remains an explicitly selectable source, never an automatic fallback.

A search result represents one selected edition of a work. Its title, image,
page count, publication date, ISBN, format and publisher come from that edition.
The work's first-publication year is not substituted for the edition date.
English is the preferred search language (`BOOK_LANGUAGE=en`); this is a
preference, not a filter excluding other languages. The edition's language is
shown on its detail page, and Other editions opens a paginated edition list.
Authors and publishers have internal book lists. Original author information can
be inherited from the work when the edition has no author list. Contributions
without a catalogue identity remain plain text rather than guessed author links.

Wikidata connections use the Open Library identifier property P648. Edition
records can explicitly link to a work through P629. Relationships use P179 with
P1545 ordinals; deprecated statements are ignored and preferred statements take
precedence. Search-index results are checked against the actual statements.
Ambiguous identities fail visibly. Titles are never used to manufacture a match.
Series pages sort numeric positions numerically, retain fractional/unnumbered
entries under Other books, and offer Books / Other books / All and grid/list
filters. No series name or title has special-case logic.

Series members map back to Open Library in page-sized batches of identifiers.
Unlinked members remain visible with an explicit Search books action. Their
existence in Wikidata is not represented as a verified Open Library match.
When a work contains an edition already tracked by the signed-in account,
search/series/author cards reuse that account's tracked edition and native
tracking, history and custom-list controls. Edition selection pages keep each
edition distinct. Account state is never included in shared public caches.

Series load separately after the main book page. A failed provider call produces
a visible error and Retry control, not an empty successful result. Public HTTP
responses are cached (15 minutes for searches, 24 hours for catalogue records).
Connections and reads are bounded, requests share a Redis rate limit of two per
second per host. Safe GETs retry temporary connection failures and HTTP 502/503/504
at most twice, with one/two-second delays and 5-second connection / 10-second read
timeouts. HTTP 429 and longer Retry-After deadlines are honored immediately;
Retry-After accepts seconds or an HTTP date. Exhausted transient failures start
a 15-second shared cooldown; unqualified rate limits use 60 seconds. Continuing
outages return HTTP 503 with the actual retry delay and private/no-store headers.
They are never mistaken for a successful empty catalogue or another provider.

A failed composite book load retains successful, fresh component responses for
the next attempt. Ordinary loads use these caches; native Refresh metadata
explicitly bypasses edition, work, author and rating caches. Failed/partial
composite records are never cached as successes. `BOOK_CATALOGUE_USER_AGENT` identifies the deployment.
Only user-requested interactive requests use Wikimedia's Action API; future bulk
or background ingestion must use dumps and the appropriate maxlag policy.

Sources:
- https://openlibrary.org/developers/api
- https://openlibrary.org/dev/docs/api/search
- https://www.wikidata.org/wiki/Wikidata:WikiProject_Books
- https://www.mediawiki.org/wiki/Help:Extension:WikibaseCirrusSearch
- https://www.mediawiki.org/wiki/Manual:Maxlag_parameter

## Existing Hardcover entries

Hardcover book pages show contributors and roles, the edition's publisher,
series memberships and position. Contributor and publisher links open book lists
within Yamtrack. Author lists have role filters. Series pages have Books, Other
books, Collections and All filters, with grid/list layouts and pagination. Every
linked book uses the native tracking, custom-list and history controls.

Series ordering uses Hardcover's numeric positions. The Books filter shows one
non-compilation title per positive whole-number position, choosing the most-read
catalogue entry when several unmerged editions occupy that position. Fractional
positions, unnumbered companions and additional entries appear under Other books;
compilations appear under Collections. Explicit canonical aliases are excluded
from the series query. No title-string matching invents series membership.
All entries remain accessible under All. Book detail pages show up to twelve
numbered volumes and link to the full series; the current volume is highlighted.

Public catalogue records are copied before adding owner-specific controls.
Detail-card modal IDs include the series ID so a volume in multiple series does
not send an action to the wrong modal. Existing Hardcover work/edition metadata
limitations remain attached to those source records rather than silently
reassigning them to a different edition in Open Library.
