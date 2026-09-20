# 🗳️ AutoPollEv (PollEverywhere Automation & GUI Tool)

AutoPollEv is an advanced monitoring and voting tool for [PollEverywhere](https://pollev.com/). It monitors presenter activity in real time and supports interactive or automatic voting.

Detailed architecture, detection-failure analysis, remediation notes, tests,
and troubleshooting are available in
[docs/TECHNICAL_REPORT.md](docs/TECHNICAL_REPORT.md).

### ✨ Key Features
- **Real-time Monitoring**: Low-latency detection of new polls via Firehose.
- **Interactive Voting**: Pop-up notifications with option buttons and countdown timers.
- **Auto Fallback**: Automatically submits a random vote if no choice is made within the timeout (default 30s).
- **GUI Mode**: Compact ChatGPT/Codex-inspired dark Tkinter interface with editable host, monitoring status, resizable vote history, and a toggleable scrolling log.
- **Built-in Login Browser**: Captures and stores your `polleverywhere_session_id` automatically — no manual DevTools copying — and re-captures it if it expires.
- **System Notifications**: Native notifications for new polls on macOS and Windows.
- **Locked Poll Handling**: Automatically records failures if a poll is locked by the presenter.

### 🚀 Getting Started
1. **Clone & Install** (recommended: a Conda/Miniforge environment named `pollev`):
   ```bash
   git clone https://github.com/SamoyedCodes/autopollev.git
   cd autopollev
   conda create -n pollev python=3.11
   conda activate pollev
   pip install -r requirements.txt
   playwright install chromium          # browser used by the built-in login window
   ```
   Conda/Miniforge Python bundles Tk, so the Tkinter GUI works out of the box. The
   included `.vscode/settings.json` already points VS Code at this `pollev` interpreter.

   Prefer a plain virtualenv? Use `python3 -m venv .venv && source .venv/bin/activate`,
   then the same `pip install` / `playwright install` steps. On Homebrew Python you'll
   also need Tk once: `brew install python-tk@3.11` (match your Python version; no sudo).
2. **Log in — no manual cookie copying**:
   AutoPollEv has a built-in login browser that captures and stores your
   `polleverywhere_session_id` for you. Just:
   - `python main.py --gui`, then click **Log in**, **or**
   - `python main.py --login` (captures from the terminal, then exits).

   A Chromium window opens at `pollev.com`; log in normally and it closes itself
   once your session is captured and saved to `config.json`. Right after capture,
   AutoPollEv shows the **email of the account it read** (in both the GUI status
   panel and the CLI) so you can confirm the right PollEv account was captured.
   The login is remembered (in `.pw_profile/`), so if the session later expires
   while monitoring, AutoPollEv **re-captures it automatically** and keeps going.

   `config.json` ships with the placeholder host `your-presenter-id`. Enter your own Poll Everywhere presenter ID in the GUI's **Host** field and click
   **Save**. If monitoring is already active, AutoPollEv safely restarts it for the
   new presenter. The value is also persisted to `config.json` for future runs.

   `config.json` is generated on first run and can still be edited manually:
   ```json
   {
     "host": "your-presenter-id",
     "cookies": { "polleverywhere_session_id": "<captured automatically>" },
     "poll_interval": 5,
     "answer_delay": 2,
     "user_choice_timeout": 30,
     "log_dir": "logs"
   }
   ```
   | Field | Description |
   |---|---|
   | `host` | Presenter ID on PollEverywhere |
   | `polleverywhere_session_id` | Session cookie — captured for you by the login browser |
   | `poll_interval` | Polling interval in seconds |
   | `answer_delay` | Delay before submitting vote (seconds) |
   | `user_choice_timeout` | Seconds to wait for user choice before auto-submitting |
   | `log_dir` | Directory for run logs and vote history (default `logs/`) |

   (You can still paste the cookie in by hand from the browser DevTools if you prefer.)
3. **Run**:
   | Command | Mode |
   |---|---|
   | `python main.py` | GUI (default) |
   | `python main.py --gui` | GUI — includes the login/capture button |
   | `python main.py --login` | Capture the session cookie, then exit |
   | `python main.py --cli` | Interactive terminal — notifies and waits for your choice |
   | `python main.py --auto` | Fully automatic — silent random vote |
   | `python main.py --history` | Print vote history and statistics, then exit |

   `--config PATH` points any of these at an alternate config file.

### 🗂️ Project Layout
```
main.py               CLI entry point and mode dispatch
autopollev/
  config.py           config.json load / validate / save
  auth.py             session cookie validation, account identity
  session_capture.py  Playwright login window that captures the cookie
  monitor.py          Firehose poll detection
  voter.py            vote submission (random and interactive)
  gui.py              Tkinter interface
  notifier.py         native desktop notifications
  logger.py           run log + vote history (JSONL)
  endpoints.py        PollEverywhere URLs
  i18n.py             user-facing strings
docs/TECHNICAL_REPORT.md   detection-failure analysis and remediation
tests/                unittest suite
```

### 📦 Windows Release
Tagging a version builds the Windows bundle on GitHub Actions
(`.github/workflows/release.yml`) and attaches `AutoPollEv-windows.zip` to the
release:
```bash
git tag v2.0.0 && git push origin v2.0.0
```
Chromium is baked into the bundle, so users just unzip and run
`AutoPollEv.exe` — `config.json`, `logs/` and `.pw_profile/` are written next to
the executable. The build is windowed (GUI only, no console); `--cli`, `--auto`,
`--login` and `--history` are source-run modes and fall back to the GUI there.

Use the workflow's **Run workflow** button to build a test zip
without tagging.

**Defender false positives:** the build is unsigned, so Windows Defender's ML
heuristics may quarantine it as `Trojan:Win32/Bearfoos.A!ml`. `--noupx` and the
version resource reduce this; report any hit at
<https://www.microsoft.com/en-us/wdsi/filesubmission> to get it cleared for
everyone. A code-signing certificate is the only durable fix.

### 🧪 Tests
Stdlib `unittest` only — no test dependencies:
```bash
python -m unittest discover -s tests
```
`tests/gui_smoke.py` is a manual GUI check, run directly; add `--preview` to
leave the mock windows open for inspection:
```bash
python tests/gui_smoke.py --preview
```

### ⚠️ Disclaimer
This tool is for educational purposes only. Please comply with the terms of service of the target platform and your institution.
