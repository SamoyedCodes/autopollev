# AutoPollEv (Poll Everywhere automation and GUI tool)

AutoPollEv watches a [Poll Everywhere](https://pollev.com/) presenter for new
polls and votes on them, either by asking you first or automatically.

For how it works internally, why poll detection failed at first, and how that
was fixed, see [docs/TECHNICAL_REPORT.md](docs/TECHNICAL_REPORT.md).

### Features
- Detects new polls through Poll Everywhere's Firehose endpoint, so there is
  very little delay.
- Shows a popup with a button per option and a countdown, so you can pick an
  answer yourself.
- If you do not answer before the countdown ends (30 seconds by default), it
  picks a random option and submits that.
- Dark-themed Tkinter GUI with an editable host field, monitoring status, a
  resizable vote history table, and a log panel you can show or hide.
- Logs you in through a built-in browser window and stores the session cookie
  itself, so you never have to copy anything out of DevTools. If the cookie
  expires later, it logs in again on its own.
- Desktop notifications on macOS and Windows when a new poll appears.
- If the presenter locks a poll before you answer, that gets recorded as a
  failed vote instead of crashing.

### Getting started
1. **Install.** A Conda or Miniforge environment is easiest:
   ```bash
   git clone https://github.com/SamoyedCodes/autopollev.git
   cd autopollev
   conda create -n pollev python=3.11
   conda activate pollev
   pip install -r requirements.txt
   playwright install chromium
   ```
   That last line installs the browser used by the login window. Conda's Python
   already includes Tk, so the GUI works without extra setup.

   A plain virtualenv works too:
   `python3 -m venv .venv && source .venv/bin/activate`, then the same two
   install commands. If you are on Homebrew Python you need Tk once as well:
   `brew install python-tk@3.11` (match your Python version, no sudo needed).

2. **Log in.** You do not need to copy a cookie by hand. Either run
   `python main.py --gui` and click **Log in**, or run `python main.py --login`
   to do it from the terminal and exit afterwards.

   A Chromium window opens on pollev.com. Log in like normal and the window
   closes once the session cookie has been saved to `config.json`. The email
   address of the account it captured is then shown in the GUI and in the
   terminal, so you can check it grabbed the right account. The login is kept in
   `.pw_profile/`, so if the session expires while monitoring is running, it
   logs back in and carries on.

   `config.json` starts with `your-presenter-id` as a placeholder host. Put the
   real presenter ID in the **Host** field in the GUI and click **Save**. If
   monitoring is already running it will restart on the new presenter, and the
   value is written to `config.json` for next time.

   `config.json` is created on the first run and can be edited by hand:
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
   | Field | What it does |
   |---|---|
   | `host` | The presenter's ID on Poll Everywhere |
   | `polleverywhere_session_id` | Session cookie, filled in by the login window |
   | `poll_interval` | How often to check for a new poll, in seconds |
   | `answer_delay` | How long to wait before submitting a vote, in seconds |
   | `user_choice_timeout` | How long to wait for your answer before voting randomly |
   | `log_dir` | Where run logs and vote history go (`logs/` by default) |

   Pasting the cookie in yourself from DevTools still works if you would rather
   do that.

3. **Run it.**
   | Command | What it does |
   |---|---|
   | `python main.py` | GUI (the default) |
   | `python main.py --gui` | Same, and includes the login button |
   | `python main.py --login` | Capture the session cookie and exit |
   | `python main.py --cli` | Terminal mode, asks before each vote |
   | `python main.py --auto` | Votes randomly with no prompting |
   | `python main.py --history` | Print past votes and stats, then exit |

   Add `--config PATH` to any of these to use a different config file.

### Project layout
```
main.py               entry point, picks which mode to run
autopollev/
  config.py           loads, validates and saves config.json
  auth.py             checks the session cookie, reads the account identity
  session_capture.py  Playwright login window that grabs the cookie
  monitor.py          poll detection through Firehose
  voter.py            submits votes, random or chosen
  gui.py              the Tkinter interface
  notifier.py         desktop notifications
  logger.py           run log and vote history (JSONL)
  endpoints.py        Poll Everywhere URLs
  i18n.py             strings shown to the user
docs/TECHNICAL_REPORT.md   how detection failed and how it was fixed
tests/                unittest suite
```

### Windows build
Pushing a version tag makes GitHub Actions build the Windows version and attach
`AutoPollEv-windows.zip` to the release. The workflow is in
[.github/workflows/release.yml](.github/workflows/release.yml).

```bash
git tag v2.0.0 && git push origin v2.0.0
```

Chromium is included in the zip, so it is just unzip and run
`AutoPollEv.exe`. `config.json`, `logs/` and `.pw_profile/` are written next to
the exe. The build has no console window, so `--cli`, `--auto`, `--login` and
`--history` do not work there and fall back to the GUI. Run those from source
instead.

There is a **Run workflow** button on the Actions page if you want a test build
without tagging anything.

One thing to know: the exe is not code signed, so Windows Defender sometimes
flags it as `Trojan:Win32/Bearfoos.A!ml`. It is a false positive. Building with
`--noupx` and a version resource makes it happen less often. If you hit it, you
can report the file at
<https://www.microsoft.com/en-us/wdsi/filesubmission> and it gets cleared for
everyone. Buying a code signing certificate is the only real fix.

### Tests
Plain `unittest`, nothing extra to install:
```bash
python -m unittest discover -s tests
```

`tests/gui_smoke.py` is a manual check of the GUI, so run it directly. Pass
`--preview` to keep the windows open so you can look at them:
```bash
python tests/gui_smoke.py --preview
```

### Disclaimer
This was written as a learning project. Check the terms of service of the site
and the rules of your institution before using it.
