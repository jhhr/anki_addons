"""Code-mode notices for this addon's code editors.

``code_edit_layout`` provides the generic pieces - the prefix, the HTML
caveat, and the available-names renderer - because they describe the sandbox
itself.  The notices below describe what a *copy definition* expects the code
to return, which only means something here, so they live outside the widget.
"""

from ..shared.ui.code_edit_layout import (
    CODE_NOTICE_HTML_WARNING,
    CODE_NOTICE_PREFIX,
    code_notice_available_names,
)

_AVAILABLE_NAMES = code_notice_available_names()

FIELD_CODE_NOTICE = (
    CODE_NOTICE_PREFIX
    + "<b>returns a string</b>. "
    + _AVAILABLE_NAMES
    + "<br>"
    + CODE_NOTICE_HTML_WARNING
)

FILE_CODE_NOTICE = (
    CODE_NOTICE_PREFIX
    + "<b>returns a <tt>list</tt> of <tt>(filename, content)</tt> string tuples</b>. "
    "Each tuple will be written as a separate file. "
    + _AVAILABLE_NAMES
    + "<br>"
    "<small>Example: "
    "<tt>return [('_file.html', '&lt;b&gt;' + note['Field'] + '&lt;/b&gt;')]</tt>"
    "</small><br>"
    + CODE_NOTICE_HTML_WARNING
)


CARD_ACTION_CODE_NOTICE = (
    CODE_NOTICE_PREFIX
    + "<b>returns a <tt>dict</tt> or <tt>None</tt></b>. Return <tt>None</tt> to skip all actions"
    " for this card type. The dict may include any of these keys (all"
    " optional):<br>—<tt>change_deck</tt>: str (deck name) <br>— <tt>set_flag</tt>: int"
    " 0–7 (0=no flag, 1=red, 2=orange, 3=green, 4=blue, 5=pink, 6=turquoise, 7=purple)"
    " <br>— <tt>suspend</tt>: bool <br>— <tt>bury</tt>: bool <br>—"
    " <tt>set_desired_retention</tt>: float 0.01–0.99 or custom-data key (str)<br>"
    + _AVAILABLE_NAMES
    + CODE_NOTICE_HTML_WARNING
)
