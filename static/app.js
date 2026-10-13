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
