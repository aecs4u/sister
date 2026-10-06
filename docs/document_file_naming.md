# SISTER downloaded document filenames

Downloads from the SISTER **Richieste** page are stored with a stable basename
built from the parsed document data. The filename does not depend on the
free-form `Oggetto` text shown by the portal.

## Naming rules

Property visure use the view, province code, sheet and parcel; a subunit is
included when present:

```text
vi_{att|sto|sin}[_ter]_{PROV}_FG{foglio}_PT{particella}[_SUB{subalterno}]
```

Examples:

```text
vi_sto_PA_FG134_PT122_SUB21.xml
vi_att_RA_FG104_PT2154_SUB68.p7m
vi_sto_ter_RA_FG002_PT246.xml
```

Subject visure use the view, the queried fiscal identifier, and the requested
cadastre scope:

```text
vs_{att|sto|sin}_{codice_fiscale-or-partita_iva}_{CE|CF|CT}
```

`CE` means both cadastres (`TipoCatasto=E`), `CF` means buildings, and `CT`
means land. For example:

```text
vs_sin_FRSLSE77B54E730C_CE.xml
```

When a canonical basename already exists, the new document gets the SISTER
practice number and year as a suffix, such as `_T353113_2024`. If no practice
number is available, the request ID is used as `_DOC_{id}`. This keeps repeated
downloads distinct.

## Source filename links

The normalized file is stored in `SISTER_OUTPUTS_DIR/documents`. A symlink with
the original SISTER download name, usually `DOC_{id}.p7m`, is stored in the
sibling `document_links` directory and points to the normalized file. Extracted
XML files use the same normalized basename as their P7M; a matching
`DOC_{id}.xml` source link points to that XML as well.

Set `SISTER_DOCUMENT_LINKS_DIR` to override the link directory. Its default is
`SISTER_OUTPUTS_DIR/document_links`. The existing filenames in the documents
folder are not rewritten; this convention applies to new Richieste downloads.
