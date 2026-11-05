"""
Check a trained classifier on labelled images it has NOT been trained on - the same way
the station decides (fail score >= threshold -> FAIL).

    python evaluate.py --images can_dataset/val
    python evaluate.py --images new_cans --model ../models/can_classifier.pt --threshold 0.6

--images holds pass/ and fail/ sub-folders. Prints every wrong image, a confusion table,
and the threshold that would have worked best. Writes evaluate_results.csv.
"""
import argparse
import csv
from pathlib import Path

from ultralytics import YOLO

ROOT = Path(__file__).resolve().parent
EXTS = {".jpg", ".jpeg", ".png", ".bmp"}
GOOD = {"good", "ok", "pass", "no_defect", "normal"}

ap = argparse.ArgumentParser()
ap.add_argument("--images", required=True)
ap.add_argument("--model", default=str(ROOT.parent / "models" / "can_classifier.pt"))
ap.add_argument("--threshold", type=float, default=0.6, help="station setting: Min confidence")
args = ap.parse_args()

model = YOLO(args.model)
rows = []
for truth in ("pass", "fail"):
    for path in sorted((Path(args.images) / truth).glob("*")):
        if path.suffix.lower() not in EXTS:
            continue
        r = model.predict(str(path), verbose=False)[0]          # runs at the size it was trained at
        probs = r.probs.data.cpu().tolist()
        fail = 1.0 - sum(p for i, p in enumerate(probs) if str(r.names[i]).lower() in GOOD)
        rows.append({"file": f"{truth}/{path.name}", "truth": truth, "fail_score": round(fail, 4),
                     "predicted": "fail" if fail >= args.threshold else "pass",
                     "ms": round(sum(r.speed.values()), 1)})

if not rows:
    raise SystemExit(f"No images in {args.images}/pass or {args.images}/fail")

wrong = [r for r in rows if r["predicted"] != r["truth"]]
for r in wrong:
    kind = "MISSED DEFECT" if r["truth"] == "fail" else "false reject"
    print(f"  {kind:<14} {r['file']}  fail score {r['fail_score']:.2f}")
tab = {(t, p): sum(r["truth"] == t and r["predicted"] == p for r in rows) for t in ("pass", "fail") for p in ("pass", "fail")}
print(f"\n{len(rows)} images, {len(rows) - len(wrong)} right = {(len(rows) - len(wrong)) / len(rows):.1%}"
      f"   (threshold {args.threshold:.2f})")
print(f"  good cans:      {tab['pass', 'pass']} passed, {tab['pass', 'fail']} rejected by mistake")
print(f"  defective cans: {tab['fail', 'fail']} rejected, {tab['fail', 'pass']} MISSED")
print(f"  average time:   {sum(r['ms'] for r in rows) / len(rows):.0f} ms per image on this computer")

best = min((sum((r["fail_score"] >= t) != (r["truth"] == "fail") for r in rows), t)
           for t in [i / 20 for i in range(1, 20)])
print(f"  best threshold on these images: {best[1]:.2f} ({best[0]} wrong) - only trust it with many images")

out = ROOT / "evaluate_results.csv"
with open(out, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
print("Saved:", out)
