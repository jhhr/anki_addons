import unittest
from unittest import mock

from addon_modules import load_ops_module

meanings = load_ops_module("make_all_meanings")


class MergeExistingMeaningsTests(unittest.TestCase):
    def test_uses_object_root_response_schema(self):
        existing_meanings = [
            {"jp_meaning": "受け取る", "en_meaning": "to receive"},
            {"jp_meaning": "食べる", "en_meaning": "to eat"},
        ]
        merged_meanings = [
            {"jp_meaning": "受け取る・食べる", "en_meaning": "to receive; to eat"}
        ]
        meanings_dict = {"頂く_いただく": existing_meanings}

        with mock.patch.object(
            meanings,
            "get_response",
            return_value={"meanings": merged_meanings},
        ) as get_response:
            result = meanings.merge_existing_meanings_for_word(
                {"make_meanings_model": "gpt-5.6-luna"},
                "頂く",
                "いただく",
                meanings_dict,
            )

        self.assertEqual(result, meanings.MakeMeaningsResult.SUCCESS)
        self.assertEqual(meanings_dict["頂く_いただく"], merged_meanings)
        response_schema = get_response.call_args.kwargs["response_schema"]
        self.assertEqual(response_schema["type"], "object")
        self.assertEqual(response_schema["required"], ["meanings"])
        self.assertEqual(response_schema["properties"]["meanings"]["type"], "array")


if __name__ == "__main__":
    unittest.main()
