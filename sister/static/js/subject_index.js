/**
 * /persons and /companies: sortable, filterable archive table (Tabulator).
 *
 * The server does the work: <route>/index.json returns one page (search, column filters, sort and paging are SQL) and
 * <route>/index.csv the whole filtered list. Columns are sized from the archive-wide widest/max values the page carries
 * (data-overview), so no rows are needed up front. Sort, filters and search live in the URL
 * (?q=…&sort=…&dir=…&f_<field>=…); the columns shown are remembered per browser; under 768 px the table is replaced by
 * cards. No inline handlers (CSP): the page only sets data-* attributes.
 */
(function () {
    "use strict";
    var root = document.getElementById("subjects-table");
    if (!root || typeof Tabulator === "undefined") return;

    var KIND = root.getAttribute("data-kind");
    var ROUTE = root.getAttribute("data-route");
    var JSON_URL = root.getAttribute("data-json-url");
    var CSV_URL = root.getAttribute("data-csv-url");
    var OVERVIEW = JSON.parse(root.getAttribute("data-overview") || "{}");
    var TOTAL = OVERVIEW.total || 0;
    var INITIAL_QUICK = root.getAttribute("data-initial-quick") || "";
    var TAGS_EDITABLE = root.getAttribute("data-tags-editable") === "true";
    var IS_PERSON = KIND === "person";
    var NOUN = IS_PERSON ? ["persona", "persone"] : ["società", "società"];
    var CODE_LABEL = IS_PERSON ? "Codice fiscale" : "Partita IVA";
    var DEFAULT_SORT = {field: "last_seen", dir: "desc"};
    var phone = window.matchMedia("(max-width: 767.98px)");

    var statusEl = document.getElementById("subjects-status");
    var countEl = document.getElementById("subjects-count");
    var quick = document.getElementById("subjects-quick");
    var nameStatus = document.getElementById("subjects-name-status");
    var tagSelect = document.getElementById("subjects-tag");
    var tagInput = document.getElementById("subjects-tag-input");
    var tagList = document.getElementById("subjects-tag-list");
    var cardsEl = document.getElementById("subjects-cards");
    var chooserBody = document.getElementById("subjects-columns-body");

    function setStatus(text, isError) {
        if (!statusEl) return;
        statusEl.textContent = text || "";
        statusEl.classList.toggle("text-danger", !!isError);
        statusEl.hidden = !text;
    }
    function store(key, value) { try { window.localStorage.setItem(key, value); } catch (e) { /* private mode */ } }
    function recall(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }

    // ---------------------------------------------------------------- formatting
    var intFmt = new Intl.NumberFormat("it-IT", {maximumFractionDigits: 0, useGrouping: "always"});
    var eurFmt = new Intl.NumberFormat("it-IT", {style: "currency", currency: "EUR", maximumFractionDigits: 0, useGrouping: "always"});
    function numText(v) { return v === null || v === undefined ? "–" : intFmt.format(v); }
    function eurText(v) { return v === null || v === undefined ? "–" : eurFmt.format(v); }
    function dateLabel(iso) { return iso ? iso.slice(0, 10).split("-").reverse().join("/") : ""; }
    function displayName(d) {
        if (IS_PERSON) {
            var surname = String(d.last_name || "").trim().toLocaleUpperCase("it-IT");
            var given = String(d.first_name || "").trim().toLocaleLowerCase("it-IT")
                .replace(/(^|[\s'-])([^\s'-])/gu, function (_match, separator, initial) {
                    return separator + initial.toLocaleUpperCase("it-IT");
                });
            var parts = [surname, given].filter(Boolean);
            if (parts.length) return parts.join(" ");
        }
        return d.name || "";
    }

    // The value columns say how far to trust the figure: properties without a computable value, and properties
    // counted in full because no quota is recorded (pro quota only).
    function valueNote(d, field) {
        var parts = [];
        if (d.unvalued) parts.push("+" + d.unvalued + " n.v.");
        if (field === "cadastral_value_share" && d.unknown_share) parts.push("*");
        return parts.join(" ");
    }
    function valueTitle(d, field) {
        var lines = [];
        if (d.searched_at) lines.push("Dati della ricerca del " + dateLabel(d.searched_at));
        if (d.unvalued) lines.push(d.unvalued + " immobili senza valore calcolabile (categorie E/F, rendita mancante): il totale è per difetto");
        if (field === "cadastral_value_share" && d.unknown_share) lines.push("* " + d.unknown_share + " immobili senza quota registrata, contati per intero");
        return lines.join("\n");
    }
    function valueCell(field) {
        return function (cell) {
            var d = cell.getRow().getData();
            var wrap = document.createElement("span");
            wrap.appendChild(document.createTextNode(eurText(d[field])));
            var note = d[field] === null || d[field] === undefined ? "" : valueNote(d, field);
            if (note) {
                var small = document.createElement("small");
                small.className = "text-muted ms-1";
                small.textContent = note;
                wrap.appendChild(small);
            }
            var title = valueTitle(d, field);
            if (title) wrap.title = title;
            return wrap;
        };
    }
    function valueDisplay(field) {
        return function (v, d) { var n = v === null || v === undefined ? "" : valueNote(d, field); return eurText(v) + (n ? " " + n : ""); };
    }

    function profileUrl(d) { return ROUTE + "/" + encodeURIComponent(d.fiscal_code); }

    // ---------------------------------------------------------------- columns
    // compact: part of the default set; everything else is added from the "Colonne" chooser.
    var defs = [];
    function textCol(title, field, compact, extra) {
        var def = {title: title, field: field, headerFilter: "input", headerFilterPlaceholder: "filtra…", download: true,
                   sorterParams: {alignEmptyValues: "bottom"}};
        defs.push({field: field, title: title, compact: compact, def: Object.assign(def, extra || {})});
    }
    function numCol(title, field, compact, extra) {
        var def = {title: title, field: field, hozAlign: "right", sorter: "number", formatter: function (c) { return numText(c.getValue()); },
                   headerFilter: "input", headerFilterPlaceholder: "es. >10", download: true,
                   sorterParams: {alignEmptyValues: "bottom"}, headerSortStartingDir: "desc"};
        defs.push({field: field, title: title, compact: compact, def: Object.assign(def, extra || {})});
    }

    defs.push({field: "favorite_label", title: "Preferito", compact: true, fixed: true, def: {
        field: "favorite_label", hozAlign: "center", frozen: true, download: true,
        titleFormatter: function () { return '<span aria-hidden="true">★</span><span class="visually-hidden">Preferito</span>'; },
        headerFilter: "list", headerFilterParams: {values: {"": "Tutti", "si": "Preferiti", "no": "Non preferiti"}},
        formatter: function (cell) {
            var d = cell.getRow().getData();
            var button = document.createElement("button");
            button.type = "button";
            button.className = "btn btn-link p-0 subject-fav";
            button.setAttribute("data-code", d.fiscal_code);
            button.setAttribute("aria-pressed", d.favorite ? "true" : "false");
            button.setAttribute("aria-label", (d.favorite ? "Rimuovi dai preferiti: " : "Aggiungi ai preferiti: ") + (displayName(d) || d.fiscal_code));
            button.innerHTML = d.favorite ? '<i class="fas fa-star text-warning" aria-hidden="true"></i>' : '<i class="far fa-star text-muted" aria-hidden="true"></i>';
            return button;
        }}});
    textCol("Nome", "name", true, {frozen: true, tooltip: function (e, cell) { return displayName(cell.getRow().getData()); },
        formatter: function (cell) {
            var d = cell.getRow().getData();
            var wrap = document.createElement("span");
            var a = document.createElement("a");
            a.href = profileUrl(d);
            a.className = "fw-semibold";
            var name = displayName(d);
            a.textContent = name || "(nome non disponibile)";
            if (!name) a.classList.add("text-muted", "fst-italic", "fw-normal");
            wrap.appendChild(a);
            if (d.name_inferred) {
                // no name of its own: the first registry record of the code. Several records = several entities sharing it.
                var note = document.createElement("small");
                note.className = "text-muted ms-2";
                if (d.name_records > 1) note.textContent = "+" + (d.name_records - 1);
                note.title = d.name_records > 1
                    ? "Il codice ha " + d.name_records + " denominazioni distinte nel registro di SISTER: questa è la prima"
                    : "Denominazione dal registro di SISTER";
                wrap.appendChild(note);
            }
            if (!d.name) {
                var req = document.createElement("a");
                req.href = "/forms";
                req.className = "ms-2 small";
                req.title = "Il nome non è in archivio: richiedi la visura per soggetto di " + d.fiscal_code;
                req.setAttribute("aria-label", "Richiedi la visura per soggetto di " + d.fiscal_code);
                req.innerHTML = '<i class="fas fa-file-circle-plus" aria-hidden="true"></i>';
                wrap.appendChild(req);
            }
            return wrap;
        }});
    textCol(CODE_LABEL, "fiscal_code", true, {cssClass: "font-monospace small"});
    if (IS_PERSON) {
        textCol("Sesso", "gender", false, {hozAlign: "center", headerFilter: "list", headerFilterParams: {values: {"": "Tutti", "M": "M", "F": "F"}}});
        textCol("Nascita", "birth_date", false, {headerFilterPlaceholder: "anno…",
                formatter: function (cell) { return cell.getRow().getData().birth_label || "–"; }});
        numCol("Età", "age", false);
        textCol("Luogo di nascita", "birth_place", true, {
            formatter: function (cell) {
                var d = cell.getRow().getData();
                return (d.birth_place || "–") + (d.birth_province ? " (" + d.birth_province + ")" : "");
            },
            });
    }
    textCol("Province immobili", "provinces", true, {tooltip: true, headerFilterPlaceholder: "es. MI"});
    numCol("N. prov.", "province_count", false);
    numCol("Immobili", "properties", true, {headerTooltip: "Immobili elencati dall'ultima ricerca per soggetto"});
    numCol("Fabbricati", "buildings", true, {headerTooltip: "Fabbricati secondo l'elenco province di SISTER"});
    numCol("Terreni", "land", true, {headerTooltip: "Terreni secondo l'elenco province di SISTER"});
    numCol("Piena propr.", "full_ownership", false, {headerTooltip: "Immobili in proprietà per 1/1"});
    numCol("Rendita €", "rendita", false, {formatter: function (c) { return eurText(c.getValue()); }, headerTooltip: "Somma delle rendite catastali dei fabbricati"});
    numCol("Reddito dom. €", "reddito_dominicale", false, {formatter: function (c) { return eurText(c.getValue()); }, headerTooltip: "Somma dei redditi dominicali dei terreni"});
    numCol("Valore catastale €", "cadastral_value", false, {formatter: valueCell("cadastral_value"), headerTooltip: "Base imponibile IMU dell'intero immobile"});
    numCol("Valore pro quota €", "cadastral_value_share", true, {formatter: valueCell("cadastral_value_share"),
        headerTooltip: "Valore catastale ridotto alla quota del soggetto; * = quota mancante, contata per intero; n.v. = immobili senza valore"});
    numCol("Non valutati", "unvalued", false, {headerTooltip: "Immobili di cui non si può calcolare il valore catastale"});
    numCol("Visure", "visure", false);
    numCol("Ricerche", "searches", false);
    textCol("Dati del", "searched_at", false, {headerFilterPlaceholder: "data…",
        formatter: function (cell) { return cell.getRow().getData().searched_label || "–"; }});
    textCol("Aggiornato", "last_seen", true, {headerFilterPlaceholder: "data…",
        formatter: function (cell) { return cell.getRow().getData().last_seen_label || "–"; }});
    // Tags group profiles; they are shared by everybody who signs in. A chip filters by its tag, the pencil edits the cell.
    textCol("Tag", "tags", true, {headerFilter: false, headerSort: false, tooltip: function (e, cell) { return (cell.getRow().getData().tags || []).join(", "); },
        formatter: function (cell) { return tagsCell(cell.getRow()); }});

    // The widest sample of each column over the WHOLE archive (the page carries it: data-overview), for the width
    // measurement; must match the formatters above. Numeric columns use the largest value.
    var NOTE_ALLOWANCE = " +99 n.v. *";
    var SAMPLES = {
        tags: function () { return ["tag lungo tag lungo   ✎"]; },
        favorite_label: function () { return ["★"]; },
        name: function () { return [OVERVIEW.widest.name || "", "(nome non disponibile)      ", (OVERVIEW.widest.name || "") + " +99"]; },
        fiscal_code: function () { return [OVERVIEW.widest.fiscal_code || ""]; },
        gender: function () { return ["M"]; },
        birth_date: function () { return ["00/00/0000"]; },
        birth_place: function () { return [OVERVIEW.widest.birth_place || ""]; },
        provinces: function () { return [OVERVIEW.widest.provinces || ""]; },
        searched_at: function () { return ["00/00/0000"]; },
        last_seen: function () { return ["00/00/0000"]; }
    };
    var MONEY = {rendita: 1, reddito_dominicale: 1, cadastral_value: 1, cadastral_value_share: 1};
    defs.forEach(function (item) {
        if (SAMPLES[item.field]) return;
        SAMPLES[item.field] = function () {
            var max = (OVERVIEW.max || {})[item.field];
            if (max === undefined) return ["0"];
            return MONEY[item.field] ? [eurText(max) + NOTE_ALLOWANCE] : [numText(max)];
        };
    });
    var CELL_CLASS = {fiscal_code: "font-monospace small", name: "fw-semibold"};
    var MAX_WIDTH = {name: 420, provinces: 300, tags: 320};
    var MIN_WIDTH = 64;  // room for a header filter ("Tutti", "es. >10")

    // ---------------------------------------------------------------- URL state
    var query = new URLSearchParams(window.location.search);
    var urlFilters = {};
    query.forEach(function (value, key) { if (key.indexOf("f_") === 0 && value !== "") urlFilters[key.slice(2)] = value; });
    var urlSort = defs.some(function (d) { return d.field === query.get("sort"); }) ? query.get("sort") : null;
    var urlDir = query.get("dir") === "asc" ? "asc" : "desc";
    var startQuick = query.has("q") ? query.get("q") : (query.get("code") || INITIAL_QUICK);
    var startTag = query.get("tag") || "";
    if (nameStatus) {
        var requestedNameStatus = query.get("name_status");
        nameStatus.value = requestedNameStatus === "named" || requestedNameStatus === "missing" ? requestedNameStatus : "";
    }

    // v2: an earlier version defaulted to a short list and saved it, which hid columns; those choices are dropped.
    var COLUMNS_KEY = "sister.subjects." + KIND + ".columns.v2";
    function visibleFromStorage() {
        var saved = null;
        try { saved = JSON.parse(recall(COLUMNS_KEY) || "null"); } catch (e) { saved = null; }
        var known = defs.map(function (d) { return d.field; });
        if (Array.isArray(saved)) return saved.filter(function (f) { return known.indexOf(f) !== -1; });
        return known;  // every column, until the user hides some from "Colonne"
    }
    var visible = visibleFromStorage();
    // A column named by the URL (a filter or the sort) must show, or the link would filter on something unseen.
    Object.keys(urlFilters).concat(urlSort ? [urlSort] : []).forEach(function (f) { if (visible.indexOf(f) === -1) visible.push(f); });

    // ---------------------------------------------------------------- table (remote paging, sort and filters)
    var table = null;
    var matching = TOTAL;     // rows matching the current search/filters (from the server)
    var ready = false;

    // Derived display fields of a received row.
    function prepare(d) {
        d.birth_label = d.birth_date ? d.birth_date.split("-").reverse().join("/") : "";
        d.last_seen_label = dateLabel(d.last_seen);
        d.searched_label = dateLabel(d.searched_at);
        d.favorite_label = d.favorite ? "si" : "no";
        d.tags = d.tags || [];
        return d;
    }

    // Query string of the current state (the names the page keeps in its own URL); `paging` adds page and size.
    function stateQuery(params, paging) {
        var p = new URLSearchParams();
        if (paging) { p.set("page", params.page || 1); p.set("size", params.size || 50); }
        var q = quick.value.trim();
        if (q) p.set("q", q);
        if (nameStatus && nameStatus.value) p.set("name_status", nameStatus.value);
        if (tagSelect && tagSelect.value) p.set("tag", tagSelect.value);
        var sorter = (params.sort || [])[0];
        if (sorter) { p.set("sort", sorter.field); p.set("dir", sorter.dir); }
        (params.filter || []).forEach(function (f) { if (f.value !== "" && f.value !== null && f.value !== undefined) p.set("f_" + f.field, f.value); });
        return p.toString();
    }
    function currentParams() {
        return {
            sort: table.getSorters().map(function (x) { return {field: x.field, dir: x.dir}; }),
            filter: table.getHeaderFilters()
        };
    }

    function fitColumns(columns) {
        var ctx = document.createElement("canvas").getContext("2d");
        var probe = document.createElement("div");
        probe.className = "tabulator";
        probe.style.cssText = "position:absolute;visibility:hidden;left:-9999px;top:0";
        probe.innerHTML = '<div class="tabulator-header"><div class="tabulator-col"><div class="tabulator-col-content">' +
            '<div class="tabulator-col-title">X</div></div></div></div><div class="tabulator-row"><div class="tabulator-cell">X</div></div>';
        document.body.appendChild(probe);
        function fontOf(node) {
            var cs = getComputedStyle(node);
            return {font: [cs.fontStyle, cs.fontWeight, cs.fontSize, cs.fontFamily].join(" "),
                    pad: (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0)};
        }
        var cell = probe.querySelector(".tabulator-cell");
        var headFont = fontOf(probe.querySelector(".tabulator-col-title"));
        var headPad = fontOf(probe.querySelector(".tabulator-col-content")).pad;
        var cellFonts = {};
        function cellFont(field) {
            var cls = CELL_CLASS[field] || "";
            if (!cellFonts[cls]) { cell.className = "tabulator-cell " + cls; cellFonts[cls] = fontOf(cell); }
            return cellFonts[cls];
        }
        function measure(text, font) { ctx.font = font; return ctx.measureText(text).width; }
        columns.forEach(function (col) {
            var title = col.field === "favorite_label" ? "★" : col.title;
            var head = measure(title, headFont.font) + headPad + (col.headerSort === false ? 0 : 22);
            var f = cellFont(col.field);
            var widest = 0;
            SAMPLES[col.field]().forEach(function (text) { widest = Math.max(widest, measure(text, f.font)); });
            var width = Math.ceil(Math.max(MIN_WIDTH, head, widest + f.pad) + 8);
            col.width = MAX_WIDTH[col.field] ? Math.min(width, MAX_WIDTH[col.field]) : width;
        });
        document.body.removeChild(probe);
    }

    (function build() {
        var columns = defs.map(function (item) {
            var def = Object.assign({}, item.def);
            def.visible = item.fixed || visible.indexOf(item.field) !== -1;
            return def;
        });
        fitColumns(columns);
        var initialFilters = Object.keys(urlFilters).filter(function (f) { return defs.some(function (d) { return d.field === f; }); })
            .map(function (f) { return {field: f, value: urlFilters[f]}; });
        quick.value = startQuick;
        if (tagSelect && startTag) selectTag(startTag);
        table = new Tabulator(root, {
            ajaxURL: JSON_URL,
            ajaxURLGenerator: function (url, config, params) { return url + "?" + stateQuery(params, true); },
            ajaxResponse: function (url, params, response) {
                matching = response.total;
                response.data.forEach(prepare);
                return response;
            },
            ajaxError: function (error) { setStatus("Impossibile caricare l'elenco (" + (error && error.message ? error.message : "errore") + ").", true); },
            index: "fiscal_code",
            layout: "fitData",
            placeholder: "Nessuna " + NOUN[0] + " corrisponde ai filtri",
            pagination: true,
            paginationMode: "remote",
            sortMode: "remote",
            filterMode: "remote",
            paginationSize: phone.matches ? 25 : 50,
            paginationSizeSelector: [25, 50, 100, 250, 500],
            paginationCounter: function (pageSize, currentRow, currentPage, totalRows, totalPages) {
                var first = matching ? (currentPage - 1) * pageSize + 1 : 0;
                return "Righe " + intFmt.format(first) + "–" + intFmt.format(Math.min(currentPage * pageSize, matching)) + " di " + intFmt.format(matching);
            },
            movableColumns: true,
            initialSort: [urlSort ? {column: urlSort, dir: urlDir} : {column: DEFAULT_SORT.field, dir: DEFAULT_SORT.dir}],
            initialHeaderFilter: initialFilters,
            locale: "it",
            langs: {it: {pagination: {first: "«", first_title: "Prima pagina", last: "»", last_title: "Ultima pagina",
                                      prev: "‹", prev_title: "Pagina precedente", next: "›", next_title: "Pagina successiva",
                                      all: "Tutte", page_size: "Righe"}}},
            columns: columns
        });
        table.on("tableBuilt", onBuilt);
        table.on("dataLoaded", onLoaded);
        table.on("renderComplete", function () { setTimeout(widenOverflowing, 0); });
    })();

    function onBuilt() {
        root.querySelectorAll(".tabulator-col").forEach(function (col) {
            var title = col.querySelector(".tabulator-col-title");
            var label = "Filtra per " + (title ? title.textContent.trim().replace(/^★/, "") || "Preferito" : "colonna");
            col.querySelectorAll(".tabulator-header-filter input, .tabulator-header-filter select").forEach(function (c) {
                c.setAttribute("aria-label", label);
            });
        });
        buildChooser();
        ready = true;
    }

    // After every page: count, ranking highlight, cards, and the URL.
    var pageRows = [];
    function onLoaded(rows) {
        pageRows = rows || [];
        setStatus("");
        updateCount();
        markRank();
        renderCards();
        writeUrl();
    }

    // The canvas estimate can miss a few pixels (sort-arrow padding, digit widths): widen any column whose header or
    // rendered cells still overflow. Only ever widens.
    function widenOverflowing() {
        if (!table) return;
        table.getColumns().forEach(function (col) {
            if (!col.isVisible()) return;
            var field = col.getField();
            var need = 0;
            var title = col.getElement().querySelector(".tabulator-col-title");
            if (title) need = Math.max(need, title.scrollWidth - title.clientWidth);
            root.querySelectorAll('.tabulator-row .tabulator-cell[tabulator-field="' + field + '"]').forEach(function (node) {
                need = Math.max(need, node.scrollWidth - node.clientWidth);
            });
            var cap = MAX_WIDTH[field];
            if (need > 0 && (!cap || col.getWidth() < cap)) {
                col.setWidth(cap ? Math.min(cap, col.getWidth() + need + 2) : col.getWidth() + need + 2);
            }
        });
    }

    // ---------------------------------------------------------------- search, filters, sort, URL
    // One request: back to page 1 (which loads it), or reload page 1 when already there.
    function reload() {
        if (!table || !ready) return;
        if (table.getPage() !== 1) table.setPage(1); else table.replaceData();
    }

    function writeUrl() {
        if (!ready) return;
        var params = currentParams();
        var sorter = params.sort[0];
        if (sorter && sorter.field === DEFAULT_SORT.field && sorter.dir === DEFAULT_SORT.dir) params.sort = [];
        var qs = stateQuery(params, false);
        try { window.history.replaceState(null, "", window.location.pathname + (qs ? "?" + qs : "")); } catch (e) { /* ignore */ }
    }

    function updateCount() {
        countEl.textContent = intFmt.format(matching) + " di " + intFmt.format(TOTAL) + " " + (TOTAL === 1 ? NOUN[0] : NOUN[1]);
    }

    function markRank() {
        var sorter = table.getSorters()[0];
        document.querySelectorAll("[data-rank]").forEach(function (link) {
            var on = !!sorter && link.getAttribute("data-rank") === sorter.field && sorter.dir === "desc";
            link.classList.toggle("active", on);
            if (on) link.setAttribute("aria-current", "true"); else link.removeAttribute("aria-current");
        });
    }
    function ensureVisible(field) {
        var col = table.getColumn(field);
        if (col && !col.isVisible()) { col.show(); syncChooser(); persistColumns(); }
    }

    var timer = null;
    quick.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(reload, 250); });
    quick.addEventListener("search", reload);
    if (nameStatus) nameStatus.addEventListener("change", reload);
    if (tagSelect) tagSelect.addEventListener("change", reload);

    document.querySelectorAll("[data-rank]").forEach(function (link) {
        link.addEventListener("click", function (event) {
            if (!table || event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;
            event.preventDefault();
            var field = link.getAttribute("data-rank");
            ensureVisible(field);
            table.setSort(field, "desc");
        });
    });
    // Reset to the default view with as few requests as possible (a new load must not start while one is running).
    document.getElementById("subjects-reset").addEventListener("click", function () {
        var hadSearch = quick.value.trim() !== "" || !!(nameStatus && nameStatus.value) || !!(tagSelect && tagSelect.value);
        quick.value = "";
        if (nameStatus) nameStatus.value = "";
        if (tagSelect) tagSelect.value = "";
        var sorter = table.getSorters()[0];
        var sortChanged = !sorter || sorter.field !== DEFAULT_SORT.field || sorter.dir !== DEFAULT_SORT.dir;
        var hadFilters = table.getHeaderFilters().length > 0;
        function sortThenDone() { if (sortChanged) table.setSort(DEFAULT_SORT.field, DEFAULT_SORT.dir); }
        if (hadFilters) {
            var once = function () { table.off("dataLoaded", once); sortThenDone(); };
            table.on("dataLoaded", once);
            table.clearHeaderFilter();
        } else if (sortChanged) {
            sortThenDone();
        } else if (hadSearch) {
            reload();
        }
    });
    // The whole filtered list, built by the server (the table only holds one page).
    document.getElementById("subjects-csv").addEventListener("click", function () {
        window.location.href = CSV_URL + "?" + stateQuery(currentParams(), false);
    });

    // ---------------------------------------------------------------- column chooser
    function persistColumns() {
        store(COLUMNS_KEY, JSON.stringify(
            table.getColumns().filter(function (c) { return c.isVisible() && c.getField() !== "favorite_label"; })
                .map(function (c) { return c.getField(); })));
    }
    function syncChooser() {
        chooserBody.querySelectorAll("input[type=checkbox]").forEach(function (box) {
            var col = table.getColumn(box.value);
            box.checked = !!col && col.isVisible();
        });
    }
    function setColumn(field, on) {
        var col = table.getColumn(field);
        if (!col) return;
        if (on) { col.show(); } else {
            col.setHeaderFilterValue("");  // a hidden column must not keep filtering unseen
            col.hide();
        }
        persistColumns();
        setTimeout(widenOverflowing, 0);
    }
    function buildChooser() {
        defs.forEach(function (item) {
            if (item.fixed || item.field === "name") return;
            var id = "subjects-col-" + item.field;
            var wrap = document.createElement("div");
            wrap.className = "form-check form-check-inline";
            var box = document.createElement("input");
            box.type = "checkbox";
            box.className = "form-check-input";
            box.id = id;
            box.value = item.field;
            box.addEventListener("change", function () { setColumn(item.field, box.checked); });
            var label = document.createElement("label");
            label.className = "form-check-label small";
            label.htmlFor = id;
            label.textContent = item.title;
            wrap.appendChild(box);
            wrap.appendChild(label);
            chooserBody.appendChild(wrap);
        });
        syncChooser();
        document.getElementById("subjects-cols-compact").addEventListener("click", function () {
            defs.forEach(function (item) { if (!item.fixed && item.field !== "name") setColumn(item.field, item.compact); });
            syncChooser();
        });
        document.getElementById("subjects-cols-all").addEventListener("click", function () {
            defs.forEach(function (item) { if (!item.fixed && item.field !== "name") setColumn(item.field, true); });
            syncChooser();
        });
    }

    // ---------------------------------------------------------------- tags
    var TAG_NOUN = IS_PERSON ? ["persona", "persone"] : ["società", "società"];

    // Make `tag` the selected filter, adding it to the list when the link names a tag the page was not rendered with.
    function selectTag(tag) {
        if (!tagSelect) return;
        var found = Array.prototype.some.call(tagSelect.options, function (o) { return o.value === tag; });
        if (!found && tag && tag !== "-") {
            var option = document.createElement("option");
            option.value = tag;
            option.textContent = tag;
            tagSelect.appendChild(option);
        }
        tagSelect.value = tag;
    }
    // Rebuild the filter and the suggestions after tags were added or removed; the current choice stays if it still exists.
    function refreshTagLists(tags) {
        var current = tagSelect ? tagSelect.value : "";
        if (tagSelect) {
            while (tagSelect.options.length > 2) tagSelect.remove(2);
            tags.forEach(function (item) {
                var option = document.createElement("option");
                option.value = item.tag;
                option.textContent = item.tag + " (" + item.count + ")";
                tagSelect.appendChild(option);
            });
            tagSelect.value = current;
            if (tagSelect.value !== current) tagSelect.value = "";
        }
        if (tagList) {
            tagList.textContent = "";
            tags.forEach(function (item) {
                var option = document.createElement("option");
                option.value = item.tag;
                tagList.appendChild(option);
            });
        }
    }
    function tagChip(tag) {
        var chip = document.createElement("button");
        chip.type = "button";
        chip.className = "badge rounded-pill text-bg-light border me-1 subject-tag";
        chip.setAttribute("data-tag", tag);
        chip.title = "Mostra solo i profili con il tag «" + tag + "»";
        chip.textContent = tag;
        return chip;
    }
    function tagsCell(row) {
        var d = row.getData();
        var wrap = document.createElement("span");
        wrap.className = "subject-tags-cell";
        (d.tags || []).forEach(function (tag) { wrap.appendChild(tagChip(tag)); });
        if (TAGS_EDITABLE) {
            var edit = document.createElement("button");
            edit.type = "button";
            edit.className = "btn btn-link btn-sm p-0 subject-tag-edit";
            edit.setAttribute("data-code", d.fiscal_code);
            edit.setAttribute("aria-label", "Modifica i tag di " + (d.name || d.fiscal_code));
            edit.title = "Modifica i tag";
            edit.innerHTML = '<i class="fas fa-pen text-muted" aria-hidden="true"></i>';
            wrap.appendChild(edit);
        }
        return wrap;
    }
    function saveTags(code, tags) {
        return fetch(ROUTE + "/" + encodeURIComponent(code) + "/tags", {
            method: "PUT", credentials: "same-origin",
            headers: {"Content-Type": "application/json", "Accept": "application/json"},
            body: JSON.stringify({tags: tags})
        }).then(function (response) {
            if (response.status === 401) throw new Error("accedi per modificare i tag");
            if (response.status === 422) return response.json().then(function (body) { throw new Error(body.detail || "tag non valido"); });
            if (!response.ok) throw new Error("HTTP " + response.status);
            return response.json();
        });
    }
    function refreshTagsFromServer() {
        return fetch(ROUTE + "/tags.json", {credentials: "same-origin", headers: {"Accept": "application/json"}})
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (body) { if (body && body.tags) refreshTagLists(body.tags); })
            .catch(function () { /* the lists refresh on the next load */ });
    }
    // The cell becomes a text field ("a, b, c"); Enter saves, Escape cancels.
    function editTags(row, cellEl) {
        var d = row.getData();
        var form = document.createElement("form");
        form.className = "d-flex gap-1 align-items-center";
        var input = document.createElement("input");
        input.type = "text";
        input.className = "form-control form-control-sm";
        input.value = (d.tags || []).join(", ");
        input.setAttribute("list", "subjects-tag-list");
        input.setAttribute("aria-label", "Tag di " + (d.name || d.fiscal_code) + ", separati da virgola");
        input.placeholder = "tag, tag…";
        var save = document.createElement("button");
        save.type = "submit";
        save.className = "btn btn-primary btn-sm py-0";
        save.textContent = "OK";
        form.appendChild(input);
        form.appendChild(save);
        var original = cellEl.firstChild;
        cellEl.textContent = "";
        cellEl.appendChild(form);
        input.focus();
        function cancel() { cellEl.textContent = ""; cellEl.appendChild(tagsCell(row)); }
        input.addEventListener("keydown", function (event) { if (event.key === "Escape") { event.preventDefault(); cancel(); } });
        form.addEventListener("submit", function (event) {
            event.preventDefault();
            save.disabled = true;
            var wanted = input.value.split(",").map(function (t) { return t.trim(); }).filter(Boolean);
            saveTags(d.fiscal_code, wanted).then(function (body) {
                row.update({tags: body.tags});
                cellEl.textContent = "";
                cellEl.appendChild(tagsCell(row));
                refreshTagsFromServer();
                if (tagSelect && tagSelect.value) reload();  // the row may no longer match the tag filter
                setStatus("Tag salvati.");
            }).catch(function (error) {
                save.disabled = false;
                setStatus("Tag non salvati: " + error.message + ".", true);
                input.focus();
            });
        });
    }
    root.addEventListener("click", function (event) {
        var chip = event.target.closest(".subject-tag");
        if (chip && table) {
            event.preventDefault();
            selectTag(chip.getAttribute("data-tag"));
            reload();
            return;
        }
        var edit = event.target.closest(".subject-tag-edit");
        if (!edit || !table) return;
        event.preventDefault();
        var row = table.getRow(edit.getAttribute("data-code"));
        if (row) editTags(row, edit.closest(".subject-tags-cell").parentNode);
    });

    // One tag for every row the table matches (all pages): the server re-runs the same query, and refuses when the
    // number of matches is no longer the one the user confirmed.
    function bulkTag(action) {
        var tag = tagInput ? tagInput.value.trim() : "";
        if (!tag) { setStatus("Scrivi il nome del tag.", true); tagInput.focus(); return; }
        if (!matching) { setStatus("Nessun risultato a cui applicare il tag.", true); return; }
        var noun = matching === 1 ? TAG_NOUN[0] : TAG_NOUN[1];
        var question = (action === "add" ? "Aggiungere" : "Togliere") + " il tag «" + tag + "» " +
            (action === "add" ? "a " : "da ") + intFmt.format(matching) + " " + noun + " (tutte le pagine)?";
        if (!window.confirm(question)) return;
        [document.getElementById("subjects-tag-add"), document.getElementById("subjects-tag-remove")].forEach(function (b) { b.disabled = true; });
        fetch(ROUTE + "/tags/bulk?" + stateQuery(currentParams(), false), {
            method: "POST", credentials: "same-origin",
            headers: {"Content-Type": "application/json", "Accept": "application/json"},
            body: JSON.stringify({action: action, tag: tag, expected: matching})
        }).then(function (response) {
            if (response.ok) return response.json();
            return response.json().catch(function () { return {}; }).then(function (body) {
                throw new Error(body.detail || ("HTTP " + response.status));
            });
        }).then(function (body) {
            refreshTagLists(body.tags);
            setStatus((action === "add" ? "Tag aggiunto a " : "Tag tolto da ") + intFmt.format(body.changed) + " di " + intFmt.format(body.matched) + " " + TAG_NOUN[1] + ".");
            if (table) table.replaceData();
        }).catch(function (error) {
            setStatus("Tag non modificato: " + error.message + ".", true);
        }).then(function () {
            [document.getElementById("subjects-tag-add"), document.getElementById("subjects-tag-remove")].forEach(function (b) { b.disabled = false; });
        });
    }
    if (TAGS_EDITABLE && tagInput) {
        document.getElementById("subjects-tag-add").addEventListener("click", function () { bulkTag("add"); });
        document.getElementById("subjects-tag-remove").addEventListener("click", function () { bulkTag("remove"); });
    }

    // ---------------------------------------------------------------- favourites
    root.addEventListener("click", function (event) {
        var button = event.target.closest(".subject-fav");
        if (!button || !table) return;
        event.preventDefault();
        var code = button.getAttribute("data-code");
        var row = table.getRow(code);
        if (!row) return;
        var wanted = !row.getData().favorite;
        button.disabled = true;
        fetch(ROUTE + "/" + encodeURIComponent(code) + "/favorite", {
            method: "PUT", credentials: "same-origin",
            headers: {"Content-Type": "application/json", "Accept": "application/json"},
            body: JSON.stringify({favorite: wanted})
        }).then(function (response) {
            if (response.status === 401) throw new Error("accedi per salvare i preferiti");
            if (!response.ok) throw new Error("HTTP " + response.status);
            row.update({favorite: wanted, favorite_label: wanted ? "si" : "no"});
            setStatus(wanted ? "Aggiunto ai preferiti." : "Rimosso dai preferiti.");
        }).catch(function (error) {
            button.disabled = false;
            setStatus("Preferito non salvato: " + error.message + ".", true);
        });
    });

    // ---------------------------------------------------------------- cards (narrow screens)
    var cardsPrev = null, cardsNext = null, cardsInfo = null;
    function renderCards() {
        if (!cardsEl || !table) return;
        cardsEl.textContent = "";
        if (!phone.matches) return;
        pageRows.forEach(function (d) {
            var card = document.createElement("article");
            card.className = "card border-0 shadow-sm mb-2";
            var body = document.createElement("div");
            body.className = "card-body py-2 px-3";
            var head = document.createElement("div");
            head.className = "d-flex justify-content-between gap-2";
            var title = document.createElement("a");
            title.href = profileUrl(d);
            var name = displayName(d);
            title.className = "fw-semibold text-break" + (name ? "" : " text-muted fst-italic");
            title.textContent = name || "(nome non disponibile)";
            head.appendChild(title);
            if (d.favorite) {
                var star = document.createElement("i");
                star.className = "fas fa-star text-warning";
                star.setAttribute("role", "img");
                star.setAttribute("aria-label", "Preferito");
                head.appendChild(star);
            }
            body.appendChild(head);
            var code = document.createElement("div");
            code.className = "font-monospace small text-muted";
            code.textContent = d.fiscal_code + (IS_PERSON && d.birth_place ? " · " + d.birth_place + (d.birth_province ? " (" + d.birth_province + ")" : "") : "");
            body.appendChild(code);
            if ((d.tags || []).length) {
                var tagsLine = document.createElement("div");
                tagsLine.className = "mt-1";
                d.tags.forEach(function (tag) {
                    var badge = document.createElement("span");
                    badge.className = "badge rounded-pill text-bg-light border me-1";
                    badge.textContent = tag;
                    tagsLine.appendChild(badge);
                });
                body.appendChild(tagsLine);
            }
            var facts = document.createElement("dl");
            facts.className = "row g-0 small mb-0 mt-1";
            [["Valore pro quota", eurText(d.cadastral_value_share) + (d.cadastral_value_share == null ? "" : (valueNote(d, "cadastral_value_share") ? " " + valueNote(d, "cadastral_value_share") : ""))],
             ["Immobili", numText(d.properties) + " (" + numText(d.buildings) + " fabbricati, " + numText(d.land) + " terreni)"],
             ["Province", d.provinces || "–"]].forEach(function (pair) {
                var dt = document.createElement("dt");
                dt.className = "col-5 fw-normal text-muted";
                dt.textContent = pair[0];
                var dd = document.createElement("dd");
                dd.className = "col-7 mb-0";
                dd.textContent = pair[1];
                facts.appendChild(dt);
                facts.appendChild(dd);
            });
            body.appendChild(facts);
            card.appendChild(body);
            cardsEl.appendChild(card);
        });
        var nav = document.createElement("div");
        nav.className = "d-flex align-items-center justify-content-between gap-2";
        cardsPrev = document.createElement("button");
        cardsPrev.type = "button";
        cardsPrev.className = "btn btn-outline-primary btn-sm";
        cardsPrev.textContent = "‹ Precedenti";
        cardsPrev.disabled = table.getPage() <= 1;
        cardsPrev.addEventListener("click", function () { table.previousPage(); });
        cardsNext = document.createElement("button");
        cardsNext.type = "button";
        cardsNext.className = "btn btn-outline-primary btn-sm";
        cardsNext.textContent = "Successivi ›";
        cardsNext.disabled = table.getPage() >= table.getPageMax();
        cardsNext.addEventListener("click", function () { table.nextPage(); });
        cardsInfo = document.createElement("span");
        cardsInfo.className = "small text-muted";
        cardsInfo.textContent = "Pagina " + table.getPage() + " di " + table.getPageMax();
        nav.appendChild(cardsPrev);
        nav.appendChild(cardsInfo);
        nav.appendChild(cardsNext);
        cardsEl.appendChild(nav);
    }
    function onViewportChange() { if (table) { renderCards(); if (!phone.matches) table.redraw(true); } }
    if (phone.addEventListener) phone.addEventListener("change", onViewportChange); else phone.addListener(onViewportChange);
})();
