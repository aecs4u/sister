# Tags of persons and companies

Tags group the profiles of `/persons` and `/companies`. They are **shared** (every signed-in user sees and edits the same
ones, unlike favorites), stored in `subject_tag` (migration `20261127_subject_tags`), one row per subject, kind and tag.
Identity is the casefolded tag (`Politici` = `politici`); the spelling first written is the one shown. At most 40 characters,
20 per subject.

## In the UI

* `/persons/` and `/companies/`: **Tag** filter (`?tag=<tag>`, `-` = no tag), the search box also looks into tags, the Tag column
  shows the chips (a chip filters by that tag, the pencil edits that profile), and *Gestisci i tag dei risultati* adds or removes a
  tag on **all** the rows the current filters match (all pages; it refuses if the number of rows changed since you confirmed it).
* `/persons/<code>` and `/companies/<code>`: the same tags with an editor; each chip links to the filtered list.
* CSV export: a `Tag` column (` | `-separated).
* API: `PUT <route>/<code>/tags` `{"tags": [...]}` (replaces), `POST <route>/tags/bulk?<table query>`
  `{"action": "add"|"remove", "tag": "...", "expected": N}`, `GET <route>/tags.json`.

## Automatic tags (`scripts/tag_subjects.py`)

Rules in `sister/subject_autotag.py`. Only subjects already in the archive tables are tagged, and the script only ever **adds**
tags (a tag someone already wrote counts as present), so it is safe to run again.

| Tag | Rule | Source |
|---|---|---|
| `PVP` | the code is a person of the Portale Vendite Pubbliche, in any role, or the custodian code of a PVP sale | `pvp.pvp_persons`, `modelview.modelview_sales.custodian_fiscal_code` in the `pvp` database (`PVP_DATABASE_DSN`, default: same server and login as the SISTER DSN selected by `ENVIRONMENT`) |
| `PVP: delegato` / `custode` / `curatore` / `giudice` / `liquidatore` / `commissario` / `altro` | the role(s) (`DELEGATO`, `CUSTODE`, `CURATOR`, `GIUDICE`, `LIQUGIU`+`LIQUIDAT`+`COMMLIQUID`+`GESTLIQU`, `COMMGIU`+`COMISIO`, anything else) | same |
| `Aste: proprietario immobile` | owner of a property of a PVP sale: owner in a result of a request tagged `PVP` (the property visure that `inputs/pvp_sales` asked for) | `request_tag` + `result_owner` |
| `Politici` + `Politici: Regione` / `Libero consorzio` / `Città metropolitana` / `Comune` | the code is in `inputs/politici_siciliani.md`, tagged with the level of its section | the list; a match needs the archive name to agree with the list's (`names_agree`), the others are printed for checking |
| `Non presente in catasto` | company whose searches succeeded but found no property and no name (SISTER does not know the code); run it again after a batch that names companies | the archive |

PVP people are the professionals of a sale, not its owners: no subject was found in both the PVP roles and the owners of
auctioned properties. The other auction portals aggregated in `pvp_enriched` add no subject beyond `pvp.pvp_persons`.

```bash
python scripts/tag_subjects.py                 # dry run: what would be added
python scripts/tag_subjects.py --apply         # add it; the plan is saved in data/tag_plans/
python scripts/tag_subjects.py --only politici # one family: pvp, aste, politici, catasto
python scripts/tag_subjects.py --revert data/tag_plans/subject_tags_<stamp>.json --apply   # remove what that run added
```

`--revert` removes only rows still written by the script (`created_by = script:tag_subjects`): a tag a user re-created stays.

Run 2026-11-05: 1,985 tags added — 475 persons + 1 company in PVP, 63 owners of auctioned properties (51 persons, 12
companies), 285 politicians, 40 companies not in the cadastre. One politician match was left out because the names
disagree (`SVRRSL58S48G273G`: archive "SEVERO ROSALIA", list "Saverino Ersilia").
