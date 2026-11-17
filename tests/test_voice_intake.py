"""Focused tests for configuration, source boundaries, and processing state."""

import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
import xml.etree.ElementTree as ElementTree


def make_symlink(test: unittest.TestCase, link: Path, target: Path, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        test.skipTest("symlinks need extra privileges on this system")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ROOT = Path(__file__).resolve().parents[1]
intake = load_module("voice_intake", ROOT / "voice_intake.py")
scheduler = load_module("make_schedule", ROOT / "scripts" / "make_schedule.py")
RECORDER_UUID = "11111111-2222-3333-4444-555555555555"


class TemporaryConfigTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_path = self.root / "config.json"

    def write_config(self, **changes):
        raw = {
            "drop_folder": "drop",
            "archive_folder": "archive",
            "output_folder": "notes",
            "state_folder": "state",
            "model_path": "model.bin",
            "brands": ["Workshop", "Unclear"],
            "recorder_volumes": {RECORDER_UUID.lower(): "Pocket recorder"},
        }
        raw.update(changes)
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")
        return intake.load_config(self.config_path)

    def media(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test recording")
        os.utime(path, (1_700_000_000, 1_700_000_000))
        return path


class ConfigTests(TemporaryConfigTestCase):
    def test_loads_required_paths_relative_to_config_and_portable_defaults(self):
        config = self.write_config(brands=["Shop"])
        self.assertEqual(config.drop_folder, (self.root / "drop").resolve())
        self.assertEqual(config.archive_folder, (self.root / "archive").resolve())
        self.assertEqual(config.output_folder, (self.root / "notes").resolve())
        self.assertEqual(config.brands, ("Shop", "Unclear"))
        self.assertEqual(config.recorder_volumes, {RECORDER_UUID: "Pocket recorder"})
        self.assertEqual(config.ffmpeg_bin, "ffmpeg")
        self.assertEqual(config.whisper_bin, "whisper-cli")
        self.assertIsNone(config.icloud_drop_folder)

    def test_rejects_missing_unknown_and_invalid_keys(self):
        cases = [
            ({"archive_folder": "archive", "output_folder": "notes"}, "missing required"),
            ({
                "drop_folder": "drop", "archive_folder": "archive",
                "output_folder": "notes", "unexpected": True,
            }, "unknown config"),
            ({
                "drop_folder": "drop", "archive_folder": "archive",
                "output_folder": "notes", "brands": [],
            }, "brands must"),
        ]
        for raw, message in cases:
            with self.subTest(raw=raw):
                self.config_path.write_text(json.dumps(raw), encoding="utf-8")
                with self.assertRaisesRegex(intake.ConfigError, message):
                    intake.load_config(self.config_path)

    def test_corrupt_or_structurally_invalid_state_stops_safely(self):
        config = self.write_config()
        config.state_folder.mkdir()
        for content in ("not json", json.dumps({"processed": []})):
            with self.subTest(content=content):
                config.state_file.write_text(content, encoding="utf-8")
                with self.assertRaises(intake.StateError):
                    intake.load_state(config)

    def test_rejects_input_folder_overlapping_generated_data(self):
        with self.assertRaisesRegex(intake.ConfigError, "overlaps generated-data"):
            self.write_config(
                drop_folder="workspace",
                archive_folder="workspace/archive",
            )


class SourceBoundaryTests(TemporaryConfigTestCase):
    def setUp(self):
        super().setUp()
        self.config = self.write_config()
        self.volumes = self.root / "Volumes"
        for folder in (self.config.drop_folder, self.volumes):
            folder.mkdir(parents=True)

    def discover(self, mounted=(), extra=None):
        with patch.object(intake, "list_mounted_volumes", return_value=list(mounted)):
            folders = intake.discover_source_folders(self.config, extra or [])
            return intake.discover_recordings(folders)

    def test_unenrolled_drive_and_root_media_are_ignored(self):
        drive = self.volumes / "Ordinary Drive"
        self.media(drive / "root.mp3")
        self.media(drive / "RECORD" / "podcast.wav")
        self.assertEqual(self.discover([(drive, "UNREGISTERED")]), [])
        self.assertEqual(self.discover([(drive, None)]), [])

    def test_enrolled_recorder_uses_record_folder_only_after_rename(self):
        drive = self.volumes / "Renamed Recorder"
        recording = self.media(drive / "RECORD" / "capture.mp3")
        self.media(drive / "root.mp3")
        self.assertEqual(self.discover([(drive, RECORDER_UUID)]), [recording])

    def test_explicit_sources_work_but_symlinks_cannot_escape(self):
        local = self.media(self.config.drop_folder / "capture.m4a")
        manual = self.media(self.root / "manual" / "capture.wav")
        outside = self.media(self.root / "private" / "outside.mp3")
        make_symlink(self, self.config.drop_folder / "linked.mp3", outside)
        self.assertCountEqual(self.discover(extra=[manual.parent]), [local, manual])

    def test_recorder_folder_symlink_cannot_escape_volume(self):
        outside = self.media(self.root / "private" / "outside.mp3")
        drive = self.volumes / "Recorder"
        drive.mkdir()
        make_symlink(self, drive / "RECORD", outside.parent, directory=True)
        self.assertEqual(self.discover([(drive, RECORDER_UUID)]), [])

    def test_source_label_uses_deepest_containing_volume(self):
        outer = self.volumes / "Outer"
        inner = outer / "Inner"
        recording = self.media(inner / "RECORD" / "capture.mp3")
        mounted = [(outer, "OTHER"), (inner, RECORDER_UUID)]
        with patch.object(intake, "list_mounted_volumes", return_value=mounted):
            self.assertEqual(intake.mounted_volume_uuid(recording), RECORDER_UUID)
            self.assertEqual(intake.source_label(recording, self.config), "Pocket recorder")


class PlatformVolumeTests(unittest.TestCase):
    def test_diskutil_uuid_and_disconnect_errors(self):
        result = subprocess.CompletedProcess(
            [], 0, stdout=plistlib.dumps({"VolumeUUID": RECORDER_UUID.lower()})
        )
        with patch.object(intake.subprocess, "run", return_value=result):
            self.assertEqual(intake._diskutil_uuid(Path("/Volumes/Recorder")), RECORDER_UUID)
        with patch.object(intake.subprocess, "run", side_effect=OSError("disconnected")):
            self.assertIsNone(intake._diskutil_uuid(Path("/Volumes/Recorder")))

    def test_windows_serial_matches_vol_command_format(self):
        self.assertEqual(intake.format_windows_serial(0x4477_4470), "4477-4470")
        self.assertEqual(intake.format_windows_serial(0x0000_00AB), "0000-00AB")

    def test_linux_mounts_are_matched_to_filesystem_uuids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            devices = root / "dev"
            by_uuid = root / "by-uuid"
            devices.mkdir()
            by_uuid.mkdir()
            (devices / "sdb1").write_text("")
            (devices / "sda1").write_text("")
            for name, target in (("abcd-1234", "sdb1"), ("root-uuid", "sda1")):
                try:
                    (by_uuid / name).symlink_to(devices / target)
                except OSError:
                    self.skipTest("symlinks need extra privileges on this system")
            mounts = root / "mounts"
            mounts.write_text(
                f"{devices / 'sda1'} / ext4 rw 0 0\n"
                f"{devices / 'sdb1'} /media/me/MY\\040RECORDER vfat rw 0 0\n"
                "tmpfs /run tmpfs rw 0 0\n",
                encoding="utf-8",
            )
            volumes = intake._linux_volumes(mounts, by_uuid)
        self.assertEqual(volumes, [(Path("/media/me/MY RECORDER"), "ABCD-1234")])


class LockTests(unittest.TestCase):
    def test_second_lock_is_refused_until_first_is_released(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "scan.lock"
            child = (
                "import sys; sys.path.insert(0, sys.argv[1]); import voice_intake as v; "
                "from pathlib import Path\n"
                "with v.exclusive_lock(Path(sys.argv[2])) as ok: print(ok)"
            )
            with intake.exclusive_lock(lock) as acquired:
                self.assertTrue(acquired)
                held = subprocess.run(
                    [sys.executable, "-c", child, str(ROOT), str(lock)],
                    capture_output=True, text=True, check=True,
                )
                self.assertEqual(held.stdout.strip(), "False")
            released = subprocess.run(
                [sys.executable, "-c", child, str(ROOT), str(lock)],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(released.stdout.strip(), "True")


class ProcessingTests(TemporaryConfigTestCase):
    def setUp(self):
        super().setUp()
        self.config = self.write_config()
        intake.ensure_directories(self.config)
        self.source = self.media(self.config.drop_folder / "thought.wav")
        self.parsed = {
            "title": "A shop thought", "capture_type": "general", "brand": "Workshop",
            "summary": "A summary", "action_items": [], "decisions": [],
            "observations": [], "machine_settings": [], "ideas": [], "waiting_on": [],
            "questions": [], "suggested_destination": "Voice Intake", "sensitivity": "normal",
        }

    def test_success_writes_note_and_state_then_deduplicates(self):
        state = {"processed": {}}
        with (
            patch.object(intake, "transcribe", return_value="Remember the jig."),
            patch.object(intake, "parse_transcript", return_value=self.parsed),
        ):
            self.assertTrue(intake.process_recording(self.source, state, self.config))
            self.assertFalse(intake.process_recording(self.source, state, self.config))
        self.assertEqual(len(state["processed"]), 1)
        entry = next(iter(state["processed"].values()))
        self.assertEqual(entry["status"], "parsed")
        self.assertTrue(Path(entry["archive"]).is_file())
        self.assertIn("Remember the jig.", Path(entry["note"]).read_text(encoding="utf-8"))

    def test_transcription_failure_is_not_marked_processed_and_retries(self):
        state = {"processed": {}}
        with patch.object(intake, "transcribe", side_effect=RuntimeError("temporary failure")) as call:
            self.assertFalse(intake.process_recording(self.source, state, self.config))
            self.assertFalse(intake.process_recording(self.source, state, self.config))
        self.assertEqual(call.call_count, 2)
        self.assertEqual(state, {"processed": {}})
        self.assertEqual(list(self.config.output_folder.glob("*.md")), [])

    def test_parser_failure_preserves_transcript_once(self):
        state = {"processed": {}}
        with (
            patch.object(intake, "transcribe", return_value="A useful raw transcript."),
            patch.object(intake, "parse_transcript", side_effect=ValueError("bad response")),
        ):
            self.assertTrue(intake.process_recording(self.source, state, self.config))
        entry = next(iter(state["processed"].values()))
        note = Path(entry["note"]).read_text(encoding="utf-8")
        self.assertEqual(entry["status"], "transcript-only")
        self.assertIn("A useful raw transcript.", note)
        self.assertIn("Local parsing failed", note)

    def test_archive_rejects_corrupt_existing_copy(self):
        file_hash = intake.sha256(self.source)
        archived = intake.archive_recording(self.source, file_hash, self.config)
        archived.write_bytes(b"partial copy")
        with self.assertRaisesRegex(RuntimeError, "integrity check"):
            intake.archive_recording(self.source, file_hash, self.config)

    def test_extra_source_cannot_overlap_output(self):
        with self.assertRaisesRegex(intake.ConfigError, "overlaps generated-data"):
            intake.validate_source_boundaries(self.config, [self.config.output_folder])


class ScheduleTests(unittest.TestCase):
    config = Path("/tmp/Voice Intake/config file.json")
    python = Path("/opt/Python Runtime/bin/python3")
    script = Path("/tmp/Voice Intake/voice_intake.py")

    def test_plist_arguments_preserve_paths_with_spaces_without_a_shell(self):
        plist = scheduler.build_plist(self.config, self.python, self.script, Path("/tmp/logs"))
        self.assertEqual(plist["ProgramArguments"], [
            str(self.python), str(self.script), "--config", str(self.config),
        ])
        self.assertNotIn("Program", plist)
        self.assertEqual(plist["StartInterval"], 60)

    def test_task_xml_is_well_formed_and_quotes_paths(self):
        text = scheduler.build_task_xml(
            Path(r"C:\Users\Me\Voice Intake\config.json"),
            Path(r"C:\Python & Tools\python.exe"),
            Path(r"C:\Users\Me\Voice Intake\voice_intake.py"),
            Path(r"C:\Users\Me\AppData\Local\logs"),
            datetime(2026, 1, 2, 3, 4, 5),
        )
        namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        root = ElementTree.fromstring(text.split("\n", 1)[1])
        self.assertEqual(root.find(".//t:Command", namespace).text, r"C:\Python & Tools\python.exe")
        arguments = root.find(".//t:Arguments", namespace).text
        self.assertIn(r'"C:\Users\Me\Voice Intake\config.json"', arguments)
        self.assertIn("--log-file", arguments)
        self.assertEqual(root.find(".//t:Interval", namespace).text, "PT1M")
        self.assertEqual(root.find(".//t:StartBoundary", namespace).text, "2026-01-02T03:04:05")

    def test_systemd_units_quote_arguments_and_escape_percent(self):
        python = PurePosixPath("/opt/Python Runtime/bin/python3")
        units = scheduler.build_systemd_units(
            PurePosixPath("/home/me/Voice 100%/config.json"), python,
            PurePosixPath("/tmp/Voice Intake/voice_intake.py"),
        )
        service = units["voice-intake.service"]
        self.assertIn('"/home/me/Voice 100%%/config.json"', service)
        self.assertIn('ExecStart="/opt/Python Runtime/bin/python3"', service)
        self.assertIn('PATH=/opt/Python Runtime/bin:/usr/local/bin', service)
        self.assertIn("OnUnitActiveSec=60s", units["voice-intake.timer"])


if __name__ == "__main__":
    unittest.main()
