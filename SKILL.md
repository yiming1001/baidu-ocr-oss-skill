---
name: baidu-ocr-oss-markdown-viewer
description: Upload local files to Aliyun OSS, submit Baidu PaddleOCR-VL or Unlimited-OCR, save Markdown assets to OSS, and create a public Markdown-to-original visual review page for PaddleOCR-VL. Use when the user wants to parse PDFs/images/docs with Baidu OCR, compare PaddleOCR-VL vs Unlimited-OCR, turn a local file into public OSS-hosted Markdown, or publish an interactive Markdown OCR result page.
---

# Baidu OCR OSS Private

## What This Skill Does

This skill runs a private local workflow around Baidu Intelligent Cloud OCR:

1. Upload one local file to the configured OSS input prefix with public-read ACL, or submit an existing OSS URL from the CLI.
3. Choose either `paddle_vl` or `unlimited_ocr`, then poll the async task until it succeeds or fails.
4. Download Baidu's Markdown result, transfer temporary images into the user's OSS bucket, and upload final Markdown.
5. For `paddle_vl`, request line coordinates and publish one public `viewer.html` beside the result Markdown.

This is not offline OCR. Baidu still performs the OCR. Keep all credentials in the local, gitignored `config/local.json` file.

## Current Limits

- Storage is Aliyun OSS only. The runner currently depends on the `oss2` SDK and does not support S3, Tencent COS, Huawei OBS, local filesystem output, or other object storage providers.
- Only two Baidu document parsing models are wired in: `paddle_vl` and `unlimited_ocr`.
- The corresponding Baidu OCR API access must be enabled in the user's Baidu Intelligent Cloud account, and the configured API Key / Secret Key must have permission to call the selected model.
- The Aliyun AccessKey must have permission to upload input/result objects to the configured bucket. Local file uploads and public outputs require `public-read` ACL permission for the new object.
- The original-to-text viewer is available only for `paddle_vl`. Unlimited-OCR currently does not provide a supported coordinate result for this interaction.
- The visual viewer loads public PDF or image inputs from OSS. It has no permanent viewer for Office-style inputs.

## Model Choice

- `paddle_vl`: Baidu PaddleOCR-VL document parsing. Prefer for complex layout understanding, richer page structure, charts, tables, and mixed visual content.
- `unlimited_ocr`: Baidu Unlimited-OCR document parsing. Prefer for stable general document text extraction and Markdown-oriented conversion.

Both are async document parsing APIs and both can return `markdown_url`. PaddleOCR-VL additionally returns `parse_result_url`, which the script uses immediately to build coordinate-linked review data.

## Required Credentials

The Skill has a dedicated config area:

- `config/local.example.json`: safe template.
- `config/local.json`: private local config for this machine.
- `config/*.private.json`: optional extra private configs.

Read `references/config.md`. Prefer `config/local.json` for this user's machine. Environment variables remain supported as a fallback:

```bash
export BAIDU_API_KEY="..."
export BAIDU_SECRET_KEY="..."
export OSS_ACCESS_KEY_ID="..."
export OSS_ACCESS_KEY_SECRET="..."
export OSS_ENDPOINT="https://oss-cn-beijing.aliyuncs.com"
export OSS_BUCKET="..."
```

If dependencies are missing, install them in the active project venv or a temporary venv:

```bash
python3 -m pip install requests oss2
```

## Primary Command

Use a local file. This is the default Skill workflow; no URL entry or local web service is needed:

```bash
python3 scripts/baidu_ocr_oss.py \
  --config config/local.json \
  --local-file "/absolute/path/to/document.pdf" \
  --model paddle_vl \
  --json
```

The script uploads the file to `input_prefix` with `public-read` ACL, parses it, and returns `viewer_url` plus `markdown_url`. A local `paddle_vl` upload always generates the interactive Markdown viewer.

## Existing URL Input

Use `scripts/baidu_ocr_oss.py`:

```bash
python3 scripts/baidu_ocr_oss.py \
  --config config/local.json \
  --file-url "https://bucket.oss-cn-beijing.aliyuncs.com/input.pdf?..." \
  --model paddle_vl \
  --output-prefix "ocr_result/api/" \
  --output-url-mode public
```

Common options:

- `--config`: JSON config file. Defaults to `config/local.json` inside this Skill.
- `--model paddle_vl` or `--model unlimited_ocr`.
- `--local-file`: local document path. The Skill uploads it to OSS and makes only this new object public.
- `--file-url`: an existing URL Baidu can access. If omitted, uses `input_file_url` from config.
- `--file-name`: optional. Defaults to the decoded basename from `--file-url`.
- `--output-prefix`: OSS folder prefix for Markdown and images.
- `--output-url-mode public`: upload result objects with `public-read` and write permanent public OSS URLs into Markdown.
- `--output-url-mode signed`: keep result objects private and write signed URLs into Markdown.
- `--json`: emit machine-readable result JSON.
- `--generate-viewer`: create a public PaddleOCR-VL `viewer.html` when using an existing URL; this requires `--output-url-mode public` and a permanently public source document URL.

## Operational Notes

- Local uploads use a unique name below `input_prefix`, so similarly named source files never overwrite one another.
- Use `public` mode only when the input, output Markdown, extracted images, and PaddleOCR-VL viewer are safe to expose publicly.
- If `public` mode fails with OSS `AccessDenied`, the AccessKey or bucket policy does not allow per-object public ACL. Use `signed` mode or update OSS permissions.
- Baidu temporary result URLs expire. Always save the final Markdown and images into the user's own OSS when the output needs to persist.
- Avoid rerunning OCR if a previous task already succeeded. Reuse the task result if available, or reprocess only the Markdown and image replacement step when possible.

## Quality Checks

After editing this skill, run:

```bash
python3 /path/to/skill-creator/scripts/quick_validate.py .
python3 scripts/baidu_ocr_oss.py --help
```
