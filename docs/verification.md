# Packaging verification

## Cross-platform release, November 17, 2026

Checked on Windows 11 (Python 3.14):

- 21 automated tests passed and 3 symlink tests were skipped, because Windows
  needs Developer Mode or admin rights to create symlinks. The tests cover
  configuration, source boundaries, archive integrity, retry and deduplication
  behavior, parser fallback, volume-ID parsing for all three platforms,
  cross-process locking, and the macOS, Windows, and Linux schedule files.
- `--list-volumes` read live drive serials, and they matched the Windows `vol`
  command.
- Task Scheduler accepted a generated task XML (`schtasks /Create /XML`). The
  test task was deleted right after.

Not checked in this release: a live whisper.cpp and Ollama run on Windows or
Linux, a real recorder on Linux, and a clean-machine install on any platform.

## Original macOS package, September 8, 2026

- 15 automated tests passed.
- A synthetic spoken shop note passed through FFmpeg, the whisper.cpp English
  small.en model, and the local Ollama gemma3:12b model.
- The resulting note extracted the keychain-ring order as an undated action
  and kept a tentative plywood-video concept as an idea.
- Archived audio matched the source byte for byte. A second scan produced
  zero new notes.
- The source package was checked for personal paths, device identifiers,
  account details, private recordings, and generated runtime files.

Live Todoist routing is a template that needs the user's own agent setup and
authorization; it is not exercised by these checks.
