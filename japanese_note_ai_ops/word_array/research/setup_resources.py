"""Download what the generator needs into user_files/ and build the JMdict index, as the add-on
does on first use (resources.ensure).

    python word_array/research/setup_resources.py

An installed sudachidict_* package counts as the Sudachi dictionary, so a development Python
that has one only fetches JMdict.
"""

from _bootstrap import load

resources = load("resources")
jmdict = load("jmdict_index")

if __name__ == "__main__":
    for download in resources.missing():
        print(f"missing: {download.name} ({download.size_mb:.0f} MB)")
    resources.ensure(lambda message: print(message, end="\r", flush=True))
    print(f"\nSudachi dictionary: {resources.sudachi_dictionary()}")
    print(f"{len(jmdict.index())} JMdict forms indexed in {jmdict.INDEX_PICKLE}")
