import importlib
import io
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


class SdPreviewFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_base_dir = os.environ.get("BASE_DIR")
        os.environ["BASE_DIR"] = self.temp_dir.name

        if "bambu2obs" in sys.modules:
            del sys.modules["bambu2obs"]

        self.module = importlib.import_module("bambu2obs")

    def tearDown(self):
        if self.previous_base_dir is None:
            os.environ.pop("BASE_DIR", None)
        else:
            os.environ["BASE_DIR"] = self.previous_base_dir

        self.temp_dir.cleanup()
        if "bambu2obs" in sys.modules:
            del sys.modules["bambu2obs"]

    def test_extract_preview_name_candidates_ignores_internal_plate_name(self):
        task_data = {"title": "Cube.gcode.3mf"}
        print_data = {"subtask_name": "Cube", "gcode_file": "/data/Metadata/plate_1.gcode"}

        candidates = self.module._extract_preview_name_candidates(task_data=task_data, print_data=print_data)

        self.assertIn("Cube", candidates)
        self.assertNotIn("plate_1", [candidate.lower() for candidate in candidates])

    def test_parse_ftp_list_line_with_spaces(self):
        line = "-rw-r--r--    1 1002     1002        39605 Dec 17 20:44 Cube Test.gcode.3mf"

        parsed = self.module._parse_ftp_list_line(line)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["name"], "Cube Test.gcode.3mf")
        self.assertEqual(parsed["size"], 39605)
        self.assertFalse(parsed["is_dir"])
        self.assertIsNotNone(parsed["mtime"])

    def test_select_best_archive_prefers_name_match(self):
        entries = [
            {"name": "OtherProject.gcode.3mf", "mtime": None, "size": 1000},
            {"name": "Cube.gcode.3mf", "mtime": None, "size": 900},
        ]

        selected, mode = self.module._select_best_archive(entries, ["cube"])

        self.assertEqual(selected["name"], "Cube.gcode.3mf")
        self.assertEqual(mode, "name_match")

    def test_extract_preview_bytes_from_3mf_prefers_plate_image(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("Metadata/top_1.png", b"top-image")
            archive.writestr("Metadata/plate_1.png", b"plate-image")
            archive.writestr("Metadata/pick_1.png", b"pick-image")

        preview_bytes, preview_member = self.module._extract_preview_bytes_from_3mf(buffer.getvalue())

        self.assertEqual(preview_member, "Metadata/plate_1.png")
        self.assertEqual(preview_bytes, b"plate-image")

    def test_extract_preview_bytes_from_3mf_returns_none_when_missing(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("Metadata/readme.txt", b"no image")

        preview_bytes, preview_member = self.module._extract_preview_bytes_from_3mf(buffer.getvalue())

        self.assertIsNone(preview_bytes)
        self.assertIsNone(preview_member)

    def test_handle_print_data_attempts_sd_preview_even_with_existing_cover(self):
        Path(self.module.PRINT_COVER_IMAGE_PATH).write_bytes(b"old-cover")

        print_data = {
            "print_type": "local",
            "gcode_state": "RUNNING",
            "subtask_name": "Job Alpha",
            "gcode_file": "/data/Metadata/plate_2.gcode"
        }

        with patch.object(self.module, "attempt_sdcard_preview_fallback", return_value=False) as fallback_mock:
            self.module.handle_print_data(print_data)

        fallback_mock.assert_called_once()
        self.assertEqual(fallback_mock.call_args.kwargs["print_data"], print_data)
        self.assertFalse(fallback_mock.call_args.kwargs["force"])

    def test_handle_print_data_clears_stale_cover_on_job_change(self):
        Path(self.module.PRINT_COVER_IMAGE_PATH).write_bytes(b"old-cover")
        Path(self.module.PRINT_COVER_LEGACY_PATH).write_bytes(b"old-cover-legacy")

        with self.module.sd_preview_lock:
            self.module.sd_preview_attempt_state["job_key"] = "old_job_key"
            self.module.sd_preview_attempt_state["attempted_at"] = time.time()
            self.module.sd_preview_attempt_state["success"] = True

        print_data = {
            "print_type": "local",
            "gcode_state": "RUNNING",
            "subtask_name": "New Job",
            "gcode_file": "/data/Metadata/plate_4.gcode"
        }

        with patch.object(self.module, "attempt_sdcard_preview_fallback", return_value=False):
            self.module.handle_print_data(print_data)

        self.assertFalse(Path(self.module.PRINT_COVER_IMAGE_PATH).exists())
        self.assertFalse(Path(self.module.PRINT_COVER_LEGACY_PATH).exists())
        print_cover_text_path = Path(self.temp_dir.name) / "printCover.txt"
        self.assertTrue(print_cover_text_path.exists())
        self.assertEqual(print_cover_text_path.read_text(encoding="utf-8").strip(), "N/A")


if __name__ == "__main__":
    unittest.main()
