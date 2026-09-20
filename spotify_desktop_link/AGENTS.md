# spotify_desktop_link (Spotify Desktop Link)

Read the root [AGENTS.md](../AGENTS.md) first. One file, `__init__.py`; no config, no
tests, no shared packages.

Card templates call `pycmd("spotify:track:...")`. The addon handles that message on
`gui_hooks.webview_did_receive_js_message` and hands the URI to the operating system, which
opens it in the Spotify desktop app: `os.startfile` on Windows, `open` on macOS, `xdg-open`
on Linux.

## Invariants

- The handler is a **filter hook that runs for every `pycmd` in every webview**. For any
  message that does not start with `spotify:` it must return `handled` unchanged, and do so
  cheaply. For its own messages it returns `(True, None)`.
- The message comes from card content, which can come from a shared deck, and it is passed to
  the OS. The `spotify:` prefix check is the only thing between a card and
  `os.startfile`. Never loosen it to a general "open this URI" handler, never pass the string
  through a shell, and keep `subprocess.Popen` in list form.
- The card templates that call `pycmd` live in the user's collection, not in this repo.

The module docstring's install instructions mention an `open_spotify_uri` folder; that is
the old name. `from anki.hooks import addHook` is an unused import.

## Shared code

Declares none. Before adding a helper here, check
[docs/shared-code.md](../docs/shared-code.md).
