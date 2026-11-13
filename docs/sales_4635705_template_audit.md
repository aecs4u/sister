# Design & quality audit — `/sales/4635705/*`

Date: 2026-11-12 · Service: `http://localhost:8016` (the `aecs4u-dse` app, not the SISTER service on 8025) · Signed in as Admin.
Subject: sale #4635705, "Villa Ginanni Corradini", Via Petrosa 126, Campiano (Ravenna), Tribunale di Ravenna RGE 56/2023, **Lot 4**.

## 1. Scope and method

Pages audited (13 reachable from the sale sub-navigation or the "AI Services" block, plus one that fails):

| Route | Final URL | Status |
|---|---|---|
| `/sales/4635705` (Overview) | same | 200 |
| `/appraisal` | same | 200 |
| `/notice` | same | 200 |
| `/order` | same | 200 |
| `/scorecard` | same | 200 (its `/scorecard/state` call returns 500) |
| `/templates` | same | 200 |
| `/condo-fees` | same | 200 |
| `/rent-agreement` | same | 200 |
| `/business-plan` | same | 200 |
| `/due-diligence` | same | 200 (its `/due-diligence/ic-signoffs` call returns 500) |
| `/auction` | same | 200 |
| `/cashflow` | **redirects** to `/business-plan?tab=scenarios` | 200 |
| `/investor` | **redirects** to `/business-plan?tab=investor` | 200 |
| `/dse-strategy` | same | 200 |
| `/activities` | same | **500** (HTML error page) |

Checks run on every page, with Playwright:
- 1440×900 desktop, 768×1024 tablet and 390×844 mobile (horizontal overflow, touch-target size).
- DOM and computed-style measurements: headings, landmarks, fonts, card and button styles, form labels, alt text, duplicate IDs, inline styles, stylesheets loaded.
- Cross-page extraction of money, date, area and lot figures.
- Console and network errors.
- Visual inspection of 8 viewport screenshots (Overview, Appraisal, Order, Scorecard, Condo Fees, Business Plan, Due Diligence, Auction).

**Limits**
- I did not visually inspect every page at every width. The tablet and mobile results are measurements only (overflow and target sizes), not screenshots.
- I did not test dark mode.
- I did not run a contrast tool. The one contrast item below is a visual observation.
- Interactive behaviour (saving, exporting, calculating) was not exercised, to avoid writing to or charging the account.
- `/appraisal`, `/due-diligence`, `/condo-fees` and `/rent-agreement` are long. I read their key figures, not every table.
- Whether the Lexend heading font is actually loaded was not verified.

Severity: **High** = wrong or contradictory data, or a broken feature. **Medium** = clear inconsistency or accessibility gap. **Low** = polish.

---

## 2. Executive summary

The sale sub-pages share a shell (header, sidebar, breadcrumb, sub-navigation, footer). They do not share a content design. Each page was built with its own stylesheet and conventions. The findings fall into five groups:

1. **Data consistency (High).**
   - Lot 4 is shown as 3 assets on the Overview and as 5 cadastral units (subs 62, 63, 30, 31 and particella 229) on Appraisal, Due Diligence and Condo Fees.
   - The appraisal value is shown as €217,000 (Overview), €241,451.40 / €257,374.70 (Appraisal) and other figures on Due Diligence.
   - The area of particella 229 is 3, 4 or 24 m² depending on the page.
   - The Auction page shows "0 offers / 0 bidders" and "~7 unique bidders" as facts, but the bidders are unknown until the auction opens on 17 Nov 2026 (next week).
2. **Failing requests (High).** `/activities`, `/scorecard/state` and `/due-diligence/ic-signoffs` return 500. The user sees "Unable to load activities.", a 0% checklist and a "Clearance: Approved" sign-off whose own data failed to load.
3. **Visual system drift (Medium).**
   - H1 sizes are 24, 28, 28.8 and 48 px; H1 weights are 500 and 700/800.
   - H2 sizes are 16, 20 and 32 px.
   - There are two card-shadow tokens and 15+ button class combinations.
   - Three styles of export/print buttons, three title patterns, and three date formats on one page.
4. **Navigation (Medium).** The sub-navigation omits Investor, Cashflow and DSE Strategy. `/cashflow` and `/investor` are aliases of Business Plan with different titles. `/dse-strategy` sits outside the sale shell.
5. **Accessibility and responsive (Medium).**
   - `/business-plan?tab=scenarios` has 80 of 122 visible inputs without a label.
   - Footer headings ("Legal", "Support", "Company") are `<h2>` on every page.
   - Due Diligence and Cashflow overflow horizontally on mobile.
   - Touch targets under 32 px on every page (8 to 68).

---

## 3. Data and content consistency

### 3.1 Lot 4 composition (High)

| Page | What it shows |
|---|---|
| Overview → Assets (3) | #1 Garage/autorimessa 28 m² (id 579634), #2 Magazzini/deposito 9 m² (579635), #3 Uffici/studi 131 m², 5 rooms (579633) |
| Appraisal | "Ufficio (Sub 62) + cantina-magazzino (Sub 63) + posti auto scoperti (Sub 30-31) + rimessa (Particella 229)" |
| Due Diligence | NCEU Foglio S/101 Part. 2: Sub 62 (A/10, 5 vani), Sub 63 (C/2, 9 mq), Sub 30 and Sub 31 (C/6, 12 mq each), plus NCT Part. 229 (C/6, 3 mq) |
| Condo Fees | Sub 62 131 m², Sub 63 9 m², Sub 30 12 m², Sub 31 12 m², Parcel 229 **4** m² ("formerly Sub 58") |

- The Overview has no cadastral identifiers. It merges subs 30, 31 and part. 229 into one 28 m² "garage" (12 + 12 + 4 = 28, which matches the Condo Fees area), and shows subs 62 and 63 as separate assets. Someone reading the Overview cannot tell which subalterni they would be bidding on.
- The area of particella 229 is **3 mq** (Due Diligence, Appraisal "3 m2"), **4 m²** (Condo Fees) and **24 m²** (Appraisal: "ente urbano di 24 m2" from the land extract). The extract values may be right; the pages should say which source each one comes from.
- The Appraisal page covers the whole perizia ("4 lots · 4 properties · 10 cadastral units") with the other lots crossed out. The text and the surfaces on the page (216.78, 157.0, 224.37 mq, "1.720 m2") are mostly other lots' figures. This is correct but noisy for a Lot 4 page; the Lot 4 figures should come first.
- Asset fields on the Overview are mostly empty: Rooms, Bathrooms, Energy rating, Occupied and Cadastre are blank for #1 and #2. The Appraisal and Due Diligence pages say the units are **"locati"** (let). Overview "Is Occupied" is blank.

### 3.2 Money figures (High)

| Figure | Overview | Appraisal | Notice / Order | Due Diligence | Auction / other |
|---|---|---|---|---|---|
| Base price | €46,000 | not shown | €46,000 | not shown | €46,000 (Auction) |
| Min. offer | €34,500 | not shown | €34,500 | not shown | €34,500 |
| Appraisal value | **€217,000** | **EUR 241,451.40** final, **EUR 257,374.70** market (the page also repeats "€217,000." in prose) | — | **€205,500.00 / €154,125.00 / €972,000.00** (labels not captured) | — |
| Cadastral income | — | — | — | RC Sub 62 €1,665.57 | €1,665.57 (Business Plan) |

- Four different "value" figures with no page explaining which one is the court appraisal for Lot 4.
- Discounts: Overview shows **−25.0%** (min. offer vs base price). Notice shows **75.0%** (the same ratio, other direction). Due Diligence shows 100%, −60%, 78%, 84%. The label is not defined on any page.
- Expense fund: Order shows **€3,000** and **€2,500** (top-up); the Overview's "Minimum raise" is also **€3,000**. The same number with different meanings sits close together.

### 3.3 Provenance banners (Medium — verify)

- Condo Fees, Rent, Business Plan and Due Diligence open with a cyan alert: "same-lot … data is reused from sale **#4511062**". Condo Fees also prints the source file `spese_condominiali.4511062.json`.
- The wording is inconsistent ("condo fee data", "rent data", "due-diligence data"). The Business Plan page says "rent data" although it is a business plan.
- Nothing says whether the earlier sale is the same lot re-auctioned, or why that is safe. The figures (e.g. €183,348.27 total 2019-2026, latest period €0.00) are those of the earlier sale.

### 3.4 Sale type and auction state (Medium)

- Overview: "Sale type: Sale without auction", "Sale mode: Asynchronous online auction", "Auction date 17 Nov 2026 11:20". These contradict each other.
- Order says "Senza Incanto / Asincrona Telematica" (Italian) for the same fields.
- Auction page badge: "**0 offers · 1 lots · ~7 unique bidders**", and the summary tile says "0 Bidders". "1 lots" is a plural bug and the bidder counts conflict.
- **Bidders are unknown, and the page presents them as known (High).** The auction is on 17 Nov 2026 with offers due by 16 Nov 23:59, and the identities of bidders are not disclosed before the opening. So:
  - "0 offers" and "0 Bidders" read as facts but only mean "none visible to us". Offers may already be filed. The "Offers Received 0" section and its empty table (with the "Show real bidder names" toggle) imply the opposite of what is known.
  - "~7 unique bidders" looks like an estimate (possibly from other sales of the same procedure). It is shown in the same badge as the factual counts with no label, source or confidence.
  - Fix: show "Not disclosed until the auction opens (17 Nov 2026)" instead of 0, and label any estimate as an estimate with its basis. Hide or disable the "Show real bidder names" toggle until names exist.
  - The Bidding Raises and bidder-based figures further down the page, and the Business Plan "Bidding" tab, should be checked for the same assumption.
- Auction page also references "Lotto 2377464" and "Lotto 2377441" (internal lot ids) next to "Lot 4".

### 3.5 Empty and placeholder values

- Scorecard: "Custodian/delegate" is a row of underscores. The Overview lists Paolo Castellari as delegate and custodian.
- Appraisal: Custodian "N/A", Deposit date "N/A", "Overall confidence N/A", "N/A · 2023".
- Order page prints the literal word **"None"** 4 times.
- Overview: Deposit Info block is a list of "—"; "Predictive Justice" shows field names with no values.
- Scorecard "Visit date" and Business Plan "Analysis date" are today's date (2026-11-12): they are auto-filled, not entered.
- Appraisal date shows **2024-11-12**, exactly two years before today. It should be checked against the source.

### 3.6 Internal wording shown to users

- Appraisal: "Source file / Record — Structured appraisal records, real_estates PostgreSQL database".
- Condo Fees: `spese_condominiali.4511062.json`.
- Overview: "Flat fields on Sale", "Address Detail (related)", "Lot Location (flat)", "Lot Location (related table)", "Location (flat)", "Asset Location (related)". These are database terms.
- Due Diligence: "v2.0 Enhanced - 1 March 2026", "Generated at: 1 Mar 2026, 00:00:00" — a static document header rather than the current data date.

---

## 4. Visual and typographic consistency

### 4.1 Heading scale

| Page | H1 size / weight | H2 size / weight | Notes |
|---|---|---|---|
| Overview | 28.8 / 700 | 16 / 500 and 32 / 500 | Title is a 3-line truncated description ending in "…" |
| Appraisal, Notice, Order, Templates, Rent | 28 / 500 | 16 / 500 grey | |
| Scorecard | **24** / 500 | 16 / 500 | Only page at 24 |
| Condo Fees | 28 / 500 | **20** / 500 | Body font computes to **Arial**, not Inter |
| Business Plan, Cashflow | 28 / 500 | **32** / 500 | An h3 at **28 px** (same as the H1) |
| Due Diligence | **48 / 800, white** on a dark hero | 16 / 500 | Own design system, no `.card`, 28 tables |
| Auction | 28 / 500 | 32 / 500 | |
| Investor | 28 / 500 | 20 / 500 | |
| DSE strategy | 28 / 500 | 20 / 500 | |

- The H1 weight differs between the Overview (700) and the sub-pages (500).
- H2 at 32 px (Business Plan, Auction, Rent) is larger than the page's own card titles.
- Headings compute to **Lexend**. Only Inter 300 and 400 is requested from Google Fonts, so 500/700/800 are either synthesised or come from another source. Check that this is intended.
- **Condo Fees** is the only page whose body font is `Arial, sans-serif`.

### 4.2 Heading levels

- Heading levels are skipped on Appraisal (1), Notice (1), Rent (1), Due Diligence (1), Auction (**2**) and Cashflow (1).
- The footer's "Legal", "Support" and "Company" are `<h2>` on every page and appear in the document outline as page sections. The Overview lists them after "Predictive Justice".
- Overview has no h3-level style (measured `null` for visible h3) although it uses h3s for the asset names.

### 4.3 Cards and panels

- Two card shadow tokens: `rgba(0,0,0,0.08)` (Overview, Appraisal, Notice, Order, Scorecard, Templates) and `rgba(17,24,39,0.0x)` (Condo Fees, Rent, Business Plan, Auction, Investor, DSE). The Overview mixes both.
- Due Diligence uses no `.card` at all.
- Left edge of content: 324 px (Overview, Appraisal, Order, Scorecard…), 340 px (breadcrumb and sub-navigation), 348 px (Condo Fees cards) and 364 px (Condo Fees breadcrumb). The content column shifts by up to 40 px between pages.
- Vertical rhythm: the hero starts at different heights (Overview 235 px, Condo Fees 258 px, Business Plan 260 px).
- The Overview hero uses only the left ~60% of its width; the right side is empty at 1440 px.

### 4.4 Buttons

- The Overview uses **13 distinct button class combinations**: `btn-link`, `btn-light`, `btn-outline-secondary` (three size variants), `btn-outline-primary` (two orders of `btn-sm`), `btn-outline-success`, a **non-standard `btn-secondary btn-ghost`**, and 31 buttons with no `btn-*` class.
- `btn btn-sm btn-outline-primary` and `btn btn-outline-primary btn-sm` mean the same thing; class order varies between templates.
- Three styles for Export / Print:
  - Due Diligence: grey solid Print, **red solid** Export PDF and Investor Deck, **blue solid** Export Google Docs, with *underlined* labels.
  - Auction: outlined red PDF, outlined green Excel, outlined grey Print.
  - Scorecard: outlined Print, solid primary Export PDF.
  - Appraisal and Order: no export or print buttons in the header (Notice not checked visually).
- The action colour is not consistent. "Auction underwriting workflow" on the Overview mixes outlined blue (paid actions: "Due Diligence · 20 credits"), outlined grey and outlined green ("Prepare Offer Draft") with no key.

### 4.5 Status pills, badges and tabs

- Overview pills: filled blue (procedure type), plain blue link ("Lot 4"), dark grey (type), link with icon ("pvp"), dark teal ("Published"). Order uses a different filled blue pill ("Ordinanza Di Vendita"). Due Diligence uses dark translucent pills.
- Tabs: the sale sub-navigation (boxed, icon + label), Business Plan inner tabs (underlined with a left accent bar), and Auction "Lot 4" tab (underline). Three tab styles on three pages.
- The sub-navigation scrolls horizontally on every page at 1440 px with a visible native scrollbar. "Business Plan", "Due Diligence" and "Auction" are cut off on the Overview, Appraisal and Scorecard. Condo Fees and Business Plan open with the strip scrolled to the active tab, which hides Overview and Appraisal. There is no fade or arrow affordance other than the scrollbar.

### 4.6 Label styles

- Field labels are uppercase purple small caps on Appraisal, Order and Scorecard.
- The same pages also have non-styled labels ("CREDITORS" in grey, normal case on Appraisal).
- Condo Fees uses small grey labels in normal case ("Years Tracked", "Latest Period") and uppercase dark labels ("CONDOMINIUM", "ADMINISTRATOR").
- Overview uses UPPERCASE grey labels ("BASE PRICE", "MINIMUM OFFER").

### 4.7 Stylesheet hygiene

- `design-tokens.css` is loaded **twice** (versioned and unversioned) on every page.
- `bootstrap-icons.css` and `bootstrap-icons.min.css` are both loaded on Condo Fees and Rent. Bootstrap Icons, Font Awesome (`all.min.css`) and flag-icons load on every page.
- `investor.css` and `business-plan.css` load on all three plan pages (Business Plan, Cashflow, Investor).
- `contrast-fixes.css` is loaded last on every page, which suggests the contrast defects are patched after the fact.
- Inline `style=` attributes: Overview **466**, Business Plan / Cashflow / Investor 105, Scorecard 91, Due Diligence 50, Appraisal 33. Page-specific stylesheets exist (`appraisal.css`, `notice-details.css`…) but the templates still lean on inline styles.
- One external request: Google Fonts (`css2?family=Inter`).

---

## 5. Titles, naming and navigation

### 5.1 `<title>` patterns

| Page | `<title>` |
|---|---|
| Overview | Sale #4635705 – Immobile Commerciale, Ravenna - AECS4U |
| Appraisal | **Property Appraisal - AECS4U** (no sale number) |
| Notice / Order / Templates / Business Plan / Due Diligence / Auction | `<Name> – Sale #4635705 - AECS4U` |
| Scorecard | Viewing Scorecard #4635705 - Lot 4 - AECS4U |
| Condo Fees | **Condo Fees \| Sale #4635705 \|** (trailing pipe, no brand) |
| Rent | **Lease Agreement #4635705 \|** (trailing pipe, no brand) |
| Investor | Investor KPI Calculator – Sale #4635705 - AECS4U |
| DSE strategy | DSE strategy inputs · #4635705 · AECS4U |
| 500 page | 500 — AECS4U |

At least five patterns (` – … - `, ` | … |`, ` · `, `… #n - Lot 4 -`, no id). Mixed en dash, hyphen, pipe and middle dot separators.

### 5.2 Label, title and H1 do not match

| Nav label | `<title>` | H1 |
|---|---|---|
| Scorecard | Viewing Scorecard | Judicial Auction Offer Preparation Checklist |
| Rent | Lease Agreement | Lease Agreement |
| Condo Fees | Condo Fees | Condominium Fees |
| Appraisal | Property Appraisal | Property Appraisal (Perizia di Stima) |
| Templates | Templates | Document Templates |
| Business Plan | Real Estate Business Plan | Real Estate Business Plan |
| Due Diligence | Due Diligence Report | **VILLA GINANNI CORRADINI** (property name, all caps) |

The Due Diligence H1 is the property name, not the page name.

### 5.3 Navigation structure

- Sub-navigation (13 items) has no Investor, Cashflow, DSE Strategy or Activities entries. They are reached only through the Overview's "AI Services" block, and Investor and Cashflow highlight "Business Plan" as active.
- `/cashflow` and `/investor` are aliases that redirect to `/business-plan?tab=…`. The tab content has its own H1/title (Investor shows "Investor KPI Calculator"; Cashflow shows "Real Estate Business Plan" with unlabelled inputs).
- "Back to Sale" link: present on Appraisal, Notice, Order ("Back to Sale") and DSE ("Back to sale", lower-case, with a "←"). Absent from all other pages. Breadcrumbs exist everywhere except DSE strategy.
- `/dse-strategy` has no sub-navigation and no breadcrumb. It has **two `<main>` landmarks**, 2 `<nav>` instead of 3.
- The sidebar highlights "PVP (Portale Vendite Pubbliche)" correctly on all pages, but the Sales index tab list in the DOM also exposes 40+ admin links in a menu; unrelated to this sale.
- The sub-navigation tab labelled "Rent" leads to "Lease Agreement"; "Condo Fees" to "Condominium Fees".

### 5.4 Language

- `<html lang="en-GB">` on every page, with large blocks of Italian content (court descriptions, perizia text, Order status values). None are marked with `lang="it"`.
- Mixed UI language inside one page: "Senza Incanto / Asincrona Telematica" and "Ordinanza Di Vendita" (Italian, title-cased) next to English labels; Overview shows the same fields in English.
- Headings mix both: "Property Appraisal (Perizia di Stima)", "Lot 4" vs "Lotto 4", "Rent" vs "Locato".

---

## 6. Formatting consistency (dates, money, numbers)

### 6.1 Dates

Formats seen on the sale pages:

- `24 Sept 2026` (Overview: "Sept" is not the usual abbreviation)
- `16 Nov 2026, 23:59:00` and `17 Nov 2026 11:20` (comma / no comma, seconds / no seconds, in one block)
- `17 Nov 2026`, `18 Dec 2024` (Order)
- `2026-11-12` ISO (Scorecard, Business Plan, Appraisal date)
- `1 Mar 2026, 00:00:00` (Due Diligence header; midnight with seconds)
- `01/05/2026 - 30/04/2027` dd/mm/yyyy (Condo Fees) next to `1 May 2022 to 30 Apr 2023` and `1° Settembre 2025` (Italian)
- Due Diligence page counts: 4 ISO, 14 dd/mm/yyyy and 16 "d Mon yyyy" dates on one page; Condo Fees has 1 ISO, 26 dd/mm and 24 "d Mon yyyy".

### 6.2 Money

- `€46,000` (no decimals), `€1,665.57`, `€3,000.` (full stop is sentence punctuation), `EUR 241,451.40` (prefix code; 49 occurrences on Appraisal), `€13,79` (Italian decimal comma on Appraisal), and amounts with the symbol after the number (`1.234 €`-style) on Condo Fees (221), Due Diligence (22) and Auction (11).
- Counts: Appraisal has 75 `€`-prefix amounts, 49 `EUR` amounts and 88 en-format decimals. Condo Fees has 374 prefix and 221 suffix amounts.
- No page uses the Italian locale (`€ 46.000,00`) although the content is Italian cadastral data. The user-selected language "English (UK)" is applied inconsistently.

### 6.3 Areas and units

`m²`, `mq`, `m2` and `sq m` appear on the same pages (e.g. Appraisal: `3 m2`, `216.78 mq`, `1.720 m2` — note the Italian thousands point on one and the English decimal point on another).

---

## 7. Functional and error handling

| Where | Problem | Effect |
|---|---|---|
| `GET /sales/4635705/activities` | 500, HTML error page ("Something went wrong") with the full app shell | Overview shows "Unable to load activities." and logs a console error on every load |
| `GET /sales/4635705/scorecard/state` | 500 | Scorecard shows "Checklist Completion 0% · 0 / 56 items": a saved state failing to load looks the same as no progress |
| `GET /sales/4635705/due-diligence/ic-signoffs` | 500 | Report shows "RISK: LOW" and "Clearance: Approved" while the sign-off call failed |
| `/cashflow`, `/investor` | Silent redirect to a query-string tab | Browser back button and bookmarks land on a different title than the one clicked |
| Overview | 436 `<img>` elements (213 media items) loaded on one page | Heavy page (6,839 px tall); drag-to-reorder thumbnails exposed to every viewer |

No broken images, no duplicate IDs and no `undefined`/`NaN` text were found on any page. The literal "None" appears on Order (4 times).

---

## 8. Accessibility

| Item | Finding |
|---|---|
| Form labels | `/business-plan?tab=scenarios` (Cashflow): **80 of 122** visible inputs have no label, `aria-label` or title. All other pages: 0 |
| Landmarks | `/dse-strategy` has 2 `<main>`; the Overview has 4 `<nav>` and the others 3 |
| Heading order | Skipped levels on 6 pages (see 4.2); footer headings are `<h2>` |
| Headings vs size | Business Plan h3 is 28 px (white on a dark block), the same size as the H1 |
| Placeholder links | Overview has 2 `href="#"` anchors |
| Colour contrast | Not measured. In screenshots, the muted grey labels and the grey "Hearing / Procedure" subtitle lines are light; the Due Diligence "Clearance: Approved" green on navy and the cyan alert inside the dark hero are worth a check. The existence of `contrast-fixes.css` suggests known defects |
| Link styling | Breadcrumb links underlined on every page; Due Diligence buttons have underlined labels; button-looking links elsewhere do not |
| `<abbr>` | PVP, ROI use dotted underlines (fine) but are not consistent: "RGE", "RC", "NCEU" are not expanded |
| Language | No `lang` attributes on Italian blocks (see 5.4) |
| Alt text | All 436 images on the Overview have an `alt` attribute (not checked for quality) |
| Icon-only buttons | No unnamed buttons or links found (checked: visible `button` and `a` without text, label or title) |

---

## 9. Responsive behaviour

| Page | 768 px overflow | 390 px overflow | Touch targets < 32 px at 390 px |
|---|---|---|---|
| Overview | no | no | 13 |
| Appraisal | no | no | 10 |
| Notice | no | no | 18 |
| Order | no | no | 11 |
| Scorecard | no | no | 17 |
| Templates | no | no | 11 |
| Condo Fees | no | no | 10 |
| Rent | no | no | 10 |
| Business Plan | no | no | 12 |
| Due Diligence | no | **yes (386 px)** | 12 |
| Auction | no | no | 14 |
| Cashflow | no | **yes (690 px)** | **68** |
| Investor | no | no | 10 |
| DSE strategy | no | no | 9 |

- Due Diligence is 24,204 px tall on desktop with 28 tables. It is the only page with horizontal overflow on a phone besides Cashflow.
- Cashflow scenario inputs push the layout to 690 px on a 390 px screen.
- Every page has small targets from the sub-navigation, header icons and footer. The Cashflow inputs and buttons add 58 more.
- The 768 px layout never overflows, but the 13-tab sub-navigation relies on a scrollbar there too.

---

## 10. Page-by-page notes

**Overview** — The title is a 3-line truncated sentence; the city is a separate blue line. Very long page with a "Jump to" bar (Overview, Images, Assets 3, Attachments 73, Contacts 3, All Details). Duplicated information: price block appears in the header, in the Pricing section and in the right column; the address appears about 6 times. Collapsible sections use database-style names. Some blocks exist only as headings ("Predictive Justice", "Deposit Info").

**Appraisal** — Four summary cards then long structured tables. Mixes Lot 4 with the other three lots. Mono-space type for codice fiscale and "Structured appraisal records". Mixed currency formats. Missing "Back" consistency (it has one, others do not).

**Notice** — Short and clean (2,587 px). Money in `€`; the only page that gives the discount as 75.0%. 18 small touch targets on mobile (highest of the short pages).

**Order** — Clear two-card layout. Uses Italian values for status and sale type, prints "None" four times, and explains that the filed order and the PVP listing "are shown separately".

**Scorecard** — Title and H1 differ; 24 px H1; 91 inline styles. Underscore placeholder for the custodian. The status legend wraps in the middle of "To Do". The 0% progress is probably a failed state load (see 7).

**Templates** — Minimal, one card; the least content of any page (1,917 px) and no money or dates. Fine as a hub.

**Condo Fees** — Different body font (Arial), wider left inset, and a card-heavy layout. Most dense money formatting on the sale. Three date styles. Uses `bootstrap-icons.min.css` twice.

**Rent** — Short; no table; the page's own header repeats the provenance banner. Titles with a trailing pipe.

**Business Plan** — Inner tabs with a different visual language; empty space in the Introduction card; address repeated three times (below the sub-navigation, under the H1, in the Property panel). Cashflow and Investor reuse the same template and stylesheets.

**Due Diligence** — A self-contained report design: dark gradient hero, white 48 px H1 in all caps, translucent chips, red/blue/grey export buttons, a nested light alert on a dark hero. 24k px, 28 tables. A language selector appears only here. It reads as a different product from its neighbours.

**Auction** — Standard card layout but with 32 px H2s. Contradictory counts ("0 offers · 1 lots · ~7 unique bidders"). Internal lot ids appear.

**Investor / Cashflow** — Same shell as Business Plan. The Cashflow tab is the weakest page for accessibility and mobile.

**DSE strategy** — Standalone page outside the sale shell: no sub-navigation, no breadcrumb, a "← Sale #…" back link, two `<main>` landmarks, the `·` title separator.

**Activities (500)** — Error page uses the full shell and a generic "Something went wrong" message. There is no hint of the failing sale or a retry link.

---

## 11. Recommendations (in priority order)

0. **Before the 17 Nov 2026 auction (time-sensitive).** Replace "0 offers / 0 bidders / ~7 unique bidders" on the Auction page with an "unknown until opening" state and label any estimate. Fix the three 500s so the Scorecard and Due Diligence do not show 0% or "Approved" from missing data. Reconcile the Lot 4 composition (next item) and the appraisal value.
1. **Reconcile Lot 4 data.** Show the five cadastral units (subs 62, 63, 30, 31, particella 229) on the Overview assets block with foglio/particella/sub, category, rendita and area. One source of truth for each area (particella 229: 3, 4 or 24 m²).
2. **Label every value.** Say which figure is the court appraisal, which is the platform estimate and which the market value. Define "discount".
3. **Fix the three 500s** (`/activities`, `/scorecard/state`, `/due-diligence/ic-signoffs`). Until fixed, show an explicit "could not load" state instead of 0% or "Approved".
4. **Explain reused data.** Replace the four "reused from sale #4511062" alerts with one shared component and a sentence on why the earlier sale applies.
5. **One page template.** Shared page header (H1 24–28 px/600, same left edge, same top spacing), shared export/print toolbar, one card token, one button vocabulary. Bring Due Diligence and Condo Fees into the system, or label them as report layouts.
6. **Normalise titles.** `<Page> – Sale #<id> – AECS4U` everywhere; align nav label, title and H1.
7. **Complete navigation.** Add Investor, Cashflow and DSE Strategy to the sub-navigation or fold them into Business Plan tabs; give DSE the shell and breadcrumb; make the sub-navigation show scroll affordances.
8. **Locale-aware formatting.** One helper for dates (`17 Nov 2026, 11:20`), money (`€ 46,000` or Italian), area (`m²`) and enums (translate Order values).
9. **Accessibility pass.** Label the 80 Cashflow inputs, fix the duplicate `<main>`, demote footer headings, add `lang="it"` to Italian blocks, run a contrast tool and check `contrast-fixes.css` against the tokens.
10. **Mobile.** Fix Due Diligence and Cashflow overflow; raise touch targets to 44 px on the sub-navigation and header.
11. **Stylesheet cleanup.** Load `design-tokens.css` once, one icon font per page, move inline styles (466 on the Overview) into the page stylesheets, remove `btn-ghost`.
12. **Hide internals.** Remove "real_estates PostgreSQL database", "flat/related" labels and raw filenames from user-facing pages.

---

## 12. Reproduction notes

- Measurements were taken with Playwright against `http://localhost:8016/sales/4635705/<page>` while signed in.
- Screenshots used for the visual review: `.playwright-mcp/audit/{main,appr,order,score,condo,bp,dd,auction}.png` (git-ignored).
- Per-page measurements (counts of headings, buttons, inline styles, date and money formats) are summarised in the tables above; they can be re-run with the same checks after fixes.
