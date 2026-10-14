// Live table search: <input data-filter="tableId">
document.querySelectorAll('[data-filter]').forEach(function (input) {
    input.addEventListener('input', function () {
        var q = input.value.toLowerCase();
        document.querySelectorAll('#' + input.dataset.filter + ' tbody tr').forEach(function (tr) {
            tr.style.display = tr.textContent.toLowerCase().indexOf(q) === -1 ? 'none' : '';
        });
    });
});

// Edit modals: a button with data-modal="modalId" data-action="/url" data-fields='{"name":"x"}'
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
    box.setAttribute('aria-label', 'Filter the list');
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
