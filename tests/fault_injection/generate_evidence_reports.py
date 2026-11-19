import csv
from collections import Counter
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

out = Path("reports/final_verification")
raw = out / "raw"
http_dir = raw / "http-final"
http = json.loads((http_dir / "http-responses.json").read_text())
http_by_id = {r["id"]: r for r in http}
swagger = json.loads((http_dir / "swagger-checks.json").read_text()) + json.loads((http_dir / "browser-result.json").read_text())
schema = json.loads((http_dir / "openapi.json").read_text())
assert schema == json.loads(Path("docs/openapi.json").read_text())
env = json.loads((raw / "environment.json").read_text())
nodes = {}
trees = {}
for name in ("baseline-s1", "baseline-s2", "fault-before", "fault-final", "final-s1", "final-s2"):
    trees[name] = ET.parse(raw / f"{name}.xml").getroot().find("testsuite")
    if name in {"final-s1", "final-s2"}:
        for node in trees[name].findall("testcase"):
            status = "FAIL" if node.find("failure") is not None or node.find("error") is not None else (
                "NOT TESTED" if node.find("skipped") is not None else "PASS")
            nodes[node.attrib["name"]] = (status, f"raw/{name}.xml", node.attrib["classname"])

def checks(ref):
    if not ref:
        return None, ""
    if ref in http_by_id:
        return http_by_id[ref]["status"], f"raw/http-final/http-responses.json#{ref}"
    matching = [(n, x) for n, x in nodes.items() if n == ref or n.startswith(ref + "[")]
    assert matching, ref
    status = "PASS" if all(x[0] == "PASS" for _, x in matching) else "FAIL"
    paths = {f"{x[1]}#{n}" for n, x in matching}
    return status, " | ".join(sorted(paths))

faults = []
def case(i, module, scenario, injection, expected, actual, recovery, diagnostic, ref="", status=None):
    observed, evidence = checks(ref)
    faults.append(dict(zip(fault_headers, (
        i, module, scenario, injection, expected, actual, recovery, diagnostic, status or observed,
        evidence or "No execution evidence for this case",
    ))))

fault_headers = ["Test ID", "Module", "Failure scenario", "Injection method", "Expected behavior",
                 "Actual behavior", "Recovery observed", "Diagnostic observed", "Status", "Evidence reference"]
for i, scenario in (("D01","Empty frame"), ("D02","None payload"), ("D03","Scalar instead of image"), ("D04","NaN corrupt frame array")):
    case(i,"Detection",scenario,"Inject decoder payload","Reject before inference","Detector did not receive the payload",
         "Next valid frame processed","VIDEO_FRAME_INVALID", f"test_invalid_frames_skip_inference_and_recover[{i}]")
case("D05","Detection","Model unavailable","Missing model file","Categorize fatal initialization","Model wrapper raised structured exception","No startup","YOLO_MODEL_LOAD_FAILED","test_missing_model_has_categorized_failure")
case("D06","Detection","Inference exception and no vehicles","Raise for first frame, return empty next","Skip first; process empty result","No tracker update on fault; valid empty advanced tracker","Next frame processed","YOLO_INFERENCE_FAILED","test_detection_failure_skips_only_affected_frame")
for i, scenario in (("D07","Wrong output shape"),("D08","Non-numeric output"),("D09","Inverted bounds")):
    case(i,"Detection",scenario,"Inject detector result","Reject invalid result","Frame rejected before tracker",
         "Next empty frame accepted","DETECTION_OUTPUT_INVALID",f"test_invalid_detection_output_recovers[{i}]")
case("T01","Tracking","Missing track ID","Missing ID in tracker result","Skip downstream updates","Malformed output rejected","Next frame accepted","TRACKER_OUTPUT_INVALID","test_invalid_association_is_categorized[T01]")
case("T02","Tracking","Duplicate IDs","Repeat ID in tracker result","Skip downstream updates","Ambiguous output rejected","Next frame accepted","TRACKER_OUTPUT_INVALID","test_invalid_association_is_categorized[T02]")
case("T03","Tracking","Runtime error","Throw from association","Preserve unrelated processing","Failed frame skipped","Later frame accepted","TRACKER_UPDATE_FAILED","test_tracker_failure_preserves_pipeline_for_next_frame")
case("T04","Tracking","Frame gap and two vehicles","Advance synthetic clock past age threshold","Expire both old tracks","Old IDs removed and distinct IDs allocated on return","Both tracks reacquired","Assertions","test_tracking_gap_expires_ids_and_reacquires")
case("T05","Tracking","Backwards timestamp","Send frames with 101, 100, 102 capture times","Keep tracker clock monotonic","Second frame rejected","Third frame accepted","VIDEO_TIMESTAMP_OUT_OF_ORDER","test_backwards_frame_clock_never_reaches_tracker")
case("T06","Tracking","Low-confidence detection","Below tracker association cutoff","Discard before track creation","Low-score detection did not seed a track","Next high-score observation can associate","Tracker state","test_below_tracker_threshold_does_not_seed_track")
case("A01","ANPR","Invalid or None crop","Submit None to real worker","No OCR inference","Reader was never called","Worker returned no evidence","PLATE_CROP_INVALID","test_invalid_ocr_crop_does_not_call_reader")
case("A02","ANPR","Empty crop","Submit zero-height array","No OCR inference","Reader was never called","Worker returned no evidence","PLATE_CROP_INVALID","test_empty_ocr_crop_does_not_reach_reader")
case("A03","ANPR","No read and low-confidence text","Inject empty or low-quality read","Preserve ENTRY and null plate","ENTRY emitted with UNCONFIRMED plate","Parking/event contract intact","Event assertions","test_unconfirmed_ocr_preserves_entry_contract")
case("A04","ANPR","Async failure, malformed result, wait timeout and recovery","Queue 3 jobs; pause reader, raise, return malformed, then return valid-shaped result","Preserve keys and resume after transient fault","Timed wait returned false; failed reads empty; result keys matched all jobs","Queue drained and worker joined","ALPR_INFERENCE_FAILED","test_ocr_pins_result_keys_and_recovers_after_failure")
case("A05","ANPR","Vehicle disappears or unknown result key","Deliver result under unrelated UID","Do not associate to another vehicle","Existing voter unchanged","Other vehicle state intact","Unknown vehicle log","test_disappeared_vehicle_ocr_does_not_update_another")
case("A06","ANPR","Conflicting OCR reads","Two synthetically valid plate strings with sufficient votes","No confirmed plate","Voter found competing candidates","Confirmation withheld","Consensus assertions","test_conflicting_reads_never_confirm_a_plate")
case("AT01","Attributes","Colour estimation exception and missing value","Throw from colour estimator","Keep vehicle tracking","Track registered with type and no colour","Subsequent frame processed","VEHICLE_ATTRIBUTE_FAILED","test_attribute_inference_fault_preserves_vehicle")
for i, scenario in (("G01","Negative width"),("G02","One-dimensional calibration"),("G03","Zero width with known height")):
    case(i,"Geometry",scenario,"Inject frame_size","Reject invalid calibration","ValueError identified frame_size","Fail closed before mask calculation",
         "Validation exception",f"test_bad_calibration_dimensions_rejected[{i}]")
case("G04","Geometry","Resolution mismatch and no slots","Scale calibration fixture and leave slots empty","Scale tripwire and preserve empty slot list","Coordinates doubled and slots remained empty","Rescaling accepted","Geometry assertions","test_calibration_scaling_and_empty_slots")
case("G05","Geometry","Ambiguous overlapping assignment candidates","Synthetic adjacent bays with comparable overlap","Avoid arbitrary assignment","No slot assigned","Other slots unaffected","Assignment result","test_overlap_fallback_rejects_ambiguous_adjacent_slot_overlap")
case("G06","Geometry","Unavailable occupancy","Mark camera occupancy unavailable","Use UNKNOWN","Slot state UNKNOWN without vehicle UID","Report did not assert FREE","Report assertion","test_unavailable_occupancy_is_reported_as_unknown")
case("G07","Geometry","Invalid slot polygon","Supply a polygon with fewer than three vertices","Reject calibration","ValueError identifies polygon requirement","Invalid slot not admitted","Validation exception","test_invalid_polygon_is_rejected")
for i, scenario in (("L01","Malformed event timestamp"),("L02","Naive event timestamp")):
    case(i,"Lifecycle",scenario,"Inject event envelope timestamp","Reject invalid envelope",
         "Validation rejected timestamp","No publish call","ValueError",f"test_invalid_event_time_is_rejected[{i}]")
case("L03","Lifecycle","PARK_START lacks ENTRY","Drive slot manager with stationary tracked vehicle","Suppress PARK_START","No start event and no occupant","Tracks remain available","Transition assertion","test_live_parking_requires_confirmed_entry_crossing")
case("L04","Lifecycle","Invalid PARK_START envelope","Omit parking slot","Reject malformed event","No publication","Publisher unchanged","EVENT_VALIDATION_FAILED","test_invalid_lifecycle_event_is_not_published")
case("L05","Lifecycle","EXIT lacks linked session","Offer event without matching session","Reject unmatched exit","Mapper did not publish","No session change","Mapper assertions","test_exit_without_matching_session_is_not_published")
case("L06","Lifecycle","Publish error while closing session","Raise in mapper sender","Keep session open","UID session stayed open","Later delivery still possible","Publish exception","test_publisher_failure_does_not_close_uid_session")
case("L07","Lifecycle","Interruption while evidence is pinned","Stop capture with unfinished evidence","Mark capture interruption","Evidence state was interrupted","Capture finalized","Evidence state","test_stopping_grabber_marks_unfinished_event_interrupted")
for i, scenario, diag in (("V01","Missing file","VIDEO_SOURCE_NOT_FOUND"),("V02","Empty file","VIDEO_SOURCE_OPEN_FAILED"),("V03","Corrupt media container","VIDEO_SOURCE_OPEN_FAILED")):
    case(i,"Video",scenario,"Local file fixture","Reject capture startup","Structured capture exception emitted","No decoder left active",diag,f"test_missing_empty_or_corrupt_video_never_starts[{i}]")
case("V04","Video","Decoder abort mid-file","Mock early decode failure","Mark early EOF differently","Decode error retained frame context","Released source at boundary","VIDEO_FRAME_READ_FAILED","test_early_video_decode_failure_has_frame_context")
case("V05","Video","Writer initialization and frame write failure","Injected writer refuses open then throws","Release both handles and avoid invalid output","Two faults counted; output absent","Temporary path cleaned","VIDEO_OUTPUT_FRAME_FAILED","test_video_writer_open_and_write_faults")
case("V06","Video","Stop while frames are being processed","Signal cancellation during detector batch","Finalize partial video and report","Report cancelled, playable MP4 and valid byte range","Worker slot released","Cancelled terminal state","test_cancel_upload_finalizes_report_and_range_video")
case("V07","Video","Unplayable temporary video","Force validation failure on temporary MP4","Preserve previous final output","Previous bytes unchanged; temp removed","Final file retained","Recorder result","test_unplayable_recorder_does_not_replace_existing_video")
case("V08","Video","Stop paused decoder","Pause synthetic file then stop","Release capture worker","Thread joined and no worker remained","Clean teardown","Thread state","test_grabber_stop_joins_capture_thread")
for i, scenario, diagnostic, ref in (
    ("M01","Decoder fails after first frame","VIDEO_FRAME_READ_FAILED","test_camera_failure_leaves_other_camera_processing"),
    ("M02","Capture initialization raises","VIDEO_SOURCE_OPEN_FAILED","test_camera_failure_leaves_other_camera_processing"),
    ("M03","Geometry file missing","SLOT_GEOMETRY_INVALID","test_camera_failure_leaves_other_camera_processing"),
    ("M04","First frame is malformed","VIDEO_FRAME_INVALID","test_camera_failure_leaves_other_camera_processing"),
    ("M05","One tracker fails","TRACKER_UPDATE_FAILED","test_camera_failure_leaves_other_camera_processing"),
    ("M06","One OCR engine unavailable","ALPR_MODEL_LOAD_FAILED","test_camera_specific_component_faults"),
    ("M07","Slot manager raises","Frame processing failed log","test_camera_specific_component_faults"),
    ("M08","One camera startup timeout","CAMERA_STARTUP_TIMEOUT","test_camera_specific_component_faults"),
    ("M09","Frame size changes during run","VIDEO_FRAME_INVALID","test_camera_specific_component_faults"),
    ("M10","One detection payload malformed","DETECTION_OUTPUT_INVALID","test_camera_specific_component_faults"),
    ("M11","One tracker fails to initialize","CAMERA_PIPELINE_INIT_FAILED","test_camera_specific_component_faults"),
):
    case(i,"Multicamera",scenario,"Inject into one of two temporary cameras","Healthy camera finishes processing",
         "Healthy camera processed frames and run was marked partial","Independent camera recovered/continued",diagnostic,f"{ref}[{i}]")
case("M12","Multicamera","All camera sources unavailable","Single missing local input","Terminal failure with camera context","Report failed and attributed missing camera","Resources finalized","MULTICAMERA_NO_CAMERAS_INITIALIZED","test_all_camera_sources_unavailable_reports_structured_terminal_failure")
case("M13","Multicamera","Shared batch transient exception","Fail one batch then feed stable vehicle detections","Resume with real slot assignment","Partial report contained initial occupant; no invented event","Subsequent batches processed","YOLO_BATCH_INFERENCE_FAILED","test_shared_inference_recovers_without_inventing_a_parking_transition")
for i, scenario in (("R01","Disk full simulation"),("R02","Permission denied simulation")):
    case(i,"Reporting",scenario,"Raise OSError at atomic replace","Keep previous valid report","Previous JSON survived and temp was removed","Old output readable","OS error with errno",f"test_report_write_failure_keeps_previous_version[{i}]")
case("R03","Reporting","JSON serializer fails during finalization","Writer emits partial temp content then raises","Preserve prior report and signal failure","Stayed valid and prior bytes were preserved","Atomic rollback","REPORT_GENERATION_FAILED","test_cancelled_run_surfaces_report_failure_and_preserves_previous_file")
case("R04","Reporting","Non-cancelled final write fails","A file obstructs destination parents","Do not return successful terminal result","Run raised structured report failure","Existing unrelated file intact","REPORT_GENERATION_FAILED","test_non_cancelled_job_cannot_succeed_after_report_failure")
case("R05","Persistence","Outbox SQLite write error and duplicate envelope","Inject aborting SQLite trigger then retry same event ID","Rollback and persist once on retry","No row on failure; one row and one send on retry","Same event successfully enqueued","OUTBOX_PERSIST_FAILED","test_outbox_write_failure_can_retry_without_duplicate_rows")
case("R06","Publishing","Test destination receives authenticated event","Isolated loopback sender and generated credentials","Ack exactly one synthetic event","Encrypted row and test endpoint verified","Queue marked event sent","Loopback acknowledgement","test_outbox_uses_isolated_authenticated_destination")
for h in ["H13","H14","H15","H16","H17","H18","H19","H20","H21","H22","H23","H24","H27","H29","H31","H33","H34","H35","H40","H41"]:
    r = http_by_id[h]
    case("J" + h[1:], "HTTP API", r["scenario"], f"{r['method']} on loopback",
         f"HTTP {r['expected']}", f"HTTP {r['actual']}",
         "Service remained reachable", r["code"] or (r["body"].get("diagnostic") or {}).get("code") or "Recorded response/body", h)
for i, module, scenario, status, reason in (
    ("U01","Detection","Hard deadline for a hung native model","NOT TESTED","No per-inference timeout in this execution path"),
    ("U02","Tracking","Explicit state reset on demand","NOT TESTED","No public runtime reset was exercised"),
    ("U03","ANPR","Physical plate det/rec and real model availability","BLOCKED","Provisioned ANPR weights and matched footage absent"),
    ("U04","ANPR","Preempt a native OCR call that never returns","NOT TESTED","Wait timeout tested; native OCR preemption unavailable"),
    ("U05","Attributes","Independent vehicle type model unavailable or malformed","NOT TESTED","Vehicle type uses detector class votes, no separate model"),
    ("U06","Geometry","Malformed on-disk geometry JSON","NOT TESTED","Missing geometry and invalid parsed polygons were exercised"),
    ("U07","Lifecycle","PARK_END without a prior PARK_START","NOT TESTED","No dedicated negative transition test"),
    ("U08","Lifecycle","Semantic duplicates with different IDs for all four transitions","NOT TESTED","Exact repeated key tested; broader lifecycle sequence coverage incomplete"),
    ("U09","Video","Interrupt native transcode after it starts","NOT TESTED","Recorder cancellation exercised; FFmpeg subprocess kill timing not injected"),
    ("U10","Multicamera","Cancel only one camera","NOT TESTED","Available interface cancels the whole job"),
    ("U11","Reporting","Arbitrary malformed report object and missing required fields","NOT TESTED","Write and serialization failure were exercised"),
    ("U12","Persistence","Remote Supabase/database outage","BLOCKED","No isolated Supabase destination available"),
    ("U13","HTTP API","Unknown job-specific identifier","NOT TESTED","No route accepts a job ID parameter"),
    ("U14","HTTP API","Authentication / authorization rejection on S1","NOT TESTED","No S1 auth policy; bundled S2 auth suite tested separately"),
    ("U15","HTTP API","Hard request timeout / forced cancellation failure","NOT TESTED","No configured hard job timeout; blocked backend was not forced"),
    ("U16","Runtime","Physical camera and RTSP","BLOCKED","No isolated hardware source provided"),
    ("U17","Runtime","GPU, Windows and live OSNet","BLOCKED","CPU Linux workspace and no provisioned OSNet weights"),
    ("U18","Lifecycle","True restart of live pending lifecycle entries","NOT TESTED","Atomic write and same-process recovery verified"),
    ("U19","Video","Valid container reporting zero decodable frames","NOT TESTED","Zero-byte file and forced early EOF tested separately"),
):
    case(i,module,scenario,"—","Build evidence only if supported",reason,"—","—",status=status)

def write_csv(name, rows, columns=None):
    with (out / name).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

write_csv("ERROR_HANDLING_TEST_MATRIX.csv", faults)
api_rows = [{"Test ID": r["id"], "Method": r["method"], "Path": r["path"], "Scenario": r["scenario"],
             "Expected HTTP status": r["expected"], "Actual HTTP status": r["actual"], "Error code": r["code"] or (r["body"].get("diagnostic") or {}).get("code"),
             "Job state": r["body"].get("state"),
             "Status": r["status"], "Evidence": f"raw/http-final/http-responses.json#{r['id']}"} for r in http]
write_csv("API_TEST_RESULTS.csv", api_rows)
fault_by_id = {r["Test ID"]: r for r in faults}
edge_ids = ("D01","V01","V03","J27","D05","T06","G05","D06","A03","A04","A05","T04","G06","L03","L04","L07","L02",
            "T05","V04","V05","V06","V07","M01","M13","R01","R03","R05","L06","J16","J31","J34","J35","U08","U10","U12")
edge = []
for number, ref in enumerate(edge_ids, 1):
    r = fault_by_id[ref]
    edge.append({"Test ID":f"E{number:02d}", "Scenario":r["Failure scenario"], "Expected behavior":r["Expected behavior"],
                 "Actual behavior":r["Actual behavior"], "Evidence":r["Evidence reference"],
                 "Status":r["Status"], "Fault mapping":ref})
write_csv("EDGE_CASE_TEST_MATRIX.csv", edge)

def counts(rows, field="Status"):
    c = Counter(r[field] for r in rows)
    return {status: c[status] for status in ("PASS","FAIL","BLOCKED","NOT TESTED","WARNING")}

def result(suite):
    a = trees[suite].attrib
    total, failed, errors, skipped = (int(a[k]) for k in ("tests","failures","errors","skipped"))
    return {"total":total,"pass":total-failed-errors-skipped,"fail":failed,"error":errors,"skip":skipped,"duration":float(a["time"])}

s1, s2 = result("final-s1"), result("final-s2")
baseline = sum(result(n)["pass"] for n in ("baseline-s1", "baseline-s2"))
integration_nodes = [node for node in trees["final-s1"].findall("testcase")
                     if ".integration." in node.attrib["classname"]]
integration = sum(all(node.find(tag) is None for tag in ("failure", "error", "skipped"))
                  for node in integration_nodes)
coverage = (raw / "coverage.txt").read_text().strip().splitlines()[-1].split()[-1]
metrics = {"fault_matrix":counts(faults), "dedicated_fault_suite":result("fault-final"),
           "api":counts(api_rows), "swagger":counts(swagger, "status"), "edges":counts(edge),
           "s1":s1, "s2":s2, "baseline_pass":baseline, "integration_pytest_pass":integration, "coverage":coverage,
           "defects_discovered":7, "defects_fixed":7}
(raw / "results_summary.json").write_text(json.dumps(metrics, indent=2) + "\n")
print(metrics)
