import re
from html.parser import HTMLParser
from html import unescape

CONTEXT_SENTENCE_RE = re.compile(r"<i>.*?</i>", re.DOTALL)
# The edge of a <div> or <p> block, with the breaks and white space around it: Anki's editor
# writes a field's lines as <div> blocks, often with a <br> inside, and one edge is one break
BLOCK_EDGE_RE = re.compile(r"(?:\s|<br\s*/?>)*</?(?:div|p)(?:\s[^>]*)?>(?:\s|<br\s*/?>)*", re.I)
BR_RE = re.compile(r"<br\s*/?>", re.I)
TAG_RE = re.compile(r"<[^>]*>")


def blocks_as_breaks(field: str) -> str:
    """`field` with each edge of its <div> and <p> blocks a <br>, none before the first line or
    after the last: `<div>a</div><div>b</div>` is `a<br>b`. A field without blocks comes back
    as it was."""
    if not BLOCK_EDGE_RE.search(field):
        return field
    return "<br>".join(piece for piece in BLOCK_EDGE_RE.split(field) if piece.strip())


def text_lines(field: str) -> list[str]:
    """A field's lines as plain text. A <br> in any form (<br />, <BR>), the edge of a block
    (`blocks_as_breaks`) and a line break in the HTML source end a line; every other tag is
    dropped (an SRT's <font> color, an editor's <span style>) and character references are
    read (&amp; is &, &nbsp; a space). Text written back into a field escapes &, < and >."""
    text = TAG_RE.sub("", BR_RE.sub("\n", blocks_as_breaks(field)))
    return unescape(text).replace("\xa0", " ").splitlines()


def strip_context_sentences(sentence: str) -> str:
    """A sentence field without the sentences around the one it is about.

    The word extraction field keeps the neighbouring sentences of the source passage wrapped
    in <i> so a human can read it in context, but they are not what the note is about: no word
    of theirs should get a vocabulary note, and so none should reach a word array either. It
    lives here rather than in utils.py so that the word_array research scripts, which cannot
    import anki, can strip a sentence the way the ops do.
    """
    return CONTEXT_SENTENCE_RE.sub("", sentence)


class HTMLStripper(HTMLParser):
    """Custom HTML parser to strip tags while preserving text"""

    def __init__(self):
        super().__init__()
        self.strict = False
        self.convert_charrefs = True
        self.text = []

    def handle_data(self, data):
        self.text.append(data)

    def get_text(self):
        return "".join(self.text)


def strip_html(html_content):
    """
    Strip HTML tags and clean content for LLM processing

    Args:
        html_content: HTML string from dictionary

    Returns:
        Cleaned plain text string
    """
    if not html_content:
        return ""

    # Use custom HTML stripper
    stripper = HTMLStripper()
    stripper.feed(html_content)
    text = stripper.get_text()

    # Unescape HTML entities
    text = unescape(text)

    # Clean up whitespace
    text = re.sub(r"\s+", " ", text)  # Multiple spaces to single space
    text = re.sub(r"\n\s*\n", "\n\n", text)  # Multiple newlines to double newline
    text = text.strip()

    return text


def strip_html_advanced(html_content, preserve_structure=False):
    """
    Advanced HTML stripping with optional structure preservation

    Args:
        html_content: HTML string from dictionary
        preserve_structure: If True, preserve line breaks and basic formatting

    Returns:
        Cleaned plain text string
    """
    if not html_content:
        return ""

    text = html_content

    if preserve_structure:
        # Convert some HTML elements to text equivalents
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</div>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<li>", "• ", text, flags=re.IGNORECASE)
        text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)

    # Remove script and style elements completely
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # Strip all remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)

    # Unescape HTML entities
    text = unescape(text)

    # Clean up whitespace
    if preserve_structure:
        text = re.sub(r" +", " ", text)  # Multiple spaces to single
        text = re.sub(r"\n\n+", "\n\n", text)  # Limit to double newlines
    else:
        text = re.sub(r"\s+", " ", text)

    text = text.strip()

    return text
