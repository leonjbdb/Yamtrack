# Search and credits

Search remains at `/search` with the original wording, grid/list layouts,
source controls, pagination, and tracking, list and history actions on result
cards. Filmographies and company catalogues use those same cards. People and
company records link to their catalogues; media cards retain the original hover
controls and modals.

Movies, TV shows, people and every existing media category are separate filters.
All Search explicitly combines the enabled media catalogues plus people and
companies. Its candidate pool includes the first provider page per category and
the public name index, paginated locally and cached for three minutes. Category
searches retain upstream pagination and selectable catalogue sources. No account
credentials, ratings or collection states are included in that shared cache.

Person pages use compact headers and clickable media, role, department and sort
filters. Movies are the default; links from TV credits select TV. Roles preserve
actual provider jobs: Acting, Director, Writer and Screenplay remain separate.
Titles with several roles appear once, showing the matching roles. Production
companies browse movies or TV; networks browse TV. IGDB companies distinguish
developer, publisher, porting and support relationships.

## Ranking

Provider results and indexed candidates form one deduplicated list, keyed by
source, media type and catalogue ID. There is no separate Close matches section.
Textual relevance supplies these base scores:

| Match | Base score |
| --- | ---: |
| Exact normalized title | 1000 |
| Exact alias | 980 |
| Same words in another order | 940 |
| Whole phrase within a title | 800–850 |
| All exact query words | 750–800 |
| Title prefix | 740 |
| Whole title with one/two edits | 650/600 |
| All words with bounded edits or prefix completion | 400–600 |
| Other provider matches | 200 |

Aliases receive a 20-point penalty relative to title matches. Provider order adds
at most five points, including results from corrected-word queries. Matches from
the submitted query receive another five-point preference over expansion results
of equal textual quality. Subtitle length is only a weak penalty;
it must not bury relevant feature films below short documentary titles. Case,
punctuation and accents are normalized. Damerau-Levenshtein matching supports
insertions, deletions, substitutions and adjacent transpositions: no typo for
fewer than four characters, one edit for four to seven, two for longer terms.
Word completion adds a separate penalty instead of counting plural endings and
attached sequel numbers as spelling errors. Every query word must match a
distinct title word. Apostrophes and superscript numbers normalize consistently.
Tests cover exact-first ordering, Alien/Aliens/Alien³, Batman, Terminator, Star
Wars, Harry Potter, Lord of the Rings, Jurassic Park, Baldur's Gate, Christopher
Nolan, Sigourney Weaver, accents, aliases, unrelated titles and short queries.

Public audience evidence adds up to 360 points, only when every query token
matches the title or an alias. Counts are log-scaled after dividing by 1% of
an established-audience cap; a few votes must not count as a large audience.
Signals are capped, and the strongest signal wins rather than double-counting
correlated readership and votes. Average ratings do not measure prominence.
Exact queries receive 80 extra points per specific word beyond two (maximum
240); English function words do not inflate that specificity bonus. Thus a broad
franchise query can favor established novels over obscure short exact titles,
while a precise full-title query retains its intent.

Signals: Open Library reading-log counts (cap 10,000), rating counts (1,000),
and edition counts (300, weight 0.35); Hardcover readership (100,000) and ratings
(10,000); TMDB votes (25,000) and popularity (200, weight 0.85); TMDB people
popularity (100); IGDB rating counts (2,000) and hypes (500, weight 0.75);
MyAnimeList list users (1,000,000) and rating users (500,000); MangaUpdates
rating votes (10,000); BGG rating users (100,000). These are bounded relevance
heuristics, not comparisons of audience totals across services. Missing evidence
is neutral. ComicVine, TVDB, studios and networks retain textual/provider
relevance where no audience measure is exposed; no fame is inferred from issue
counts or credits. Existing pagination/candidate limits still apply, so this is
ranking of retrieved candidates, not a global popularity index. No additional
per-title requests are made: BGG statistics join the existing page image batch.
Migration 0068 preserves normalized public prominence in the shared name index;
metadata without audience fields does not erase a previously learned signal.

Candidate selection uses trigrams and short prefixes, bounded to 2,000 records
for one type or 400 per type for All Search. This prevents the larger people
index from crowding out media candidates. Up to two corrected-word queries fetch
additional provider candidates, including sequels absent from the local index.
For category searches without a useful match, at most two token/prefix probes
seed spelling candidates. The submitted query is preserved. This is bounded catalogue search, not a complete
local mirror of every provider. The implementation follows exact/word/typo
ranking principles documented by Elasticsearch and Algolia:

- https://www.elastic.co/guide/en/elasticsearch/reference/current/query-dsl-fuzzy-query.html
- https://www.algolia.com/blog/engineering/inside-the-algolia-enginepart-4-textual-relevance

## Credits and public index

TMDB movie, TV-series and season details show key creative credits, studios,
networks and full cast/crew. Series credits aggregate seasons; season credits
keep the selected scope. Other catalogues retain their existing metadata rather
than receiving inferred credit mappings.

Migration 0066 adds `DiscoveryEntry` without changing collections. Interactive
searches and credits populate it. Celery indexes public catalogue identities
already present in the database at 03:15 in the configured timezone, with a
seven-day interval between successful background credit refreshes. Interactive
provider caches last one day. A one-hour lock prevents overlapping runs.
`app.discovery.tasks.index_catalogue_credits.delay()` seeds an existing database.
Network-name search uses names learned from TV credits because TMDB has no
network-name search endpoint. Provider failures remain errors.

Manual titles, private notes, ownership and connection keys are excluded from
shared indexes and caches. Native cards add only the requesting account's data
after public results are copied from cache; HTTP responses are private/no-store.
The Hearth CI workflow validates ranking, result actions, source/pagination
retention, roles, season scope, cache and account isolation, all search categories,
statistics, sync and status behavior, profiles and the earlier status migration.
