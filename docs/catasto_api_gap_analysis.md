# Catasto API gap analysis: SISTER and Openapi Catasto

**Assessment date:** 2026-11-03
**Scope:** SISTER's own HTTP API and client in `sister/main.py`, `sister/client.py`, and `sister/query_forms.py`, plus related authenticated JSON routes in `sister/web.py`. This is a source and contract comparison; it does not claim live runtime or provider-account verification.

## Executive summary

SISTER and Openapi Catasto overlap in cadastral searches, subject/property queries, visure, mortgage inspections, map documents, and elaborati planimetrici. They are **not API-compatible**: SISTER submits work to its local queue and automates SISTER portal forms, while Openapi Catasto exposes its own bearer-authenticated service, resource paths, request schemas, and result/document lifecycle.

The largest functional gaps are Openapi's territory/conservatoria reference endpoints, standalone monitoring, and direct Geo Catasto lookups (`/foglio` and `/particella`). SISTER has related local map views and JSON payloads, but those are tied to stored people, companies, or documents and do not provide the same general-purpose lookup contract.

If the goal is to add Openapi Catasto as a provider, implement it as a separate adapter and preserve the existing SISTER API. If the goal is only to compare user-visible workflows, several rows below are functional overlaps despite route and response differences.

## Implementation update (2026-11-03)

SISTER now has local, API-key-protected reference endpoints for territory, Conservatorie, and courts. The endpoint paths mirror the territory lookup workflow, while records and IDs remain SISTER-owned; this does not add an Openapi bearer-authenticated provider adapter.

| Local API | Database source |
|---|---|
| `GET /territorio`, `/territorio/{provincia}`, `/territorio/{provincia}/{comune}` | Province names are stored in `territory_province`; municipalities and cadastral codes are read from the shared ISTAT municipalities table. `tipo_catasto=F` or `T` returns sections recorded in the Conservatoria catalog, or `null` when no sections are stored. |
| `GET /territorio/conservatorie`, `/territorio/conservatorie/{conservatoria_or_id}` | `conservatoria_office`, `conservatoria_municipality`, and `conservatoria_section`, joined to ISTAT municipality records. The optional `comune` parameter returns that municipality's stored sections for both cadastral types. |
| `GET /courts`, `/courts/{court_id}` | `court` and `court_municipality`, joined to ISTAT municipality records. |

The Alembic revision `20261121_territory_courts` creates the province, court, Conservatoria, coverage, and section tables. It seeds the existing province code/name catalog. Court and Conservatoria records are not seeded: the database did not contain an authoritative catalog, and the old `land_registry_office` rows describe provincial territorial offices rather than Conservatorie. Their APIs return an empty `data` list until curated records are loaded. The Agenzia delle Entrate [Archivio Comuni](https://arcom.agenziaentrate.gov.it/CitizenArCom/) reports cadastral and real-estate registry competence per municipality; Justice court competence is published through [Giustizia Map](https://www.giustizia.it/giustizia/it/mg_form_view.wp?uid=G_MAP). These are candidate authoritative sources for a separately reviewed data import.

## Sources and version notes

- [Openapi Catasto documentation](https://console.openapi.com/it/apis/catasto/documentation)
- [Published Catasto OpenAPI document](https://console.openapi.com/oas/it/catasto.openapi.json)
- Local implementation: `sister/main.py`, `sister/client.py`, `sister/query_forms.py`, and `sister/web.py`

The retrieved OpenAPI document declares OpenAPI 3.0.0 and `info.version` **3.5.0**, with **29 paths and 35 operations**. The documentation page footer separately displays **Web Service Revision 2.0.0**. Record both values when pinning a contract snapshot; they are not the same version field.

The query catalog defines 22 SISTER commands: five use typed client methods and the generic route handles the generic-search forms. SISTER also exposes request polling/history and persisted multi-step workflows. Its client defaults to the local service and sends `X-API-Key`; the published Catasto spec declares HTTP bearer authentication. No direct Openapi Catasto client was found in the inspected modules. This is a material integration boundary, not just a naming difference.

## Capability comparison

| Openapi Catasto capability | SISTER implementation found | Gap and assessment |
|---|---|---|
| **Territory and offices**: `GET /territorio`, province/municipality detail, and `GET /territorio/conservatorie` with office detail (5 operations). | Local API routes now expose province/municipality and Conservatoria lookups from PostgreSQL. Separate `/courts` endpoints expose court records and municipal jurisdiction. | **Partial.** Territory/office route families and database tables exist. Court and Conservatoria catalogs still need authoritative data loads; local IDs, API-key authentication, and response semantics are SISTER-specific. |
| **General cadastral requests**: `POST /richiesta/{endpoint}/`, `GET /richiesta`, and `GET /richiesta/{id}`. | SISTER has `POST /visura`, `/visura/intestati`, `/visura/soggetto`, `/visura/persona-giuridica`, `/visura/elenco-immobili`, and `POST /visura/{search_type}` for configured form queries. It returns local request IDs/status and supports `GET /visura/{request_id}` and `/visura/history`. | **Partial / strong workflow overlap.** The query families overlap, but request names, input fields, result envelope, supported query set, and persistence are SISTER-specific. This is not a drop-in `/richiesta` implementation. |
| **Address lookup**: `POST /indirizzo` creates an address-search resource; `GET /indirizzo` searches and `GET /indirizzo/{id}` reads its details. | `POST /visura/indirizzo` submits SISTER's multi-page cadastral address form (municipality, street text, and optional civic-number range). | **Partial.** SISTER can search cadastral records by address, but does not expose the Openapi address resource/search/detail contract or its `id_indirizzo` lifecycle. Map geocoding fallbacks are separate and are not equivalent to cadastral address lookup. |
| **Cadastral visure**: `POST /visura_catastale`, collection/detail reads, and `GET /visura_catastale/{id}/documento`. The spec describes property and subject visure and PDF/XML/JSON document forms. | SISTER supports immobile, owner, person, company, and property-list queries; it also has subject-document and historical-visura commands. Requests are polled via `/visura/{request_id}`; downloaded/archived documents are handled through SISTER's document workflows. | **Partial / strong workflow overlap.** Similar user tasks exist, but SISTER does not expose the provider's unified visura resource, document route, or documented PDF/XML/JSON response contract. Verify output formats per SISTER query before treating them as equivalent. |
| **Mortgage inspections and formalities**: three request types (`/ipotecarie-ispezione_nazionale`, `/ipotecarie-elenco-note`, `/ipotecarie-dettaglio-nota`) plus list/detail/document reads (6 operations). | SISTER has `POST /visura/ispezione-ipotecaria`, generic `ispezioni` and `ispezioni-cartacee` searches, and polling through `/visura/{request_id}`. The generic forms accept note/registry fields; the dedicated model accepts a narrower search type, subject/property identifiers, note number/year, and confirmation flag. | **Partial.** Related inspection use cases exist, but the route split, required office/conservatoria and register identity, result lifecycle, and direct document download are not aligned to the provider contract. Check field-by-field parity for each inspection type. |
| **Map extract**: `POST /estratto_mappa`, list/detail, and document download (4 operations). | SISTER has generic `mappa` and `export-mappa` portal queries and local document/map views. | **Partial.** Map-related retrieval exists, but no matching `/estratto_mappa` resource lifecycle or provider document-download contract. |
| **Elaborato planimetrico**: create/list/detail and document download (4 operations). | SISTER has the `elaborato-planimetrico` query command. Stored documents may be viewed or converted to GeoJSON through document-specific routes. | **Partial / strong workflow overlap.** The search is present, but the Openapi request resource and `{id}/documento` lifecycle are not exposed. A stored planimetry document/GeoJSON endpoint is not the same API operation. |
| **Monitoring**: `POST /monitoraggio`, list/detail, and `DELETE /monitoraggio/{id}`; optional callback notifications are described. | No monitoring-resource create/list/delete API or provider callback registration was found. SISTER favorites, notes, and request history are user-interface features and do not substitute for change monitoring. | **Gap.** No equivalent service for ongoing cadastral-change monitoring, subscription state, or callback lifecycle. |
| **Geo Catasto**: `GET /foglio` and `GET /particella` return WGS84 GeoJSON; support identifier, coordinate, and address searches, plus neighboring parcels and registered building footprints for parcel searches. | SISTER has authenticated document/person/company parcel-map JSON routes and map UI backed by linked/local records. It also serves planimetry GeoJSON for stored documents. | **Gap in general lookup API; partial in map presentation.** The inspected routes do not offer arbitrary `/foglio` or `/particella` lookups by cadastral identity/coordinates/address, the provider's neighborhood radius, or its registered-building-footprint schema. Local map payloads are scoped to known records and must retain their own geometry/source provenance. |

## Cross-cutting differences

| Area | Openapi Catasto | SISTER | Effect |
|---|---|---|---|
| Authentication | Bearer token (`bearerAuth`). | Local API key (`X-API-Key`) for service routes when configured; authenticated web/session routes for user-facing JSON actions. | Existing clients cannot switch base URL and keep working. Use separate credentials and auth handling. |
| Submission and polling | Provider POST operations are documented as asynchronous in production and synchronous in sandbox; resource families have their own collection/detail routes. Geo Catasto GET operations are synchronous. | SISTER queues portal work, returns local request IDs, then polls `GET /visura/{request_id}`. Workflows have separate persisted lifecycle routes. | Similar async concept for some queries, different statuses, IDs, polling paths, and sandbox behavior. |
| Source and freshness | Openapi's service describes structured cadastral data and official-document services. | SISTER's query API targets SISTER portal forms and stores local request/results/documents. | A cached or previously archived SISTER result must not be represented as a live Openapi response. Surface source and retrieval time if combining data. |
| Billing and confirmation | Provider operations use the provider's own account, scopes, and charging rules. | The web query proxy has SISTER-side quote/credit confirmation for non-admin users; the local service API has its own key boundary. | Do not assume SISTER's UI quote is an Openapi quote. Provider cost and entitlement handling would need its own adapter path. |
| Response and error contracts | Provider-specific schemas, error codes, and document responses in the published spec. | SISTER-specific queued/cached/completed envelopes, local errors, and stored-document routes. | Normalize only at an explicit adapter boundary; do not silently coerce provider errors or document states into SISTER statuses. |

## Recommended work if provider parity is required

1. **Add a separate Openapi Catasto client/adapter.** Configure its production and sandbox base URLs and bearer token independently from `VISURA_API_URL` / `API_KEY`; keep existing `/visura/*` behavior intact.
2. **Implement the missing capability families first:** territory/conservatoria lookups, monitor lifecycle/callbacks, and `/foglio` / `/particella` GeoJSON queries. These have no direct equivalent API in the current route set.
3. **Then add resource and document lifecycles** for visure, ipotecarie, estratti mappa, and elaborati planimetrici, including list/detail/download and the production async versus sandbox sync behavior.
4. **Keep provider and local contracts explicit.** Either publish new namespaced routes or document SISTER routes as a separate abstraction; do not label SISTER portal automation as a direct Openapi proxy.
5. **Pin and review the OpenAPI contract** during implementation. Generate/validate typed request and response models from the published JSON and cover path parameters, required fields, status/error responses, callbacks, and binary/document responses.

## Evidence and limits

- Local typed routes and workflow lifecycle: `sister/main.py` around lines 880–1016 and 1102–1122.
- Local client methods and polling: `sister/client.py` around lines 152–275, 347–428, and 430–532.
- The 22 SISTER query definitions, including `indirizzo`, `mappa`, `export-mappa`, `ispezioni`, and `elaborato-planimetrico`: `sister/query_forms.py`.
- Authenticated map and planimetry JSON routes: `sister/web.py` around lines 8477–8761.
- The comparison is static. No live SISTER service, Openapi account, sandbox call, production call, or behavior test was used. It assesses route/schema coverage, not the current availability, price, legal eligibility, or data quality of either provider.
