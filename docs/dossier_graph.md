# Dossier graph: owners ↔ properties

`scripts/build_dossier_graph.py` builds the ownership graph around a set of seed persons/companies and keeps
following it until no new node appears:

* **owner → properties**: every immobile of the owner with its details (ubicazione, classamento, consistenza,
  rendita, titolarità);
* **property → owners**: every intestato of each of those immobili (identity, share), who become owners to
  expand in turn.

It runs entirely through `POST /visura/soggetto-immobili?con_intestati=1` (`sister query soggetto-immobili
--con-intestati`): no document is requested and **no CAPTCHA** is involved, but the AdE session must be logged
in (see `docs/portal_session.md`). Companies use the persona giuridica flow (`--azienda`).

```bash
../.venv/bin/python scripts/build_dossier_graph.py --seed PGGPLA52C15H199K
../.venv/bin/python scripts/build_dossier_graph.py --from-batch outputs/pdf_parseable_batch   # every id found
../.venv/bin/python scripts/build_dossier_graph.py --resume <graph.json> [--retry-errors]
```

| Option | Meaning |
|---|---|
| `--seed ID` | codice fiscale (16) or partita IVA (11); repeatable |
| `--from-batch DIR` | seed with every id found in batch result JSON |
| `--resume FILE` / `--out FILE` | continue / choose the graph file |
| `--max-depth N`, `--max-nodes N` | stop expanding beyond (0 = no limit) |
| `--max-requests N` | portal requests in this run (default 300; resumable) |
| `--retry-errors` | re-queue ids whose expansion failed |

The graph is saved after every expansion, so a session expiry (exit status **3**), Ctrl-C or the request cap just
means: log in again and `--resume`. A service reload during a request fails that owner's expansion
(`--retry-errors` redoes it).

## Output

One JSON per run in `SISTER_DOSSIER_GRAPHS_DIR` (default: a `dossier_graphs/` folder next to the documents
folder, e.g. `/data/aecs4u.it/sister/dossier_graphs`; kept outside `dossiers/` so the web index is not affected):

```json
{ "meta": {...},
  "nodes": { "<CF|PIVA>": {"type": "person|company", "nome": "...", "depth": 0},
             "F|RA|H199|103|1714||2": {"type": "property", "Ubicazione": "...", "Classamento": "...", "comune": "RAVENNA"} },
  "edges": { "<owner>-><property>": {"owner": "...", "property": "...", "titolarita": "Proprieta'", "quota": "1/2"} },
  "queue": [...], "expanded": {...} }
```

Property keys are `catasto|provincia|codice comune|foglio|particella|sezione|sub`. Without limits the graph
follows every owner, so a company that owns many properties expands a lot; use `--max-depth` / `--max-nodes`
when exploring.

## Related queries

* `sister query visura-storica` — historical (Storica Analitica) visura per immobile, without the per-owner
  Visura per Soggetto step (one CAPTCHA per property).
* `sister query soggetto-documento` — requests the Visura per Soggetto document (XML) of a persona fisica, once
  per province (`--vista analitica|sintetica`).
* Property *documents* of the graph are separate (they need CAPTCHAs): export the property list to a CSV for
  `sister query batch`.
