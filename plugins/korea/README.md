# Korea plugin

Private Telegram location context and Korea tools backed by official Kakao HTTP
APIs, plus a zero-API local browser handoff for shopping.

## Runtime secrets

Keep real values only in `/opt/data/.env` (the deployment injects that file into
the process environment):

```dotenv
KAKAO_REST_API_KEY=
```

The plugin reads only that environment variable. It does not read another
credential file or emit secret-bearing URLs.

The key alone is not sufficient for `dapi.kakao.com`. In Kakao Developers, set
**Kakao Map > Usage settings > Status** to **ON** and complete the REST API key's
Kakao Map settings. Kakao's current setup guide says that, since 2026-07-21, only
the first Kakao Map-enabled app on a developer account receives the free quota;
another app requires Biz Wallet connection and paid API usage. A `403` from
Local/geocoding/walking/transit therefore means the app is not ready for those
APIs (or lacks permission), even when Kakao Mobility car routing works with the
same REST key. This is product activation/key configuration, not an OAuth scope.

## Telegram location privacy

At plugin registration, an idempotent guard wraps
`BasePlatformAdapter.handle_message`, before Hermes' busy-session FIFO, interrupt,
or shutdown-pending paths. It sanitizes the event and adds a coordinate-free
sentinel. The registered `pre_gateway_dispatch` hook covers the normal cold path
and no-ops on that sentinel. A native **static** Telegram pin is stored only in
process RAM under an unguessable `loc_*` token, and its coordinate-bearing
text/raw message is replaced before pending/session persistence. The fixed TTL is
10 minutes and the store is bounded to 128 locations. The token permits one
successful coarse search-context lookup, one nearby search, and then one route.
Each operation holds an exclusive in-process lease: a successful route consumes
and zeroes the token, while an API failure releases the lease so the user can
retry. Expiration and eviction actively erase the entry.

`location_search_context` is the provider-neutral bridge for Google, DDGS,
Tavily, and browser searches. It uses Kakao coordinate-to-address internally but
returns only administrative locality names and an optional localized query. It
discards coordinates, street/parcel addresses, building, postal code, and other
exact fields before producing tool output. The coarse locality can be reused for
several web searches in the task; it is an area hint, not an exact-nearest result.

`korea_reverse_geocode` still rejects `location_token`: resolving a private current
point into an exact address would make that location persistable in chat history.
Nearby results expose only coarse distance bands and relative rank, never Kakao's
exact origin-to-POI `distance_m`, which prevents straightforward triangulation
across several known POIs. Token-based place searches also force a fixed 3 km
radius and distance sort; caller-controlled radius probing cannot be used as a
binary-search oracle. The route layer never returns origin coordinates, provider
route landing URLs, path coordinates, or origin-bearing map links.

Live location is intentionally unsupported. The initial live pin is rewritten to
a coordinate-free instruction to send a static pin; Telegram edits of that live
message are skipped to avoid repeated agent runs.

This controls Hermes plugin/session storage, not Telegram's own processing of the
original location. The hook catches location extraction failures and rewrites them
without coordinates. This deployment starts Hermes through
`scripts/hermes_korea_gateway.py`, which first performs the official Hermes profile
bootstrap, verifies that CLI and gateway modules came from `/opt/hermes`, loads the
privacy module by its exact read-only path, and installs the guard before gateway
dispatch. It refuses to start an unguarded gateway. If the plugin is copied into
another deployment without that launcher, the operator must preserve the same
boundary. Registration also fails if the expected Hermes ingress class cannot be
patched.

## Routing reality

- Walking: Kakao Map `GET https://dapi.kakao.com/v2/routing/walk`.
- Public transit: Kakao Map `GET https://dapi.kakao.com/v2/routing/publictraffic`.
- Car: Kakao Mobility `GET https://apis-navi.kakaomobility.com/v1/directions`.

All three use `Authorization: KakaoAK ${KAKAO_REST_API_KEY}`. Walking and transit
are current documented Kakao Map REST endpoints, not partnership-only endpoints,
but they remain unavailable until the app's Kakao Map activation/settings above
are complete. The plugin returns a typed `403` setup/permission error rather than
claiming that a failed mode worked.

Credentialed requests never follow HTTP redirects. Idempotent GETs retry only
bounded transport failures, 408, 429, and 5xx responses; other 4xx errors fail
immediately with a typed, body-free error.

KakaoMap destination and “directions from current location” URLs use the
documented `map.kakao.com/link/...` patterns. Origin coordinates are never placed
in returned URLs. Kakao Mobility publishes an official Kakao T app-launch page,
but no public URL for pre-filling a taxi destination. The plugin returns the
launcher with `destination_prefilled: false`; it deliberately does not invent an
undocumented app scheme.

## Data limitations

Kakao Local Search does not expose current hours, `open now`, prices, review
counts, or inventory. There is no Naver API integration or Naver credential.
`korea_shopping_search` only generates a Naver Shopping web URL for the local
browser and performs no network request. Browser catalog results do not prove
physical shelf stock. Callers must label those fields as unknown or unverified
and may inspect a returned place/site URL through the local browser when current
details are needed.

Every API result reports the actual backend in `meta.provider_used`.

Official references:

- https://developers.kakao.com/docs/en/kakaomap/rest-api
- https://developers.kakao.com/docs/en/kakaomap/common
- https://developers.kakaomobility.com/guide/navi-api/directions
- https://apis.map.kakao.com/web/guide/
- https://service.kakaomobility.com/launch/kakaot/
