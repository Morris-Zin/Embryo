#!/usr/bin/env python3
"""Strict screenshot comparison used by migration verification."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference")
    parser.add_argument("native")
    parser.add_argument("--report", required=True)
    parser.add_argument("--max-failed-pixel-ratio", type=float, default=0.0)
    args = parser.parse_args()

    reference = Path(args.reference)
    native = Path(args.native)
    report = {
        "reference": str(reference),
        "native": str(native),
        "referenceSha256": sha256(reference) if reference.exists() else None,
        "nativeSha256": sha256(native) if native.exists() else None,
        "passed": False,
    }
    if not reference.exists() or not native.exists():
        report["error"] = "missing screenshot input"
    else:
        try:
            from PIL import Image, ImageChops
        except ImportError:
            report["error"] = "Pillow is required for pixel comparison (python -m pip install --user pillow)"
        else:
            with Image.open(reference) as left_image, Image.open(native) as right_image:
                left = left_image.convert("RGBA")
                right = right_image.convert("RGBA")
                report["referenceSize"] = list(left.size)
                report["nativeSize"] = list(right.size)
                if left.size != right.size:
                    report["dimensionsMatch"] = False
                    report["failedPixelRatio"] = 1.0
                    report["error"] = "screenshot dimensions differ"
                else:
                    diff = ImageChops.difference(left, right)
                    pixels = diff.getdata()
                    failed = sum(1 for pixel in pixels if pixel[:3] != (0, 0, 0))
                    total = left.size[0] * left.size[1]
                    ratio = failed / total if total else 1.0
                    report.update(
                        dimensionsMatch=True,
                        totalPixels=total,
                        failedPixels=failed,
                        failedPixelRatio=ratio,
                        maxFailedPixelRatio=args.max_failed_pixel_ratio,
                    )
                    diff_path = Path(args.report).with_suffix(".diff.png")
                    diff_path.parent.mkdir(parents=True, exist_ok=True)
                    diff.save(diff_path)
                    report["diffImage"] = str(diff_path)
                    report["passed"] = ratio <= args.max_failed_pixel_ratio

    output = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("passed", "error", "failedPixelRatio", "failedPixels")}, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
