# Collection statistics

The Statistics page is a lifetime collection dashboard with separate monthly
activity controls (3, 12 or 36 months). Every media type is represented regardless
of sidebar visibility. A type filter drills into games, movies, TV, seasons,
anime, books, manga, comics or board games. The CSV export contains the owning
user's selected collection, with spreadsheet formula prefixes escaped.

## Measurements

- Games: cumulative minutes, with the highest value per catalogue item across
  repeat rows. Unmatched connected-library minutes are included in the headline
  and reported separately from catalogue charts. Steam baseline imports are not
  assigned to the import day as played hours.
- Movies: one catalogue runtime for each completed tracking entry, including
  rewatches. Planned movies do not contribute viewing time.
- TV: each recorded episode watch contributes its known episode runtime. Rewatches
  and specials count; unknown runtimes do not. Season cards expose the same data,
  and are excluded from combined totals to avoid double counting.
- Anime: watched episodes multiplied by the catalogue's average episode duration.
- Books, manga, comics and board games: pages, chapters, issues and plays. These
  have no recorded session duration, so the dashboard does not invent hours.

Viewing hours are runtime-based estimates, not stopwatch measurements. Coverage
shows which watched units have known runtimes. Catalogue facts are public data
persisted in CollectionFacts; external requests run in Celery, not in page
rendering. A refresh is checked when opening statistics and can be requested
manually. Each item is refreshed at most weekly. Failed requests preserve prior
successful facts, display an error count and may retry after fifteen minutes on a later refresh.

## Views

The dashboard includes collection composition, status distribution, time by type,
monthly recorded time, monthly additions, a one-year activity calendar, genre
frequency, release decades, time-investment leaders, oldest planned entries,
a collection ledger, ratings and catalogue sources. Games add playtime bands
and supported platforms; reading/anime views add progress-percentage bands for
titles with a known length. Reading and board-game views lead with their native
units and rank their greatest progress rather than presenting empty hour totals.

Collection additions measure when entries entered Yamtrack, including imports.
Monthly consumption uses dated movie/episode watches and positive changes after
the initial game/anime/reading/play-count baseline. It is the date progress was
recorded, not recovered Steam session history. Resetting and re-adding progress
counts another increment. Missing dates stay in lifetime totals but cannot enter
a dated trend. Titles are unique within a media type and catalogue source;
alternative editions and sources are not silently merged.

Ratings and written notes are separate metrics. Notes are not assumed to be
reviews. Genre and platform charts can count one title in multiple categories;
platforms describe catalogue availability, not the device used to play.

## Validation

The focused suite covers all nine media types, tenant isolation, rewatches,
unknown runtimes, TV/season deduplication, cumulative game time, Steam import
baselines, zero ratings, CSV safety, route authorization and game status rules.
`src/app/tests/checks/collection_migration.py` migrates a disposable database from
the preceding schema, verifies current/history/filter state mappings and checks
preservation of time, notes, ratings, dates and non-game statuses.
