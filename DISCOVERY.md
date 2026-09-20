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
Ranking weights enforce the following precedence:

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
at most five points, including results from corrected-word queries, so it cannot
move a fuzzy result above an exact title. Subtitle length is only a weak penalty;
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
