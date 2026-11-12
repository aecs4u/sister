# Proposal for gate review: show every extracted field on the ispezione ipotecaria page

Date: 2026-11-12 · Drafted with Claude Code · Detailed plan:
`docs/ispezione_template_plan.md`

## Decision requested

Approve changing the ispezione ipotecaria document page so that it shows every field of the stored structured
extraction, in two stages, and decide the three open questions at the end.

## Problem

SISTER stores structured extractions of mortgage-inspection documents (notes, formalities lists, titles) produced by
Ocular. The document page shows only part of them:

- 14 typed columns that are stored (fee, page count, interest-rate percentages, lien conditions, party office and domicile,
  and others) are never printed.
- Two whole kinds of document have no rendering at all. The formalities list (`elenco_sintetico`, schema 1.3.0) and
  the notarial title (`titolo`, schema 1.2.0) appear only inside a raw JSON dump.
- The card always says "Dati strutturati Ocular · Estrazione automatica", including when the data is SISTER's own
  text parse or a hand-made extraction.
- When a document has two extractions (one per model), only the latest is reachable.

A reader of the page therefore cannot tell what was extracted, by what, or what is missing.

## Evidence

Survey of `/data/aecs4u.it/ocular/outputs/*/final/structured.json` and the SISTER database on 2026-11-12:

| Measure | Value |
|---|---|
| Ocular final outputs, all schemas | 765 |
| Of which mortgage-inspection schema | 28 (11 on 1.0.0, 1 on 1.2, 1 on 1.2.0, 15 on 1.3.0) |
| Distinct leaf fields across those 28 | 220 |
| Inspection extractions in the database with typed rows | 12 of 25 |
| Inspection extractions stored as JSON only | 13 (6 Ocular 1.3.0, 7 SISTER native parse) |
| Ocular outputs not imported | 1 (the 24-page title of inspection T 91777, document 2295) |

The page that prompted this, `/documents/2291`, has no Ocular extraction at all: it shows SISTER's native parse under
the Ocular heading.

## Proposal

**Stage 1 — template only, no data or schema change.**

1. Label the card by the real provider, model and schema.
2. Print the 14 stored-but-hidden columns.
3. Render the remaining note fields that live only in JSON (convention, printed summary counts, original texts).
4. Add a generic fallback that prints any unhandled key as a label/value list, so a future schema version cannot
   hide a field. The raw JSON stays as the last resort.

**Stage 2 — new sections and a small backend change.**

5. Dedicated sections for the formalities list and for the title with its attachments and page-referenced evidence.
6. An extraction picker (`?extraction=<id>`) when a document has more than one.
7. Import the one missing Ocular output (title, document 2295).
8. Rendering tests on anonymised fixtures for schema 1.0.0, 1.2.0 and 1.3.0, including a check that every non-empty
   field of a fixture appears in the page.

The template is shared (`aecs4u-theme`, `catasto/ispezione_ipotecaria.html`); SISTER picks it up through a theme
bump in `uv.lock`.

## Not included

- New typed tables for the formalities list and the title (needs a migration and a model design; useful for search
  and subject links, not for display).
- Running Ocular on the seven notes downloaded on 2026-11-11, including document 2291. After stage 1 their pages
  state correctly that only the native parse exists.
- The PDF export of these documents.

## Effort and order

| Stage | Items | Estimate |
|---|---|---|
| 1 | 1–4 | about 1 day |
| 2 | 5–8 | about 2–3 days |

Stage 1 can ship alone. Stage 2 depends on it only for the fallback macro.

## Risks

| Risk | Likelihood | Handling |
|---|---|---|
| Other apps using the shared template change appearance | low | new blocks render only when their keys exist; no context variable renamed |
| Page becomes heavy (20+ evidence rows, 25 pages of text for a title) | medium | collapsed sections, page text on demand |
| Two models disagree on one document | certain for 3 documents | shown one at a time through the picker, never merged |
| Personal data in test fixtures | medium | anonymised as in `tests/fixtures/single_step/` |

## Acceptance criteria

- For each fixture, every non-empty field of the stored extraction is visible on the page without opening the raw
  JSON.
- The card names the provider and model that produced the data; SISTER's native parse is never labelled Ocular.
- A document with two extractions lets the reader switch between them.
- The existing test baseline is unchanged.

## Open questions for the gate

1. **Stage 2 now or later?** Stage 1 alone removes the misleading label and the hidden columns; the formalities list
   and title stay in the fallback list until stage 2.
2. **Who runs Ocular on the seven Richieste notes?** Without it, document 2291 gains nothing beyond the correct
   label.
3. **Typed tables for formalities lists and titles:** schedule as a follow-up, or leave as JSON?
