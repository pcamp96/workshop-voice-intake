"""Focused tests for configuration, source boundaries, and processing state."""

import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ROOT = Path(__file__).resolve().parents[1]
intake = load_module("voice_intake", ROOT / "voice_intake.py")
scheduler = load_module("make_launch_agent", ROOT / "scripts" / "make_launch_agent.py")
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
        patcher = patch.object(intake, "VOLUMES_FOLDER", self.volumes)
        patcher.start()
        self.addCleanup(patcher.stop)

    def discover(self, uuid=None, extra=None):
        with patch.object(intake, "mounted_volume_uuid", return_value=uuid):
            folders = intake.discover_source_folders(self.config, extra or [])
            return intake.discover_recordings(folders)

    def test_unenrolled_drive_and_root_media_are_ignored(self):
        drive = self.volumes / "Ordinary Drive"
        self.media(drive / "root.mp3")
        self.media(drive / "RECORD" / "podcast.wav")
        self.assertEqual(self.discover(uuid="UNREGISTERED"), [])

    def test_enrolled_recorder_uses_record_folder_only_after_rename(self):
        drive = self.volumes / "Renamed Recorder"
        recording = self.media(drive / "RECORD" / "capture.mp3")
        self.media(drive / "root.mp3")
        self.assertEqual(self.discover(uuid=RECORDER_UUID), [recording])

    def test_explicit_sources_work_but_symlinks_cannot_escape(self):
        local = self.media(self.config.drop_folder / "capture.m4a")
        manual = self.media(self.root / "manual" / "capture.wav")
        outside = self.media(self.root / "private" / "outside.mp3")
        (self.config.drop_folder / "linked.mp3").symlink_to(outside)
        self.assertCountEqual(self.discover(extra=[manual.parent]), [local, manual])

    def test_recorder_folder_symlink_cannot_escape_volume(self):
        outside = self.media(self.root / "private" / "outside.mp3")
        drive = self.volumes / "Recorder"
        drive.mkdir()
        (drive / "RECORD").symlink_to(outside.parent, target_is_directory=True)
        self.assertEqual(self.discover(uuid=RECORDER_UUID), [])

    def test_diskutil_uuid_and_disconnect_errors(self):
        result = subprocess.CompletedProcess(
            [], 0, stdout=plistlib.dumps({"VolumeUUID": RECORDER_UUID.lower()})
        )
        with patch.object(intake.subprocess, "run", return_value=result):
            self.assertEqual(
                intake.mounted_volume_uuid(self.volumes / "Recorder" / "RECORD"),
                RECORDER_UUID,
            )
        with patch.object(intake.subprocess, "run", side_effect=OSError("disconnected")):
            self.assertIsNone(intake.mounted_volume_uuid(self.volumes / "Recorder"))


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


class LaunchAgentTests(unittest.TestCase):
    def test_program_arguments_preserve_paths_with_spaces_without_a_shell(self):
        plist = scheduler.build_plist(
            Path("/tmp/Voice Intake/config file.json"),
            Path("/Applications/Python Runtime/bin/python3"),
            Path("/tmp/Voice Intake/voice_intake.py"),
            Path("/tmp/Voice Intake/logs"),
        )
        self.assertEqual(plist["ProgramArguments"], [
            "/Applications/Python Runtime/bin/python3",
            "/tmp/Voice Intake/voice_intake.py",
            "--config",
            "/tmp/Voice Intake/config file.json",
        ])
        self.assertNotIn("Program", plist)
        self.assertEqual(plist["StartInterval"], 60)


if __name__ == "__main__":
    unittest.main()
