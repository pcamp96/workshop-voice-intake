#!/usr/bin/env python3
"""Generate a macOS launch-agent plist; never install or start it."""
import argparse
import os
from pathlib import Path
import plistlib
import sys

LABEL = "com.createdworkshop.voice-intake"


def build_plist(config: Path, python: Path, script: Path, logs: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(python), str(script), "--config", str(config)],
        "RunAtLoad": True,
        "StartInterval": 60,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PATH": os.pathsep.join(dict.fromkeys([
                str(python.parent), "/opt/homebrew/bin", "/usr/local/bin",
                "/usr/bin", "/bin", os.environ.get("PATH", "")
            ]))
        },
        "StandardOutPath": str(logs / "voice-intake.log"),
        "StandardErrorPath": str(logs / "voice-intake-error.log"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = args.config.expanduser().resolve()
    python = args.python.expanduser().resolve()
    script = Path(__file__).resolve().parents[1] / "voice_intake.py"
    for path in (config, python, script):
        if not path.is_file():
            parser.error(f"File does not exist: {path}")
    output = args.output.expanduser().resolve()
    logs = Path.home() / "Library" / "Logs" / "workshop-voice-intake"
    logs.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids replacing an existing user's schedule.
    with output.open("xb") as handle:
        plistlib.dump(build_plist(config, python, script, logs), handle)
    print(f"Generated {output}. Nothing was installed or started.")


if __name__ == "__main__":
    main()
