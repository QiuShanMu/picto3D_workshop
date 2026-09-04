from __future__ import annotations

"""Production WebUI: batch board + capture wizard + generate + validate/size workbench.

Aggregates filesystem state into a single shell app with sidebar navigation.
The live-camera wizard reuses the capture module's CameraWorker.
"""

import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent.parent.parent

from pipeline.webui.state import scan_batch, sku_state, gen_queue_state, validate_workbench, sku_detail, DEFAULT_CAPTURE_ROOT
import pipeline.webui.actions as actions
from pipeline.capture.webapp import CameraWorker, WebOptions


def create_app(
    batch: str = "0812",
    *,
    cam_opts: WebOptions | None = None,
    start_camera: bool = False,
    provider: str = "auto",
) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["BATCH"] = batch
    app.config["PROVIDER"] = provider

    worker = None
    if start_camera:
        cam_opts = cam_opts or WebOptions(batch_id=batch, sku_id="")
        worker = CameraWorker(cam_opts)
        worker.start()

    # ---- pages ----
    @app.route("/")
    def index():
        return render_template("board.html", page="board", batch=batch)

    @app.route("/capture")
    def capture_page():
        sku = request.args.get("sku", "")
        start_index = request.args.get("index", "01")
        if start_index not in {f"{n:02d}" for n in range(1, 11)}:
            start_index = "01"
        return render_template(
            "capture.html", page="capture", batch=batch, sku=sku, start_index=start_index,
        )

    @app.route("/generate")
    def generate_page():
        return render_template("generate.html", page="generate", batch=batch)

    @app.route("/validate")
    def validate_page():
        sku = request.args.get("sku", "")
        return render_template("validate.html", page="validate", batch=batch, sku=sku)

    @app.route("/archive")
    def archive_page():
        return render_template("archive.html", page="archive", batch=batch)

    @app.route("/viewer3d")
    def viewer3d_page():
        sku = request.args.get("sku", "APP-0821-001")
        return render_template("viewer3d.html", page="viewer3d", batch=batch, sku=sku)

    @app.route("/sku/<sku_id>")
    def sku_detail_page(sku_id: str):
        # independent SKU detail: image-browser around captured views + stage state
        return render_template("skudetail.html", page="board", batch=batch, sku=sku_id)

    # ---- read APIs ----
    @app.route("/api/batch/<batch_id>")
    def api_batch(batch_id: str):
        return jsonify(scan_batch(batch_id))

    @app.route("/api/sku/<batch_id>/<sku_id>")
    def api_sku(batch_id: str, sku_id: str):
        return jsonify(sku_state(batch_id, sku_id))

    @app.route("/api/sku-detail/<batch_id>/<sku_id>")
    def api_sku_detail(batch_id: str, sku_id: str):
        return jsonify(sku_detail(batch_id, sku_id))

    @app.route("/api/capture-img/<batch_id>/<sku_id>/<path:rel>")
    def api_capture_img(batch_id: str, sku_id: str, rel: str):
        from flask import send_file
        from pathlib import Path as _P
        cap_root = _P(DEFAULT_CAPTURE_ROOT)
        p = (cap_root / batch_id / sku_id / rel).resolve()
        # guard: only serve files inside this sku's capture dir
        base = (cap_root / batch_id / sku_id).resolve()
        if not str(p).startswith(str(base)):
            return jsonify({"error": "forbidden"}), 403
        if not p.is_file():
            return jsonify({"error": f"not found: {p}"}), 404
        return send_file(p)

    @app.route("/api/capture-shots/<batch_id>/<sku_id>")
    def api_capture_shots(batch_id: str, sku_id: str):
        # Already-captured frames for the capture workbench, so the operator can
        # review/align angles mid-shoot without leaving the page.
        from pipeline.views import SLOT_BY_INDEX
        st = sku_state(batch_id, sku_id)
        frames = st.get("frames", [])
        shots = []
        for f in frames:
            if not f.get("ok"):
                continue
            idx = f.get("index")
            rel = f.get("color") or ""
            slot = SLOT_BY_INDEX.get(idx)
            shots.append({
                "index": idx,
                "yaw_deg": f.get("yaw_deg", slot.degrees if slot else None),
                "pose": slot.pose_name if slot else "",
                "degrees": slot.degrees if slot else None,
                "url": f"/api/capture-img/{batch_id}/{sku_id}/{rel}" if rel else "",
            })
        return jsonify({"sku_id": sku_id, "batch_id": batch_id, "shots": shots})

    @app.route("/api/gen/<batch_id>")
    def api_gen(batch_id: str):
        return jsonify(gen_queue_state(batch_id))

    @app.route("/api/validate/<batch_id>")
    def api_validate(batch_id: str):
        return jsonify(validate_workbench(batch_id))

    @app.route("/api/validate/<batch_id>/<sku_id>/<version>")
    def api_validate_item(batch_id: str, sku_id: str, version: str):
        import json as _json
        rep = BASE_DIR / "data" / "work" / batch_id / sku_id / version / "report.json"
        if rep.exists():
            return jsonify(_json.loads(rep.read_text(encoding="utf-8")))
        return jsonify({"verdict": "unvalidated", "checks": [], "metrics": {}})

    @app.route("/api/glb/<batch_id>/<sku_id>/<version>")
    def api_glb(batch_id: str, sku_id: str, version: str):
        from flask import send_file
        p = BASE_DIR / "data" / "work" / batch_id / sku_id / version / "model.glb"
        if not p.exists():
            return jsonify({"error": f"model not found: {p}"}), 404
        return send_file(p, mimetype="model/gltf-binary", as_attachment=False)

    @app.route("/api/archive")
    def api_archive():
        return jsonify(actions.list_archives())

    # ---- action APIs ----
    @app.route("/gen/build", methods=["POST"])
    def gen_build():
        payload = request.get_json(silent=True) or {}
        batch_id = payload.get("batch", batch)
        skus = payload.get("skus", [])
        provider = payload.get("provider", app.config["PROVIDER"])
        size_mm = payload.get("size_mm") or None
        result = actions.start_generate_task(batch_id, skus, provider=provider, size_mm=size_mm)
        return jsonify(result), 200 if result.get("ok") else 400

    @app.route("/gen/status/<batch_id>")
    def gen_status(batch_id: str):
        return jsonify(actions.get_generate_status(batch_id))

    @app.route("/sku/size", methods=["POST"])
    def sku_size_save():
        payload = request.get_json(silent=True) or {}
        result = actions.save_size_mm(
            payload.get("sku", ""), payload.get("batch", batch),
            payload.get("size_mm", ""),
        )
        return jsonify(result), 200 if result.get("ok") else 400

    @app.route("/capture/frame/delete", methods=["POST"])
    def capture_frame_delete():
        payload = request.get_json(silent=True) or {}
        result = actions.delete_capture_frame(
            payload.get("sku", ""),
            payload.get("batch", batch),
            payload.get("index", ""),
        )
        return jsonify(result), 200 if result.get("ok") else 400

    @app.route("/size/correct", methods=["POST"])
    @app.route("/size/apply", methods=["POST"])
    def size_correct():
        payload = request.get_json(silent=True) or {}
        result = actions.size_correct(
            payload.get("sku", ""), payload.get("batch", batch),
            payload.get("version", ""), payload.get("size_mm", ""),
        )
        return jsonify(result), 200 if result.get("ok") else 400

    @app.route("/archive/run", methods=["POST"])
    def archive_run():
        payload = request.get_json(silent=True) or {}
        result = actions.archive_sku_web(
            payload.get("sku", ""), payload.get("batch", batch),
            payload.get("version", ""), category=payload.get("category", "general"),
        )
        return jsonify(result), 200 if result.get("ok") else 400

    @app.route("/gen/rerun", methods=["POST"])
    def gen_rerun():
        payload = request.get_json(silent=True) or {}
        provider = payload.get("provider", app.config["PROVIDER"])
        result = actions.rerun_sku(
            payload.get("batch", batch), payload.get("sku", ""), provider=provider,
        )
        return jsonify(result), 200 if result.get("ok") else 400

    # ---- camera endpoints ----
    if worker is not None:
        @app.route("/stream")
        def stream():
            def gen():
                while True:
                    if worker.latest_jpeg is not None:
                        yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + worker.latest_jpeg + b"\r\n")
                    time.sleep(0.03)
            from flask import Response
            return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

        @app.route("/info")
        def info():
            return jsonify({"serial": worker.serial, "color": worker.color})

        @app.route("/camera/status")
        def camera_status():
            return jsonify(worker.status())

        @app.route("/camera/exposure", methods=["POST"])
        def camera_exposure():
            payload = request.get_json(silent=True) or {}
            result = worker.request_exposure(payload)
            return jsonify(result), 200 if result.get("ok") else 400

        @app.route("/capture", methods=["POST"])
        def capture():
            payload = request.get_json(silent=True) or {}
            result = worker.request_capture(payload)
            return jsonify(result), 200 if result.get("ok") else 400

        @app.route("/barcode", methods=["POST"])
        def barcode():
            payload = request.get_json(silent=True) or {}
            result = worker.request_barcode_capture(payload)
            return jsonify(result), 200 if result.get("ok") else 400

        @app.route("/sku/register", methods=["POST"])
        def sku_register():
            payload = request.get_json(silent=True) or {}
            result = worker.register_sku(
                payload.get("batch", batch),
                payload.get("sku", ""),
                payload.get("source", ""),
            )
            return jsonify(result), 200 if result.get("ok") else 400

        @app.route("/shading/calibrate", methods=["POST"])
        def shading_calibrate():
            result = worker.recalibrate_from_live()
            return jsonify(result), 200 if result.get("ok") else 400

    return app
