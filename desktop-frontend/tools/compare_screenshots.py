#!/usr/bin/env python3
"""Strict screenshot comparison used by migration verification.

The primary metric is exact pixel equality. Additional color-difference metrics are
reported so failures are diagnosable, but they never weaken the configured threshold.
"""
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
            try:
                from png_codec import read_png_rgba, write_png_rgba

                left_width, left_height, left_pixels = read_png_rgba(reference)
                right_width, right_height, right_pixels = read_png_rgba(native)
            except Exception as exc:
                report["error"] = f"screenshot decoding failed without Pillow: {exc}"
            else:
                report["referenceSize"] = [left_width, left_height]
                report["nativeSize"] = [right_width, right_height]
                report["byteIdentical"] = reference.read_bytes() == native.read_bytes()
                if (left_width, left_height) != (right_width, right_height):
                    report["dimensionsMatch"] = False
                    report["failedPixelRatio"] = 1.0
                    report["error"] = "screenshot dimensions differ"
                else:
                    width, height = left_width, left_height
                    total = width * height
                    diff_pixels = bytearray(len(left_pixels))
                    channel_deltas = []
                    nonzero = []
                    failed = 0
                    for pixel_index in range(total):
                        offset = pixel_index * 4
                        max_delta = 0
                        for channel in range(4):
                            delta = abs(left_pixels[offset + channel] - right_pixels[offset + channel])
                            diff_pixels[offset + channel] = delta
                            max_delta = max(max_delta, delta)
                        channel_deltas.append(max_delta)
                        if max_delta:
                            failed += 1
                            nonzero.append((pixel_index % width, pixel_index // width))
                    ratio = failed / total if total else 1.0
                    exact_or_near = {}
                    for tolerance in (1, 2, 5, 10, 25):
                        near = sum(1 for delta in channel_deltas if delta <= tolerance)
                        exact_or_near[f"within{tolerance}PixelRatio"] = near / total if total else 0.0
                    regions = {}
                    grid_x, grid_y = 8, 5
                    for gy in range(grid_y):
                        for gx in range(grid_x):
                            left_bound = gx * width // grid_x
                            top_bound = gy * height // grid_y
                            right_bound = (gx + 1) * width // grid_x
                            bottom_bound = (gy + 1) * height // grid_y
                            region_total = (right_bound - left_bound) * (bottom_bound - top_bound)
                            region_failed = 0
                            for y in range(top_bound, bottom_bound):
                                for x in range(left_bound, right_bound):
                                    offset = (y * width + x) * 4
                                    luminance = (
                                        diff_pixels[offset] * 299
                                        + diff_pixels[offset + 1] * 587
                                        + diff_pixels[offset + 2] * 114
                                        + 500
                                    ) // 1000
                                    if luminance:
                                        region_failed += 1
                            regions[f"{gx},{gy}"] = round(region_failed / region_total, 6) if region_total else 1.0
                    report.update(
                        dimensionsMatch=True,
                        pixelsExactlyEqual=failed == 0,
                        totalPixels=total,
                        failedPixels=failed,
                        failedPixelRatio=ratio,
                        maxFailedPixelRatio=args.max_failed_pixel_ratio,
                        meanMaxChannelDelta=(sum(channel_deltas) / total) if total else 255.0,
                        maxChannelDelta=max(channel_deltas, default=255),
                        nearEqualRatios=exact_or_near,
                        regionalFailedPixelRatio=regions,
                    )
                    if nonzero:
                        xs = [point[0] for point in nonzero]
                        ys = [point[1] for point in nonzero]
                        report["diffBounds"] = {
                            "left": min(xs),
                            "top": min(ys),
                            "right": max(xs) + 1,
                            "bottom": max(ys) + 1,
                        }
                    diff_path = Path(args.report).with_suffix(".diff.png")
                    diff_path.parent.mkdir(parents=True, exist_ok=True)
                    write_png_rgba(diff_path, width, height, bytes(diff_pixels))
                    report["diffImage"] = str(diff_path)
                    report["passed"] = ratio <= args.max_failed_pixel_ratio
        else:
            with Image.open(reference) as left_image, Image.open(native) as right_image:
                left = left_image.convert("RGBA")
                right = right_image.convert("RGBA")
                report["referenceSize"] = list(left.size)
                report["nativeSize"] = list(right.size)
                report["byteIdentical"] = reference.read_bytes() == native.read_bytes()
                if left.size != right.size:
                    report["dimensionsMatch"] = False
                    report["failedPixelRatio"] = 1.0
                    report["error"] = "screenshot dimensions differ"
                else:
                    diff = ImageChops.difference(left, right)
                    pixels = list(diff.getdata())
                    failed = sum(1 for pixel in pixels if pixel != (0, 0, 0, 0))
                    total = left.size[0] * left.size[1]
                    channel_deltas = [max(pixel) for pixel in pixels]
                    nonzero = [(index % left.size[0], index // left.size[0]) for index, pixel in enumerate(pixels) if pixel != (0, 0, 0, 0)]
                    ratio = failed / total if total else 1.0
                    exact_or_near = {}
                    for tolerance in (1, 2, 5, 10, 25):
                        near = sum(1 for delta in channel_deltas if delta <= tolerance)
                        exact_or_near[f"within{tolerance}PixelRatio"] = near / total if total else 0.0
                    regions = {}
                    grid_x, grid_y = 8, 5
                    for gy in range(grid_y):
                        for gx in range(grid_x):
                            box = (gx * left.size[0] // grid_x, gy * left.size[1] // grid_y, (gx + 1) * left.size[0] // grid_x, (gy + 1) * left.size[1] // grid_y)
                            region = diff.crop(box).convert("L")
                            histogram = region.histogram()
                            region_total = (box[2] - box[0]) * (box[3] - box[1])
                            regions[f"{gx},{gy}"] = round((region_total - histogram[0]) / region_total, 6) if region_total else 1.0
                    report.update(
                        dimensionsMatch=True,
                        pixelsExactlyEqual=failed == 0,
                        totalPixels=total,
                        failedPixels=failed,
                        failedPixelRatio=ratio,
                        maxFailedPixelRatio=args.max_failed_pixel_ratio,
                        meanMaxChannelDelta=(sum(channel_deltas) / total) if total else 255.0,
                        maxChannelDelta=max(channel_deltas, default=255),
                        nearEqualRatios=exact_or_near,
                        regionalFailedPixelRatio=regions,
                    )
                    if nonzero:
                        xs = [point[0] for point in nonzero]
                        ys = [point[1] for point in nonzero]
                        report["diffBounds"] = {
                            "left": min(xs),
                            "top": min(ys),
                            "right": max(xs) + 1,
                            "bottom": max(ys) + 1,
                        }
                    diff_path = Path(args.report).with_suffix(".diff.png")
                    diff_path.parent.mkdir(parents=True, exist_ok=True)
                    diff.save(diff_path)
                    report["diffImage"] = str(diff_path)
                    report["passed"] = ratio <= args.max_failed_pixel_ratio

    output = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("passed", "error", "failedPixelRatio", "failedPixels", "pixelsExactlyEqual")}, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
