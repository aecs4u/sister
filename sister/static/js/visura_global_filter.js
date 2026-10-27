(function () {
    'use strict';

    function normalize(value) {
        return String(value == null ? '' : value)
            .toLocaleLowerCase('it')
            .normalize('NFD')
            .replace(/[\u0300-\u036f]/g, '');
    }

    function initialize() {
        var tables = Array.prototype.slice.call(
            document.querySelectorAll('.imm-tables .cat-compact-table')
        );
        if (!tables.length || document.getElementById('visura-global-filter')) return;

        tables.forEach(function (table) {
            var wrapper = table.closest('.table-responsive');
            var candidates = [wrapper && wrapper.previousElementSibling, table.previousElementSibling];
            var toolbar = candidates.find(function (element) {
                return element && (element.querySelector('input[type="search"]')
                    || Array.prototype.some.call(element.querySelectorAll('button'), function (button) {
                        return /csv/i.test(button.textContent);
                    }));
            });
            if (toolbar) toolbar.remove();
        });

        var filterBar = document.createElement('div');
        filterBar.className = 'd-flex flex-wrap align-items-center gap-2 mb-3 p-2 border rounded bg-light no-print';
        filterBar.setAttribute('role', 'search');
        filterBar.setAttribute('aria-label', 'Filtra ed esporta gli immobili della visura');

        var label = document.createElement('label');
        label.className = 'form-label fw-semibold mb-0';
        label.htmlFor = 'visura-global-filter';
        label.textContent = 'Filtra tutti gli immobili';

        var input = document.createElement('input');
        input.type = 'search';
        input.id = 'visura-global-filter';
        input.className = 'form-control form-control-sm';
        input.placeholder = 'Comune, foglio, particella…';
        input.setAttribute('aria-label', 'Filtra tutti gli immobili della visura');
        input.style.maxWidth = '24rem';

        var count = document.createElement('span');
        count.className = 'small text-muted ms-sm-auto';
        count.setAttribute('aria-live', 'polite');

        var exportButton = document.createElement('button');
        exportButton.type = 'button';
        exportButton.className = 'btn btn-sm btn-outline-secondary';
        exportButton.innerHTML = '<i class="fas fa-download me-1" aria-hidden="true"></i>Esporta CSV';
        exportButton.title = 'Esporta in un unico file CSV le righe visibili di fabbricati e terreni';

        filterBar.appendChild(label);
        filterBar.appendChild(input);
        filterBar.appendChild(count);
        filterBar.appendChild(exportButton);

        var groups = Array.prototype.slice.call(document.querySelectorAll('details.visura-group'));
        if (groups.length > 1) {
            [['Apri tutti', true, 'fa-angles-down'], ['Chiudi tutti', false, 'fa-angles-up']].forEach(function (entry) {
                var button = document.createElement('button');
                button.type = 'button';
                button.className = 'btn btn-sm btn-outline-secondary';
                button.innerHTML = '<i class="fas ' + entry[2] + ' me-1" aria-hidden="true"></i>' + entry[0];
                button.addEventListener('click', function () {
                    groups.forEach(function (group) { if (!group.hidden) group.open = entry[1]; });
                });
                filterBar.insertBefore(button, exportButton);
            });
        }

        var firstGroups = document.querySelector('.visura-groups');
        var firstTables = document.querySelector('.imm-tables');
        var firstHeading = firstTables && firstTables.previousElementSibling;
        if (firstGroups) firstGroups.before(filterBar);
        else if (firstHeading && /^H[1-6]$/.test(firstHeading.tagName)) firstHeading.before(filterBar);
        else if (firstTables) firstTables.before(filterBar);

        var rows = tables.reduce(function (all, table) {
            return all.concat(Array.prototype.slice.call(table.querySelectorAll('tbody tr')));
        }, []);

        function cellText(cell) {
            return cell.textContent.trim().replace(/\s+/g, ' ');
        }

        function tableType(table, index) {
            var wrapper = table.closest('.cat-table-wrap') || table.parentElement;
            var heading = wrapper && wrapper.previousElementSibling;
            if (!heading) return 'Tabella ' + (index + 1);
            var copy = heading.cloneNode(true);
            copy.querySelectorAll('.badge').forEach(function (badge) { badge.remove(); });
            return cellText(copy) || ('Tabella ' + (index + 1));
        }

        function exportVisibleRows() {
            var columns = ['Tipo immobile'];
            var columnIndexes = Object.create(null);
            columnIndexes[normalize(columns[0])] = 0;
            var tableData = tables.map(function (table, index) {
                var headers = Array.prototype.map.call(table.querySelectorAll('thead th'), cellText);
                var indexes = headers.map(function (header) {
                    var key = normalize(header);
                    if (!Object.prototype.hasOwnProperty.call(columnIndexes, key)) {
                        columnIndexes[key] = columns.length;
                        columns.push(header);
                    }
                    return columnIndexes[key];
                });
                return { table: table, type: tableType(table, index), indexes: indexes };
            });

            var output = [columns];
            tableData.forEach(function (data) {
                Array.prototype.forEach.call(data.table.querySelectorAll('tbody tr'), function (row) {
                    if (row.style.display === 'none') return;
                    var values = new Array(columns.length).fill('');
                    values[0] = data.type;
                    Array.prototype.forEach.call(row.querySelectorAll('td'), function (cell, index) {
                        if (data.indexes[index] !== undefined) values[data.indexes[index]] = cellText(cell);
                    });
                    output.push(values);
                });
            });

            var csv = '\ufeff' + output.map(function (row) {
                return row.map(function (value) {
                    return '"' + String(value == null ? '' : value).replace(/"/g, '""') + '"';
                }).join(',');
            }).join('\r\n');
            var url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8;' }));
            var link = document.createElement('a');
            link.href = url;
            link.download = 'visura-immobili.csv';
            link.click();
            window.setTimeout(function () { URL.revokeObjectURL(url); }, 0);
        }

        function applyFilter() {
            var query = normalize(input.value.trim());
            var visible = 0;
            rows.forEach(function (row) {
                var matches = !query || normalize(row.textContent).includes(query);
                row.style.display = matches ? '' : 'none';
                if (matches) visible += 1;
            });
            count.textContent = visible + ' di ' + rows.length + ' righe';
            syncGroups(query);
        }

        // A filter opens the groups that have matching rows and hides the others; clearing it restores what was open.
        function syncGroups(query) {
            groups.forEach(function (group) {
                var tableRows = group.querySelectorAll('.imm-tables tbody tr');
                var matching = Array.prototype.some.call(tableRows, function (row) { return row.style.display !== 'none'; });
                if (query) {
                    if (group.dataset.openBefore === undefined) group.dataset.openBefore = group.open ? '1' : '0';
                    group.hidden = tableRows.length > 0 && !matching;
                    if (matching) group.open = true;
                } else {
                    if (group.dataset.openBefore !== undefined) {
                        group.open = group.dataset.openBefore === '1';
                        delete group.dataset.openBefore;
                    }
                    group.hidden = false;
                }
            });
        }

        input.addEventListener('input', applyFilter);
        exportButton.addEventListener('click', exportVisibleRows);
        applyFilter();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize, { once: true });
    } else {
        initialize();
    }
})();
