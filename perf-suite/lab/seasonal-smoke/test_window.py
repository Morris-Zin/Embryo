import copy
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "telemetry")]
import window


class CgroupFirstIO(unittest.TestCase):
    def setUp(self):
        self.cg = ["/sys/fs/cgroup/demo", 1, 28]
        self.blocks = {"252:0": ["/sys/devices/vda", 27, "17"]}
        start = {"io":{"devices":{}}, "cgroup_path":self.cg[0]}
        end = {"io":{"devices":{"252:0":dict.fromkeys(window.telemetry.IO_FIELDS, 18)}}, "cgroup_path":self.cg[0]}
        self.target = {"start":start, "end":end, "samples":[start,end], "identity_changes":[]}

    def account(self, group=None, blocks=None):
        return window.account_first_io(self.target, self.cg, self.cg if group is None else group,
                                       self.blocks, self.blocks if blocks is None else blocks)

    def test_stable_first_io_preserves_observed_empty_file(self):
        observed = self.account()
        self.assertEqual(observed["observed_start"], {})
        self.assertEqual(self.target["samples"][0]["io"]["devices"], {})
        result = window.telemetry.analyze_target("frontend", self.target, [], [], [])
        self.assertTrue(result["io"]["valid"])
        self.assertEqual(result["io"]["wbytes_delta"], 18)

    def test_changed_or_unknown_lifecycles_fail_closed(self):
        self.assertFalse(self.account(group=[self.cg[0],1,29]))
        self.assertFalse(self.account(blocks={}))
        self.assertFalse(self.account(blocks={"252:0":[self.blocks["252:0"][0],27,"18"]}))
        self.target["identity_changes"] = [{"field":"container_id"}]
        self.assertFalse(self.account())
        self.target["identity_changes"] = []
        self.target["start"]["io"]["devices"]["254:1"] = {}
        self.assertFalse(self.account())

    def test_missing_or_malformed_boundary_fails_closed(self):
        self.target["start"]["missing_files"] = ["io.stat"]
        self.assertFalse(self.account())
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "io.stat").write_text("252:0 wbytes=1 rbytes=bad")
            with self.assertRaises(ValueError):
                window.io_boundary(p)


if __name__ == "__main__":
    unittest.main()
