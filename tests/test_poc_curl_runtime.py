import unittest

from poc_repro.curl_runtime import resolve_requested_version
from poc_tool import _build_record


class PocCurlRuntimeTest(unittest.TestCase):
    def test_exact_version_is_accepted(self):
        self.assertEqual(
            resolve_requested_version("curl 8.4.0"),
            ("8.4.0", "VERSION_RESOLVED"),
        )

    def test_parser_version_must_exist_in_raw_report(self):
        record = _build_record({
            "report_id": "RPT-TEST",
            "parser": {"affected_version": "8.4.0"},
            "raw_report_txt": "This report only mentions curl 8.5.0.",
        })
        self.assertIsNone(record["parser"]["affected_version"])

    def test_reported_version_is_preserved(self):
        record = _build_record({
            "report_id": "RPT-TEST",
            "parser": {"affected_version": "curl 8.4.0"},
            "raw_report_txt": "Affected version: curl 8.4.0",
        })
        self.assertEqual(record["parser"]["affected_version"], "curl 8.4.0")


if __name__ == "__main__":
    unittest.main()
