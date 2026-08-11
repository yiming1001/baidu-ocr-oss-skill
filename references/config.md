# Configuration

The Skill has a dedicated private config area:

```text
config/local.example.json  # template
config/local.json          # default private config, gitignored
config/*.private.json      # optional named private configs, gitignored
```

For this local machine, prefer `config/local.json`. Do not commit or share it.

## Current Limits

- Output storage must be Aliyun OSS. This Skill currently uses the `oss2` Python SDK only.
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
  "output_url_mode": "public",
  "signed_url_expires": 604800,
  "poll_interval": 5,
  "max_wait": 1800
}
```

Command-line options override config values.

`input_prefix` is used by `--local-file`. Each local source file is uploaded below this prefix with a unique key and object-level `public-read` ACL. Existing objects below the prefix are not modified. Keep confidential documents outside this prefix.

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

## Output URL Modes

`public`:

- Result Markdown and extracted images are uploaded with object ACL `public-read`.
- Markdown contains stable public OSS URLs.
- This does not change the whole bucket ACL.

`signed`:

- Result objects remain private.
- Markdown contains signed URLs.
- URLs expire according to `--signed-url-expires`.

`--local-file --model paddle_vl` requires `public` output, because it publishes permanent public Markdown and an interactive `viewer.html`.

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
