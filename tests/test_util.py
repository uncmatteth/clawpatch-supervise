from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from clawpatch_supervise.util import _fsync_parent_directory, atomic_write_text


class AtomicWriteTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX directory durability")
    def test_atomic_write_fsyncs_file_then_replacement_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "checkpoint.json"
            temporary = str(Path(temp) / ".checkpoint.json.temporary")
            handle = MagicMock()
            handle.fileno.return_value = 11
            context = MagicMock()
            context.__enter__.return_value = handle
            events: list[tuple[str, object]] = []

            with (
                patch(
                    "clawpatch_supervise.util.tempfile.mkstemp",
                    return_value=(11, temporary),
                ),
                patch("clawpatch_supervise.util.os.fdopen", return_value=context),
                patch(
                    "clawpatch_supervise.util.os.fsync",
                    side_effect=lambda descriptor: events.append(("fsync", descriptor)),
                ),
                patch(
                    "clawpatch_supervise.util.os.replace",
                    side_effect=lambda source, destination: events.append(
                        ("replace", (source, destination))
                    ),
                ),
                patch(
                    "clawpatch_supervise.util.os.open",
                    side_effect=lambda directory, flags: (
                        events.append(("open", (directory, flags))) or 22
                    ),
                ),
                patch(
                    "clawpatch_supervise.util.os.close",
                    side_effect=lambda descriptor: events.append(("close", descriptor)),
                ),
            ):
                atomic_write_text(path, "checkpoint\n")

            expected_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
            self.assertEqual(
                events,
                [
                    ("fsync", 11),
                    ("replace", (temporary, path)),
                    ("open", (path.parent, expected_flags)),
                    ("fsync", 22),
                    ("close", 22),
                ],
            )

    def test_parent_directory_fsync_is_explicitly_unsupported_off_posix(self) -> None:
        with (
            patch("clawpatch_supervise.util.os.name", "nt"),
            patch("clawpatch_supervise.util.os.open") as open_directory,
            patch("clawpatch_supervise.util.os.fsync") as fsync,
        ):
            _fsync_parent_directory(Path("checkpoint.json"))

        open_directory.assert_not_called()
        fsync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
