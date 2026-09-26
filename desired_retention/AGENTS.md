# desired_retention (Desired Retention in Card Stats)

Read the root [AGENTS.md](../AGENTS.md) first. One file, `__init__.py`; no config, no
tests, no shared packages.

Adds a "Desired Retention" row to the card info dialog, showing `card.desired_retention`
(FSRS cards only; the row is removed when the value is absent).

## How it works, and why it is fragile

The card info view is a SvelteKit page inside an `AnkiWebView`, and Anki offers no hook for
it. So the addon **monkeypatches** `aqt.browser.card_info.CardInfoDialog`:

- `_setup_ui` is wrapped to remember the card id and, on `loadFinished`, inject `_JS_SETUP`
  and call `window._setDR(value)`.
- `update_card` is wrapped to store the new id **before** calling the original and then push
  the new value, because `anki.updateCard()` re-renders asynchronously.
- The JS inserts the row after the last row whose value ends in `%`, falling back to the end
  of `.stats-table tbody`. A `MutationObserver` re-inserts it after each Svelte re-render,
  and is disconnected around the addon's own DOM write so that it does not trigger itself.

Everything it depends on is private: the `_setup_ui` name and signature, `update_card`, the
`web` attribute, the `.stats-table` markup, and the assumption that percentage rows are the
FSRS ones. An Anki release can break any of them without notice. When it stops working,
check those in the running Anki version first. Failures must stay silent and harmless:
`_get_dr` returns `None` on any error, and the patch must never stop the dialog from
opening.

The patch is applied once at import. Do not make it re-entrant without a guard against
double wrapping.

## Shared code

Declares none. Before adding a helper here, check
[docs/shared-code.md](../docs/shared-code.md).
