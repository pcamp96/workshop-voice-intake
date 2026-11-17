#!/usr/bin/env python3
"""Generate a background-scan schedule for this OS; never install or start it.

macOS:   a launchd agent plist (voice-intake.plist)
Windows: a Task Scheduler XML file (voice-intake-task.xml)
Linux:   a systemd user service and timer (voice-intake.service, voice-intake.timer)
"""
from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path, PurePath
import plistlib
import sys
from xml.sax.saxutils import escape

LABEL = "com.workshop.voice-intake"
INTERVAL_SECONDS = 60


def default_log_folder(platform: str) -> Path:
    if platform == "macos":
        return Path.home() / "Library" / "Logs" / "workshop-voice-intake"
    if platform == "windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "workshop-voice-intake" / "logs"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "workshop-voice-intake"


def build_plist(config: Path, python: Path, script: Path, logs: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(python), str(script), "--config", str(config)],
        "RunAtLoad": True,
        "StartInterval": INTERVAL_SECONDS,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PATH": os.pathsep.join(dict.fromkeys([
                str(python.parent), "/opt/homebrew/bin", "/usr/local/bin",
                "/usr/bin", "/bin", os.environ.get("PATH", ""),
            ]))
        },
        "StandardOutPath": str(logs / "voice-intake.log"),
        "StandardErrorPath": str(logs / "voice-intake-error.log"),
    }


def windows_quote(value: str) -> str:
    """Quote one argument for a Windows command line."""
    return '"' + value.replace('"', '\\"') + '"'


def build_task_xml(config: Path, python: Path, script: Path, logs: Path, start: datetime) -> str:
    # pythonw.exe runs without flashing a console window every minute.
    windowless = python.with_name("pythonw.exe")
    command = windowless if python.name.lower() == "python.exe" and windowless.is_file() else python
    arguments = " ".join(windows_quote(str(item)) for item in (
        script, "--config", config, "--log-file", logs / "voice-intake.log",
    ))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Workshop Voice Intake: scan enrolled recorders and drop folders every minute.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start.strftime("%Y-%m-%dT%H:%M:%S")}</StartBoundary>
      <Repetition>
        <Interval>PT1M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(command))}</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def systemd_quote(value: str) -> str:
    """Quote one argument for a systemd ExecStart line."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def build_systemd_units(config: PurePath, python: PurePath, script: PurePath) -> dict[str, str]:
    command = " ".join(systemd_quote(str(item)) for item in (python, script, "--config", config))
    path = ":".join(item for item in dict.fromkeys([
        str(python.parent), "/usr/local/bin", "/usr/bin", "/bin",
        os.environ.get("PATH", "") if os.name != "nt" else "",
    ]) if item)
    service = f"""[Unit]
Description=Workshop Voice Intake scan

[Service]
Type=oneshot
Environment={systemd_quote("PATH=" + path)}
ExecStart={command}
"""
    timer = f"""[Unit]
Description=Run Workshop Voice Intake every minute

[Timer]
OnBootSec={INTERVAL_SECONDS}s
OnUnitActiveSec={INTERVAL_SECONDS}s

[Install]
WantedBy=timers.target
"""
    return {"voice-intake.service": service, "voice-intake.timer": timer}


def detect_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return "linux"


def write_new(path: Path, data: bytes) -> None:
    # Exclusive creation avoids replacing an existing schedule.
    with path.open("xb") as handle:
        handle.write(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="Folder for the generated file(s); existing files are never replaced.")
    parser.add_argument("--platform", choices=["auto", "macos", "windows", "linux"], default="auto")
    args = parser.parse_args()
    platform = detect_platform() if args.platform == "auto" else args.platform
    config = args.config.expanduser().resolve()
    python = args.python.expanduser().resolve()
    script = Path(__file__).resolve().parents[1] / "voice_intake.py"
    for path in (config, python, script):
        if not path.is_file():
            parser.error(f"File does not exist: {path}")
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    if platform == "macos":
        logs = default_log_folder(platform)
        logs.mkdir(parents=True, exist_ok=True)
        target = output / "voice-intake.plist"
        write_new(target, plistlib.dumps(build_plist(config, python, script, logs)))
        written = [target]
    elif platform == "windows":
        logs = default_log_folder(platform)
        logs.mkdir(parents=True, exist_ok=True)
        target = output / "voice-intake-task.xml"
        xml = build_task_xml(config, python, script, logs, datetime.now())
        write_new(target, xml.encode("utf-16"))
        written = [target]
    else:
        written = []
        for name, text in build_systemd_units(config, python, script).items():
            target = output / name
            write_new(target, text.encode("utf-8"))
            written.append(target)
    for target in written:
        print(f"Generated {target}")
    print("Nothing was installed or started. See the README for the install command.")


if __name__ == "__main__":
    main()
