# Configuration

The Skill has a dedicated private config area:

```text
config/local.example.json  # template
config/local.json          # default private config, gitignored
config/*.private.json      # optional named private configs, gitignored
```

For this local machine, prefer `config/local.json`. Do not commit or share it.

## Current Limits

- Cloud output storage uses Aliyun OSS. Local mode stores generated Markdown, images, and viewer files beneath `.results/`.
- Supported Baidu models are limited to `paddle_vl` and `unlimited_ocr`.
- The selected Baidu OCR API must be activated on the Baidu Intelligent Cloud account behind the configured API Key / Secret Key.
- Aliyun credentials must have upload permission for the target bucket. Public output also requires permission to set object ACL to `public-read` during upload.

## Config File Keys

```json
{
  "baidu_api_key": "your Baidu API Key",
  "baidu_secret_key": "your Baidu Secret Key",
  "oss_access_key_id": "your Aliyun AccessKeyId",
  "oss_access_key_secret": "your Aliyun AccessKeySecret",
  "oss_endpoint": "https://oss-cn-beijing.aliyuncs.com",
  "oss_bucket": "your-bucket",
  "model": "paddle_vl",
  "input_file_url": "",
  "input_prefix": "ocr-test/",
  "output_prefix": "ocr_result/api/",
  "storage_mode": "cloud",
  "local_output_dir": ".results",
  "local_url_prefix": "/results",
  "output_url_mode": "public",
  "signed_url_expires": 604800,
  "poll_interval": 5,
  "max_wait": 1800
}
```

Command-line options override config values.

`input_prefix` is used by `--local-file` in `cloud` mode. Each local source file is uploaded below this prefix with a unique key and object-level `public-read` ACL. Existing objects below the prefix are not modified. In `local` mode, `--local-file` uses Baidu's `file_data` parameter instead.

## Environment Variable Fallback

If no config file is present, the runner can still read:

```bash
export BAIDU_API_KEY="your Baidu API Key"
export BAIDU_SECRET_KEY="your Baidu Secret Key"
export OSS_ACCESS_KEY_ID="your Aliyun AccessKeyId"
export OSS_ACCESS_KEY_SECRET="your Aliyun AccessKeySecret"
export OSS_ENDPOINT="https://oss-cn-beijing.aliyuncs.com"
export OSS_BUCKET="your-bucket"
```

## Input URL

The OCR input file must be accessible by Baidu's servers:

- Public OSS object URL is fine.
- Private OSS object signed URL is fine if the expiration covers the OCR processing time.
- Keep the URL under Baidu's documented length limit.
- Avoid hotlink protection, CDN auth, IP allowlists, or referer rules that block Baidu.

## Storage Modes

`cloud` is the default. Result Markdown, images, and viewer HTML are uploaded to OSS.

`local` writes result Markdown, images, viewer HTML, and the original file under `local_output_dir` (default `.results/`). The selected local file is sent directly to Baidu as the `file_data` Base64 request parameter; no OSS input or output object is created. When using the Flask console, results are available only from the same machine at `/results/...`.

For direct `file_data` upload, Baidu limits images to 10 MB and other supported documents to 50 MB. Larger documents require `cloud` mode with a `file_url`.

## Output URL Modes

`public`:

- Result Markdown and extracted images are uploaded with object ACL `public-read`.
- Markdown contains stable public OSS URLs.
- This does not change the whole bucket ACL.

`signed`:

- Result objects remain private.
- Markdown contains signed URLs.
- URLs expire according to `--signed-url-expires`.

`--local-file --model paddle_vl` requires `public` output only in `cloud` mode. Local mode serves the generated viewer from the local Flask service.

## Example

```bash
python3 scripts/baidu_ocr_oss.py \
  --config config/local.json \
  --file-url "https://example-bucket.oss-cn-beijing.aliyuncs.com/docs/test.pdf?Expires=..." \
  --model paddle_vl \
  --output-prefix "ocr_result/api/" \
  --output-url-mode public
```

## Local File Workflow

```bash
python3 scripts/baidu_ocr_oss.py \
  --config config/local.json \
  --local-file "/absolute/path/to/document.pdf" \
  --model paddle_vl \
  --json
```

The script uploads only the named file, sets that new OSS object to `public-read`, and returns public `viewer_url` and `markdown_url` values.
