"""
Anki addon to display card desired_retention in the card info modal.

The card info view (hotkey I in reviewer, Ctrl+Shift+I in browser) is a
SvelteKit page loaded inside an AnkiWebView. There is no official hook for
it, so this addon monkey-patches CardInfoDialog to inject JavaScript that
inserts a "Desired Retention" row directly into the SvelteKit-rendered
stats table (FSRS cards only; hidden when desired_retention is absent).
"""
from __future__ import annotations

import json

from anki.errors import NotFoundError
from aqt.browser.card_info import CardInfoDialog
from aqt.qt import qconnect

# JavaScript injected once per page load.
# Sets up a MutationObserver that watches for the SvelteKit-rendered
# .stats-table and inserts/updates our row whenever the DOM changes.
_JS_SETUP = r"""
(function () {
    'use strict';

    function insertOrUpdateDR(drValue) {
        var tbody = document.querySelector('.stats-table tbody');
        if (!tbody) return;

        // Disconnect first to prevent the DOM write from re-triggering us.
        if (window._ankiDRObserver) window._ankiDRObserver.disconnect();

        try {
            var row = document.getElementById('anki-dr-row');

            if (drValue === null || drValue === undefined) {
                if (row) row.remove();
                return;
            }

            var pct = (drValue * 100).toFixed(0) + '%';

            if (row) {
                // Row already present -- just update the value cell.
                row.querySelector('td').textContent = pct;
                return;
            }

            row = document.createElement('tr');
            row.id = 'anki-dr-row';
            row.innerHTML =
                '<th class="align-start">Desired Retention</th>' +
                '<td>' + pct + '</td>';

            // Insert after the last td that ends with '%'
            // (FSRS Difficulty / FSRS Retrievability rows).
            // Fall back to appending at the end of the table.
            var rows = Array.from(tbody.querySelectorAll('tr:not(#anki-dr-row)'));
            var insertAfter = null;
            for (var i = 0; i < rows.length; i++) {
                var td = rows[i].querySelector('td');
                if (td && td.textContent.trim().endsWith('%')) {
                    insertAfter = rows[i];
                }
            }

            if (insertAfter) {
                insertAfter.insertAdjacentElement('afterend', row);
            } else {
                tbody.appendChild(row);
            }
        } finally {
            if (window._ankiDRObserver) {
                window._ankiDRObserver.observe(document.body, {
                    childList: true,
                    subtree: true,
                });
            }
        }
    }

    // Public API used by the Python side.
    window._setDR = function (value) {
        window._ankiDRValue = value;
        insertOrUpdateDR(value);
    };

    // (Re-)create observer, discarding any previous one.
    if (window._ankiDRObserver) window._ankiDRObserver.disconnect();
    var observer = new MutationObserver(function () {
        if (typeof window._ankiDRValue !== 'undefined') {
            insertOrUpdateDR(window._ankiDRValue);
        }
    });
    window._ankiDRObserver = observer;
    observer.observe(document.body, { childList: true, subtree: true });

    // In case the value was already set before this script ran.
    if (typeof window._ankiDRValue !== 'undefined') {
        insertOrUpdateDR(window._ankiDRValue);
    }
})();
"""


def _get_dr(dialog: CardInfoDialog, card_id: object) -> float | None:
    if card_id is None:
        return None
    try:
        card = dialog.mw.col.get_card(card_id)  # type: ignore[arg-type]
        return card.desired_retention
    except (NotFoundError, Exception):
        return None


def _inject_dr(dialog: CardInfoDialog) -> None:
    assert dialog.web is not None
    dr = _get_dr(dialog, dialog._dr_card_id)  # type: ignore[attr-defined]
    dialog.web.eval(_JS_SETUP)
    dialog.web.eval(f"window._setDR({json.dumps(dr)});")


def _patch_card_info_dialog() -> None:
    original_setup_ui = CardInfoDialog._setup_ui
    original_update_card = CardInfoDialog.update_card

    def patched_setup_ui(self: CardInfoDialog, card_id: object) -> None:
        original_setup_ui(self, card_id)  # type: ignore[arg-type]
        self._dr_card_id = card_id  # type: ignore[attr-defined]

        def on_load_finished(ok: bool) -> None:
            if ok and self.web:
                _inject_dr(self)

        qconnect(self.web.loadFinished, on_load_finished)  # type: ignore[union-attr]

    def patched_update_card(self: CardInfoDialog, card_id: object) -> None:
        self._dr_card_id = card_id  # type: ignore[attr-defined]
        original_update_card(self, card_id)  # type: ignore[arg-type]
        # anki.updateCard() triggers an async SvelteKit re-render; update our
        # stored value now so the MutationObserver picks it up when the new
        # table lands in the DOM.
        if self.web:
            dr = _get_dr(self, card_id)
            self.web.eval(
                f"if (typeof window._setDR !== 'undefined')"
                f" window._setDR({json.dumps(dr)});"
            )

    CardInfoDialog._setup_ui = patched_setup_ui  # type: ignore[method-assign]
    CardInfoDialog.update_card = patched_update_card  # type: ignore[method-assign]


_patch_card_info_dialog()