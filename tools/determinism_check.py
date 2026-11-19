"""Run the REAL multi-camera pipeline N times on the same recorded files and
prove the output is identical.

    python tools/determinism_check.py                # 3 runs, transfer + exit scenarios
    python tools/determinism_check.py --runs 5

It uses the synthetic cameras + colour-blob detector from synth_smoke_test.py
(no GPU / YOLO / network needed). It proves scheduling, OCR/ReID ordering,
lifecycle events and IST timestamps are repeatable. It does NOT prove YOLO or
plate accuracy on real footage - run it again on Kaggle with the real config
(python tools/determinism_check.py --real-config config/config.kaggle_flow.yaml).
"""
import argparse
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

spec = importlib.util.spec_from_file_location("synth", os.path.join(ROOT, "tools", "synth_smoke_test.py"))
synth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(synth)

FIXED_START = 1_760_000_000.0
VOLATILE = {"event_id", "vehicle_uid", "uid", "generated_at", "snapshot", "original_png", "clahe_png",
            "vehicle_id", "global_id", "gid", "identity_id", "identities", "ids", "uuid"}


def normalise(obj):
    """Drop random identifiers (uuid4) so only behaviour is compared."""
    if isinstance(obj, dict):
        return {k: normalise(v) for k, v in sorted(obj.items()) if k not in VOLATILE}
    if isinstance(obj, list):
        return [normalise(v) for v in obj]
    return obj


T0 = 17.0   # existing config treats cars first seen in the first 15 s as "already parked"


def script_via_tripwire(t):
    """Enter, park in A-01, leave through the tripwire (confirmed EXIT)."""
    if t < T0 or t >= T0 + 25:
        return None
    if t < T0 + 6:
        u = (t - T0) / 6
        return synth.lerp(320, 180, u), synth.lerp(420, 135, u)
    if t < T0 + 19:
        return 180, 135
    u = (t - T0 - 19) / 6
    return synth.lerp(180, 320, u), synth.lerp(135, 420, u)


def script_leave_without_tripwire(t):
    """Enter, park, then slide off the side of the frame (no tripwire crossing)."""
    if t < T0 or t >= T0 + 30:
        return None
    if t < T0 + 6:
        u = (t - T0) / 6
        return synth.lerp(320, 180, u), synth.lerp(420, 135, u)
    if t < T0 + 16:
        return 180, 135
    u = (t - T0 - 16) / 5
    x = synth.lerp(180, 760, u)
    return (x, 135) if x < 700 else None


def script_b_after_transfer(t):
    """Same car shows up on camera B after leaving camera A and parks in B-01."""
    if t < T0 + 23 or t >= T0 + 48:
        return None
    if t < T0 + 29:
        u = (t - T0 - 23) / 6
        return synth.lerp(320, 180, u), synth.lerp(420, 135, u)
    return 180, 135


def build_cfg(videos, geoms, out_dir):
    from s1.config import load_config
    cfg = load_config(os.path.join(ROOT, "config", "config.kaggle_flow.yaml"))
    cfg["cameras"] = [{"camera_id": f"synth-cam-{chr(97 + i)}", "source": v, "geometry_file": g}
                      for i, (v, g) in enumerate(zip(videos, geoms))]
    cfg["detector"]["device"] = "cpu"
    cfg["plate"]["enabled"] = False
    cfg["demo_video"]["enabled"] = False
    cfg["camera_motion"]["enabled"] = False
    cfg["publisher"]["outbox_db"] = os.path.join(out_dir, "outbox.db")
    cfg["determinism"] = {"fixed_start_epoch": FIXED_START}
    cfg["report"] = {"path": os.path.join(out_dir, "report.json"),
                     "plate_evidence_dir": os.path.join(out_dir, "plates")}
    return cfg


def run_once(cfg, label):
    from s1.multicam import run_multi_camera
    run_multi_camera(cfg, synth.ColourBlobDetector(), show=False, no_publisher=True)
    rep = json.load(open(cfg["report"]["path"]))
    keep = {
        "summary": rep["summary"],
        "timeline": rep["lifecycle_timeline"],
        "s2": rep["service2_events"],
        "raw": rep["lifecycle_events"],
        "cams": {c: {k: v for k, v in d.items() if k in
                     ("frames_seen", "processed_frames", "dropped_frames", "skipped_idle_frames",
                      "occupied_slots", "free_slots", "event_counts")}
                 for c, d in rep["cameras"].items()},
        "slots": {c: [{k: s.get(k) for k in ("slot_id", "state", "plate", "colour", "baseline")}
                      for s in d["slots"]] for c, d in rep["cameras"].items()},
        "validation": rep["validation"],
    }
    return normalise(keep), rep


def scenario(name, scripts, baseline, duration, out_dir, runs):
    os.makedirs(out_dir, exist_ok=True)
    vids, geoms = [], []
    for i, sc in enumerate(scripts):
        v, g = os.path.join(out_dir, f"cam{i}.mp4"), os.path.join(out_dir, f"geom{i}.json")
        geom = json.loads(json.dumps(synth.GEOM_A))
        for s, n in zip(geom["slots"], (f"{chr(65 + i)}-01", f"{chr(65 + i)}-02")):
            s["slot_id"] = n
        synth.render_cam(v, sc, baseline_slot_center=baseline if i == 0 else None, duration=duration)
        json.dump(geom, open(g, "w"))
        vids.append(v); geoms.append(g)
    results = []
    for r in range(runs):
        cfg = build_cfg(vids, geoms, out_dir)
        norm, full = run_once(cfg, f"{name}#{r + 1}")
        results.append(norm)
        print(f"[{name}] run {r + 1}/{runs}: events={[ (e['camera_id'], e['event_type']) for e in norm['raw']]}")
    identical = all(json.dumps(x, sort_keys=True) == json.dumps(results[0], sort_keys=True) for x in results[1:])
    print(f"[{name}] IDENTICAL across {runs} runs: {identical}")
    if not identical:
        base = json.dumps(results[0], sort_keys=True, indent=1).splitlines()
        for i, x in enumerate(results[1:], 2):
            other = json.dumps(x, sort_keys=True, indent=1).splitlines()
            diffs = [(a, b) for a, b in zip(base, other) if a != b][:8]
            print(f"  run1 vs run{i} first diffs: {diffs}")
    return identical, results[0], full


def real_config_check(path, runs):
    """Same proof on YOUR videos with the real YOLO/plate/ReID stack (run on Kaggle)."""
    from run_kaggle import resolve_sources
    from s1.config import load_config
    from s1.main import run
    out_dir = os.path.join(ROOT, "data", "determinism_check", "real")
    os.makedirs(out_dir, exist_ok=True)
    results = []
    for r in range(runs):
        cfg = load_config(path)
        problems = resolve_sources(cfg)
        if problems and os.path.isdir("videos"):          # laptop layout: ./videos
            import glob
            problems = []
            for cam in cfg["cameras"]:
                if not os.path.isfile(str(cam["source"])):
                    hits = glob.glob(os.path.join("videos", "**", os.path.basename(str(cam["source"]))), recursive=True)
                    if hits:
                        cam["source"] = hits[0]
                    else:
                        problems.append(f"{cam['camera_id']}: video not found: {cam['source']}")
        if problems:
            print("Cannot start:\n  - " + "\n  - ".join(problems))
            return 2
        cfg["determinism"] = {"fixed_start_epoch": FIXED_START}
        cfg["demo_video"] = {"enabled": False}
        cfg["report"] = {"path": os.path.join(out_dir, f"report_{r + 1}.json"),
                         "plate_evidence_dir": os.path.join(out_dir, f"plates_{r + 1}")}
        run(cfg, show=False, no_publisher=True)
        rep = json.load(open(cfg["report"]["path"]))
        if not (rep.get("run_info", {}).get("determinism", {}) or {}).get("enabled"):
            print("Deterministic mode was NOT active:", rep.get("run_info"))
            return 3
        results.append(normalise({
            "summary": rep["summary"], "timeline": rep["lifecycle_timeline"],
            "s2": rep["service2_events"], "raw": rep["lifecycle_events"],
            "slots": {c: [{k: s.get(k) for k in ("slot_id", "state", "plate", "colour", "baseline")}
                          for s in d["slots"]] for c, d in rep["cameras"].items()},
            "frames": {c: [d["frames_seen"], d["processed_frames"], d["skipped_idle_frames"]]
                       for c, d in rep["cameras"].items()},
        }))
        print(f"[real] run {r + 1}/{runs}: summary={results[-1]['summary']}")
    same = all(json.dumps(x, sort_keys=True) == json.dumps(results[0], sort_keys=True) for x in results[1:])
    print("RESULT:", "DETERMINISTIC (all runs identical)" if same else "NOT DETERMINISTIC - see diffs below")
    if not same:
        base = json.dumps(results[0], sort_keys=True, indent=1).splitlines()
        for i, x in enumerate(results[1:], 2):
            other = json.dumps(x, sort_keys=True, indent=1).splitlines()
            print(f"run1 vs run{i}:", [(a, b) for a, b in zip(base, other) if a != b][:12])
    return 0 if same else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--real-config", help="e.g. config/config.kaggle_flow.yaml (needs GPU/videos)")
    a = ap.parse_args()
    if a.real_config:
        sys.exit(real_config_check(a.real_config, a.runs))
    base = os.path.join(ROOT, "data", "determinism_check")
    ok = True
    r1, first, full1 = scenario("tripwire-exit", [script_via_tripwire], (460, 135), 46,
                                os.path.join(base, "s1"), a.runs)
    ok &= r1
    r2, second, full2 = scenario("no-tripwire-exit", [script_leave_without_tripwire], None, 52,
                                 os.path.join(base, "s2"), a.runs)
    ok &= r2
    r3, third, full3 = scenario("two-camera-transfer", [script_via_tripwire, script_b_after_transfer], None, 68,
                                os.path.join(base, "s3"), a.runs)
    ok &= r3
    print("\nTIMELINE tripwire-exit      :", json.dumps(first["timeline"], indent=1))
    print("TIMELINE no-tripwire-exit   :", json.dumps(second["timeline"], indent=1))
    print("\nRESULT:", "DETERMINISTIC" if ok else "NOT DETERMINISTIC")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
