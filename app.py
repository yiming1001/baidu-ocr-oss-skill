#!/usr/bin/env python3
"""百度 OCR + 阿里云 OSS 的本地可视化网页界面。

在浏览器里操作：上传本地文件或粘贴 URL、选择模型、查看实时进度、
预览 OCR 结果（Markdown 链接 + PaddleOCR-VL 可视化查看器）。
"""

import threading
import traceback
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

# 复用现有脚本里的核心逻辑
from scripts.baidu_ocr_oss import (
    PADDLE_VL_MODEL,
    config_value,
    get_model,
    load_config,
    run_document,
    upload_local_file_to_oss,
)

SKILL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SKILL_DIR / "config" / "local.json"
UPLOAD_DIR = SKILL_DIR / ".uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
RESULTS_DIR = SKILL_DIR / ".results"
RESULTS_DIR.mkdir(exist_ok=True)

app = Flask(__name__, template_folder=str(SKILL_DIR / "templates"))
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB 上传上限

# 任务存储：job_id -> {status, messages, result, error}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _new_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "running", "messages": [], "result": None, "error": None}
    return job_id


def _append_message(job_id: str, message: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job["messages"].append(message)


def _finish_job(job_id: str, result=None, error=None) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        if error is not None:
            job["status"] = "error"
            job["error"] = error
        else:
            job["status"] = "done"
            job["result"] = result


def _run_job(job_id: str, params: dict) -> None:
    """后台线程：执行完整的 OCR 工作流。"""
    try:
        config = load_config(str(CONFIG_PATH))

        def status_callback(message: str) -> None:
            _append_message(job_id, message)

        model = get_model(params["model"])
        storage_mode = params["storage_mode"]
        output_url_mode = params["output_url_mode"]
        generate_viewer = params["generate_viewer"]

        # paddle_vl 生成可视化查看器时必须用 public 模式
        if storage_mode == "cloud" and generate_viewer and model == PADDLE_VL_MODEL and output_url_mode != "public":
            output_url_mode = "public"
            _append_message(job_id, "提示：可视化查看器需要 public 模式，已自动切换为 public")

        merged = {}
        file_url = None
        if params.get("local_path"):
            if storage_mode == "local":
                file_name = Path(params["local_path"]).name
                _append_message(job_id, "本地结果模式：正在将文件直接提交给百度 OCR")
            else:
                _append_message(job_id, "正在上传本地文件到 OSS ...")
                upload = upload_local_file_to_oss(config, params["local_path"], status_callback=status_callback)
                merged.update(upload)
                file_url = upload["source_url"]
                file_name = upload["source_file_name"]
                _append_message(job_id, f"上传完成：{file_name}")
        else:
            file_url = params["file_url"]
            file_name = params.get("file_name") or None

        result = run_document(
            config=config,
            file_url=file_url,
            file_name=file_name,
            model_name=model.id,
            output_url_mode=output_url_mode,
            generate_viewer=generate_viewer,
            status_callback=status_callback,
            storage_mode=storage_mode,
            local_output_dir=str(RESULTS_DIR),
            local_url_prefix="/results",
            local_file=params.get("local_path") if storage_mode == "local" else None,
        )
        merged.update(result)
        _finish_job(job_id, result=merged)
    except Exception as exc:  # noqa: BLE001 — 界面需要把任何错误回传给用户
        _append_message(job_id, f"出错：{exc}")
        _finish_job(job_id, error=f"{exc}\n\n{traceback.format_exc()}")
    finally:
        # 清理临时上传文件
        local_path = params.get("local_path")
        if local_path:
            try:
                Path(local_path).unlink(missing_ok=True)
            except OSError:
                pass


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/results/<path:result_path>")
def local_result(result_path):
    return send_from_directory(RESULTS_DIR, result_path)


@app.route("/api/config")
def api_config():
    """返回非敏感的默认配置，用于预填界面（不含任何密钥）。"""
    config = load_config(str(CONFIG_PATH))
    storage_mode = config_value(config, "storage_mode", default="cloud")
    return jsonify(
        {
            "configured": bool(config.get("baidu_api_key") and (storage_mode == "local" or config.get("oss_bucket"))),
            "model": config_value(config, "model", default="paddle_vl"),
            "storage_mode": storage_mode,
            "output_url_mode": config_value(config, "output_url_mode", default="public"),
            "oss_bucket": config_value(config, "oss_bucket", default=""),
            "input_file_url": config_value(config, "input_file_url", default=""),
        }
    )


@app.route("/api/run", methods=["POST"])
def api_run():
    mode = request.form.get("mode", "file")
    model = request.form.get("model", "paddle_vl")
    storage_mode = request.form.get("storage_mode", "cloud")
    output_url_mode = request.form.get("output_url_mode", "public")
    generate_viewer = request.form.get("generate_viewer", "true").lower() == "true"

    params = {
        "model": model,
        "storage_mode": storage_mode,
        "output_url_mode": output_url_mode,
        "generate_viewer": generate_viewer,
    }

    if mode == "file":
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify({"error": "没有选择文件"}), 400
        safe_name = secure_filename(uploaded.filename) or "upload"
        # 保留原始文件名（secure_filename 可能会丢中文），做兜底
        original = uploaded.filename
        save_name = f"{uuid.uuid4().hex[:8]}-{safe_name}"
        save_path = UPLOAD_DIR / save_name
        uploaded.save(str(save_path))
        # 用原始扩展名重命名以保留后缀（secure_filename 会保留扩展名，通常没问题）
        params["local_path"] = str(save_path)
        params["original_name"] = original
    else:
        file_url = request.form.get("file_url", "").strip()
        if not file_url:
            return jsonify({"error": "URL 不能为空"}), 400
        params["file_url"] = file_url
        file_name = request.form.get("file_name", "").strip()
        if file_name:
            params["file_name"] = file_name

    job_id = _new_job()
    thread = threading.Thread(target=_run_job, args=(job_id, params), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "任务不存在"}), 404
        # 返回副本，避免并发修改
        return jsonify(
            {
                "status": job["status"],
                "messages": list(job["messages"]),
                "result": job["result"],
                "error": job["error"],
            }
        )


if __name__ == "__main__":
    print("=" * 60)
    print("  百度 OCR 可视化界面已启动")
    print("  请在浏览器打开： http://127.0.0.1:5000")
    print("  按 Ctrl+C 停止服务")
    print("=" * 60)
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)
