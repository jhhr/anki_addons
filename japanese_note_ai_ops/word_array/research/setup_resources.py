"""Download JMdict_e into user_files/jmdict/ and build the lookup index.

    python word_array/research/setup_jmdict.py
"""

from _bootstrap import load

jmdict = load("jmdict_index")

if __name__ == "__main__":
    if not jmdict.JMDICT_XML.exists():
        print(f"Downloading {jmdict.JMDICT_URL} ...")
        jmdict.download()
    print(f"{len(jmdict.index())} forms indexed in {jmdict.INDEX_PICKLE}")
