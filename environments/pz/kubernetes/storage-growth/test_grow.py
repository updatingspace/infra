import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("grow", Path(__file__).with_name("grow.py"))
grow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grow)


def initial():
    return {
        "disk_bytes": 100 * grow.GIB, "root_partition_bytes": 100 * grow.GIB - grow.GIB // 2,
        "root_filesystem_bytes": 100 * grow.GIB - grow.GIB // 2,
        "root_available_bytes": 60 * grow.GIB, "other_unallocated_bytes": grow.GIB // 2,
        "image_bytes": grow.ORIGINAL, "image_allocated_bytes": 24 * grow.GIB,
        "loop": "/dev/loop2", "loop_bytes": grow.ORIGINAL, "filesystem_bytes": grow.ORIGINAL,
    }


class Guards(unittest.TestCase):
    def test_old_cloud_disk_is_refused(self):
        value = initial(); value["disk_bytes"] = 60 * grow.GIB
        with self.assertRaisesRegex(RuntimeError, "cloud_disk"):
            grow.validate(value)

    def test_enlarged_cloud_disk_requires_both_guest_layers(self):
        for field in ("root_partition_bytes", "root_filesystem_bytes"):
            with self.subTest(field=field):
                value = initial(); value[field] = 60 * grow.GIB
                with self.assertRaisesRegex(RuntimeError, "not_yet_expanded"):
                    grow.validate(value)

    def test_full_allocation_and_other_filesystems_keep_host_reserve(self):
        value = initial()
        needed = grow.RESERVE + value["other_unallocated_bytes"] + grow.TARGET - value["image_allocated_bytes"]
        value["root_available_bytes"] = needed
        grow.validate(value)
        value["root_available_bytes"] -= 1
        with self.assertRaisesRegex(RuntimeError, "host_reserve"):
            grow.validate(value)

    def test_shrink_or_inconsistent_layers_are_refused(self):
        for field, number in (("image_bytes", grow.TARGET + 1), ("loop_bytes", grow.ORIGINAL + 1),
                              ("filesystem_bytes", grow.ORIGINAL - 1)):
            with self.subTest(field=field):
                value = initial(); value[field] = number
                with self.assertRaisesRegex(RuntimeError, "would_shrink"):
                    grow.validate(value)


class Resume(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.image = Path(self.temp.name) / "zomboid.ext4"
        with self.image.open("wb") as handle:
            handle.write(b"existing-data")
            handle.truncate(grow.ORIGINAL)  # Sparse fixture; allocates no GiB.
        self.image.with_name("migration.lock").touch()
        self.image_patch = patch.object(grow, "IMAGE", self.image)
        self.image_patch.start()
        self.state = initial()
        s = self.image.stat()
        self.state.update(image_device=s.st_dev, image_inode=s.st_ino)
        self.commands = []
        self.allocations = 0
        self.phases = []
        self.interrupt = None

    def tearDown(self):
        self.image_patch.stop()
        self.temp.cleanup()

    def allocate(self, fd, offset, length):
        self.assertEqual((offset, length), (0, grow.TARGET))
        self.allocations += 1
        self.state["root_available_bytes"] -= grow.TARGET - self.state["image_allocated_bytes"]
        os.ftruncate(fd, length)
        self.state.update(image_bytes=length, image_allocated_bytes=length)
        if self.interrupt == "allocate":
            raise RuntimeError("simulated interruption")

    def command(self, arguments):
        self.commands.append(arguments)
        if arguments == ["losetup", "--set-capacity", "/dev/loop2"]:
            self.state["loop_bytes"] = grow.TARGET
            phase = "loop"
        elif arguments == ["resize2fs", "/dev/loop2", "50G"]:
            self.state["filesystem_bytes"] = grow.TARGET
            phase = "filesystem"
        else:
            self.fail("Unexpected mutating command")
        if self.interrupt == phase:
            raise RuntimeError("simulated interruption")

    def apply(self):
        return grow.grow(lambda: copy.deepcopy(self.state), self.command, self.allocate,
                         lambda phase, _: self.phases.append(phase))

    def test_growth_preserves_existing_bytes_and_is_noop_when_complete(self):
        self.assertTrue(grow.complete(self.apply()))
        with self.image.open("rb") as handle:
            self.assertEqual(handle.read(13), b"existing-data")
        before = (len(self.commands), self.allocations, len(self.phases))
        self.apply()
        self.assertEqual(before, (len(self.commands), self.allocations, len(self.phases)))

    def test_each_interrupted_phase_resumes_without_shrink(self):
        for phase in ("allocate", "loop", "filesystem"):
            with self.subTest(phase=phase):
                self.state.update(initial())
                with self.image.open("r+b") as handle:
                    handle.truncate(grow.ORIGINAL)
                self.interrupt = phase
                with self.assertRaisesRegex(RuntimeError, "simulated"):
                    self.apply()
                self.interrupt = None
                self.assertTrue(grow.complete(self.apply()))

    def test_allocation_failure_does_not_resize_loop_or_filesystem(self):
        def fail(*_):
            raise OSError("simulated ENOSPC")
        with self.assertRaises(OSError):
            grow.grow(lambda: copy.deepcopy(self.state), self.command, fail, lambda *_: None)
        self.assertEqual(self.commands, [])

    def test_changed_inode_is_rejected_before_allocation(self):
        self.state["image_inode"] += 1
        with self.assertRaisesRegex(RuntimeError, "image_changed"):
            self.apply()
        self.assertEqual(self.allocations, 0)


if __name__ == "__main__":
    unittest.main()
