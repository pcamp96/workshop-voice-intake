#!/usr/bin/env python3
"""Local voice-recorder intake for macOS, Windows, and Linux.

Copies recordings from explicitly configured folders or enrolled recorder
volumes, transcribes them with whisper.cpp, structures them with Ollama, and
writes reviewable Markdown notes. Source recordings are never changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin"
MODEL_SIZE = 487_614_201
MODEL_SHA256 = "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d"
OLLAMA_URL = "http://127.0.0.1:11434"
VOLUMES_FOLDER = Path("/Volumes")

AUDIO_EXTENSIONS = {
    ".aac", ".aiff", ".alac", ".flac", ".m4a", ".mp3", ".ogg",
    ".opus", ".wav", ".wma",
}
RECORDER_FOLDER_NAMES = (
    "[RECORD]", "RECORD", "Record", "Record File", "Recording",
    "Recordings", "recordings",
)
MAC_SYSTEM_VOLUMES = {"Macintosh HD", "com.apple.TimeMachine.localsnapshots"}
LINUX_SKIPPED_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var"}
LOG_FILE: Path | None = None
IGNORED_AUDIO_FILENAMES = {"speaker_test.wav", "pro_speaker_test.wav"}
DEFAULT_BRANDS = ("Workshop", "Personal", "Unclear")
DEFAULT_WHISPER_PROMPT = (
    "workshop, project, material, machine settings, task, idea, reminder"
)
ALLOWED_CONFIG_KEYS = {
    "drop_folder", "archive_folder", "output_folder", "state_folder",
    "model_path", "ollama_model", "whisper_prompt", "brands",
    "recorder_volumes", "icloud_drop_folder", "ffmpeg_bin", "whisper_bin",
}


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid."""


class StateError(RuntimeError):
    """Raised when the processing state cannot be trusted."""


def default_state_folder() -> Path:
    """Per-user application data folder for state, locks, and the model."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "workshop-voice-intake"


def _config_path(value: Any, name: str, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty path string")
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


@dataclass(frozen=True)
class Config:
    drop_folder: Path
    archive_folder: Path
    output_folder: Path
    state_folder: Path
    model_path: Path
    ollama_model: str
    whisper_prompt: str
    brands: tuple[str, ...]
    recorder_volumes: dict[str, str]
    icloud_drop_folder: Path | None
    ffmpeg_bin: str
    whisper_bin: str

    @property
    def state_file(self) -> Path:
        return self.state_folder / "processed.json"

    @property
    def lock_file(self) -> Path:
        return self.state_folder / "voice-intake.lock"


def load_config(path: Path) -> Config:
    """Load a JSON config, resolving relative paths beside the config file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigError(f"config file not found: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read config {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a JSON object")
    unknown = sorted(set(raw) - ALLOWED_CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(unknown)}")
    missing = [key for key in ("drop_folder", "archive_folder", "output_folder") if key not in raw]
    if missing:
        raise ConfigError(f"missing required config key(s): {', '.join(missing)}")

    base = path.expanduser().resolve().parent
    state = _config_path(
        raw.get("state_folder", str(default_state_folder())),
        "state_folder", base,
    )
    model = _config_path(
        raw.get("model_path", str(state / "models" / "ggml-small.en.bin")),
        "model_path", base,
    )
    brands_value = raw.get("brands", list(DEFAULT_BRANDS))
    if (
        not isinstance(brands_value, list)
        or not brands_value
        or not all(isinstance(item, str) and item.strip() for item in brands_value)
    ):
        raise ConfigError("brands must be a non-empty array of non-empty strings")
    brands = tuple(dict.fromkeys(item.strip() for item in brands_value))
    if "Unclear" not in brands:
        brands += ("Unclear",)

    volumes_value = raw.get("recorder_volumes", {})
    if not isinstance(volumes_value, dict) or not all(
        isinstance(key, str) and key.strip()
        and isinstance(value, str) and value.strip()
        for key, value in volumes_value.items()
    ):
        raise ConfigError("recorder_volumes must map volume UUID strings to labels")
    volumes = {key.strip().upper(): value.strip() for key, value in volumes_value.items()}

    ollama_model = raw.get("ollama_model", "gemma3:12b")
    whisper_prompt = raw.get("whisper_prompt", DEFAULT_WHISPER_PROMPT)
    ffmpeg_bin = raw.get("ffmpeg_bin", "ffmpeg")
    whisper_bin = raw.get("whisper_bin", "whisper-cli")
    for key, value in (
        ("ollama_model", ollama_model), ("whisper_prompt", whisper_prompt),
        ("ffmpeg_bin", ffmpeg_bin), ("whisper_bin", whisper_bin),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{key} must be a non-empty string")

    cloud_value = raw.get("icloud_drop_folder")
    config = Config(
        drop_folder=_config_path(raw["drop_folder"], "drop_folder", base),
        archive_folder=_config_path(raw["archive_folder"], "archive_folder", base),
        output_folder=_config_path(raw["output_folder"], "output_folder", base),
        state_folder=state,
        model_path=model,
        ollama_model=ollama_model.strip(),
        whisper_prompt=whisper_prompt.strip(),
        brands=brands,
        recorder_volumes=volumes,
        icloud_drop_folder=(
            _config_path(cloud_value, "icloud_drop_folder", base)
            if cloud_value is not None else None
        ),
        ffmpeg_bin=ffmpeg_bin.strip(),
        whisper_bin=whisper_bin.strip(),
    )
    validate_source_boundaries(config, [])
    return config


def log(message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    if LOG_FILE is not None:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    # pythonw.exe and some schedulers run without a console.
    if sys.stdout is not None:
        print(line, flush=True)


def ensure_directories(config: Config) -> None:
    folders = [
        config.drop_folder, config.archive_folder, config.output_folder,
        config.state_folder, config.model_path.parent,
    ]
    if config.icloud_drop_folder is not None:
        folders.append(config.icloud_drop_folder)
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)


def load_state(config: Config) -> dict[str, Any]:
    if not config.state_file.exists():
        return {"processed": {}}
    try:
        state = json.loads(config.state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise StateError(
            f"cannot safely read state file {config.state_file}: {error}. "
            "Repair it or move it aside before retrying."
        ) from error
    if not isinstance(state, dict) or not isinstance(state.get("processed"), dict):
        raise StateError(
            f"invalid state structure in {config.state_file}; expected a processed object"
        )
    return state


def save_state(config: Config, state: dict[str, Any]) -> None:
    temporary = config.state_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(config.state_file)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_slug(value: str, fallback: str = "Voice Capture") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|#^[\]]+', "-", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    return (cleaned or fallback)[:100]


def _diskutil_uuid(volume: Path) -> str | None:
    """macOS: return the filesystem UUID reported by diskutil."""
    try:
        result = subprocess.run(
            ["/usr/sbin/diskutil", "info", "-plist", str(volume)],
            check=True, capture_output=True, timeout=10,
        )
        info = plistlib.loads(result.stdout)
    except (OSError, plistlib.InvalidFileException, subprocess.SubprocessError):
        return None
    volume_uuid = info.get("VolumeUUID")
    return str(volume_uuid).upper() if volume_uuid else None


def _mac_volumes() -> list[tuple[Path, str | None]]:
    volumes: list[tuple[Path, str | None]] = []
    if VOLUMES_FOLDER.exists():
        for volume in VOLUMES_FOLDER.iterdir():
            if volume.is_dir() and volume.name not in MAC_SYSTEM_VOLUMES:
                volumes.append((volume, _diskutil_uuid(volume)))
    return volumes


def format_windows_serial(serial: int) -> str:
    """Format a volume serial the way the Windows `vol` command prints it."""
    text = f"{serial & 0xFFFFFFFF:08X}"
    return f"{text[:4]}-{text[4:]}"


def _windows_volumes() -> list[tuple[Path, str | None]]:
    """Windows: removable and fixed drive letters other than the system drive."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Suppress "insert a disk" dialogs for empty card readers.
    previous_mode = kernel32.SetErrorMode(0x0001 | 0x8000)
    system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\").upper()
    volumes: list[tuple[Path, str | None]] = []
    try:
        mask = kernel32.GetLogicalDrives()
        for index in range(26):
            if not mask & (1 << index):
                continue
            letter = f"{chr(65 + index)}:"
            root = letter + "\\"
            # 2 = removable, 3 = fixed. Skip network, optical, and RAM drives.
            if letter == system_drive or kernel32.GetDriveTypeW(root) not in (2, 3):
                continue
            serial = wintypes.DWORD()
            if kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(root), None, 0, ctypes.byref(serial), None, None, None, 0,
            ):
                volumes.append((Path(root), format_windows_serial(serial.value)))
    finally:
        kernel32.SetErrorMode(previous_mode)
    return volumes


def _unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _linux_volumes(
    mounts_file: Path = Path("/proc/self/mounts"),
    uuid_folder: Path = Path("/dev/disk/by-uuid"),
) -> list[tuple[Path, str | None]]:
    """Linux: mounted block devices that have a filesystem UUID."""
    device_uuids: dict[str, str] = {}
    try:
        for link in uuid_folder.iterdir():
            device_uuids[os.path.realpath(link)] = link.name.upper()
    except OSError:
        return []
    try:
        lines = mounts_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    volumes: list[tuple[Path, str | None]] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 2:
            continue
        device, mount_point = fields[0], _unescape_mount(fields[1])
        if mount_point in LINUX_SKIPPED_MOUNTS:
            continue
        volume_uuid = device_uuids.get(os.path.realpath(device))
        if volume_uuid:
            volumes.append((Path(mount_point), volume_uuid))
    return volumes


def list_mounted_volumes() -> list[tuple[Path, str | None]]:
    """Return (mount root, stable filesystem ID) for candidate external volumes."""
    if sys.platform == "darwin":
        return _mac_volumes()
    if os.name == "nt":
        return _windows_volumes()
    return _linux_volumes()


def mounted_volume_uuid(path: Path) -> str | None:
    """Return the stable filesystem ID of the volume that contains path."""
    best: tuple[int, str | None] | None = None
    for root, volume_uuid in list_mounted_volumes():
        if _within(path, root):
            depth = len(root.resolve().parts)
            if best is None or depth > best[0]:
                best = (depth, volume_uuid)
    return best[1] if best else None


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _paths_overlap(first: Path, second: Path) -> bool:
    return _within(first, second) or _within(second, first)


def validate_source_boundaries(config: Config, extra_sources: list[Path]) -> None:
    """Prevent generated files or state from being used as intake sources."""
    inputs = [config.drop_folder, *extra_sources]
    if config.icloud_drop_folder is not None:
        inputs.append(config.icloud_drop_folder)
    protected = [config.archive_folder, config.output_folder, config.state_folder]
    for source in inputs:
        for destination in protected:
            if _paths_overlap(source, destination):
                raise ConfigError(
                    f"source folder {source} overlaps generated-data folder {destination}"
                )


def source_label(path: Path, config: Config) -> str:
    if _within(path, config.drop_folder):
        return "Manual recorder drop"
    if config.icloud_drop_folder and _within(path, config.icloud_drop_folder):
        return "Synced recorder drop"
    volume_uuid = mounted_volume_uuid(path)
    if volume_uuid in config.recorder_volumes:
        return config.recorder_volumes[volume_uuid]
    return "Voice recorder"


def discover_source_folders(config: Config, extra_sources: list[Path]) -> list[Path]:
    folders = [config.drop_folder]
    if config.icloud_drop_folder is not None:
        folders.append(config.icloud_drop_folder)
    folders.extend(extra_sources)

    for volume, volume_uuid in list_mounted_volumes():
        # Automatic import is permitted only for explicitly enrolled IDs.
        if volume_uuid is None or volume_uuid not in config.recorder_volumes:
            continue
        for folder_name in RECORDER_FOLDER_NAMES:
            candidate = volume / folder_name
            if candidate.is_dir() and _within(candidate, volume):
                folders.append(candidate)

    unique: list[Path] = []
    seen: set[str] = set()
    for folder in folders:
        try:
            key = str(folder.resolve()).casefold()
        except OSError:
            key = str(folder).casefold()
        if folder.is_dir() and key not in seen:
            seen.add(key)
            unique.append(folder)
    return unique


def discover_recordings(folders: list[Path]) -> list[Path]:
    recordings: list[Path] = []
    now = datetime.now().timestamp()
    for folder in folders:
        try:
            source_root = folder.resolve()
            for path in folder.rglob("*"):
                try:
                    stat = path.stat()
                    if (
                        path.is_file()
                        and _within(path, source_root)
                        and path.suffix.lower() in AUDIO_EXTENSIONS
                        and path.name.lower() not in IGNORED_AUDIO_FILENAMES
                        and stat.st_size > 0
                        and (now - stat.st_mtime >= 15 or stat.st_mtime - now > 60)
                    ):
                        recordings.append(path)
                except OSError:
                    continue
        except OSError:
            continue
    return sorted(recordings, key=lambda item: item.stat().st_mtime)


def archive_recording(source: Path, file_hash: str, config: Config) -> Path:
    captured = datetime.fromtimestamp(source.stat().st_mtime)
    folder = config.archive_folder / captured.strftime("%Y") / captured.strftime("%m")
    folder.mkdir(parents=True, exist_ok=True)
    name = (
        f"{captured.strftime('%Y-%m-%d %H%M%S')} - {file_hash[:8]} - "
        f"{safe_slug(source.stem, 'recording')}{source.suffix.lower()}"
    )
    destination = folder / name
    if destination.exists():
        if sha256(destination) != file_hash:
            raise RuntimeError(f"existing archive copy failed integrity check: {destination}")
        return destination
    descriptor, temporary_name = tempfile.mkstemp(prefix=".voice-intake-", dir=folder)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        if sha256(temporary) != file_hash:
            raise RuntimeError(f"new archive copy failed integrity check: {source}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def resolve_executable(value: str) -> str | None:
    has_folder = os.sep in value or bool(os.altsep and os.altsep in value)
    if has_folder or value.startswith("~"):
        path = Path(value).expanduser()
        if path.is_file() and (os.name == "nt" or os.access(path, os.X_OK)):
            return str(path)
        return None
    return shutil.which(value)


def run_checked(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=True, **kwargs)


def transcribe(audio: Path, work_folder: Path, config: Config) -> str:
    ffmpeg = resolve_executable(config.ffmpeg_bin)
    whisper = resolve_executable(config.whisper_bin)
    if not ffmpeg or not whisper:
        raise RuntimeError("ffmpeg and whisper-cli must be installed and executable")
    wav_path = work_folder / "normalized.wav"
    run_checked([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(audio),
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav_path),
    ])
    output_base = work_folder / "transcript"
    run_checked([
        whisper, "-m", str(config.model_path), "-f", str(wav_path), "-l", "en",
        "-otxt", "-of", str(output_base), "-nt", "-np", "--prompt",
        config.whisper_prompt,
    ])
    transcript = output_base.with_suffix(".txt").read_text(encoding="utf-8").strip()
    if not transcript:
        raise RuntimeError("whisper.cpp produced an empty transcript")
    return transcript


def parse_transcript(transcript: str, config: Config) -> dict[str, Any]:
    string_array = {"type": "array", "items": {"type": "string"}}
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "capture_type": {"type": "string", "enum": [
                "shop_log", "post_call_debrief", "daily_shutdown",
                "content_brainstorm", "product_time_study", "general",
            ]},
            "brand": {"type": "string", "enum": list(config.brands)},
            "summary": {"type": "string"},
            "action_items": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"}, "due_hint": {"type": "string"},
                    "project_hint": {"type": "string"},
                },
                "required": ["text", "due_hint", "project_hint"],
            }},
            "decisions": string_array, "observations": string_array,
            "machine_settings": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "machine": {"type": "string"}, "material": {"type": "string"},
                    "parameter": {"type": "string"}, "value": {"type": "string"},
                    "certainty": {"type": "string", "enum": ["confirmed", "experimental"]},
                },
                "required": ["machine", "material", "parameter", "value", "certainty"],
            }},
            "ideas": string_array, "waiting_on": string_array, "questions": string_array,
            "suggested_destination": {"type": "string"},
            "sensitivity": {"type": "string", "enum": ["normal", "confidential", "unclear"]},
        },
        "required": [
            "title", "capture_type", "brand", "summary", "action_items", "decisions",
            "observations", "machine_settings", "ideas", "waiting_on", "questions",
            "suggested_destination", "sensitivity",
        ],
    }
    prompt = f"""Parse this private voice capture into the supplied JSON schema.

Rules:
- Use only information explicitly present in the transcript.
- Do not invent deadlines, promises, settings, measurements, people, or destinations.
- Action items must begin with a verb and be executable.
- Separate decisions already made from ideas being considered.
- Treat machine settings as experimental unless explicitly confirmed.
- Return an empty list or empty string when a category has no evidence.
- Suggested routing is advisory. Use Voice Intake when unclear.
- Mark sensitivity confidential only when the transcript explicitly says it is restricted.
- A due hint must be an explicitly spoken date or deadline.
- Preserve useful terminology and product names.

Transcript:
{transcript}"""
    body = json.dumps({
        "model": config.ollama_model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False, "format": schema, "options": {"temperature": 0},
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        response_body = json.loads(response.read().decode("utf-8"))
    parsed = json.loads(response_body["message"]["content"])
    if not isinstance(parsed, dict) or not parsed.get("title"):
        raise ValueError("local parser returned an empty or invalid object")
    return normalize_parsed(parsed, transcript)


def normalize_parsed(parsed: dict[str, Any], transcript: str) -> dict[str, Any]:
    transcript_lower = transcript.lower()
    explicit_due = re.compile(
        r"\b(today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|"
        r"saturday|sunday|this week|next week|by\s+\w+|on\s+\w+|"
        r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|(?:january|february|march|april|"
        r"may|june|july|august|september|october|november|december)\s+\d{1,2})\b",
        flags=re.IGNORECASE,
    )
    for action in parsed.get("action_items") or []:
        if not isinstance(action, dict):
            continue
        hint = str(action.get("due_hint", "")).strip()
        if not hint or not explicit_due.search(hint):
            action["due_hint"] = ""
        elif hint.lower() not in transcript_lower:
            words = hint.lower().removeprefix("by ").removeprefix("on ")
            if words not in transcript_lower:
                action["due_hint"] = ""
    restricted = (
        "confidential", "private", "under nda", "non-disclosure", "nondisclosure",
        "embargo", "do not share", "not for publication",
    )
    if parsed.get("sensitivity") == "confidential" and not any(
        term in transcript_lower for term in restricted
    ):
        parsed["sensitivity"] = "normal"
    if not str(parsed.get("suggested_destination", "")).strip():
        parsed["suggested_destination"] = "Voice Intake — needs review"
    return parsed


def markdown_list(items: list[Any], empty_text: str = "None captured.") -> str:
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    return "\n".join(f"- {item}" for item in cleaned) if cleaned else empty_text


def create_note(
    source: Path, archived_audio: Path, file_hash: str, transcript: str,
    parsed: dict[str, Any] | None, parse_error: str | None, config: Config,
) -> Path:
    captured = datetime.fromtimestamp(source.stat().st_mtime)
    parsed = parsed or {}
    title = safe_slug(str(parsed.get("title", "")), source.stem)
    destination = config.output_folder / f"{captured:%Y-%m-%d %H%M} - {title}.md"
    counter = 2
    while destination.exists():
        destination = config.output_folder / f"{captured:%Y-%m-%d %H%M} - {title} {counter}.md"
        counter += 1
    actions = []
    for action in parsed.get("action_items") or []:
        if not isinstance(action, dict) or not action.get("text"):
            continue
        details = [str(action.get(key, "")).strip() for key in ("due_hint", "project_hint")]
        suffix = "; ".join(item for item in details if item)
        actions.append(f"- {action['text']}" + (f" — {suffix}" if suffix else ""))
    settings = ["| Machine | Material | Parameter | Value | Status |", "|---|---|---|---|---|"]
    for setting in parsed.get("machine_settings") or []:
        if isinstance(setting, dict):
            values = [str(setting.get(key, "")).replace("|", "\\|") for key in (
                "machine", "material", "parameter", "value", "certainty",
            )]
            settings.append("| " + " | ".join(values) + " |")
    if len(settings) == 2:
        settings.append("|  |  |  |  | No settings captured |")
    parse_status = "parsed" if parsed else "transcript-only"
    warning = f"\n> [!warning] Local parsing failed\n> {parse_error}\n" if parse_error else ""
    content = f'''---
type: voice-capture
status: inbox
captured: {captured.isoformat(timespec="seconds")}
source_recorder: {json.dumps(source_label(source, config))}
capture_type: {json.dumps(str(parsed.get("capture_type", "general")))}
brand: {json.dumps(str(parsed.get("brand", "Unclear")))}
parse_status: {parse_status}
audio_sha256: {file_hash}
tags:
  - voice/intake
---

# {title}

> [!info] Intake
> Automatically copied, transcribed, and parsed locally. Review action candidates and suggested routing before filing them. The source recording was not deleted.

## Summary

{str(parsed.get("summary", "")).strip() or "No automatic summary was produced."}
{warning}
## Action candidates

{chr(10).join(actions) if actions else "None captured."}

## Decisions

{markdown_list(parsed.get("decisions") or [])}

## Observations and results

{markdown_list(parsed.get("observations") or [])}

## Machine settings

{chr(10).join(settings)}

## Ideas

{markdown_list(parsed.get("ideas") or [])}

## Waiting on

{markdown_list(parsed.get("waiting_on") or [])}

## Open questions and verification needs

{markdown_list(parsed.get("questions") or [])}

## Suggested routing

- Brand: {parsed.get("brand", "Unclear")}
- Destination: {parsed.get("suggested_destination", "Needs review")}
- Sensitivity: {parsed.get("sensitivity", "unclear")}

## Source

- Recorder: {source_label(source, config)}
- Original filename: `{source.name}`
- [Open archived audio](<{archived_audio.as_uri()}>)
- SHA-256: `{file_hash}`

## Transcript

{transcript}
'''
    destination.write_text(content, encoding="utf-8")
    return destination


def process_recording(source: Path, state: dict[str, Any], config: Config) -> bool:
    file_hash = sha256(source)
    if file_hash in state["processed"]:
        return False
    log(f"Importing {source}")
    archived = archive_recording(source, file_hash, config)
    try:
        with tempfile.TemporaryDirectory(prefix="voice-intake-") as temporary:
            transcript = transcribe(archived, Path(temporary), config)
    except Exception as error:
        # Leave this hash absent from state so the next scan retries transcription.
        log(f"Transcription failed for {source.name}: {type(error).__name__}: {error}")
        return False

    parsed: dict[str, Any] | None = None
    parse_error: str | None = None
    try:
        parsed = parse_transcript(transcript, config)
    except Exception as error:
        parse_error = f"{type(error).__name__}: {error}"
        log(f"Parser warning for {source.name}: {parse_error}")
    note = create_note(source, archived, file_hash, transcript, parsed, parse_error, config)
    state["processed"][file_hash] = {
        "source": str(source), "archive": str(archived), "note": str(note),
        "processed_at": datetime.now().isoformat(timespec="seconds"),
        "status": "parsed" if parsed else "transcript-only",
    }
    save_state(config, state)
    log(f"Created {note}")
    return True


def download_model(config: Config) -> None:
    if (
        config.model_path.exists()
        and config.model_path.stat().st_size == MODEL_SIZE
        and sha256(config.model_path) == MODEL_SHA256
    ):
        log(f"Whisper model already installed and verified: {config.model_path}")
        return
    temporary = config.model_path.with_suffix(".download")
    log("Downloading the local Whisper small.en model (about 466 MB)...")
    last_error: Exception | None = None
    for attempt in range(1, 4):
        temporary.unlink(missing_ok=True)
        try:
            digest = hashlib.sha256()
            request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "voice-intake"})
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as out:
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    out.write(block)
                    digest.update(block)
            if temporary.stat().st_size != MODEL_SIZE or digest.hexdigest() != MODEL_SHA256:
                raise RuntimeError("download failed size or SHA-256 verification")
            temporary.replace(config.model_path)
            log(f"Installed Whisper model: {config.model_path}")
            return
        except (OSError, urllib.error.URLError, RuntimeError) as error:
            last_error = error
            log(f"Download attempt {attempt} failed: {error}")
    temporary.unlink(missing_ok=True)
    raise RuntimeError(f"Whisper model download failed: {last_error}")


def check_dependencies(config: Config, extra_sources: list[Path]) -> int:
    """Validate dependencies and configured paths without changing the filesystem."""
    failures: list[str] = []
    try:
        validate_source_boundaries(config, extra_sources)
    except ConfigError as error:
        failures.append(str(error))
    for label, binary in (("ffmpeg", config.ffmpeg_bin), ("whisper.cpp", config.whisper_bin)):
        resolved = resolve_executable(binary)
        if resolved:
            log(f"OK {label}: {resolved}")
        else:
            failures.append(f"{label} executable not found: {binary}")
    if not config.model_path.is_file():
        failures.append(f"Whisper model not found: {config.model_path}")
    else:
        size = config.model_path.stat().st_size
        if size != MODEL_SIZE:
            failures.append(f"Whisper model has unexpected size: {size} bytes")
        elif sha256(config.model_path) != MODEL_SHA256:
            failures.append("Whisper model failed SHA-256 verification")
        else:
            log(f"OK Whisper model: {config.model_path}")
    for label, folder in (
        ("drop_folder", config.drop_folder),
        ("archive_folder parent", config.archive_folder.parent),
        ("output_folder parent", config.output_folder.parent),
        ("state_folder parent", config.state_folder.parent),
    ):
        if folder.exists():
            log(f"OK {label}: {folder}")
        else:
            failures.append(f"{label} does not exist: {folder}")
    if config.icloud_drop_folder is not None and not config.icloud_drop_folder.exists():
        failures.append(f"icloud_drop_folder does not exist: {config.icloud_drop_folder}")
    for source in extra_sources:
        if not source.is_dir():
            failures.append(f"additional source is not a directory: {source}")
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as response:
            tags = json.loads(response.read().decode("utf-8"))
        names = {item.get("name") for item in tags.get("models", []) if isinstance(item, dict)}
        if config.ollama_model not in names:
            failures.append(f"Ollama model is not installed: {config.ollama_model}")
        else:
            log(f"OK Ollama model: {config.ollama_model}")
    except (OSError, ValueError, urllib.error.URLError) as error:
        failures.append(f"cannot reach local Ollama at {OLLAMA_URL}: {error}")
    for failure in failures:
        log(f"ERROR {failure}")
    return 0 if not failures else 2


@contextmanager
def exclusive_lock(path: Path) -> Iterator[bool]:
    """Non-blocking lock across processes; yields False if another scan holds it."""
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def print_volumes() -> int:
    """List mounted volumes and the IDs to copy into recorder_volumes."""
    volumes = list_mounted_volumes()
    if not volumes:
        log("No candidate external volumes found. Plug in the recorder and try again.")
        return 0
    for root, volume_uuid in volumes:
        print(f"{volume_uuid or '(no ID available)'}\t{root}")
    return 0


def scan(config: Config, extra_sources: list[Path]) -> int:
    validate_source_boundaries(config, extra_sources)
    ensure_directories(config)
    if not config.model_path.exists():
        log("Whisper model missing. Run this script with --install.")
        return 2
    with exclusive_lock(config.lock_file) as acquired:
        if not acquired:
            log("Another voice-intake scan is already running.")
            return 0
        folders = discover_source_folders(config, extra_sources)
        recordings = discover_recordings(folders)
        state = load_state(config)
        processed = sum(process_recording(item, state, config) for item in recordings)
        log(f"Scan complete: {processed} new recording(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Path to the JSON configuration file.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--install", action="store_true", help="Create folders and download the Whisper model.")
    mode.add_argument("--check", action="store_true", help="Validate config and dependencies without making changes.")
    mode.add_argument("--list-volumes", action="store_true", help="Show mounted volumes and their IDs for recorder_volumes.")
    parser.add_argument("--source", action="append", default=[], type=Path, help="Additional explicit source folder; repeatable.")
    parser.add_argument("--log-file", type=Path, help="Also append log lines to this file.")
    return parser


def main(argv: list[str] | None = None) -> int:
    global LOG_FILE
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.log_file is not None:
        LOG_FILE = arguments.log_file.expanduser()
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if arguments.list_volumes:
        return print_volumes()
    if arguments.config is None:
        parser.error("--config is required")
    try:
        config = load_config(arguments.config)
        sources = [item.expanduser().resolve() for item in arguments.source]
        if arguments.check:
            return check_dependencies(config, sources)
        ensure_directories(config)
        if arguments.install:
            download_model(config)
            return 0
        return scan(config, sources)
    except (ConfigError, StateError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        log(f"ERROR {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
