#!/usr/bin/env python3
"""Shared Baidu OCR and Aliyun OSS workflow used by the CLI and local console."""

import argparse
import json
import mimetypes
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote, urlparse

try:
    import oss2
    import requests
    from oss2 import headers
except ImportError as exc:
    missing = exc.name or "dependency"
    raise SystemExit(
        f"Missing dependency: {missing}. Install with: python3 -m pip install -r requirements.txt"
    ) from exc


@dataclass(frozen=True)
class BaiduParserModel:
    id: str
    display_name: str
    submit_url: str
    query_url: str
    running_statuses: set[str]


PADDLE_VL_MODEL = BaiduParserModel(
    id="paddle_vl",
    display_name="Baidu PaddleOCR-VL",
    submit_url="https://aip.baidubce.com/rest/2.0/brain/online/v2/paddle-vl-parser/task",
    query_url="https://aip.baidubce.com/rest/2.0/brain/online/v2/paddle-vl-parser/task/query",
    running_statuses={"pending", "processing", "running"},
)

UNLIMITED_OCR_MODEL = BaiduParserModel(
    id="unlimited_ocr",
    display_name="Baidu Unlimited-OCR",
    submit_url="https://aip.baidubce.com/rest/2.0/brain/online/v2/unlimited-ocr-parser/task",
    query_url="https://aip.baidubce.com/rest/2.0/brain/online/v2/unlimited-ocr-parser/task/query",
    running_statuses={"pending", "running", "processing"},
)

MODEL_ALIASES = {
    "paddle_vl": PADDLE_VL_MODEL,
    "paddleocr_vl": PADDLE_VL_MODEL,
    "vl": PADDLE_VL_MODEL,
    "ovl": PADDLE_VL_MODEL,
    "unlimited_ocr": UNLIMITED_OCR_MODEL,
    "unlimited": UNLIMITED_OCR_MODEL,
    "ocr": UNLIMITED_OCR_MODEL,
}

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
LOCAL_INPUT_SUFFIXES = IMAGE_SUFFIXES | {".pdf", ".ofd", ".doc", ".docx", ".txt", ".wps", ".ppt", ".pptx"}
SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = SKILL_DIR / "config" / "local.json"
VIEWER_TEMPLATE_PATH = SKILL_DIR / "assets" / "ocr_viewer.html"
StatusCallback = Callable[[str], None] | None


def load_config(path: str | None) -> dict:
    config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as f:
        config = json.load(f)
    if not isinstance(config, dict):
        raise ValueError(f"Config must be a JSON object: {config_path}")
    return config


def config_value(config: dict, key: str, env_name: str | None = None, default=None):
    if key in config and config[key] not in (None, ""):
        return config[key]
    if env_name:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return default


def required_config(config: dict, key: str, env_name: str) -> str:
    value = config_value(config, key, env_name)
    if not value:
        raise RuntimeError(
            f"Missing required config value: {key}. Set it in {DEFAULT_CONFIG_PATH} or export {env_name}."
        )
    return str(value)


def get_model(model_name: str) -> BaiduParserModel:
    normalized_name = model_name.strip().lower().replace("-", "_")
    try:
        return MODEL_ALIASES[normalized_name]
    except KeyError as exc:
        valid_names = ", ".join(sorted(MODEL_ALIASES))
        raise ValueError(f"Unknown model: {model_name}. Valid names: {valid_names}") from exc


def normalize_endpoint_host(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    return parsed.netloc if parsed.netloc else endpoint


def build_oss_public_url(bucket_name: str, endpoint: str, object_key: str) -> str:
    endpoint_host = normalize_endpoint_host(endpoint)
    return f"https://{bucket_name}.{endpoint_host}/{quote(object_key, safe='/')}"


def create_bucket(config: dict, connect_timeout: int = 120) -> tuple[oss2.Bucket, str, str]:
    access_key_id = required_config(config, "oss_access_key_id", "OSS_ACCESS_KEY_ID")
    access_key_secret = required_config(config, "oss_access_key_secret", "OSS_ACCESS_KEY_SECRET")
    endpoint = required_config(config, "oss_endpoint", "OSS_ENDPOINT")
    bucket_name = required_config(config, "oss_bucket", "OSS_BUCKET")
    auth = oss2.Auth(access_key_id, access_key_secret)
    # 延长连接超时，缓解网络较慢时的上传/下载超时
    bucket = oss2.Bucket(auth, endpoint, bucket_name, connect_timeout=connect_timeout)
    return bucket, bucket_name, endpoint


def normalize_prefix(prefix: str) -> str:
    value = prefix.strip().strip("/")
    return f"{value}/" if value else ""


def upload_local_file_to_oss(
    config: dict,
    local_file: str,
    input_prefix: str | None = None,
    status_callback: StatusCallback = None,
) -> dict[str, str]:
    """Upload one local source file with public-read ACL and return its permanent OSS URL."""
    source_path = Path(local_file).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Local input file not found: {source_path}")
    suffix = source_path.suffix.lower()
    if suffix not in LOCAL_INPUT_SUFFIXES:
        raise ValueError(f"Unsupported local input type: {suffix or '(none)'}")
    size_limit = 100 * 1024 * 1024 if suffix in IMAGE_SUFFIXES | {".pdf", ".ofd"} else 50 * 1024 * 1024
    if source_path.stat().st_size > size_limit:
        raise ValueError(f"Local input exceeds the {size_limit // 1024 // 1024} MB limit: {source_path.name}")

    bucket, bucket_name, endpoint = create_bucket(config)
    prefix = normalize_prefix(str(input_prefix or config_value(config, "input_prefix", default="ocr-test/")))
    object_key = f"{prefix}{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:12]}-{source_path.name}"
    content_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
    upload_headers = {headers.OSS_OBJECT_ACL: oss2.OBJECT_ACL_PUBLIC_READ, "Content-Type": content_type}

    # 用断点续传（分片上传）：文件切成小块分别上传，单块超时可自动续传，
    # 对不稳定/较慢的网络更可靠。整体再包一层重试。
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            oss2.resumable_upload(
                bucket,
                object_key,
                str(source_path),
                headers=upload_headers,
                multipart_threshold=1 * 1024 * 1024,
                part_size=1 * 1024 * 1024,
                num_threads=3,
            )
            break
        except (oss2.exceptions.RequestError, oss2.exceptions.ServerError) as exc:
            last_exc = exc
            message = f"上传到 OSS 超时/失败，正在重试（第 {attempt}/3 次）…"
            print(message, file=sys.stderr)
            if status_callback:
                status_callback(message)
            if attempt < 3:
                time.sleep(3 * attempt)
    else:
        raise RuntimeError(f"多次重试后仍无法上传到 OSS：{last_exc}")

    return {
        "source_key": object_key,
        "source_url": build_oss_public_url(bucket_name, endpoint, object_key),
        "source_file_name": source_path.name,
    }


def build_oss_download_url(
    bucket: oss2.Bucket,
    bucket_name: str,
    endpoint: str,
    object_key: str,
    output_url_mode: str,
    signed_url_expires: int,
) -> str:
    if output_url_mode == "signed":
        return bucket.sign_url("GET", object_key, signed_url_expires, slash_safe=True)
    if output_url_mode == "public":
        return build_oss_public_url(bucket_name, endpoint, object_key)
    raise ValueError('output_url_mode must be "signed" or "public"')


def put_result_object(
    bucket: oss2.Bucket,
    bucket_name: str,
    endpoint: str,
    object_key: str,
    data: bytes,
    output_url_mode: str,
    signed_url_expires: int,
    content_type: str | None = None,
) -> str:
    request_headers = {}
    if output_url_mode == "public":
        request_headers[headers.OSS_OBJECT_ACL] = oss2.OBJECT_ACL_PUBLIC_READ
    if content_type:
        request_headers["Content-Type"] = content_type
    bucket.put_object(object_key, data, headers=request_headers or None)
    return build_oss_download_url(
        bucket, bucket_name, endpoint, object_key, output_url_mode, signed_url_expires
    )


def post_with_retry(
    session: requests.Session,
    url: str,
    *,
    retries: int = 3,
    backoff: float = 3.0,
    status_callback: StatusCallback = None,
    **kwargs,
) -> requests.Response:
    """对百度接口的 POST 请求做超时/连接错误重试，缓解偶发网络抖动。"""
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return session.post(url, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            message = f"网络超时/连接失败，正在重试（第 {attempt}/{retries} 次）…"
            print(message, file=sys.stderr)
            if status_callback:
                status_callback(message)
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"多次重试后仍无法连接百度接口：{last_exc}")


def get_baidu_access_token(
    session: requests.Session,
    api_key: str,
    secret_key: str,
    status_callback: StatusCallback = None,
) -> str:
    response = post_with_retry(
        session,
        "https://aip.baidubce.com/oauth/2.0/token",
        params={
            "grant_type": "client_credentials",
            "client_id": api_key,
            "client_secret": secret_key,
        },
        timeout=60,
        status_callback=status_callback,
    )
    response.raise_for_status()
    payload = response.json()
    if "access_token" not in payload:
        raise RuntimeError(f"Failed to get Baidu access_token: {payload}")
    return payload["access_token"]


def parse_key_value_options(raw_options: list[str]) -> dict[str, str]:
    options = {}
    for raw in raw_options:
        if "=" not in raw:
            raise ValueError(f"Option must use key=value format: {raw}")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Option key cannot be empty: {raw}")
        options[key] = value.strip()
    return options


def submit_parser_task(
    session: requests.Session,
    access_token: str,
    model: BaiduParserModel,
    file_url: str,
    file_name: str,
    model_options: dict[str, str],
    status_callback: StatusCallback = None,
) -> str:
    data = {"file_url": file_url, "file_name": file_name}
    data.update(model_options)
    response = post_with_retry(
        session,
        model.submit_url,
        params={"access_token": access_token},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=data,
        timeout=120,
        status_callback=status_callback,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error_code"):
        raise RuntimeError(f"Failed to submit {model.display_name} task: {payload}")
    try:
        return payload["result"]["task_id"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"Unexpected submit response: {payload}") from exc


def wait_for_parser_result(
    session: requests.Session,
    access_token: str,
    model: BaiduParserModel,
    task_id: str,
    poll_interval: int,
    max_wait: int,
    status_callback: StatusCallback = None,
) -> dict:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(poll_interval)
        response = session.post(
            model.query_url,
            params={"access_token": access_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"task_id": task_id},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error_code"):
            raise RuntimeError(f"Failed to query {model.display_name} task: {payload}")
        result = payload.get("result", {})
        status = result.get("status")
        message = f"{model.display_name} status: {status}"
        print(message, file=sys.stderr)
        if status_callback:
            status_callback(message)
        if status == "success":
            return result
        if status == "failed":
            raise RuntimeError(f"{model.display_name} failed: {result.get('task_error', result)}")
        if status not in model.running_statuses:
            raise RuntimeError(f"Unknown task status: {payload}")
    raise TimeoutError(f"{model.display_name} timed out, task_id={task_id}")


def download_text(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=120)
    response.raise_for_status()
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        response.encoding = response.apparent_encoding or response.encoding or "utf-8"
        return response.text


def download_json(session: requests.Session, url: str) -> dict:
    response = session.get(url, timeout=120)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Baidu parse_result_url did not return a JSON object.")
    return payload


def guess_image_suffix(url: str, content_type: str | None) -> str:
    suffix = Path(urlparse(url).path).suffix
    if suffix:
        return suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if guessed:
            return guessed
    return ".png"


def extract_image_urls(markdown_content: str) -> list[str]:
    url_pattern = re.compile(r"https?://[^\s)\"'<>]+")
    image_urls = []
    seen = set()
    for url in url_pattern.findall(markdown_content):
        if Path(urlparse(url).path).suffix.lower() not in IMAGE_SUFFIXES or url in seen:
            continue
        seen.add(url)
        image_urls.append(url)
    return image_urls


def transfer_markdown_images_to_oss(
    session: requests.Session,
    bucket: oss2.Bucket,
    bucket_name: str,
    endpoint: str,
    markdown_content: str,
    image_output_prefix: str,
    output_url_mode: str,
    signed_url_expires: int,
    status_callback: StatusCallback = None,
) -> tuple[str, dict[str, str]]:
    url_mapping: dict[str, str] = {}
    image_urls = extract_image_urls(markdown_content)
    for index, image_url in enumerate(image_urls):
        if status_callback:
            status_callback(f"Copying OCR image {index + 1}/{len(image_urls)} to OSS")
        image_response = session.get(image_url, timeout=120)
        image_response.raise_for_status()
        suffix = guess_image_suffix(image_url, image_response.headers.get("Content-Type"))
        image_key = f"{image_output_prefix}img_{index}{suffix}"
        url_mapping[image_url] = put_result_object(
            bucket,
            bucket_name,
            endpoint,
            image_key,
            image_response.content,
            output_url_mode,
            signed_url_expires,
            image_response.headers.get("Content-Type"),
        )
    for old_url, new_url in url_mapping.items():
        markdown_content = markdown_content.replace(old_url, new_url)
    return markdown_content, url_mapping


def _polygon_from_layout(layout: dict) -> list[list[float]]:
    polygon = layout.get("polygon")
    if isinstance(polygon, list) and polygon:
        if isinstance(polygon[0], (list, tuple)):
            return [[float(point[0]), float(point[1])] for point in polygon if len(point) >= 2]
        if len(polygon) >= 8:
            return [[float(polygon[index]), float(polygon[index + 1])] for index in range(0, 8, 2)]
    position = layout.get("position")
    if isinstance(position, list) and len(position) >= 4:
        x, y, width, height = (float(value) for value in position[:4])
        return [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]
    return []


def _polygon_from_span(span: dict, fallback: list[list[float]]) -> list[list[float]]:
    location = span.get("location")
    if isinstance(location, list) and location:
        if isinstance(location[0], (list, tuple)):
            return [[float(point[0]), float(point[1])] for point in location if len(point) >= 2]
        if len(location) >= 8:
            return [[float(location[index]), float(location[index + 1])] for index in range(0, 8, 2)]
        if len(location) >= 4:
            x1, y1, x2, y2 = (float(value) for value in location[:4])
            if x2 > x1 and y2 > y1:
                return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
            return [[x1, y1], [x1 + x2, y1], [x1 + x2, y1 + y2], [x1, y1 + y2]]
    return fallback


def _text_value(value) -> str:
    if isinstance(value, list):
        return "".join(str(item) for item in value).strip()
    return str(value or "").strip()


def normalize_vl_parse_result(parse_result: dict) -> dict:
    """Keep only data required by the browser viewer, never Baidu temporary URLs."""
    pages = []
    for page_index, page in enumerate(parse_result.get("pages", [])):
        meta = page.get("meta") or {}
        entries = []
        for layout_index, layout in enumerate(page.get("layouts", [])):
            layout_text = _text_value(layout.get("text"))
            layout_polygon = _polygon_from_layout(layout)
            layout_id = str(layout.get("layout_id") or f"page-{page_index}-layout-{layout_index}")
            spans = layout.get("span_boxes") or []
            added_span = False
            for span_index, span in enumerate(spans):
                span_text = _text_value(span.get("text"))
                if not span_text:
                    continue
                entries.append(
                    {
                        "id": f"{layout_id}-span-{span_index}",
                        "text": span_text,
                        "type": layout.get("type") or "text",
                        "polygon": _polygon_from_span(span, layout_polygon),
                        "level": "line",
                    }
                )
                added_span = True
            if not added_span and layout_text and layout_polygon:
                entries.append(
                    {
                        "id": layout_id,
                        "text": layout_text,
                        "type": layout.get("type") or "text",
                        "polygon": layout_polygon,
                        "level": "layout",
                    }
                )
        pages.append(
            {
                "page_number": int(page.get("page_num", page_index)) + 1,
                "width": float(meta.get("page_width") or 1),
                "height": float(meta.get("page_height") or 1),
                "entries": entries,
            }
        )
    if not pages:
        raise RuntimeError("PaddleOCR-VL parse result has no pages for the viewer.")
    return {"file_name": parse_result.get("file_name", "document"), "pages": pages}


def build_viewer_html(viewer_data: dict, source_pdf_url: str, markdown_content: str) -> str:
    if not VIEWER_TEMPLATE_PATH.exists():
        raise RuntimeError(f"Viewer template is missing: {VIEWER_TEMPLATE_PATH}")
    template = VIEWER_TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = json.dumps(viewer_data, ensure_ascii=False).replace("</", "<\\/")
    return (
        template.replace("__VIEWER_DATA__", payload)
        .replace("__SOURCE_PDF_URL__", json.dumps(source_pdf_url, ensure_ascii=False))
        .replace("__MARKDOWN_CONTENT__", json.dumps(markdown_content, ensure_ascii=False).replace("</", "<\\/"))
    )


def run_document(
    config: dict,
    file_url: str,
    file_name: str | None = None,
    model_name: str | None = None,
    output_prefix_base: str | None = None,
    output_url_mode: str | None = None,
    signed_url_expires: int | None = None,
    poll_interval: int | None = None,
    max_wait: int | None = None,
    model_options: dict[str, str] | None = None,
    generate_viewer: bool = False,
    status_callback: StatusCallback = None,
) -> dict:
    api_key = required_config(config, "baidu_api_key", "BAIDU_API_KEY")
    secret_key = required_config(config, "baidu_secret_key", "BAIDU_SECRET_KEY")
    bucket, bucket_name, endpoint = create_bucket(config)
    model = get_model(str(model_name or config_value(config, "model", default="paddle_vl")))
    output_prefix_base = output_prefix_base or config_value(config, "output_prefix", default="ocr_result/api/")
    output_url_mode = output_url_mode or config_value(config, "output_url_mode", default="signed")
    signed_url_expires = signed_url_expires or int(config_value(config, "signed_url_expires", default=604800))
    poll_interval = poll_interval or int(config_value(config, "poll_interval", default=5))
    max_wait = max_wait or int(config_value(config, "max_wait", default=1800))
    file_name = str(file_name or unquote(Path(urlparse(file_url).path).name) or "input.pdf")
    output_prefix = f"{str(output_prefix_base).rstrip('/')}/{model.id}/"
    model_options = dict(model_options or {})
    if generate_viewer:
        if model != PADDLE_VL_MODEL:
            raise ValueError("The visual viewer is available only for paddle_vl.")
        if output_url_mode != "public":
            raise ValueError("The visual viewer requires output_url_mode=public.")
        model_options.setdefault("return_span_boxes", "true")

    def update(message: str) -> None:
        if status_callback:
            status_callback(message)

    with requests.Session() as session:
        update("Requesting Baidu access token")
        access_token = get_baidu_access_token(session, api_key, secret_key, status_callback)
        update(f"Submitting {model.display_name} task")
        task_id = submit_parser_task(
            session, access_token, model, file_url, file_name, model_options, status_callback
        )
        update(f"Task submitted: {task_id}")
        result = wait_for_parser_result(
            session, access_token, model, task_id, poll_interval, max_wait, status_callback
        )
        markdown_url = result.get("markdown_url")
        if not markdown_url:
            raise RuntimeError(f"Successful task response has no markdown_url: {result}")
        update("Downloading Markdown result")
        markdown_content = download_text(session, markdown_url)
        markdown_content, image_mapping = transfer_markdown_images_to_oss(
            session,
            bucket,
            bucket_name,
            endpoint,
            markdown_content,
            f"{output_prefix}images/",
            str(output_url_mode),
            signed_url_expires,
            status_callback,
        )
        final_md_key = f"{output_prefix}{Path(file_name).stem}_final.md"
        update("Saving final Markdown to OSS")
        final_md_url = put_result_object(
            bucket,
            bucket_name,
            endpoint,
            final_md_key,
            markdown_content.encode("utf-8"),
            str(output_url_mode),
            signed_url_expires,
            "text/markdown; charset=utf-8",
        )
        viewer_url = None
        viewer_key = None
        parse_result_available = bool(result.get("parse_result_url"))
        viewer_unavailable_reason = None
        if generate_viewer:
            parse_result_url = result.get("parse_result_url")
            if not parse_result_url:
                raise RuntimeError("PaddleOCR-VL task succeeded but did not return parse_result_url.")
            update("Downloading PaddleOCR-VL coordinates")
            viewer_data = normalize_vl_parse_result(download_json(session, parse_result_url))
            viewer_html = build_viewer_html(viewer_data, file_url, markdown_content)
            viewer_key = f"{output_prefix}{Path(file_name).stem}_viewer.html"
            update("Publishing public visual viewer")
            viewer_url = put_result_object(
                bucket,
                bucket_name,
                endpoint,
                viewer_key,
                viewer_html.encode("utf-8"),
                "public",
                signed_url_expires,
                "text/html; charset=utf-8",
            )
        elif model == UNLIMITED_OCR_MODEL:
            viewer_unavailable_reason = "Unlimited-OCR currently has no supported coordinate result for visual linking."

    update("Completed")
    return {
        "model": model.id,
        "model_display_name": model.display_name,
        "task_id": task_id,
        "markdown_key": final_md_key,
        "markdown_url": final_md_url,
        "transferred_image_count": len(image_mapping),
        "image_url_mapping": image_mapping,
        "parse_result_available": parse_result_available,
        "viewer_key": viewer_key,
        "viewer_url": viewer_url,
        "viewer_unavailable_reason": viewer_unavailable_reason,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit a document URL to Baidu OCR, save Markdown and images into Aliyun OSS."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument("--file-url", help="Document URL accessible by Baidu.")
    input_group.add_argument("--local-file", help="Local file to upload to OSS and parse.")
    parser.add_argument("--file-name", help="Filename sent to Baidu.")
    parser.add_argument("--model", help="paddle_vl or unlimited_ocr.")
    parser.add_argument("--output-prefix", help="OSS output prefix.")
    parser.add_argument("--output-url-mode", choices=["public", "signed"])
    parser.add_argument("--signed-url-expires", type=int)
    parser.add_argument("--poll-interval", type=int)
    parser.add_argument("--max-wait", type=int)
    parser.add_argument("--model-option", action="append", default=[], help="key=value; repeatable")
    parser.add_argument("--generate-viewer", action="store_true", help="Generate a public PaddleOCR-VL viewer.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable result JSON.")
    return parser


def run(args: argparse.Namespace) -> dict:
    config = load_config(args.config)
    if args.local_file:
        model = get_model(str(args.model or config_value(config, "model", default="paddle_vl")))
        if model == PADDLE_VL_MODEL and (args.output_url_mode or config_value(config, "output_url_mode", default="signed")) != "public":
            raise ValueError("Local PaddleOCR-VL processing requires output_url_mode=public for its public viewer.")
        upload = upload_local_file_to_oss(config, args.local_file)
        result = run_document(
            config=config,
            file_url=upload["source_url"],
            file_name=args.file_name or upload["source_file_name"],
            model_name=model.id,
            output_prefix_base=args.output_prefix,
            output_url_mode=args.output_url_mode,
            signed_url_expires=args.signed_url_expires,
            poll_interval=args.poll_interval,
            max_wait=args.max_wait,
            model_options=parse_key_value_options(args.model_option),
            generate_viewer=model == PADDLE_VL_MODEL,
        )
        return {**upload, **result}
    file_url = args.file_url or config_value(config, "input_file_url")
    if not file_url:
        raise RuntimeError("Missing input file URL. Pass --file-url or set input_file_url in config.")
    return run_document(
        config=config,
        file_url=str(file_url),
        file_name=args.file_name or config_value(config, "file_name"),
        model_name=args.model,
        output_prefix_base=args.output_prefix,
        output_url_mode=args.output_url_mode,
        signed_url_expires=args.signed_url_expires,
        poll_interval=args.poll_interval,
        max_wait=args.max_wait,
        model_options=parse_key_value_options(args.model_option),
        generate_viewer=args.generate_viewer,
    )


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = run(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for key in ("task_id", "model_display_name", "markdown_key", "markdown_url", "viewer_url"):
            if result.get(key):
                print(f"{key}: {result[key]}")
        print(f"transferred_image_count: {result['transferred_image_count']}")
        if result["viewer_unavailable_reason"]:
            print(result["viewer_unavailable_reason"])


if __name__ == "__main__":
    main()
