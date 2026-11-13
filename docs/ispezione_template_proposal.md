# Proposal for gate review: show every extracted field on the ispezione ipotecaria page

Date: 2026-11-12 · Drafted with Claude Code · Detailed plan:
`docs/ispezione_template_plan.md`

## Decision requested

Approve changing the ispezione ipotecaria document page so that it shows every field of the stored structured
extraction, in two stages, and decide the three open questions at the end.

## Gate outcome — 2026-11-12

- Stage 1 proceeds first and is implemented in `aecs4u-theme` commit `9ff1a9e`;
  SISTER's `uv.lock` now pins that revision.
- Stage 2 is the next increment after Stage 1. The seven Ocular runs are a
  separate operations task and do not block the page work; the run owner still
  needs to be assigned.
- New typed tables for formalities lists and titles are deferred until search
  or subject-linking requirements justify them.

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
4. Add a generic recursive label/value view of the full payload, so a future schema version cannot hide a field.
   It can repeat values summarized above; the raw JSON stays as the technical last resort.

**Stage 2 — new sections and a small backend change.**

5. Dedicated sections for the formalities list and for the title with its attachments and page-referenced evidence.
6. An extraction picker (`?extraction=<id>`) when a document has more than one.
7. Import the one missing Ocular output (title, document 2295).
8. Rendering tests on anonymised fixtures for schema 1.0.0, 1.2.0 and 1.3.0, including a check that every non-empty
   field of a fixture appears in the page.
9. Update `docs/data_extraction.md` and the `CLAUDE.md` extraction note.

Proposal steps 1–4 map to plan items 1–3 and 6. Proposal steps 5–9 map to plan items 4, 5, and 7–10.

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
| 2 | 5–9 | about 2–3 days |

Stage 1 can ship alone. Stage 2 depends on it only for the fallback macro.

## Risks

| Risk | Likelihood | Handling |
|---|---|---|
| Other apps using the shared template change appearance | low | new blocks render only when their keys exist; no context variable renamed |
| Page becomes heavy (20+ evidence rows, page text for the 24-page title; field/value view repeats summarized values) | medium | collapsed sections, page text on demand |
| Two extractions exist for each of three documents and may differ | known for 3 documents | shown one at a time through the picker, never merged |
| Personal data in test fixtures | medium | anonymised as in `tests/fixtures/single_step/` |

## Acceptance criteria

- For each fixture, every non-empty field of the stored extraction, including `0` and `false`, is visible in the
  field/value view without opening the raw JSON.
- The card names the provider and model that produced the data; SISTER's native parse is never labelled Ocular.
- A document with two extractions lets the reader switch between them; an extraction ID belonging to another
  document cannot be selected.
- New fixture tests pass, and the existing test baseline gains no failures.

## Follow-up assignment

The Ocular operations owner for the seven Richieste notes still needs to be named. That separate assignment does not
block Stage 2; Stage 1 also displays the fields available in the native SISTER parse, while a future Ocular run would
add extraction data without changing that provenance label.

## Raised after the gate — titles are not notes (2026-11-12)

Not covered by the gate outcome above.

A title (the notarial act, e.g. document 2295) is delivered under a cover page reading "Ispezione … per titolo
telematico" and "Nota di Trascrizione - Titolo telematico". SISTER's own parser classified it as a note.

- Done: `_parse_ispezione_ipotecaria_pdf` now returns `inspection_document_type = "titolo_telematico"` for that
  cover. Four of the 23 stored inspection PDFs are titles (inspections T 91777, T 131857 twice, T 129836).
- Open, Stage 2: on a title the page should lead with the `titolo` section and show the note header only as the
  note the title belongs to. Ocular's schema 1.2.0 output for the same file still says
  `document_kind: nota_dettaglio`.
- Decided and done (2026-11-13): titles have their own `document_type = titolo_telematico`, so they appear in the
  catalogue's "Titolo Telematico (Atto Notarile)" category. The four stored titles (documents 2073, 2074, 2084,
  2295) were updated. They keep the `isp_` file prefix, the same document page and their place among a subject's
  and a parcel's inspections (`web._MORTGAGE_DOCUMENT_TYPES`); a rescan no longer resets them to
  `ispezione_ipotecaria`.
