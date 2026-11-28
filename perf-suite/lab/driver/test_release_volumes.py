import json
import unittest
from unittest.mock import patch

import arm_guest


class VolumeReleaseTests(unittest.TestCase):
    def simulate(self, bad_label=None, attached=None):
        project = "khitmee-arm-" + "a" * 16
        volumes = {k: project + "-" + k for k in ("db", "sites", "redis-queue")}
        removed = []

        def docker(*args):
            if args[1:3] == ("volume", "ls"):
                return "\n".join(volumes.values()).encode()
            name = args[-1]
            if args[1:3] == ("volume", "inspect"):
                scope = "unrelated" if name == volumes.get(bad_label) else project
                return json.dumps([{"Labels": {"khitmee.lab.owner": "khitmee-lab-adapter",
                                              "khitmee.lab.namespace": scope}}]).encode()
            if args[1] == "ps":
                return b"foreign-container" if name == "volume=" + str(volumes.get(attached)) else b""
            if args[1:3] == ("volume", "rm"):
                removed.append(name)
                return b""
            self.fail(f"unexpected operation {args}")

        with patch.object(arm_guest, "command", docker):
            if bad_label or attached:
                with self.assertRaises(ValueError):
                    arm_guest.release_volumes(project, {"volumes":volumes})
                self.assertEqual(removed, [])
            else:
                self.assertEqual(arm_guest.release_volumes(project, {"volumes":volumes}), list(volumes.values()))
                self.assertEqual(removed, list(volumes.values()))

    def test_only_fully_verified_detached_set_is_released(self):
        self.simulate()
        self.simulate(bad_label="redis-queue")
        self.simulate(attached="sites")
