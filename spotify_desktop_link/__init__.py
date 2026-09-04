"""
Open Spotify URI
================
Handles `pycmd("spotify:...")` calls from card templates and opens the URI
in the Spotify desktop app via the OS, bypassing Anki's webview.

Installation
------------
Copy the `open_spotify_uri` folder into your Anki add-ons folder:
  - Windows : %APPDATA%\Anki2\addons21\
  - macOS   : ~/Library/Application Support/Anki2/addons21/
  - Linux   : ~/.local/share/Anki2/addons21/

Then restart Anki.
"""

import subprocess
import sys

from anki.hooks import addHook
from aqt import gui_hooks


def handle_spotify_pycmd(handled: tuple, message: str, context) -> tuple:
    """
    Intercepts pycmd messages that start with "spotify:".
    Returns (True, None) to signal that the message has been handled,
    preventing Anki from treating it as an unknown command.
    """
    if not message.startswith("spotify:"):
        return handled

    uri = message.strip()
    _open_uri(uri)
    return (True, None)


def _open_uri(uri: str) -> None:
    if sys.platform == "win32":
        # os.startfile is the idiomatic Windows way for custom URI schemes
        import os
        os.startfile(uri)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", uri])
    else:
        # Linux — xdg-open delegates to the desktop environment
        subprocess.Popen(["xdg-open", uri])


# gui_hooks.webview_did_receive_js_message fires for every pycmd() call
gui_hooks.webview_did_receive_js_message.append(handle_spotify_pycmd)
