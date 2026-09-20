# Book discovery

Hardcover book pages show every returned contributor and role, the edition's
publisher, series memberships and position. Contributor and publisher links open
book lists within Yamtrack. Author lists have role filters. Series pages have
Books, Other books, Collections and All filters, with grid/list layouts and
pagination. Every book uses the native tracking, custom-list and history controls.

Series ordering uses Hardcover's numeric positions. The Books filter shows one
non-compilation title per positive whole-number position, choosing the most-read
catalogue entry when several unmerged editions occupy that position. Fractional
positions, unnumbered companions and additional entries appear under Other books;
compilations appear under Collections. Explicit canonical aliases are excluded
from the series query. No title-string matching invents series membership.
All entries remain accessible under All. Book detail pages show up to twelve
numbered volumes and link to the full series; the current volume is highlighted.

Public catalogue records are cached separately from account tracking state.
Records are copied before adding owner-specific controls. Detail-card modal IDs
include the series ID so the same volume in multiple series does not send an
action to the wrong modal. Old book metadata caches refresh when relationship
fields are absent. Existing metadata refresh invalidation remains supported.

Open Library retains its edition-focused metadata and other-edition links; this
release does not infer series membership or silently switch its catalogue.
Missing Hardcover relationships remain absent, rather than becoming guessed links.

Live read-only catalogue checks cover Harry Potter's seven numbered novels, the
six original Dune novels, author bibliography and publisher books. Automated tests
cover numeric ordering, companion/compilation separation, long-series pagination,
account isolation, native actions, unique modal IDs, old cache refresh and errors.
