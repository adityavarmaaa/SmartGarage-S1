"""Record actual requests against a temporary, publishing-disabled Uvicorn service."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import cv2
import httpx
import numpy as np
import yaml

from s1.api import app

METHODS = {"get", "head", "post", "put", "delete", "patch", "options", "trace"}


def save(path, data):
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


def browser_evidence(base, dest):
    chromium = shutil.which("chromium")
    checks = []
    try:
        from playwright.sync_api import sync_playwright
        if not chromium:
            raise RuntimeError("Chromium executable unavailable")
        with sync_playwright() as playwright, httpx.Client(timeout=12, follow_redirects=True) as assets:
            browser = playwright.chromium.launch(executable_path=chromium, headless=True,
                                                args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 1050})
                def supply_public_asset(route):
                    # Route the browser's public static assets through the workspace HTTP proxy.
                    response = assets.get(route.request.url)
                    route.fulfill(status=response.status_code, body=response.content,
                                  content_type=response.headers.get("content-type"))
                page.route("https://**/*", supply_public_asset)
                for check_id, url, selector, filename in (
                    ("S09", "/docs", "#swagger-ui .opblock", "swagger.png"),
                    ("S10", "/redoc", ".redoc-wrap", "redoc.png"),
                ):
                    try:
                        page.goto(base + url, wait_until="domcontentloaded", timeout=16000)
                        page.locator(selector).first.wait_for(state="visible", timeout=16000)
                        page.get_by_text("/api/upload", exact=False).first.wait_for(timeout=12000)
                        page.screenshot(path=dest / filename)
                        checks.append({"id": check_id, "status": "PASS", "evidence": filename})
                    except Exception as exc:
                        checks.append({"id": check_id, "status": "BLOCKED", "reason": str(exc)})
            finally:
                browser.close()
    except Exception as exc:
        checks.append({"id": "S09", "status": "BLOCKED", "reason": str(exc)})
        checks.append({"id": "S10", "status": "BLOCKED", "reason": str(exc)})
    save(dest / "browser-result.json", checks)


def collect(dest, scratch):
    dest.mkdir(parents=True, exist_ok=True)
    sample_cfg = yaml.safe_load(Path("config/config.local.yaml").read_text())
    camera = sample_cfg["cameras"][0]
    geometry = Path(camera["geometry_file"]).resolve()
    weights = Path(sample_cfg["detector"]["model_path"]).resolve()
    assert geometry.is_file() and weights.is_file()
    source = scratch / f"{uuid4().hex}.mp4"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 12, (320, 240))
    assert writer.isOpened()
    for i in range(84):
        frame = np.full((240, 320, 3), 74, np.uint8)
        cv2.rectangle(frame, (32 + i % 48, 90), (175 + i % 48, 156), (40, 150, 220), -1)
        writer.write(frame)
    writer.release()
    clip = source.read_bytes()
    assert len(clip) < 1024**2
    cfg_path = scratch / "run.yml"
    cfg = {
        "replay": "sync", "parking_area_id": uuid4().hex,
        "detector": {"model_path": str(weights), "device": "cpu", "max_fps": 12, "input_size": 320},
        "cameras": [{"camera_id": uuid4().hex, "source": str(source), "geometry_file": str(geometry)}],
        "global_reid": {"enabled": False}, "plate": {"enabled": False},
        "camera_motion": {"enabled": False}, "inference": {"motion_gate": {"enabled": False}},
        "exit_restore": {"preview_db_dir": str(scratch / "db")},
        "report": {"path": str(scratch / "report.json"), "plate_evidence_dir": str(scratch / "evidence")},
        "publisher": {"snapshot_dir": str(scratch / "snapshots"), "s2_url": "http://127.0.0.1:1/isolated"},
        "demo_video": {"enabled": True, "fps": 12, "tile_height": 240},
    }
    cfg_path.write_text(yaml.safe_dump(cfg))
    upload_dir = scratch / "uploads"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SP_", "S2_", "AWS_", "SUPABASE_", "SNAPSHOT_STORAGE"))}
    env.update(SMART_PARKING_CONFIG=str(cfg_path), SMART_PARKING_API_PUBLISH="0",
               SMART_PARKING_UPLOAD_DIR=str(upload_dir), SMART_PARKING_OUTPUT_DIR=str(scratch / "outputs"),
               SMART_PARKING_MAX_UPLOAD_MB="1", YOLO_CONFIG_DIR=str(scratch / "yolo"))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    results, swagger = [], []
    save(dest / "http-environment.json", {"base": base, "weights": weights.name, "input": "synthetic moving rectangle", "publishing": "disabled"})
    with (dest / "uvicorn.log").open("w") as log:
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "s1.api:app", "--fd", str(listener.fileno()),
             "--timeout-graceful-shutdown", "8"], pass_fds=(listener.fileno(),),
            env=env, stdout=log, stderr=log,
        )
        listener.close()
        try:
            with httpx.Client(base_url=base, timeout=25, trust_env=False) as client:
                def record(case, method, path, response, expected, scenario, code=None, body=None):
                    if body is None:
                        ct = response.headers.get("content-type", "")
                        if "json" in ct and case != "H04":
                            body = response.json()
                        else:
                            body = {"bytes": len(response.content), "content_type": ct}
                    actual_code = body.get("error", {}).get("code") if isinstance(body.get("error"), dict) else None
                    passed = response.status_code == expected and (code is None or code == actual_code)
                    results.append({"id": case, "method": method, "path": path, "scenario": scenario,
                                    "expected": expected, "actual": response.status_code,
                                    "expected_code": code, "code": actual_code,
                                    "headers": dict(response.headers), "body": body,
                                    "status": "PASS" if passed else "FAIL"})
                    save(dest / "http-responses.json", results)
                    assert passed, results[-1]
                    return response

                def req(case, method, path, expected, scenario, code=None, **kwargs):
                    return record(case, method, path, client.request(method, path, **kwargs),
                                  expected, scenario, code)

                for _ in range(180):
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except (httpx.ConnectError, httpx.ReadError):
                        assert server.poll() is None
                    time.sleep(.05)
                def check(check_id, assertion, evidence):
                    swagger.append({"id": check_id, "status": "PASS" if assertion else "FAIL", "evidence": evidence})
                    save(dest / "swagger-checks.json", swagger)
                    assert assertion, check_id

                req("H01", "GET", "/", 200, "Root service links")
                req("H02", "GET", "/health", 200, "Health and process startup")
                req("H03", "GET", "/api/status", 200, "Initial job status")
                spec = req("H04", "GET", "/openapi.json", 200, "Generated OpenAPI").json()
                save(dest / "openapi.json", spec)
                swagger_html = req("H05", "GET", "/docs", 200, "Swagger HTML").text
                redoc_html = req("H06", "GET", "/redoc", 200, "ReDoc HTML").text
                req("H07", "GET", "/docs/oauth2-redirect", 200, "Built-in OAuth redirect HTML")
                check("S01", "SwaggerUIBundle" in swagger_html and "/openapi.json" in swagger_html, "H05")
                check("S02", "redoc" in redoc_html.casefold() and "/openapi.json" in redoc_html, "H06")
                check("S03", spec["openapi"].startswith("3.") and "version" in spec["info"], "openapi.json")
                runtime = {(route.path, method.lower()) for route in app.routes
                           if getattr(route, "include_in_schema", False)
                           for method in route.methods}
                declared = {(path, method) for path, operations in spec["paths"].items()
                            for method in operations if method in METHODS}
                check("S04", declared == runtime, "openapi.json and runtime route declarations")
                upload = spec["paths"]["/api/upload"]["post"]
                body_ref = upload["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"].rsplit("/", 1)[-1]
                schemas = spec["components"]["schemas"]
                check("S05", "file" in schemas[body_ref]["required"] and "202" in upload["responses"], "upload schema")
                live_start = spec["paths"]["/api/live/start"]["post"]
                check("S06", "source" in schemas["LiveStartRequest"]["required"]
                      and schemas["LiveStartRequest"]["properties"]["source"]["examples"]
                      and "422" in live_start["responses"], "camera request and examples")
                stream = spec["paths"]["/api/output/stream"]
                get_responses = stream["get"]["responses"]
                head_responses = stream["head"]["responses"]
                check("S07", {"200", "206", "404", "409", "416"}.issubset(get_responses)
                      and get_responses.keys() == head_responses.keys()
                      and "video/mp4" in get_responses["200"]["content"]
                      and all("content" not in item for item in head_responses.values())
                      and "Content-Type" in head_responses["200"]["headers"],
                      "MP4 and bodyless HEAD definitions")
                check("S08", "ErrorResponse" in schemas and "HealthResponse" in schemas
                      and all(operation.get("description") or operation.get("summary")
                              for path in spec["paths"].values() for operation in path.values()), "response schemas and route descriptions")

                req("H08", "GET", "/api/video/uploaded", 200, "Empty upload inventory")
                req("H09", "GET", "/api/frames/latest", 501, "Documented unavailable frame endpoint", "API_FEATURE_UNAVAILABLE")
                req("H10", "GET", "/api/output/stream", 404, "No finalized output", "API_OUTPUT_UNAVAILABLE")
                absent = client.head("/api/output/stream")
                record("H11", "HEAD", "/api/output/stream", absent, 404, "Missing output HEAD has no body",
                       body={"bytes": len(absent.content)})
                req("H12", "POST", "/api/live/stop", 200, "Stop while idle")
                req("H13", "POST", "/api/live/start", 422, "Missing camera source", "API_VALIDATION", json={})
                req("H14", "POST", "/api/live/start", 422, "Wrong source type", "API_VALIDATION", json={"source": []})
                req("H15", "POST", "/api/live/start", 400, "Invalid source scheme", "API_INVALID_REQUEST", json={"source": "file:///invalid"})
                req("H16", "POST", "/api/live/start", 422, "Malformed JSON", "API_VALIDATION", content="{", headers={"Content-Type": "application/json"})
                req("H17", "GET", "/missing-" + uuid4().hex, 404, "Unknown route", "API_ROUTE_NOT_FOUND")
                req("H18", "PUT", "/health", 405, "Unsupported method", "API_REQUEST_FAILED")
                req("H19", "POST", "/api/upload", 422, "Missing multipart file", "API_VALIDATION")
                req("H20", "POST", "/api/upload", 415, "Unsupported upload extension", "API_UNSUPPORTED_VIDEO_FORMAT",
                    files={"file": (source.with_suffix(".txt").name, b"bad")})
                req("H21", "POST", "/api/upload", 400, "Zero-byte upload", "API_INVALID_REQUEST",
                    files={"file": (source.name, b"")})
                req("H22", "POST", "/api/upload", 413, "Over configured size", "API_UPLOAD_TOO_LARGE",
                    files={"file": (source.name, b"x" * (1024**2 + 1))})
                assert not list(upload_dir.iterdir())
                upload_dir.rmdir()
                upload_dir.write_text("controlled storage obstruction")
                req("H23", "POST", "/api/upload", 500, "Upload destination cannot be created", "API_STORAGE_FAILED",
                    files={"file": (source.name, clip)})
                req("H24", "GET", "/api/video/uploaded", 500, "Upload inventory internal exception", "API_INTERNAL")
                upload_dir.unlink()
                req("H25", "GET", "/api/video/uploaded", 200, "Inventory recovers after obstruction removal")
                def post(case, contents, scenario):
                    return req(case, "POST", "/api/upload?replay=sync&no_plates=true", 202,
                               scenario, files={"file": (source.name, contents)}).json()["job_id"]
                def wait(expected):
                    deadline = time.monotonic() + 24
                    while time.monotonic() < deadline:
                        response = client.get("/api/status")
                        state = response.json()
                        if expected(state):
                            return response
                        time.sleep(.04)
                    raise AssertionError(state)
                def terminal(case, job, expected, scenario, code=None):
                    response = wait(lambda r: r["job_id"] == job and not r["worker_alive"])
                    record(case, "GET", "/api/status", response, 200, scenario)
                    state = response.json()
                    assert state["state"] == expected, state
                    if code:
                        assert state["diagnostic"]["code"] == code
                    return state

                cfg_path.write_text("detector:\n  max_fps: 0\n")
                job = req("H26", "POST", "/api/live/start", 202,
                          "Accepted camera job with a controlled invalid configuration",
                          json={"source": "0", "no_plates": True}).json()["job_id"]
                terminal("H27", job, "error", "Asynchronous configuration failure", "CONFIG_VALIDATION_FAILED")
                cfg_path.write_text(yaml.safe_dump(cfg))
                job = post("H28", b"corrupt video", "Queued corrupt video")
                terminal("H29", job, "error", "Asynchronous capture failure", "MULTICAMERA_NO_CAMERAS_INITIALIZED")
                job = post("H30", clip, "Real CPU processing job")
                ready = wait(lambda r: r.get("live_output_url") or not r["worker_alive"]).json()
                assert ready["state"] == "running"
                req("H31", "POST", "/api/live/start", 409, "Valid camera request conflicts with active upload", "API_JOB_ACTIVE",
                    json={"source": "0", "no_plates": True})
                req("H32", "GET", "/api/output/stream", 409, "Final video pending", "API_OUTPUT_PENDING")
                req("H33", "GET", "/api/output/stream?live=invalid", 422, "Invalid stream query", "API_VALIDATION")
                with client.stream("GET", ready["live_output_url"]) as live:
                    output = b""
                    iterator = live.iter_bytes()
                    for chunk in iterator:
                        output += chunk
                        end = output.find(b"\xff\xd9")
                        if end >= 0:
                            break
                    start = output.index(b"\xff\xd8")
                    jpeg = output[start:end + 2]
                    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                    assert image is not None
                    (dest / "live-preview.jpg").write_bytes(jpeg)
                    must_stop = req("H34", "POST", "/api/live/stop", 200, "Cancellation while MJPEG response is open")
                    assert must_stop.json()["state"] == "cancelling"
                    remaining = b"".join(iterator)
                    assert remaining.endswith(b"--frame--\r\n")
                    record("H35", "GET", ready["live_output_url"], live, 200, "Live JPEG decoded and stream ended",
                           body={"jpeg_bytes": len(jpeg), "decoded_shape": image.shape, "closed_boundary": True})
                final = terminal("H36", job, "cancelled", "Pipeline released worker slot")
                assert final["output_url"]
                req("H37", "HEAD", final["output_url"], 200, "MP4 response headers")
                response = req("H38", "GET", final["output_url"], 200, "Playable partial MP4")
                assert response.headers["content-type"] == "video/mp4"
                (dest / "cancelled-output.mp4").write_bytes(response.content)
                decoder = cv2.VideoCapture(str(dest / "cancelled-output.mp4"))
                ok, image = decoder.read()
                decoder.release()
                assert ok and image is not None
                ranged = req("H39", "GET", final["output_url"], 206, "Byte range", headers={"Range": "bytes=0-31"})
                assert ranged.content == response.content[:32]
                unsatisfied = req("H40", "GET", final["output_url"], 416, "Range beyond EOF", headers={"Range": "bytes=999999999-"})
                assert unsatisfied.headers["content-range"] == f"bytes */{len(response.content)}"
                assert not unsatisfied.content
                req("H41", "GET", final["output_url"], 400, "Malformed range", headers={"Range": "bytes=bad"})
                req("H42", "GET", ready["live_output_url"], 404, "Live stream after cancellation", "API_OUTPUT_UNAVAILABLE")
                req("H43", "GET", "/api/video/uploaded", 200, "Accepted upload inventory")
                report = json.loads((scratch / "report.json").read_text())
                assert report["processing_status"] == "cancelled" and not report["service2_events"]
                save(dest / "cancelled-report.json", report)
                req("H44", "GET", "/health", 200, "Service recovered after repeated errors and cancellation")
                browser_evidence(base, dest)
        finally:
            server.terminate()
            server.wait(timeout=15)
            save(dest / "server-process.json", {"shutdown_return_code": server.returncode})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    opt = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        collect(opt.output.resolve(), Path(tmp))
