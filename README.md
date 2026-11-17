# Workshop Voice Intake

Record ideas while you work. Plug in your recorder later and turn the recordings
into searchable notes, task candidates, decisions, and workshop observations.

This is a shareable version of the local intake workflow built for Created
Workshop. It is a Python command-line tool for macOS, with optional background
scanning. There is no Python package dependency to install.

```text
USB recorder or drop folder
          ↓
Local audio archive → whisper.cpp transcription → Ollama parsing
                                                       ↓
                                               Markdown notes
                                                       ↓ optional, separate
                                            Agent review → Todoist / notes
```

**Todoist is a separate step.** The script creates task candidates in Markdown.
An agent with your own Todoist connection can review and route them using the
[routing template](docs/agent-routing.md). Installing this repository alone does
not create Todoist tasks.

## Requirements

- macOS and Python 3.11 or newer. Windows is not supported. Linux has not been
  validated, and automatic USB discovery and background setup are macOS-specific.
- [Homebrew](https://brew.sh/) for the setup commands below.
- [FFmpeg](https://ffmpeg.org/), [whisper.cpp](https://github.com/ggml-org/whisper.cpp),
  and a running local [Ollama](https://docs.ollama.com/quickstart) installation.
- Space for the speech model (about 466 MiB), an Ollama model, and retained audio.
  Parsing speed and memory requirements depend on the model and your Mac.
- A recorder that exposes audio files over USB, or audio exported to a folder.
  A recorder with its own AI subscription is not required by this software.

## Set up a manual scan first

Download or clone the repository and open a terminal in its folder.

1. Install the command-line dependencies:

   ```sh
   brew install python ffmpeg whisper-cpp
   ```

   The [Homebrew whisper-cpp formula](https://formulae.brew.sh/formula/whisper-cpp)
   supplies `whisper-cli`. Install and open
   [Ollama for macOS](https://ollama.com/download/mac), then download the parser:

   ```sh
   ollama pull gemma3:12b
   ```

2. Create a private configuration:

   ```sh
   cp config.example.json config.json
   ```

   Edit `config.json` to choose your drop, archive, and output folders. To use
   Obsidian, set `output_folder` to an inbox folder inside your own vault. Obsidian
   is optional; the result is ordinary Markdown. Keep input and generated-output
   folders separate. Local `config.json` is excluded from Git.

3. Download the Whisper model, then check dependencies:

   ```sh
   python3 voice_intake.py --config config.json --install
   python3 voice_intake.py --config config.json --check
   ```

   `--install` creates the configured folders and downloads the checksum-verified
   English speech model. It does not install FFmpeg, whisper.cpp, or Ollama.

4. Place a short disposable recording in the drop folder. Wait at least 15
   seconds after the copy finishes, then run:

   ```sh
   python3 voice_intake.py --config config.json
   ```

   Inspect the note, transcript, and archive. Run the command again to verify
   that it reports zero new recordings. The original recording stays in place.
   The included [fictional capture](examples/maker-capture.md) illustrates how
   tasks and ideas should be separated; it is not an accuracy benchmark.

## Connect your recorder

Automatic discovery only scans enrolled volume UUIDs. An unrelated USB drive
with a `RECORD` folder is ignored.

With your recorder mounted, inspect its identity (replace the example name):

```sh
diskutil info '/Volumes/YOUR RECORDER'
```

Copy the **Volume UUID** into `recorder_volumes` in your private configuration:

```json
"recorder_volumes": {
  "YOUR-VOLUME-UUID": "My recorder"
}
```

The importer scans common recording directories such as `RECORD`, `[RECORD]`,
and `Recordings` on enrolled volumes. It does not scan the entire drive. For a
different layout, select the recording folder explicitly on each scan:

```sh
python3 voice_intake.py --config config.json --source '/Volumes/YOUR RECORDER/Audio'
```

Enrollment follows the filesystem UUID even when the volume name changes.
Reformatting a recorder can change its UUID. Devices that only charge over USB
need an export-to-folder workflow instead.

## Optional background scanning

After the manual test succeeds, generate a launch-agent file:

```sh
python3 scripts/make_launch_agent.py --config config.json --output voice-intake.plist
plutil -lint voice-intake.plist
```

The helper creates a plist and a log directory; it does not install or start a
service. The file contains absolute paths to this checkout, your Python, and
your private configuration. Keep it out of Git. To install it yourself:

```sh
mkdir -p "$HOME/Library/LaunchAgents"
cp -n voice-intake.plist "$HOME/Library/LaunchAgents/com.createdworkshop.voice-intake.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.createdworkshop.voice-intake.plist"
```

This scans every 60 seconds while you are logged in and the Mac is awake. It
does not wake a sleeping Mac. Ollama must also be running. Logs are under
`~/Library/Logs/workshop-voice-intake/`. Moving this checkout requires regenerating
the plist. Explicit `--source` folders are not added by the helper; enroll USB
devices or use your configured drop folder for background scans.

To stop the service:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.createdworkshop.voice-intake.plist"
```

## Configuration reference

Required keys: `drop_folder`, `archive_folder`, `output_folder`.

Optional keys:

| Key | Purpose |
|---|---|
| `state_folder` | Deduplication state and lock; defaults to `~/.local/share/workshop-voice-intake` |
| `model_path` | Path to the English Whisper model |
| `ollama_model` | Installed local parser model; default `gemma3:12b` |
| `whisper_prompt` | Vocabulary hint for names and maker terminology |
| `brands` | Categories used to label notes; include `Unclear` |
| `recorder_volumes` | Enrolled volume UUID to readable label map |
| `icloud_drop_folder` | Optional additional input folder; disabled unless set |
| `ffmpeg_bin`, `whisper_bin` | Executable paths; otherwise resolved from `PATH` |

Give each independent configuration its own `state_folder`. Sharing state across
different output folders can cause recordings to be skipped as already processed.

## Data handling and practical limits

Audio transcription and parsing use local tools. Model downloads require internet
access. The optional routing agent may use a cloud model, and iCloud or another
synced folder may upload stored files. Choose those separately.

Source recordings are never deleted. Archives, notes, logs, and state stay on your
machine and can contain private information. This repository includes no real
captures, personal configuration, model weights, or credentials.

Speech recognition can mishear names and numbers. The parser can invent or
misclassify actions despite its instructions. Review output before relying on
deadlines, measurements, or machine settings. Capture timestamps come from file
modification times, which recorder exports can change. The speech model and
transcription settings in this release are English-only.

Scans ignore MP4 video and audio symlinks escaping the selected input folder.
The 15-second age check is a settling delay, not proof a long copy has finished;
finish copying before scanning. Keep original audio until you have verified the
archive and transcript. No retention or automatic cleanup policy is installed.

If transcription fails, the recording remains eligible for the next scan; inspect
the log and fix the dependency or audio problem. If only Ollama parsing fails,
the transcript is preserved in a `transcript-only` note and is deduplicated on
later scans. Review that transcript directly or let your routing agent process
it; this release does not automatically reparse existing notes. A damaged state
file stops processing rather than silently resetting duplicate protection.

## Development and publication

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic fixtures and mocked tools. They do not prove recognition
accuracy, a real USB-device workflow, or live Todoist delivery. The GitHub Actions
workflow runs the tests on macOS after publication.

Released under the [MIT License](LICENSE). The code does not bundle third-party
tools or model weights; their respective licenses still apply.
