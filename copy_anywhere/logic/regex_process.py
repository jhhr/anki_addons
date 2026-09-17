import logging
import re
import sys
from typing import Optional

logger = logging.getLogger(__name__)


def regex_process(
    text: str,
    regex: Optional[str],
    replacement: Optional[str],
    flags: Optional[str],
) -> str:
    """
    Basic regex processing step that replaces the text that matches the regex with the replacement.
    If no replacement is provided, instead only the match is returned.
    """

    if not regex:
        logger.error("Error in basic_regex_process: Missing 'regex'")
        return text

    if replacement is None:
        # Replace can be "" but not None
        logger.error("Error in basic_regex_process: Missing 'replacement'")
        return text

    if not flags:
        piped_flags = 0
    else:
        int_flags = [getattr(re, f) for f in flags.split(", ")]
        piped_flags = int_flags[0]
        # Combine rest of the flags with pipe
        for f in int_flags[1:]:
            piped_flags |= f

    logger.debug(
        "Running regex:\n---\nregex:\n%s\n---\nreplacement: %s\n---\ntext:\n %s",
        regex,
        replacement,
        text,
    )
    try:
        compiled_regex = re.compile(regex, piped_flags)
    except re.error as e:
        logger.error(
            "Error in basic_regex_process: %s\n---\ntext: %s\n---\nregex:\n%s\n---\nreplacement: %s",
            e,
            text,
            regex,
            replacement,
        )
        return text

    try:
        return compiled_regex.sub(replacement, text)
    except re.error as e:
        logger.error(
            "Error in basic_regex_process: %s\n---\ntext: %s\n---\nregex:\n%s\n---\nreplacement: %s",
            e,
            text,
            regex,
            replacement,
        )
        return text


def test(
    test_name: str,
    text: str,
    regex: str,
    replacement: str,
    flags: Optional[str],
    expected: str,
):
    result = regex_process(text, regex, replacement, flags)
    try:
        assert result == expected
    except AssertionError:
        # Re-run with logging enabled to see what went wrong
        handler = logging.StreamHandler()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            regex_process(text, regex, replacement, flags)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(logging.NOTSET)
        print(f"""\033[91m{test_name}
\033[93mExpected: {expected}
\033[92mGot:      {result}
\033[0m""")
        # Stop testing here
        sys.exit(0)


def main():
    test(
        test_name="Replacement with groups",
        text="<i>abc123</i>def456",
        regex=r"<i>(.*)</i>",
        replacement=r"\1",
        flags=None,
        expected="abc123def456",
    )
    test(
        test_name="Replacement with named groups, match",
        text="<i>abc123</i>def456",
        regex=r"<i>(?P<content>.*)</i>",
        replacement=r"\g<content>",
        flags=None,
        expected="abc123def456",
    )
    test(
        test_name="Replacement with named groups, no match",
        text="<b>abc123</b>def456",
        regex=r"<i>(?P<content>.*)</i>",
        replacement=r"\g<content>",
        flags=None,
        expected="<b>abc123</b>def456",
    )
    print("\n\033[92mTests passed\033[0m")


if __name__ == "__main__":
    main()
