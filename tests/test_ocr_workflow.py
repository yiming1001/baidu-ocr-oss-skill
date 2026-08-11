import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "baidu_ocr_oss.py"
SPEC = importlib.util.spec_from_file_location("baidu_ocr_oss", SCRIPT_PATH)
OCR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OCR)


class ViewerDataTests(unittest.TestCase):
    def test_normalizes_line_boxes_without_baidu_urls(self):
        payload = {
            "file_name": "report.pdf",
            "pages": [
                {
                    "page_num": 0,
                    "meta": {"page_width": 100, "page_height": 200},
                    "layouts": [
                        {
                            "layout_id": "layout-1",
                            "type": "text",
                            "text": "fallback text",
                            "position": [10, 20, 70, 15],
                            "span_boxes": [
                                {"text": ["first", " line"], "location": [10, 20, 80, 35]},
                                {"text": "second line", "location": [10, 40, 70, 55]},
                            ],
                            "data_url": "https://baidu.example/temporary.jpg",
                        }
                    ],
                }
            ],
        }
        normalized = OCR.normalize_vl_parse_result(payload)
        entries = normalized["pages"][0]["entries"]
        self.assertEqual([entry["text"] for entry in entries], ["first line", "second line"])
        self.assertEqual(entries[0]["polygon"], [[10.0, 20.0], [80.0, 20.0], [80.0, 35.0], [10.0, 35.0]])
        self.assertNotIn("data_url", str(normalized))

    def test_uses_layout_when_span_boxes_are_unavailable(self):
        normalized = OCR.normalize_vl_parse_result(
            {"pages": [{"meta": {"page_width": 20, "page_height": 30}, "layouts": [{"layout_id": "a", "type": "paragraph_title", "text": "Title", "position": [1, 2, 10, 5]}]}]}
        )
        entry = normalized["pages"][0]["entries"][0]
        self.assertEqual(entry["level"], "layout")
        self.assertEqual(entry["polygon"], [[1.0, 2.0], [11.0, 2.0], [11.0, 7.0], [1.0, 7.0]])

    def test_viewer_html_embeds_data_and_escapes_script_close(self):
        html = OCR.build_viewer_html(
            {"file_name": "x</script>", "pages": [{"page_number": 1, "width": 1, "height": 1, "entries": []}]},
            "https://example.com/input.pdf",
            "# Result\n\nMarkdown content",
        )
        self.assertNotIn("__VIEWER_DATA__", html)
        self.assertNotIn("__MARKDOWN_CONTENT__", html)
        self.assertIn("x<\\/script>", html)
        self.assertIn("https://example.com/input.pdf", html)


class LocalUploadTests(unittest.TestCase):
    def test_uploads_only_the_named_local_file_with_public_acl(self):
        bucket = object()
        calls = []

        def resumable_upload(bucket_arg, key, filename, headers=None, **kwargs):
            calls.append((bucket_arg, key, filename, headers, kwargs))

        with tempfile.NamedTemporaryFile(suffix=".pdf") as source:
            source.write(b"%PDF-test")
            source.flush()
            with (
                patch.object(OCR, "create_bucket", return_value=(bucket, "example-bucket", "oss-cn-beijing.aliyuncs.com")),
                patch.object(OCR.oss2, "resumable_upload", side_effect=resumable_upload),
            ):
                upload = OCR.upload_local_file_to_oss({}, source.name, "ocr-test/")

        self.assertEqual(len(calls), 1)
        upload_bucket, key, _, request_headers, _ = calls[0]
        self.assertIs(upload_bucket, bucket)
        self.assertTrue(key.startswith("ocr-test/"))
        self.assertEqual(request_headers["x-oss-object-acl"], OCR.oss2.OBJECT_ACL_PUBLIC_READ)
        self.assertEqual(upload["source_key"], key)
        self.assertTrue(upload["source_url"].startswith("https://example-bucket.oss-cn-beijing.aliyuncs.com/ocr-test/"))


if __name__ == "__main__":
    unittest.main()
