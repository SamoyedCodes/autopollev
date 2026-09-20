"""Run with pollev Python; add --preview to leave mock windows open for inspection."""

import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autopollev.gui import AutoPollEvGUI, PollDialog, SUCCESS


def main():
    with tempfile.TemporaryDirectory() as directory:
        config = Mock(host="your-presenter-id", log_dir=directory, user_choice_timeout=30,
                      cookies={})
        gui = AutoPollEvGUI(config)
        root = gui.root
        root.geometry("640x480")
        root.update()
        assert gui.history_empty.winfo_ismapped()
        assert not gui.history_tree.get_children()
        assert gui.start_btn.cget("text") == "Start"
        assert gui.capture_btn.cget("text") == "Log in"
        gui._set_session_status(
            "Ready — Alexandra Example (alexandra.long.account.name@example.edu)",
            SUCCESS,
        )
        for index in range(50):
            gui._add_history_row("2026-09-07 12:30:00", f"Question {index}: Example poll",
                                 "A sample answer", index % 2 == 0)
            gui._append_log(f"Mock event {index}: waiting for the next poll.")
        root.update()
        assert not gui.history_empty.winfo_ismapped()
        assert {gui.history_tree.item(item, "values")[-1]
                for item in gui.history_tree.get_children()} == {"Success", "Failed"}
        for geometry in ("640x480", "600x420"):
            root.geometry(geometry)
            for logs in (False, True):
                if gui.log_visible != logs:
                    gui._toggle_log()
                root.update()
                assert gui.host_var.get() == "your-presenter-id"
                for widget in (gui.host_entry, gui.start_btn, gui.capture_btn,
                               gui.history_tree, gui.session_status_label):
                    assert widget.winfo_ismapped()
                    assert widget.winfo_rootx() >= root.winfo_rootx()
                    assert widget.winfo_rootx() + widget.winfo_width() <= root.winfo_rootx() + root.winfo_width()
                    assert widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + root.winfo_height()
                assert gui.history_tree.winfo_height() >= 70
                assert bool(gui.log_frame.winfo_ismapped()) == logs
        gui.history_tree.yview_moveto(1)
        gui.log_text.yview_moveto(0)
        root.update()
        assert gui.history_tree.yview()[0] > 0
        assert gui.log_text.yview()[1] < 1
        gui._set_host_controls(False)
        assert str(gui.host_entry.cget("state")) == "disabled"
        assert str(gui.host_save_btn.cget("state")) == "disabled"
        gui._set_host_controls(True)
        gui.host_entry.focus_force()
        root.update()
        root.event_generate("<Tab>")
        root.update()
        assert root.focus_get() == gui.host_save_btn
        options = [{"value": "A long answer that wraps onto another line so it remains readable in a compact poll dialog."},
                   {"value": "A shorter answer"}]
        # Disable scheduling only for visual inspection; exercise selection and timeout below.
        with patch.object(PollDialog, "_start_countdown"):
            dialog = PollDialog(root, "How should a compact utility display a long question while keeping every answer readable?", options)
        root.update()
        assert dialog.winfo_width() <= 480
        theme = gui.history_tree.tk.call("ttk::style", "theme", "use")
        assert theme == "clam"
        dialog._on_select(1)
        assert dialog.selected_index == 1
        with patch.object(PollDialog, "_start_countdown"):
            dialog = PollDialog(root, "Timeout check", options)
        with patch("autopollev.gui.random.randint", return_value=0):
            dialog._on_timeout()
        assert dialog.selected_index == 0
        print("GUI smoke checks passed.", flush=True)
        if "--preview" in sys.argv:
            root.geometry("640x480+120+120")
            gui._set_host_controls(False)
            with patch.object(PollDialog, "_start_countdown"):
                dialog = PollDialog(root, "How should a compact utility display a long question while keeping every answer readable?", options)
            dialog.geometry("+800+120")
            root.mainloop()
        else:
            root.destroy()


if __name__ == "__main__":
    main()
