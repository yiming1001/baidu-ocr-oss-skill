import base64
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
    def test_prefers_image_content_type_over_a_misleading_url_suffix(self):
        self.assertEqual(OCR.guess_image_suffix("https://baidu.example/image.jpg", "image/png"), ".png")

    def test_task_outputs_are_isolated_by_task_id(self):
        first = OCR.build_task_output_prefix("ocr_result/api/", "paddle_vl", "task/one")
        second = OCR.build_task_output_prefix("ocr_result/api/", "paddle_vl", "task/two")

        self.assertEqual(first, "ocr_result/api/paddle_vl/task-one/")
        self.assertEqual(second, "ocr_result/api/paddle_vl/task-two/")
        self.assertNotEqual(first, second)

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


class LocalOutputTests(unittest.TestCase):
    def test_stores_images_and_rewrites_markdown_to_local_urls(self):
        class FakeResponse:
            def __init__(self, content, content_type):
                self.content = content
                self.headers = {"Content-Type": content_type}

            def raise_for_status(self):
                pass

        class FakeSession:
            def get(self, url, timeout):
                return FakeResponse(url.encode("utf-8"), "image/jpeg")

        first_url = "https://baidu.example/first.jpg"
        second_url = "https://baidu.example/second.jpg"
        markdown = f"![first]({first_url})\n![second]({second_url})"
        with tempfile.TemporaryDirectory() as output_dir:
            rewritten, mapping = OCR.transfer_markdown_images_to_local(
                FakeSession(),
                markdown,
                Path(output_dir),
                "/results/paddle_vl/task-1",
            )

            self.assertEqual(mapping[first_url], "/results/paddle_vl/task-1/images/img_0.jpg")
            self.assertEqual(mapping[second_url], "/results/paddle_vl/task-1/images/img_1.jpg")
            self.assertNotIn(first_url, rewritten)
            self.assertNotIn(second_url, rewritten)
            self.assertEqual((Path(output_dir) / "images" / "img_0.jpg").read_bytes(), first_url.encode("utf-8"))
            self.assertEqual((Path(output_dir) / "images" / "img_1.jpg").read_bytes(), second_url.encode("utf-8"))

    def test_run_document_writes_local_results_without_an_oss_bucket(self):
        class FakeResponse:
            content = b"image-bytes"
            headers = {"Content-Type": "image/jpeg"}

            def raise_for_status(self):
                pass

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def get(self, url, timeout):
                return FakeResponse()

        viewer_data = {
            "file_name": "report.pdf",
            "pages": [{"page_num": 0, "meta": {"page_width": 1, "page_height": 1}, "layouts": []}],
        }
        with tempfile.TemporaryDirectory() as output_dir:
            source_path = Path(output_dir) / "report.pdf"
            source_path.write_bytes(b"%PDF-direct-upload")
            submitted = {}

            def submit_task(*args, **kwargs):
                submitted.update(kwargs)
                return "task-1"

            with (
                patch.object(OCR.requests, "Session", return_value=FakeSession()),
                patch.object(OCR, "create_bucket", side_effect=AssertionError("local mode must not create an OSS bucket")),
                patch.object(OCR, "get_baidu_access_token", return_value="token"),
                patch.object(OCR, "submit_parser_task", side_effect=submit_task),
                patch.object(OCR, "wait_for_parser_result", return_value={"markdown_url": "https://baidu.example/result.md", "parse_result_url": "https://baidu.example/result.json"}),
                patch.object(OCR, "download_text", return_value="![image](https://baidu.example/image.jpg)"),
                patch.object(OCR, "download_json", return_value=viewer_data),
            ):
                result = OCR.run_document(
                    {"baidu_api_key": "key", "baidu_secret_key": "secret"},
                    generate_viewer=True,
                    storage_mode="local",
                    local_output_dir=output_dir,
                    local_url_prefix="/results",
                    local_file=str(source_path),
                )

            task_dir = Path(output_dir) / "paddle_vl" / "task-1"
            self.assertIsNone(submitted["file_url"])
            self.assertEqual(submitted["file_data"], base64.b64encode(b"%PDF-direct-upload").decode("ascii"))
            self.assertEqual(result["markdown_url"], "/results/paddle_vl/task-1/report_final.md")
            self.assertEqual(result["viewer_url"], "/results/paddle_vl/task-1/report_viewer.html")
            self.assertEqual(result["source_url"], "/results/paddle_vl/task-1/source/report.pdf")
            self.assertTrue((task_dir / "report_final.md").is_file())
            self.assertTrue((task_dir / "report_viewer.html").is_file())
            self.assertIn(
                "/results/paddle_vl/task-1/images/img_0.jpg",
                (task_dir / "report_final.md").read_text(encoding="utf-8"),
            )
            self.assertEqual((task_dir / "images" / "img_0.jpg").read_bytes(), b"image-bytes")
            self.assertEqual((task_dir / "source" / "report.pdf").read_bytes(), b"%PDF-direct-upload")


if __name__ == "__main__":
    unittest.main()
