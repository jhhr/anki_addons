import re
from typing import Optional
from ..shared.utils.logger import Logger

# One mora of the pitch HTML Yomitan (and Yomichan before it) writes for {pitch-accents}:
# an inline-block span holding a span per character -- a contracted sound like きょ has
# two -- and, last, a "line" span whose style says whether the mora is high and whether
# the pitch drops after it. A dropping mora's wrapper also gets padding.
KANJIUM_MORA_RE = re.compile(
    r'<span style="display:inline-block;position:relative;'
    r'(?:padding-right:0\.1em;margin-right:0\.1em;)?">'
    r"(?P<chars>.*?)"
    r'<span style="border-color:currentColor;(?P<line>[^"]*)"></span></span>'
)
HTML_TAG_RE = re.compile(r"<[^>]*>")

HIGH_LINE_STYLE = "border-top-style:solid;"
DROP_LINE_STYLE = "border-right-style:solid;"

JAVDEJONG_OVERLINE = '<span style="text-decoration:overline;">'
JAVDEJONG_DOWNSTEP = "</span>&#42780;"


def kanjium_to_javdejong_process(
    text: str,
    delimiter: Optional[str],
    logger: Logger = Logger("error"),
):
    """
    Convert a pitch accent html string that is in Kanjium format to Javdejong format.
    :param text: Text with the pitch accent html string to convert. If not in Kanjium format,
        it will be returned as is.
    :param delimiter: The delimiter to use when joining the converted pitch accent descriptions.
        Default is '・'.
    :param logger: A logger instance to log errors and debug messages.
    :return: The converted pitch accent html string in Javdejong format.
    """
    is_kanjium_pitch = re.search(r"currentColor", text)
    if not is_kanjium_pitch:
        return text

    if not delimiter:
        delimiter = "・"

    javdejong_descriptions = []
    for pitch_accent_description in text.split("・"):
        logger.debug(f"pitch_accent_description: {pitch_accent_description}")
        morae = [
            (
                HTML_TAG_RE.sub("", match["chars"]),
                HIGH_LINE_STYLE in match["line"],
                DROP_LINE_STYLE in match["line"],
            )
            for match in KANJIUM_MORA_RE.finditer(pitch_accent_description)
        ]
        logger.debug(f"morae: {morae}")
        javdejong_descriptions.append(morae_to_javdejong(morae))

    return delimiter.join(javdejong_descriptions)


def morae_to_javdejong(morae: list[tuple[str, bool, bool]]) -> str:
    """
    Build Javdejong's markup from (kana, is_high, drops_after) morae, the same way his
    `format_entry` does: open the overline on the first high mora, close it before the next
    low one, and close it with the downstep notch after a mora the pitch drops from.
    """
    result = ""
    overline_open = False
    for kana, is_high, drops_after in morae:
        if is_high and not overline_open:
            result += JAVDEJONG_OVERLINE
            overline_open = True
        elif not is_high and overline_open:
            result += "</span>"
            overline_open = False
        result += kana
        if drops_after:
            result += JAVDEJONG_DOWNSTEP
            overline_open = False
    if overline_open:
        result += "</span>"
    return result
