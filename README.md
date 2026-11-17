# Workshop Voice Intake

Record ideas while you work. Plug in your recorder later and turn the recordings
into searchable notes, task candidates, decisions, and workshop observations.

A single Python script for macOS, Windows, and Linux, with optional background
scanning. It uses only the Python standard library, so there's nothing to
`pip install`.

```text
USB recorder or drop folder
          ↓
Local audio archive → whisper.cpp transcription → Ollama parsing
                                                       ↓
                                               Markdown notes
                                                       ↓ optional, separate
                                            Agent review → Todoist / notes
```

**Todoist is a separate step.** The script writes task candidates into Markdown.
An agent that has its own Todoist connection can review and route them using the
[routing template](docs/agent-routing.md). Installing this repository alone does
not create Todoist tasks.

## Requirements

- Python 3.11 or newer.
- [FFmpeg](https://ffmpeg.org/), [whisper.cpp](https://github.com/ggml-org/whisper.cpp)
  (the `whisper-cli` program), and a running local [Ollama](https://ollama.com/download).
- Space for the speech model (about 466 MiB), an Ollama model, and the audio you keep.
  Parsing speed and memory use depend on the model and your computer.
- A recorder that shows up as a USB drive, or audio exported to a folder.
  You don't need a recorder with its own AI subscription.

### Install the dependencies

**macOS** (Homebrew):

```sh
brew install python ffmpeg whisper-cpp
```

**Windows** (winget, PowerShell):

```powershell
winget install Python.Python.3.12 Gyan.FFmpeg Ollama.Ollama
```

For whisper.cpp, download a Windows build from the
[whisper.cpp releases page](https://github.com/ggml-org/whisper.cpp/releases)
(the builds with downloads are tagged `b` plus a number). Pick `whisper-bin-x64.zip`
for most PCs, a `cuda` build for NVIDIA GPUs, or `whisper-bin-win-cpu-arm64.zip`
for Arm PCs. Unzip it
and either add the folder to your `PATH` or set `whisper_bin` in your config to
the full path of `whisper-cli.exe`.

**Linux** (Debian/Ubuntu example):

```sh
sudo apt install python3 ffmpeg
curl -fsSL https://ollama.com/install.sh | sh
```

Then either download a prebuilt `whisper-bin-ubuntu-x64.tar.gz` or
`whisper-bin-ubuntu-arm64.tar.gz` from the
[releases page](https://github.com/ggml-org/whisper.cpp/releases), or build it by
following the [quick start](https://github.com/ggml-org/whisper.cpp#quick-start). Point
`whisper_bin` at the resulting `build/bin/whisper-cli`, or put it on your `PATH`.

On every platform, download the parsing model once Ollama is running:

```sh
ollama pull gemma3:12b
```

## Set up a manual scan first

Download or clone the repository and open a terminal in its folder. On Windows,
use `python` (or `py`) wherever these steps say `python3`.

1. Create a private configuration:

   ```sh
   cp config.example.json config.json
   ```

   (Windows PowerShell: `Copy-Item config.example.json config.json`)

   Edit `config.json` to choose your drop, archive, and output folders. Paths can
   use `~` for your home folder, and Windows paths work too (in JSON, write
   `C:/Users/you/...` or escape backslashes as `C:\\Users\\you\\...`). To use
   Obsidian, set `output_folder` to an inbox folder inside your own vault.
   Obsidian is optional; the output is ordinary Markdown. Keep input and output
   folders separate. `config.json` is excluded from Git.

2. Download the Whisper model, then check dependencies:

   ```sh
   python3 voice_intake.py --config config.json --install
   python3 voice_intake.py --config config.json --check
   ```

   `--install` creates the configured folders and downloads the checksum-verified
   English speech model. It doesn't install FFmpeg, whisper.cpp, or Ollama.

3. Put a short throwaway recording in the drop folder. Wait at least 15 seconds
   after the copy finishes, then run:

   ```sh
   python3 voice_intake.py --config config.json
   ```

   Check the note, transcript, and archive. Run the command again to confirm it
   reports zero new recordings. The original recording stays where it was.
   The included [fictional capture](examples/maker-capture.md) shows how tasks
   and ideas should be separated; it isn't an accuracy benchmark.

## Connect your recorder

Automatic discovery only scans volumes you've enrolled by ID. An unrelated USB
drive with a `RECORD` folder is ignored.

With your recorder plugged in, list the mounted volumes and their IDs:

```sh
python3 voice_intake.py --list-volumes
```

| Platform | ID shown | Where it comes from |
|---|---|---|
| macOS | Volume UUID, like `1A2B3C4D-...` | `diskutil info /Volumes/NAME` |
| Windows | Volume serial, like `48B1-FFA2` | `vol E:` |
| Linux | Filesystem UUID, like `ABCD-1234` | `lsblk -o NAME,UUID,MOUNTPOINT` |

Copy your recorder's ID into `recorder_volumes` in your private configuration:

```json
"recorder_volumes": {
  "YOUR-VOLUME-ID": "My recorder"
}
```

The importer scans common recording folders such as `RECORD`, `[RECORD]`, and
`Recordings` on enrolled volumes. It doesn't scan the whole drive. For a
different layout, pass the recording folder on each scan:

```sh
python3 voice_intake.py --config config.json --source "/Volumes/YOUR RECORDER/Audio"
python  voice_intake.py --config config.json --source "E:\Audio"
```

Enrollment follows the filesystem ID even if the volume name or drive letter
changes. Reformatting a recorder changes its ID, so enroll it again afterward.
Recorders that only charge over USB need an export-to-folder workflow instead.
On Windows, the system drive and network drives are never scanned. On Linux, the
recorder must be mounted (most desktops do this automatically under `/media` or
`/run/media`).

## Optional background scanning

After the manual test works, generate a schedule for your platform. The helper
writes files only; it never installs or starts anything, and it won't overwrite
existing files.

```sh
python3 scripts/make_schedule.py --config config.json --output-dir schedule
```

It scans every 60 seconds while you're logged in and the computer is awake, and
it won't wake a sleeping machine. Ollama must also be running. The generated
files contain absolute paths to this folder, your Python, and your config, so
keep them out of Git and generate them again if you move the folder. Explicit
`--source` folders aren't included; enroll USB devices or use your drop folder.

**macOS** writes `schedule/voice-intake.plist`. Install it with:

```sh
mkdir -p "$HOME/Library/LaunchAgents"
cp -n schedule/voice-intake.plist "$HOME/Library/LaunchAgents/com.workshop.voice-intake.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.workshop.voice-intake.plist"
```

Logs go to `~/Library/Logs/workshop-voice-intake/`. To stop it:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.workshop.voice-intake.plist"
```

**Windows** writes `schedule\voice-intake-task.xml`, which runs the script with
`pythonw.exe` so no console window flashes. Install it with:

```powershell
schtasks /Create /XML schedule\voice-intake-task.xml /TN "Workshop Voice Intake"
```

Logs go to `%LOCALAPPDATA%\workshop-voice-intake\logs\voice-intake.log`. To stop
it: `schtasks /Delete /TN "Workshop Voice Intake" /F`.

**Linux** writes `schedule/voice-intake.service` and `schedule/voice-intake.timer`
as systemd user units. Install them with:

```sh
mkdir -p ~/.config/systemd/user
cp -n schedule/voice-intake.service schedule/voice-intake.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now voice-intake.timer
```

View logs with `journalctl --user -u voice-intake.service`. To stop it:
`systemctl --user disable --now voice-intake.timer`.

You can also pass `--log-file PATH` to `voice_intake.py` yourself to append log
lines to a file, which is useful with cron or any other scheduler.

## Configuration reference

Required keys: `drop_folder`, `archive_folder`, `output_folder`.

Optional keys:

| Key | Purpose |
|---|---|
| `state_folder` | Deduplication state and lock. Defaults to `%LOCALAPPDATA%\workshop-voice-intake` on Windows and `~/.local/share/workshop-voice-intake` elsewhere |
| `model_path` | Path to the English Whisper model |
| `ollama_model` | Installed local parser model; default `gemma3:12b` |
| `whisper_prompt` | Vocabulary hint for names and maker terms |
| `brands` | Categories used to label notes; include `Unclear` |
| `recorder_volumes` | Map of enrolled volume ID to a readable label |
| `icloud_drop_folder` | Optional extra input folder, such as an iCloud Drive, OneDrive, or Dropbox folder; off unless set |
| `ffmpeg_bin`, `whisper_bin` | Program names or full paths; otherwise found on `PATH` |

Give each separate configuration its own `state_folder`. Sharing state between
different output folders can cause recordings to be skipped as already processed.

## Data handling and practical limits

Transcription and parsing use local tools. Model downloads need internet access.
The optional routing agent may use a cloud model, and a synced drop folder uploads
whatever you put in it. Choose those separately.

Source recordings are never deleted. Archives, notes, logs, and state stay on your
machine and can contain private information. This repository includes no real
recordings, personal configuration, model weights, or credentials.

Speech recognition can mishear names and numbers. The parser can invent or
misclassify actions despite its instructions. Review the output before relying on
deadlines, measurements, or machine settings. Capture times come from file
modification times, which recorder exports can change. The speech model and
transcription settings in this release are English-only.

Scans ignore MP4 video and audio symlinks that point outside the selected input
folder. The 15-second age check is a settling delay, not proof that a long copy
has finished, so let copies finish before scanning. Keep the original audio until
you've checked the archive and transcript. Nothing deletes old files automatically.

If transcription fails, the recording is retried on the next scan; check the log
and fix the dependency or audio problem. If only Ollama parsing fails, the
transcript is saved in a `transcript-only` note and isn't processed again on
later scans. Review that transcript directly or let your routing agent handle
it; this release doesn't automatically reparse existing notes. A damaged state
file stops processing instead of silently resetting duplicate protection.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic files and mocked tools. They don't prove recognition
accuracy, a real USB-device workflow, or live Todoist delivery. Symlink tests
are skipped on Windows unless symlinks are allowed (Developer Mode or admin).

Released under the [MIT License](LICENSE). The code doesn't bundle third-party
tools or model weights; their own licenses still apply.
