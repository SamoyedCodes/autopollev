"""
AutoPollEv GUI module.

A tkinter-based interface providing:
- a monitoring status panel
- a vote history table
- a new-poll dialog (option buttons + countdown progress bar)
- a background monitoring thread
"""

import tkinter as tk
from tkinter import font as tkfont, ttk, messagebox
import threading
import random
import sys
import time
import queue
import textwrap
from typing import Optional

from .config import Config, ConfigError
from .auth import Auth, AuthError, CookieExpiredError, PresenterNotFoundError, account_summary
from .monitor import PollMonitor
from .voter import Voter, VoteError
from .logger import setup_logger, VoteHistoryLogger
from .notifier import notify_new_poll
from .endpoints import ENDPOINTS
from . import session_capture
from .i18n import _

logger = setup_logger("autopollev.gui")

# Fonts. These are only the fallbacks: once a root window exists,
# _adopt_system_fonts() replaces them with whatever the OS draws its own
# windows in, so the app matches the desktop instead of naming families that
# may render differently (or not exist) on someone else's machine.
if sys.platform == "darwin":
    UI_FONT = "Helvetica Neue"
    MONO_FONT = "Menlo"
elif sys.platform == "win32":
    UI_FONT = "Segoe UI"
    MONO_FONT = "Consolas"
else:
    UI_FONT = "DejaVu Sans"
    MONO_FONT = "DejaVu Sans Mono"


def _adopt_system_fonts(root):
    """Point UI_FONT/MONO_FONT at the platform's own interface faces.

    Tk's named fonts already resolve to them — .AppleSystemUIFont (SF) and
    Menlo on macOS, Segoe UI and Consolas on Windows — and both can be
    requested back by name, so this needs no per-platform table.
    """
    global UI_FONT, MONO_FONT
    try:
        UI_FONT = tkfont.nametofont("TkDefaultFont", root).actual()["family"]
        MONO_FONT = tkfont.nametofont("TkFixedFont", root).actual()["family"]
    except Exception:  # noqa: BLE001 - the per-platform fallbacks still work
        logger.debug("Falling back to %s/%s", UI_FONT, MONO_FONT)

# Placeholder prefix for an unset session cookie (config.py's default template
# writes a value that starts with "<").
_PLACEHOLDER_PREFIX = "<"


# Shared neutral theme for widgets and runtime status updates.
BG = "#181818"
SURFACE = "#212121"
BORDER = "#353535"
HOVER = "#303030"
TEXT = "#ECECEC"
MUTED = "#A0A0A0"
PRIMARY_HOVER = "#D4D4D4"
SUCCESS = "#85D6A1"
WARNING = "#F1CA7F"
ERROR = "#F28B96"


class PollDialog(tk.Toplevel):
    """
    New-poll dialog.

    Shows the poll title and option buttons with a countdown progress bar.
    The user picks an option, or a random one is submitted on timeout.
    """

    def __init__(self, parent, poll_title: str, options: list[dict],
                 timeout: float = 30):
        super().__init__(parent)
        self.title(_("dialog.title"))
        self.configure(bg=BG)
        self.resizable(False, False)

        self.poll_title = poll_title
        self.options = options
        self.timeout = timeout
        self.selected_index: Optional[int] = None
        self._cancelled = False

        # Keep the window on top
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._on_timeout)

        self._build_ui()
        self._start_countdown()

        # Center the window
        self.update_idletasks()
        w = self.winfo_width()
        h = self.winfo_height()
        x = (self.winfo_screenwidth() // 2) - (w // 2)
        y = (self.winfo_screenheight() // 2) - (h // 2)
        self.geometry(f"+{x}+{y}")

        # Force the alert to surface
        self.lift()
        self.focus_force()
        self.bell()

        # If the parent window is minimized, restore it
        if parent.state() == 'iconic':
            parent.deiconify()
        parent.lift()

    def _build_ui(self):
        """Build the dialog UI."""
        ttk.Style(self).configure("Answer.AutoPollEv.TButton", anchor="w", padding=(14, 10))
        # Title area
        title_frame = tk.Frame(self, bg=SURFACE, padx=20, pady=15)
        title_frame.pack(fill="x")

        tk.Label(
            title_frame, text=_("dialog.sub_header"),
            font=(UI_FONT, 11, "bold"), fg=TEXT, bg=SURFACE
        ).pack(anchor="w")

        tk.Label(
            title_frame, text=self.poll_title,
            font=(UI_FONT, 14, "bold"), fg=TEXT, bg=SURFACE,
            wraplength=400, justify="left"
        ).pack(anchor="w", pady=(5, 0))

        # Options area
        options_frame = tk.Frame(self, bg=BG, padx=20, pady=10)
        options_frame.pack(fill="x")

        tk.Label(
            options_frame, text=_("dialog.prompt"),
            font=(UI_FONT, 10), fg=MUTED, bg=BG
        ).pack(anchor="w", pady=(0, 8))

        for i, option in enumerate(self.options):
            value = option.get('value', option.get('keyword', f'Option {i+1}'))

            btn = ttk.Button(
                options_frame, text=textwrap.fill(f"{i+1}. {value}", width=48),
                style="Answer.AutoPollEv.TButton", cursor="hand2", takefocus=True,
                command=lambda idx=i: self._on_select(idx),
            )
            btn.pack(fill="x", pady=3)
            btn.bind("<Return>", lambda event, idx=i: self._on_select(idx))

        # Countdown area
        countdown_frame = tk.Frame(self, bg=BG, padx=20, pady=15)
        countdown_frame.pack(fill="x")

        self.countdown_label = tk.Label(
            countdown_frame,
            text=_("dialog.countdown", sec=int(self.timeout)),
            font=(UI_FONT, 9), fg=MUTED, bg=BG
        )
        self.countdown_label.pack(anchor="w")

        style = ttk.Style()
        style.configure(
            "countdown.Horizontal.TProgressbar",
            troughcolor=SURFACE,
            background=TEXT,
            thickness=4, borderwidth=0, lightcolor=TEXT, darkcolor=TEXT,
        )

        self.progress = ttk.Progressbar(
            countdown_frame,
            style="countdown.Horizontal.TProgressbar",
            orient="horizontal",
            length=360,
            mode="determinate",
            maximum=self.timeout,
            value=self.timeout,
        )
        self.progress.pack(fill="x", pady=(5, 0))

    def _start_countdown(self):
        """Start the countdown."""
        self._remaining = self.timeout
        self._tick()

    def _tick(self):
        """Update the countdown once per second."""
        if self._cancelled or self.selected_index is not None:
            return

        self._remaining -= 1
        self.progress["value"] = max(0, self._remaining)
        self.countdown_label.config(
            text=_("dialog.countdown", sec=max(0, int(self._remaining)))
        )

        if self._remaining <= 0:
            self._on_timeout()
        else:
            self.after(1000, self._tick)

    def _on_select(self, index: int):
        """The user selected an option."""
        self.selected_index = index
        self.destroy()

    def _on_timeout(self):
        """Timeout — pick a random option automatically."""
        if self.selected_index is None:
            self.selected_index = random.randint(0, len(self.options) - 1)
        self._cancelled = True
        self.destroy()


class AutoPollEvGUI:
    """
    Main AutoPollEv GUI class.

    Provides monitoring status, vote history, and the new-poll dialog.
    """

    def __init__(self, config: Config):
        self.config = config
        self.root = tk.Tk()
        _adopt_system_fonts(self.root)
        self.root.title(_("gui.title"))
        self.root.configure(bg=BG)
        self.root.minsize(600, 420)

        self.auth: Optional[Auth] = None
        self.monitor: Optional[PollMonitor] = None
        self.voter: Optional[Voter] = None
        self.history_logger = VoteHistoryLogger(config.log_dir)
        self._monitoring = False
        self._capturing = False
        self._restart_pending = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._event_queue: queue.Queue = queue.Queue()

        self._build_ui()
        self._load_history()
        self._check_session_state()
        self._process_events()

    def _build_ui(self):
        """Compact controls above an expanding history table."""
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "AutoPollEv.TButton", font=(UI_FONT, 11), padding=(12, 6),
            background=SURFACE, foreground=TEXT, bordercolor=BORDER,
            lightcolor=SURFACE, darkcolor=SURFACE, relief="flat", focuscolor=TEXT,
        )
        style.map(
            "AutoPollEv.TButton",
            background=[("disabled", SURFACE), ("pressed", BORDER), ("active", HOVER)],
            foreground=[("disabled", MUTED)],
            bordercolor=[("focus", TEXT)],
        )
        style.configure("Primary.AutoPollEv.TButton", background=TEXT, foreground=BG,
                        bordercolor=TEXT, lightcolor=TEXT, darkcolor=TEXT, focuscolor=BG)
        style.map("Primary.AutoPollEv.TButton",
                  background=[("disabled", BORDER), ("pressed", PRIMARY_HOVER),
                              ("active", PRIMARY_HOVER)],
                  foreground=[("disabled", MUTED), ("!disabled", BG)])
        style.configure("Stop.AutoPollEv.TButton", foreground=ERROR)

        header = tk.Frame(self.root, bg=BG, padx=14, pady=10)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(
            header, text=_("gui.header"), font=(UI_FONT, 14, "bold"),
            fg=TEXT, bg=BG,
        ).pack(side="left")
        self.status_label = tk.Label(
            header, text=_("gui.status.idle"), font=(UI_FONT, 10),
            fg=MUTED, bg=BG,
        )
        self.status_label.pack(side="right")

        controls = tk.Frame(self.root, bg=SURFACE, padx=12, pady=10,
                            highlightthickness=1, highlightbackground=BORDER)
        controls.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))
        controls.columnconfigure(1, weight=1)
        tk.Label(
            controls, text=_("gui.host"), font=(UI_FONT, 10),
            fg=MUTED, bg=SURFACE,
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.host_var = tk.StringVar(value=self.config.host)
        self.host_entry = tk.Entry(
            controls, textvariable=self.host_var, width=18,
            font=(UI_FONT, 11), fg=TEXT, bg=BG,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=TEXT,
            disabledforeground=MUTED, disabledbackground=BG,
        )
        self.host_entry.grid(row=0, column=1, sticky="ew", ipady=4)
        self.host_entry.bind("<Return>", self._save_host)
        self.host_save_btn = ttk.Button(
            controls, text=_("gui.host.save"), style="AutoPollEv.TButton",
            cursor="hand2", takefocus=True, command=self._save_host,
        )
        self.host_save_btn.grid(row=0, column=2, padx=(6, 0))

        session_row = tk.Frame(controls, bg=SURFACE)
        session_row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 8))
        session_row.columnconfigure(1, weight=1)
        tk.Label(
            session_row, text=_("gui.session_label"), font=(UI_FONT, 10),
            fg=MUTED, bg=SURFACE,
        ).grid(row=0, column=0, sticky="nw", padx=(0, 6))
        self.session_status_label = tk.Label(
            session_row, text=_("gui.session.ready"), font=(UI_FONT, 10),
            fg=SUCCESS, bg=SURFACE, anchor="w", justify="left",
            wraplength=440,
        )
        self.session_status_label.grid(row=0, column=1, sticky="ew")
        session_row.bind("<Configure>", lambda event: self.session_status_label.config(
            wraplength=max(100, event.width - 70)
        ))

        actions = tk.Frame(controls, bg=SURFACE)
        actions.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.start_btn = ttk.Button(
            actions, text=_("gui.btn_start"), style="Primary.AutoPollEv.TButton",
            cursor="hand2", takefocus=True, command=self._toggle_monitoring,
        )
        self.start_btn.pack(side="left")
        self.capture_btn = ttk.Button(
            actions, text=_("gui.btn_capture"), style="AutoPollEv.TButton",
            cursor="hand2", takefocus=True, command=self._capture_session,
        )
        self.capture_btn.pack(side="left", padx=6)
        self.log_text_btn = ttk.Button(
            actions, text=_("gui.btn_log"), style="AutoPollEv.TButton",
            cursor="hand2", takefocus=True, command=self._toggle_log,
        )
        self.log_text_btn.pack(side="right")

        history_frame = tk.Frame(self.root, bg=BG)
        history_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        history_frame.columnconfigure(0, weight=1)
        history_frame.rowconfigure(1, weight=1)
        history_header = tk.Frame(history_frame, bg=BG)
        history_header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        tk.Label(
            history_header, text=_("gui.history_frame"), font=(UI_FONT, 10, "bold"),
            fg=TEXT, bg=BG,
        ).pack(side="left")
        tk.Label(
            history_header, text=f'{_("gui.timeout_label")} {int(self.config.user_choice_timeout)}s',
            font=(UI_FONT, 9), fg=MUTED, bg=BG,
        ).pack(side="right")
        self.vote_count_label = tk.Label(
            history_header, text="0", font=(UI_FONT, 9, "bold"),
            fg=TEXT, bg=BG,
        )
        self.vote_count_label.pack(side="right", padx=(0, 14))
        tk.Label(
            history_header, text=_("gui.votes_count"), font=(UI_FONT, 9),
            fg=MUTED, bg=BG,
        ).pack(side="right", padx=(0, 4))

        style.configure(
            "history.Treeview", background=SURFACE, foreground=TEXT,
            fieldbackground=SURFACE, font=(UI_FONT, 10), rowheight=27,
            borderwidth=0,
        )
        style.configure(
            "history.Treeview.Heading", background=SURFACE, foreground=MUTED,
            font=(UI_FONT, 9, "bold"), relief="flat", padding=(6, 5),
        )
        style.map("history.Treeview", background=[("selected", HOVER)],
                  foreground=[("selected", TEXT)])
        style.map("history.Treeview.Heading", background=[("active", HOVER)])
        style.configure("AutoPollEv.Vertical.TScrollbar", background=BORDER,
                        troughcolor=SURFACE, arrowcolor=MUTED, borderwidth=0)
        style.configure("AutoPollEv.Horizontal.TScrollbar", background=BORDER,
                        troughcolor=SURFACE, arrowcolor=MUTED, borderwidth=0)
        columns = ("time", "title", "option", "status")
        self.history_tree = ttk.Treeview(
            history_frame, columns=columns, show="headings",
            style="history.Treeview", height=5,
        )
        for column, width, minimum in (
            ("time", 135, 130), ("title", 190, 140),
            ("option", 130, 100), ("status", 80, 75),
        ):
            self.history_tree.heading(column, text=_(f"gui.col_{column}"))
            self.history_tree.column(column, width=width, minwidth=minimum,
                                     stretch=column in {"title", "option"})
        scrollbar = ttk.Scrollbar(
            history_frame, orient="vertical", style="AutoPollEv.Vertical.TScrollbar",
            command=self.history_tree.yview,
        )
        horizontal = ttk.Scrollbar(
            history_frame, orient="horizontal", style="AutoPollEv.Horizontal.TScrollbar",
            command=self.history_tree.xview,
        )
        self.history_tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        self.history_tree.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")
        horizontal.grid(row=2, column=0, sticky="ew")
        self.history_empty = tk.Label(
            self.history_tree, text=_("gui.history_empty"), font=(UI_FONT, 11),
            fg=MUTED, bg=SURFACE, justify="center",
        )


        self.log_frame = tk.Frame(self.root, bg=BG)
        self.log_frame.columnconfigure(0, weight=1)
        self.log_visible = False
        tk.Label(
            self.log_frame, text=_("gui.log_frame"), font=(UI_FONT, 9, "bold"),
            fg=MUTED, bg=BG,
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.log_text = tk.Text(
            self.log_frame, bg=BG, fg=MUTED, font=(MONO_FONT, 9),
            height=4, relief="flat", state="disabled", wrap="word",
            padx=6, pady=4, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=TEXT,
        )
        log_scrollbar = ttk.Scrollbar(
            self.log_frame, orient="vertical", style="AutoPollEv.Vertical.TScrollbar",
            command=self.log_text.yview,
        )
        self.log_text.configure(yscrollcommand=log_scrollbar.set)
        self.log_text.grid(row=1, column=0, sticky="ew")
        log_scrollbar.grid(row=1, column=1, sticky="ns")

    def _toggle_log(self):
        """Show/hide the log panel without displacing the controls."""
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log_frame.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))
        else:
            self.log_frame.grid_remove()
        self.log_text_btn.config(text=_("gui.btn_log_hide" if self.log_visible else "gui.btn_log"))

    def _append_log(self, message: str):
        """Append a line to the log panel."""
        self.log_text.config(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _set_host_controls(self, enabled: bool):
        """Enable or lock hostname editing during conflicting operations."""
        state = "normal" if enabled else "disabled"
        self.host_entry.config(state=state)
        self.host_save_btn.config(state=state)

    def _save_host(self, _event=None):
        """Persist the hostname and restart an active monitor safely."""
        if self._capturing or self._restart_pending:
            return

        was_monitoring = self._monitoring
        try:
            changed = self.config.update_host(self.host_var.get())
        except ConfigError as e:
            messagebox.showerror(_("gui.host.invalid_title"), str(e))
            return
        except OSError as e:
            messagebox.showerror(
                _("gui.host.save_error_title"),
                _("gui.host.save_error", error=e),
            )
            return

        self.host_var.set(self.config.host)
        if not changed:
            return

        self._append_log(_("gui.host.saved", host=self.config.host))
        if not was_monitoring:
            return

        old_thread = self._monitor_thread
        self._restart_pending = True
        self._stop_monitoring()
        self._set_host_controls(False)
        self.start_btn.config(state="disabled")
        self.capture_btn.config(state="disabled")
        self.status_label.config(
            text=_("gui.status.restarting"), fg=WARNING
        )
        self._append_log(_("gui.host.restarting", host=self.config.host))

        def wait_for_monitor():
            if old_thread:
                old_thread.join()
            self._event_queue.put(("host_restart", None))

        threading.Thread(target=wait_for_monitor, daemon=True).start()

    def _load_history(self):
        """Load history records from the log file into the table."""
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)

        records = self.history_logger.get_history(limit=50)
        for record in records:
            ts = record.get("timestamp", "")[:19]
            title = record.get("poll_title", "")
            option = record.get("selected_option", "")
            status = _("gui.vote.success" if record.get("status") == "success" else "gui.vote.failed")
            self.history_tree.insert("", "end", values=(ts, title, option, status))
        if records:
            self.history_empty.place_forget()
        else:
            self.history_empty.place(relx=0.5, rely=0.5, y=14, anchor="center")

    def _add_history_row(self, timestamp: str, title: str,
                         option: str, success: bool):
        """Add a row to the top of the history table."""
        status = _("gui.vote.success" if success else "gui.vote.failed")
        self.history_empty.place_forget()
        self.history_tree.insert("", 0, values=(timestamp, title, option, status))

    # ===== Session capture =====
    def _set_session_status(self, text: str, color: str):
        """Update the session-status label in the status panel."""
        self.session_status_label.config(text=text, fg=color)

    def _check_session_state(self):
        """On startup, check whether the session cookie is set."""
        sid = self.config.cookies.get("polleverywhere_session_id", "")
        if not sid or sid.startswith(_PLACEHOLDER_PREFIX):
            self._set_session_status(_("gui.session.not_set"), WARNING)
        else:
            self._set_session_status(_("gui.session.ready"), SUCCESS)
            self._fetch_account_async()

    def _fetch_account_async(self):
        """Look up the account for the already-saved cookie (background thread)."""
        def worker():
            identity = {}
            try:
                probe = Auth(host=self.config.host, cookies=self.config.cookies)
                try:
                    identity = probe.get_account_identity()
                finally:
                    probe.close()
            except Exception:  # noqa: BLE001 - identity is best-effort
                identity = {}
            self._event_queue.put(("session_account", identity))

        threading.Thread(target=worker, daemon=True).start()

    def _on_session_account(self, identity: Optional[dict]):
        """Show whose account the saved cookie belongs to in the status panel."""
        identity = identity or {}
        account = account_summary(identity)
        has_email = bool(
            identity.get("email") or identity.get("name") or identity.get("username")
        )
        self._set_session_status(
            _("gui.session.ready_as", account=account),
            SUCCESS if has_email else WARNING,
        )

    def _capture_session(self, auto: bool = False, resume_after: bool = False):
        """
        Open the login window to capture the session cookie (runs in a worker thread).

        :param auto: if True, first try a silent headless refresh, then fall back
            to a visible window.
        :param resume_after: whether to start monitoring after a successful
            capture (used by the expiry auto-refresh flow).
        """
        if self._capturing or self._restart_pending:
            return
        self._capturing = True
        self._set_host_controls(False)
        self.capture_btn.config(state="disabled", text=_("gui.btn_capturing"))
        self._set_session_status(_("gui.session.capturing"), WARNING)

        def worker():
            try:
                def status(msg):
                    self._event_queue.put(("capture_status", msg))

                cookies = None
                if auto:
                    self._event_queue.put(
                        ("capture_status", _("gui.session.silent_try"))
                    )
                    cookies = session_capture.capture_session_id(
                        timeout=12, on_status=status, headless=True,
                    )
                if not cookies:
                    cookies = session_capture.capture_session_id(
                        timeout=300, on_status=status, headless=False,
                    )

                if cookies:
                    # Read whose account this cookie belongs to (in this worker
                    # thread, off the UI thread) so the user can verify it.
                    identity = {}
                    try:
                        probe = Auth(host=self.config.host, cookies=cookies)
                        try:
                            identity = probe.get_account_identity()
                        finally:
                            probe.close()
                    except Exception:  # noqa: BLE001 - identity is best-effort
                        identity = {}
                    self._event_queue.put(
                        ("capture_done",
                         {"cookies": cookies, "resume_after": resume_after,
                          "identity": identity})
                    )
                else:
                    self._event_queue.put(
                        ("capture_failed",
                         {"reason": _("login.failed"),
                          "resume_after": resume_after})
                    )
            except session_capture.SessionCaptureError as e:
                self._event_queue.put(
                    ("capture_failed",
                     {"reason": str(e), "resume_after": resume_after,
                      "hard": True})
                )
            except Exception as e:  # noqa: BLE001
                self._event_queue.put(
                    ("capture_failed",
                     {"reason": str(e), "resume_after": resume_after,
                      "hard": True})
                )

        threading.Thread(target=worker, daemon=True).start()

    def _on_capture_done(self, cookies: dict, resume_after: bool,
                         identity: Optional[dict] = None):
        """On success: save the cookie and (optionally) hot-swap the session or resume monitoring."""
        self._capturing = False
        self._set_host_controls(True)
        for key, value in cookies.items():
            self.config.update_cookie(key, value)

        ts = time.strftime("%H:%M")
        identity = identity or {}
        account = account_summary(identity)
        # Green when we have a real email/name/username, amber otherwise so an
        # anonymous/incomplete capture stands out as something to double-check.
        has_email = bool(
            identity.get("email") or identity.get("name") or identity.get("username")
        )
        self._set_session_status(
            _("gui.session.captured_as", time=ts, account=account),
            SUCCESS if has_email else WARNING,
        )
        self.capture_btn.config(state="normal", text=_("gui.btn_capture"))
        self._append_log(_("gui.session.saved"))
        self._append_log(_("gui.session.account_log", account=account))

        # Monitoring in progress: hot-swap the session without restarting.
        if self._monitoring and self.auth:
            try:
                self.auth.refresh_session(self.config.cookies)
            except Exception as e:  # noqa: BLE001
                self._append_log(_("gui.session.refresh_error", error=e))

        # Expiry auto-refresh: restart monitoring.
        if resume_after and not self._monitoring:
            self._start_monitoring()

    def _on_capture_failed(self, reason: str, resume_after: bool, hard: bool = False):
        """On failure: restore the button and inform the user."""
        self._capturing = False
        self._set_host_controls(True)
        self.capture_btn.config(state="normal", text=_("gui.btn_capture"))
        self._set_session_status(_("gui.session.failed"), ERROR)
        self._append_log(_("gui.session.failed_log", reason=reason))
        if hard:
            # Environment/setup errors (e.g. Playwright missing): show the real
            # reason so the failure is self-explaining, not just "Capture failed".
            messagebox.showerror(_("gui.session.title"), reason)
        elif resume_after:
            messagebox.showwarning(
                _("gui.session.title"), _("gui.session.manual_needed")
            )

    def _toggle_monitoring(self):
        """Start/stop monitoring."""
        if self._restart_pending:
            return
        if self._monitoring:
            self._stop_monitoring()
        else:
            self._start_monitoring()

    def _start_monitoring(self):
        """Start the background monitoring thread."""
        self._append_log(_("gui.log.init"))
        self.status_label.config(text=_("gui.status.connecting"), fg=WARNING)

        try:
            self.auth = Auth(host=self.config.host, cookies=self.config.cookies)
            self.auth.validate_cookie()
        except CookieExpiredError as e:
            messagebox.showerror(_("gui.error.cookie_title"), str(e))
            self.status_label.config(text=_("gui.status.cookie_invalid"), fg=ERROR)
            return
        except PresenterNotFoundError as e:
            messagebox.showerror(
                _("gui.error.presenter_title"),
                _("gui.error.presenter", error=e),
            )
            self.status_label.config(text=_("gui.status.presenter_invalid"), fg=ERROR)
            return
        except AuthError as e:
            self._append_log(_("gui.log.cookie_warn", error=e))

        self.history_logger = VoteHistoryLogger(self.config.log_dir)
        self.monitor = PollMonitor(self.auth, poll_interval=self.config.poll_interval)
        self.voter = Voter(self.auth, self.history_logger)

        try:
            # start() fetches the firehose token, which can 401 in the gap
            # after validate_cookie. Uncaught here it lands in a Tk callback,
            # which a windowed build has no console to print.
            self.monitor.start()
        except CookieExpiredError as e:
            messagebox.showerror(_("gui.error.cookie_title"), str(e))
            self.status_label.config(text=_("gui.status.cookie_invalid"), fg=ERROR)
            return

        self._monitoring = True

        self.status_label.config(text=_("gui.status.monitoring"), fg=SUCCESS)
        self.start_btn.config(text=_("gui.btn_stop"), style="Stop.AutoPollEv.TButton")
        self._append_log(_("gui.log.started", host=self.config.host))

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True
        )
        self._monitor_thread.start()

    def _stop_monitoring(self):
        """Stop monitoring."""
        self._monitoring = False
        if self.monitor:
            self.monitor.stop()
        if self.auth:
            self.auth.close()

        self.status_label.config(text=_("gui.status.stopped"), fg=MUTED)
        self.start_btn.config(text=_("gui.btn_start"), style="Primary.AutoPollEv.TButton")
        self._append_log(_("gui.log.stopped"))

    def _monitor_loop(self):
        """Main loop of the background monitoring thread."""
        while self._monitoring and self.monitor and self.monitor.is_running:
            try:
                poll_info = self.monitor.check_new_poll()

                if poll_info:
                    poll_uid = poll_info.get('uid', '')
                    poll_type = poll_info.get('type', 'unknown')

                    self._event_queue.put(("log", _("gui.log.new_poll", type=poll_type)))

                    if self.config.answer_delay > 0:
                        time.sleep(self.config.answer_delay)

                    if not self._monitoring:
                        break

                    # Notify the main thread (via the event queue) to show the dialog
                    self._event_queue.put(("new_poll", {
                        "uid": poll_uid,
                        "type": poll_type,
                    }))

                # Wait for the next round
                for _tick in range(int(self.config.poll_interval * 10)):
                    if not self._monitoring:
                        break
                    time.sleep(0.1)

            except CookieExpiredError:
                self._event_queue.put(("cookie_expired_recapture", None))
                break
            except Exception as e:
                self._event_queue.put(("log", _("gui.log.network_error", error=e)))
                # The normal wait lives in the try above, so without this a
                # repeating error spins the loop and floods the event queue.
                time.sleep(min(self.config.poll_interval, 5))

    def _process_events(self):
        """Handle events from the background thread (runs on the main thread)."""
        try:
            while True:
                event_type, data = self._event_queue.get_nowait()

                if self._restart_pending and event_type in {
                    "log", "new_poll", "cookie_expired_recapture"
                }:
                    continue

                if event_type == "log":
                    self._append_log(data)

                elif event_type == "new_poll":
                    self._handle_new_poll(data)

                elif event_type == "error":
                    messagebox.showerror(_("gui.error.title"), data)
                    self._append_log(f"❌ {data}")

                elif event_type == "stop":
                    self._stop_monitoring()

                elif event_type == "session_account":
                    self._on_session_account(data)

                elif event_type == "capture_status":
                    self._append_log(data)

                elif event_type == "capture_done":
                    self._on_capture_done(
                        data["cookies"], data.get("resume_after", False),
                        data.get("identity"),
                    )

                elif event_type == "capture_failed":
                    self._on_capture_failed(
                        data["reason"], data.get("resume_after", False),
                        data.get("hard", False)
                    )

                elif event_type == "host_restart":
                    self._restart_pending = False
                    try:
                        self._start_monitoring()
                    except Exception as e:  # noqa: BLE001 - restore GUI on restart failure
                        self._stop_monitoring()
                        self._append_log(
                            _("gui.log.network_error", error=e)
                        )
                        messagebox.showerror(_("gui.error.title"), str(e))
                    finally:
                        self._set_host_controls(True)
                        self.start_btn.config(state="normal")
                        self.capture_btn.config(
                            state="normal", text=_("gui.btn_capture")
                        )

                elif event_type == "cookie_expired_recapture":
                    if not self._monitoring:
                        continue
                    self._append_log(_("gui.session.expired_recapture"))
                    if self._monitoring:
                        self._stop_monitoring()
                    self._capture_session(auto=True, resume_after=True)

        except queue.Empty:
            pass

        self.root.after(200, self._process_events)

    def _handle_new_poll(self, poll_info: dict):
        """Handle a new poll: fetch options and pop up the selection dialog."""
        if not self._monitoring:
            return
        active_voter = self.voter
        active_monitor = self.monitor
        if not active_voter or not active_monitor:
            return
        poll_uid = poll_info["uid"]
        completed = False

        try:
            poll_data = active_voter.get_poll_options(poll_uid)
            options = poll_data.get('options', [])
            poll_title = poll_data.get('title', 'Untitled')
            state = poll_data.get('state', 'opened')

            if state != 'opened':
                # GUI specific tracking to avoid spamming the log repeatedly for the same locked poll
                if getattr(self, '_last_locked_poll', None) != poll_uid:
                    self._append_log(_("vote.poll_title", title=f"🔒 {poll_title} (Waiting for unlock...)"))
                    self._last_locked_poll = poll_uid
                return

            self._last_locked_poll = None

            if not options:
                self._append_log(_("gui.log.no_options", uid=poll_uid))
                return

            option_values = [
                o.get('value', o.get('keyword', '?')) for o in options
            ]

            # Send a system notification
            notify_new_poll(poll_title, option_values)

            # Pop up the selection dialog
            dialog = PollDialog(
                self.root, poll_title, options,
                timeout=self.config.user_choice_timeout
            )

            # Wait for the dialog to close
            self.root.wait_window(dialog)

            # Host changes can be handled by Tk's nested wait-window loop. Do
            # not submit this old host's poll through the replacement session.
            if (
                not self._monitoring
                or self.voter is not active_voter
                or self.monitor is not active_monitor
            ):
                return

            # Get the user's choice
            selected_idx = dialog.selected_index
            if selected_idx is not None:
                selected = options[selected_idx]
            else:
                selected = random.choice(options)

            option_id = selected.get('id')
            option_value = selected.get(
                'value', selected.get('keyword', 'Unknown option')
            )

            # Submit the vote
            self._append_log(_("gui.log.submitting", choice=option_value))

            result = active_voter._submit_vote(
                poll_uid, option_id, option_value, poll_title
            )

            # Update UI: only a real success counts as a success
            success = result is not None and result.get("status") == "success"

            if success:
                active_monitor.mark_answered(poll_uid)
                completed = True

            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            self._add_history_row(ts, poll_title, str(option_value), success)

            count = active_monitor.answered_count
            self.vote_count_label.config(text=str(count))

            if success:
                self._append_log(_("gui.log.vote_ok", title=poll_title, choice=option_value))
            else:
                self._append_log(_("gui.log.vote_fail", title=poll_title, choice=option_value))

        except CookieExpiredError:
            self._event_queue.put(("error", _("gui.error.cookie_expired_short")))
            self._stop_monitoring()
        except Exception as e:
            self._append_log(_("gui.log.vote_error", error=e))
        finally:
            if not completed:
                active_monitor.mark_retryable(poll_uid)

    def run(self):
        """Start the GUI main loop."""
        # Center the window
        self.root.update_idletasks()
        w, h = 640, 480
        x = (self.root.winfo_screenwidth() // 2) - (w // 2)
        y = (self.root.winfo_screenheight() // 2) - (h // 2)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        self.root.mainloop()
