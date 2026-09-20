# Discovery

The Hearth fork adds `/discover` to the sidebar and global search. Movies,
television and people can be searched together; dedicated categories cover
people, production companies, networks, game companies and the existing media
catalogues. Alternative book and manga sources remain selectable.

TMDB movie, TV-series and TV-season details show key creative credits, production
companies and networks. Full cast and crew link to internal person pages. Series
credits aggregate all seasons; season pages keep the selected season's scope.
Person pages combine movie and TV work, deduplicate titles and retain distinct
jobs and characters. Filters select media, department and exact role, with date,
popularity and title sorting. Acting is distinct from directing or screenplay.

TMDB company pages browse movies or TV; networks browse TV. IGDB game pages link
to companies, whose catalogues can be filtered by developer, publisher, porting
or support. Company-role filtering applies to the company's own relationship to
each game. Other providers retain their existing title metadata; this does not
invent people or credit mappings where their catalogues do not provide them.

## Spelling and indexing

Migration 0066 adds a public `DiscoveryEntry` table without changing collections.
Names and aliases are normalized for accents, punctuation and case. Bounded
trigram candidate selection and similarity ranking produce explicit Close
matches; results from the original query remain visible. For empty TMDB results,
at most two partial-name probes can seed candidates. This is best-effort spelling
assistance, not a complete local copy of every provider catalogue.

Searches and credit pages update the index. Celery also indexes public title
identities already present in the database daily at 03:15 in the configured
Celery timezone. Successful credit refreshes are limited to once per seven days
for this background task; interactive provider caches last one day. An explicit
initial run uses `app.discovery.tasks.index_catalogue_credits.delay()`. A shared
one-hour lock prevents overlapping runs. Provider failures surface as errors.
TMDB has no network-name search endpoint, so network search explicitly uses names
learned from indexed TV credits. Provider-specific catalogue IDs remain distinct.

Only public provider metadata enters shared caches/indexes. Manual titles,
private notes, ratings, ownership and account credentials are excluded. Tracking
badges are resolved for the requesting account after public data is copied from
cache; responses are private and not stored by HTTP caches.

## Validation

`app.tests.test_discovery` covers roles and departments, season scope, company
and network filters, name matching, aliases, private badge isolation, cache
isolation, bounded probes, query escaping, all search categories, authentication
and daily indexing. The Hearth workflow also exercises existing detail/search,
statistics, game sync/status and profile tests and the collection migration
rehearsal before publishing the image.
