// Accessibility: tie every visible label to the field that follows it (screen readers announce the label), and make
// wide, scrollable tables reachable with the keyboard.
var labelSeq = 0;
document.querySelectorAll('label.form-label:not([for])').forEach(function (label) {
    var field = label.parentNode.querySelector('input:not([type=hidden]), select, textarea');
    if (!field) return;
    if (!field.id) field.id = 'f' + (++labelSeq) + '-' + (field.name || 'field');
    label.htmlFor = field.id;
});
function markScrollable() {
    document.querySelectorAll('.table-wrap').forEach(function (w) {
        if (w.scrollWidth > w.clientWidth + 1) {
            w.tabIndex = 0; w.setAttribute('role', 'region');
            var h = w.closest('.card') && w.closest('.card').querySelector('.card-header');
            w.setAttribute('aria-label', (h ? h.textContent.trim().replace(/\s+/g, ' ').slice(0, 60) : 'Table') + ' (scrollable)');
        }
    });
}
markScrollable();
window.addEventListener('resize', markScrollable);

// Live table search: <input data-filter="tableId">
document.querySelectorAll('[data-filter]').forEach(function (input) {
    input.addEventListener('input', function () {
        var q = input.value.toLowerCase();
        document.querySelectorAll('#' + input.dataset.filter + ' tbody tr').forEach(function (tr) {
            tr.style.display = tr.textContent.toLowerCase().indexOf(q) === -1 ? 'none' : '';
        });
    });
});

// Edit modals: a button with data-edit="modalId" data-action="/url" data-fields='{"name":"x"}'
// fills the form inside that modal and points it at the right URL.
document.querySelectorAll('[data-edit]').forEach(function (btn) {
    btn.addEventListener('click', function () {
        var modal = document.getElementById(btn.dataset.edit);
        var form = modal.querySelector('form');
        form.action = btn.dataset.action;
        var fields = JSON.parse(btn.dataset.fields);
        Object.keys(fields).forEach(function (k) {
            if (form.elements[k]) form.elements[k].value = fields[k] == null ? '' : fields[k];
        });
        form.dispatchEvent(new CustomEvent('refill'));
        bootstrap.Modal.getOrCreateInstance(modal).show();
    });
});

// Confirm before destructive actions: <form data-confirm="Are you sure?">
document.querySelectorAll('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (e) {
        if (!confirm(f.dataset.confirm)) e.preventDefault();
    });
});

// Auto-dismiss flash messages
setTimeout(function () {
    document.querySelectorAll('.alert-dismissible').forEach(function (a) {
        bootstrap.Alert.getOrCreateInstance(a).close();
    });
}, 6000);

// Long dropdowns (thousands of passengers): a small type-to-filter box above <select data-filterable>
document.querySelectorAll('select[data-filterable]').forEach(function (sel) {
    var all = Array.prototype.slice.call(sel.options).map(function (o) { return { value: o.value, text: o.text, disabled: o.disabled }; });
    var box = document.createElement('input');
    box.type = 'search'; box.className = 'form-control form-control-sm mb-1'; box.placeholder = 'Type to filter…';
    box.setAttribute('aria-label', 'Filter the list of ' + ((sel.labels && sel.labels[0] && sel.labels[0].textContent.trim().toLowerCase()) || 'options'));
    sel.parentNode.insertBefore(box, sel);
    box.addEventListener('input', function () {
        var q = box.value.toLowerCase(), current = sel.value;
        sel.innerHTML = '';
        all.forEach(function (o) {
            if (o.value === '' || o.text.toLowerCase().indexOf(q) !== -1) {
                var op = document.createElement('option');
                op.value = o.value; op.text = o.text; op.disabled = o.disabled;
                if (o.value === current) op.selected = true;
                sel.add(op);
            }
        });
    });
});

// Seat picker: <form data-seatpicker> with fields s_id, travel_date, seat, a [data-seatmap] box and a [data-seathint] line.
// Draws the vehicle's seat map for the chosen trip (from /api/seats), with the aisle in the middle of every row.
document.querySelectorAll('form[data-seatpicker]').forEach(function (form) {
    var map = form.querySelector('[data-seatmap]'), hint = form.querySelector('[data-seathint]');
    var seat = form.elements.seat, schedule = form.elements.s_id, day = form.elements.travel_date;

    function say(text, cls) { hint.textContent = text; hint.className = 'small ' + (cls || 'text-muted'); }

    function mark() {
        map.querySelectorAll('.seat').forEach(function (b) {
            var on = b.dataset.no === seat.value.toUpperCase();
            b.classList.toggle('picked', on); b.setAttribute('aria-pressed', on);
        });
    }

    function draw() {
        if (!schedule.value || !day.value) return;
        fetch('/api/seats?s=' + schedule.value + '&d=' + day.value).then(function (r) { return r.json(); }).then(function (x) {
            if (x.left === null || !x.seats) { map.innerHTML = '<p class="seat-empty">No seat map for this departure.</p>'; return; }
            var aisleAfter = Math.ceil(x.per_row / 2), html = '<div class="seat-front" aria-hidden="true"><i class="bi bi-arrow-up"></i> front</div>';
            for (var i = 0; i < x.seats.length; i += x.per_row) {
                html += '<div class="seat-row"><span class="seat-rowno">' + (i / x.per_row + 1) + '</span>';
                x.seats.slice(i, i + x.per_row).forEach(function (s, c) {
                    if (c === aisleAfter) html += '<span class="seat-aisle"></span>';
                    var state = s.taken ? 'taken' : s.held ? 'held' : 'free', note = s.taken ? ' · taken' : s.held ? ' · held by a colleague' : '';
                    if (s.mine && !seat.value) seat.value = s.no;              // the seat this clerk is already holding
                    html += '<button type="button" class="seat ' + state + (s.pos === 'Window' ? ' window' : '') + '" data-no="' + s.no +
                            '" title="' + s.no + ' · ' + s.pos + note + '" aria-label="Seat ' + s.no + ', ' + s.pos.toLowerCase() + (note ? ',' + note.replace(' ·', '') : ', free') + '"' +
                            ' aria-pressed="false"' + (s.taken || s.held ? ' disabled' : '') + '>' + s.no + '</button>';
                });
                html += '</div>';
            }
            map.innerHTML = html;
            mark();
            if (!x.runs) say('This departure does not run on ' + day.value + ' (service days or a public holiday).', 'text-danger fw-semibold');
            else say(x.left + ' of ' + x.capacity + ' seats free on ' + day.value + (x.swapped ? ' (vehicle ' + x.swapped + ' runs this trip that day)' : '') + '.',
                     x.left > 0 ? 'text-success fw-semibold' : 'text-danger fw-semibold');
        });
    }

    // clicking a seat holds it for a few minutes (HoldSeat procedure), so no colleague can sell it meanwhile
    map.addEventListener('click', function (e) {
        var b = e.target.closest('.seat');
        if (!b || b.disabled) return;
        var body = new FormData();
        ['csrf_token', 's_id', 'travel_date'].forEach(function (k) { body.append(k, form.elements[k].value); });
        body.append('seat', b.dataset.no);
        fetch('/api/hold', { method: 'POST', body: body }).then(function (r) { return r.json(); }).then(function (x) {
            if (x.ok) { seat.value = b.dataset.no; mark(); say(x.message, 'text-success fw-semibold'); }
            else { say(x.message, 'text-danger fw-semibold'); draw(); }
        });
    });
    seat.addEventListener('input', mark);
    schedule.addEventListener('change', draw);
    day.addEventListener('change', draw);
    form.addEventListener('refill', draw);       // a transfer modal was filled for another ticket
});
