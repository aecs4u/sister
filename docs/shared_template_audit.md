# SISTER templates that can consume aecs4u-theme

Reviewed the current SISTER template tree against the sibling `aecs4u-theme`
checkout. Matching filenames alone do not establish interchangeable behavior.
The candidates below preserve the current SISTER route contracts, permissions,
CSRF protection, favorites and document provenance. This is an implementation
review; only the users-page migration was applied in this change.

## Completed: admin user directory

[`admin/users.html`](../sister/templates/admin/users.html) now extends
`aecs4u_theme/admin/users.html` in read-only directory mode. The shared template
owns search, account rows, roles, status and empty results. SISTER retains its
shell, labels and `SISTER_ADMIN_USERS` explanation. The theme's default
API-driven management page remains available to other consumers.

The theme loader now exposes package templates under `aecs4u_theme/`, allowing
an adapter to have the same filename as its shared parent without recursion.
Unprefixed lookups retain normal application override precedence.

## Recommended candidates

| Priority | SISTER template | Shared counterpart | What can be reused | Required adaptation |
| --- | --- | --- | --- | --- |
| 1 | [`catasto/omi_valuation.html`](../sister/templates/catasto/omi_valuation.html) | [`catasto/omi_valuation.html`](../../aecs4u-theme/aecs4u_theme/templates/catasto/omi_valuation.html) | The page header, search form, warning, zone result and chart setup already match. | Extend the namespaced shared page and retain only `extra_js` with `super()` for the local URL-fragment typology selection and scrolling. This is the smallest remaining page-level migration. |
| 2 | [`error.html`](../sister/templates/error.html) | [`error/error.html`](../../aecs4u-theme/aecs4u_theme/templates/error/error.html) | Generic HTTP error layout, heading, message and home action. | Map `status_code` to `error_code`, supply translated titles/messages, allow the SISTER shell, and add extension blocks for the 500 support/request ID and `/results` link. Use the FastAPI-compatible shared error page; older illustration templates use Flask-style `url_for`. |
| 3 | [`catasto/results.html`](../sister/templates/catasto/results.html) | [`catasto/results.html`](../../aecs4u-theme/aecs4u_theme/templates/catasto/results.html) | Executed-request statistics, filtering, Tabulator listing, pagination and import control. Both pages share a substantial layout. | Merge SISTER additions into configurable shared blocks first: ignored-filter notices, no-match counts, person/company/fiscal-code and request-type filtering, current row/detail links, table behavior and accessible self-hosted assets. Preserve admin-only import and CSRF handling. The local page has 650 lines versus 399 in the shared page; replacing it directly would drop features. |
| 4 | [`catasto/elenco_immobili.html`](../sister/templates/catasto/elenco_immobili.html) | [`catasto/elenco_immobili.html`](../../aecs4u-theme/aecs4u_theme/templates/catasto/elenco_immobili.html), [`components/catasto_document.html`](../../aecs4u-theme/aecs4u_theme/templates/components/catasto_document.html) | Document breadcrumb, heading, metadata, download actions and records table shell. | Add a DAT rendering variant or table-column/row extension blocks. SISTER renders `parsed.records` with subunit, denominator, address, civic number, staircase/interior, floors, description and unit kind; the shared page expects cadastral property mappings. Retain document favorites, original DAT download and unparseable-file notice. |
| 5 | [`profile.html`](../sister/templates/profile.html) | [`account/user_profile.html`](../../aecs4u-theme/aecs4u_theme/templates/account/user_profile.html), [`components/user_card.html`](../../aecs4u-theme/aecs4u_theme/templates/components/user_card.html) | Account identity, email, name and role display. | Prefer shared account components or introduce a read-only profile mode. Preserve principal keys, the explicit SISTER admin-role check, credit balance and gift redemption with CSRF and status messages. The shared profile includes editable identity/contact/invoice fields and connected accounts that SISTER does not currently provide. |
| 6 | [`auth_login.html`](../sister/templates/auth_login.html) | [`auth/login.html`](../../aecs4u-theme/aecs4u_theme/templates/auth/login.html), [`components/auth.html`](../../aecs4u-theme/aecs4u_theme/templates/components/auth.html) | Local-login fields, password visibility, Google/Clerk button, feedback, submit/loading states and legal footer. | Consolidate the username-or-email credential mode and provider/JS hooks first. SISTER posts `username` and uses `loginForm`, `clerkSignIn`, its login scripts and `/auth/clerk/callback`; the shared page's form IDs, email fields and Clerk context differ. It already shares `auth_base.html`, so replacing its full page directly is insufficient. |

## Component-level reuse

- [`document_image.html`](../sister/templates/document_image.html) and
  [`planimetria.html`](../sister/templates/planimetria.html) repeat document
  breadcrumb, heading and download markup available in
  `components/catasto_document.html`. Keep image fit/original-size controls,
  dimensions feedback, geospatial map behavior and favorites in SISTER.
- Repeated page breadcrumbs can consume `components/breadcrumb.html`; this is
  smaller component reuse, rather than evidence that the full pages duplicate
  one another.

## Already shared or intentionally application-specific

- `settings.html` already extends `account/settings.html`.
- `legal_privacy.html` already extends `legal/privacy.html`; its extra sections
  describe actual SISTER data processing and should remain explicit.
- `_base.html` already extends the shared shell; `_landing.html` uses shared
  landing components. These are consumers, not duplicate shared pages.
- `_macros.html` already delegates several badges and cards to theme macros.
  Keep its exported compatibility names while migrating any remaining common
  presentation helpers.
- The SISTER subject archive, ownership-history view, private notes, favorites,
  purchase/access registries, wallets and gift management have domain-specific
  contracts. Similar shared component names do not establish a safe direct
  replacement. For example, shared `components/subject_documents.html` is a
  document navigation strip, not SISTER's complete categorized archive.

## Dependency and validation boundary

The users adapter requires the updated shared package, including its namespace
loader and read-only mode. Source tests must run against that checkout or a
package built from it. The consumer lock currently points at an older published
Git revision; release/pinning and deployed worker reload are separate delivery
steps and must not be reported as completed by template rendering tests.
