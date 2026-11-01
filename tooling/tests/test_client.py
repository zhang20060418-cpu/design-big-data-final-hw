from __future__ import annotations

import unittest

from ppt_audit_tool.cli import build_parser
from ppt_audit_tool.client import InputError, _multipart, normalize_base_url


class ClientTests(unittest.TestCase):
    def test_base_url_defaults_and_scheme(self):
        self.assertEqual(normalize_base_url(""), "http://127.0.0.1:8000")
        self.assertEqual(normalize_base_url("127.0.0.1:8000/"), "http://127.0.0.1:8000")
        self.assertEqual(normalize_base_url("https://example.com/"), "https://example.com")
        with self.assertRaises(InputError):
            normalize_base_url("ftp://example.com")

    def test_multipart_contains_file_and_options(self):
        body, content_type = _multipart(
            {"name": "测试", "options": '{"enable_structure": true}'},
            "file", "a b.pptx", b"PK\x03\x04", "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
        self.assertIn("multipart/form-data; boundary=", content_type)
        self.assertIn("测试".encode("utf-8"), body)
        self.assertIn(b'filename="a b.pptx"', body)
        self.assertIn(b"PK\x03\x04", body)
        self.assertTrue(body.endswith(b"--\r\n"))


class CLIParserTests(unittest.TestCase):
    def test_audit_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["audit", "deck.pptx"])
        self.assertEqual(args.command, "audit")
        self.assertEqual(args.profile, "standard")
        self.assertEqual(args.issues, "summary")
        self.assertEqual(args.format, "json")
        self.assertFalse(args.no_wait)

    def test_audit_flags(self):
        parser = build_parser()
        args = parser.parse_args([
            "audit", "deck.pptx", "--profile", "quick", "--issues", "none",
            "--no-wait", "--format", "text",
        ])
        self.assertEqual(args.profile, "quick")
        self.assertEqual(args.issues, "none")
        self.assertTrue(args.no_wait)
        self.assertEqual(args.format, "text")

    def test_repair_flags(self):
        parser = build_parser()
        args = parser.parse_args([
            "repair", "abc", "--review", "--rounds", "2", "--download-fixed",
        ])
        self.assertTrue(args.review)
        self.assertEqual(args.rounds, 2)
        self.assertTrue(args.download_fixed)


if __name__ == "__main__":
    unittest.main()
