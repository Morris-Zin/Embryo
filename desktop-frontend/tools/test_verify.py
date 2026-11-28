#!/usr/bin/env python3
"""Self-tests for the migration verification harness using synthetic evidence."""
from __future__ import annotations

import json
import binascii
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

from png_codec import PNG_SIGNATURE, write_png_rgba



def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def write_rgb_png(path: Path, red: int, green: int, blue: int, transparent: bool) -> None:
    width, height = 8, 5
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunks = [png_chunk(b"IHDR", ihdr)]
    if transparent:
        chunks.append(png_chunk(b"tRNS", struct.pack(">HHH", red, green, blue)))
    row = b"\x00" + bytes((red, green, blue)) * width
    chunks.append(png_chunk(b"IDAT", zlib.compress(row * height)))
    chunks.append(png_chunk(b"IEND", b""))
    path.write_bytes(PNG_SIGNATURE + b"".join(chunks))

ROOT = Path(__file__).resolve().parents[1]


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class VerifyHarnessTest(unittest.TestCase):
    def make_case(self, tmp: Path, mutate_pixel=False, include_keyboard=True):
        web = tmp / "web" / "sales-voucher"
        native = tmp / "native" / "sales-voucher"
        web.mkdir(parents=True)
        native.mkdir(parents=True)
        left_pixels = bytes((255, 255, 255, 255)) * 64
        right_pixels = bytearray(left_pixels)
        if mutate_pixel:
            right_pixels[0:4] = bytes((0, 0, 0, 255))
        write_png_rgba(web / "reference.png", 8, 8, left_pixels)
        write_png_rgba(native / "native.png", 8, 8, bytes(right_pixels))
        control = {
            "tag": "button",
            "id": "save",
            "role": "button",
            "text": "Save",
            "keyboard": ["F9"],
            "geometry": {"x": 1, "y": 2, "width": 5, "height": 4},
            "visible": True,
            "disabled": False,
        }
        write_json(web / "dom.json", {"title": "Sales", "controls": [control]})
        write_json(web / "trace.json", {"events": [], "requests": []})
        element = {
            "accessibleRole": "Button",
            "accessibleLabel": "Save",
            "accessibleEnabled": True,
            "absolutePosition": {"x": 1, "y": 2},
            "size": {"width": 5, "height": 4},
            "computedOpacity": 1,
        }
        write_json(native / "element-tree.json", {"elements": [element]})
        if include_keyboard:
            write_json(native / "keyboard-results.json", {"shortcuts": {"F9": {"passed": True}}})
        registry = {
            "screens": [{
                "id": "sales-voucher",
                "source_route": "/sales-voucher",
                "acceptance": {"visual_max_failed_pixel_ratio": 0.0, "geometry_tolerance_px": 0.0},
            }]
        }
        write_json(tmp / "registry.json", registry)
        write_json(tmp / "business.json", {"passed": True})
        write_json(tmp / "performance.json", {"passed": True})
        return tmp, web, native

    def run_verify(self, tmp: Path):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "verify.py"),
                "--registry", str(tmp / "registry.json"),
                "--web-reference", str(tmp / "web"),
                "--native-evidence", str(tmp / "native"),
                "--evidence-out", str(tmp / "out"),
                "--business-report", str(tmp / "business.json"),
                "--performance-report", str(tmp / "performance.json"),
                "--screen", "sales-voucher",
            ],
            text=True,
            capture_output=True,
        )

    def test_identical_synthetic_evidence_passes(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp, _, _ = self.make_case(Path(raw))
            result = self.run_verify(tmp)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            report = json.loads((tmp / "out" / "verification-report.json").read_text())
            self.assertTrue(report["passed"])

    def test_single_pixel_difference_fails_strict_visual_gate(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp, _, _ = self.make_case(Path(raw), mutate_pixel=True)
            result = self.run_verify(tmp)
            self.assertNotEqual(result.returncode, 0)
            report = json.loads((tmp / "out" / "verification-report.json").read_text())
            visual = report["results"][0]["states"][0]["checks"]["visual-comparison"]
            self.assertFalse(visual["passed"])
            self.assertEqual(visual["failedPixels"], 1)

    def test_rgb_transparency_key_is_a_visual_difference(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            reference = tmp / "reference.png"
            native = tmp / "native.png"
            write_rgb_png(reference, 255, 0, 0, transparent=True)
            write_rgb_png(native, 255, 0, 0, transparent=False)
            result = subprocess.run(
                [
                    sys.executable,
                    "-S",
                    str(ROOT / "tools" / "compare_screenshots.py"),
                    str(reference),
                    str(native),
                    "--report",
                    str(tmp / "compare.json"),
                ],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0, result.stderr + result.stdout)
            report = json.loads((tmp / "compare.json").read_text())
            self.assertEqual(report["failedPixels"], 40)
            self.assertEqual(report["regionalFailedPixelRatio"]["0,0"], 0.0)

    def test_missing_behavioral_keyboard_evidence_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp, _, _ = self.make_case(Path(raw), include_keyboard=False)
            result = self.run_verify(tmp)
            self.assertNotEqual(result.returncode, 0)
            report = json.loads((tmp / "out" / "verification-report.json").read_text())
            keyboard = report["results"][0]["states"][0]["checks"]["keyboard"]
            self.assertFalse(keyboard["passed"])


if __name__ == "__main__":
    unittest.main()
