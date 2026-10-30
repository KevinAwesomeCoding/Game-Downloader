#!/usr/bin/env python3
"""Small, cautious cross-platform Git sync UI for Game-Downloader."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_NAME = "Game Downloader Sync"
MAX_DIFF_CHARS = 120_000


class GitError(RuntimeError):
    pass


def settings_file() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return base / APP_NAME / "settings.json"


def initial_project_path() -> str:
    candidates = [Path.cwd(), Path(__file__).resolve().parent.parent]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys.executable).resolve().parent)
    for candidate in candidates:
        for path in (candidate, *candidate.parents):
            if (path / ".git").exists():
                return str(path)
    return ""


class GitProject:
    def __init__(self, path: str):
        self.path = Path(path).expanduser().resolve()
        if not (self.path / ".git").exists():
            raise GitError("Choose the Game-Downloader repository folder (the one containing .git).")

    def run(self, *args: str, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_OPTIONAL_LOCKS"] = "0"
        try:
            result = subprocess.run(
                ["git", "-C", str(self.path), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
            )
        except FileNotFoundError as exc:
            raise GitError("Git was not found. Install Git, then reopen this app.") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"Git took longer than {timeout} seconds. Check the network or SSH connection and retry.") from exc
        if check and result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise GitError(detail or f"Git exited with code {result.returncode}.")
        return result

    def project_info(self) -> dict:
        branch = self.run("branch", "--show-current").stdout.strip()
        remotes = self.run("remote").stdout.splitlines()
        upstream = self.run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
        upstream_value = upstream.stdout.strip() if upstream.returncode == 0 else ""
        pref_remote = upstream_value.split("/", 1)[0] if "/" in upstream_value else ""
        return {"branch": branch, "remotes": remotes, "upstream": upstream_value, "preferred_remote": pref_remote}

    def changes(self) -> list[dict]:
        raw = self.run("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
        # subprocess text mode preserves NUL separators.
        entries = raw.split("\0")
        changes: list[dict] = []
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if not entry:
                continue
            if len(entry) < 4:
                continue
            code, path = entry[:2], entry[3:]
            paths = [path]
            if "R" in code or "C" in code:
                if i < len(entries) and entries[i]:
                    paths.append(entries[i])
                    i += 1
            changes.append({"code": code, "path": path, "paths": paths})
        return changes

    def staged_changes_exist(self) -> bool:
        return self.run("diff", "--cached", "--quiet", check=False).returncode != 0

    def fetch(self, remote: str, branch: str) -> None:
        self.run("fetch", "--no-tags", remote, branch, timeout=90)

    def divergence(self, remote: str, branch: str) -> tuple[int, int] | None:
        remote_ref = f"refs/remotes/{remote}/{branch}"
        exists = self.run("rev-parse", "--verify", remote_ref, check=False)
        if exists.returncode:
            return None
        counts = self.run("rev-list", "--left-right", "--count", f"HEAD...{remote_ref}").stdout.split()
        return int(counts[0]), int(counts[1])

    def diff_for(self, change: dict) -> str:
        paths = change["paths"]
        if change["code"] == "??":
            path = self.path / paths[0]
            try:
                size = path.stat().st_size
                if size > 2_000_000:
                    return f"Preview skipped: {paths[0]} is {size:,} bytes. Large files are not loaded into the preview window."
                raw = path.read_bytes()
            except OSError as exc:
                return f"Could not read {paths[0]}: {exc}"
            if b"\0" in raw[:8192]:
                return f"Binary file: {paths[0]} ({size:,} bytes). The file is selectable for committing, but its contents are not shown."
            data = raw.decode("utf-8", errors="replace")
            if len(data) > MAX_DIFF_CHARS:
                data = data[:MAX_DIFF_CHARS] + "\n\n[Preview truncated]"
            return f"New file: {paths[0]}\n\n{data}"
        result = self.run("diff", "--no-ext-diff", "--", *paths, check=False)
        text = result.stdout or result.stderr or "No unstaged text diff (the change may be binary or already staged)."
        if len(text) > MAX_DIFF_CHARS:
            text = text[:MAX_DIFF_CHARS] + "\n\n[Diff preview truncated]"
        return text

    def sync(self, remote: str, branch: str, selected_paths: list[str], message: str) -> str:
        if self.staged_changes_exist():
            raise GitError("There are already staged changes. Commit or unstage them in VS Code first; this app will not mix them with the selected files.")
        if selected_paths:
            self.run("add", "-A", "--", *selected_paths)
            if self.staged_changes_exist():
                self.run("commit", "-m", message)
            else:
                self.run("reset", "--", *selected_paths, check=False)
                raise GitError("The selected files had no changes left to commit. Nothing was committed.")

        self.fetch(remote, branch)
        counts = self.divergence(remote, branch)
        if counts is None:
            # A new remote branch is created only when explicitly selected by the user.
            if selected_paths or self.run("rev-list", "--count", "HEAD").stdout.strip() != "0":
                self.run("push", "--set-upstream", remote, f"HEAD:refs/heads/{branch}", timeout=90)
                return f"Pushed commits to {remote}/{branch}."
            raise GitError(f"The remote branch {remote}/{branch} does not exist yet.")

        ahead, behind = counts
        if ahead and behind:
            raise GitError(
                f"{remote}/{branch} and this branch have diverged ({ahead} local commit(s), {behind} remote commit(s)). "
                "No automatic merge was attempted. Resolve the branch in VS Code, then check again."
            )
        if behind:
            if self.changes():
                raise GitError(
                    f"{remote}/{branch} has {behind} new commit(s), but there are still uncommitted files. "
                    "Nothing was pulled. Commit or set aside those files in VS Code, then retry."
                )
            self.run("merge", "--ff-only", f"refs/remotes/{remote}/{branch}", timeout=90)
            return f"Pulled {behind} commit(s) from {remote}/{branch} with a fast-forward."
        if ahead:
            self.run("push", remote, f"HEAD:refs/heads/{branch}", timeout=90)
            return f"Pushed {ahead} commit(s) to {remote}/{branch}."
        return f"Already up to date with {remote}/{branch}."


class SyncApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("900x680")
        self.root.minsize(720, 540)
        self.results: queue.Queue = queue.Queue()
        self.busy = False
        self.change_rows: dict[str, dict] = {}
        self.selected: set[str] = set()
        self.project_var = tk.StringVar(value=initial_project_path())
        self.remote_var = tk.StringVar()
        self.branch_var = tk.StringVar(value="main")
        self.status_var = tk.StringVar(value="Choose the repository, then check for changes.")
        self.summary_var = tk.StringVar(value="")

        self._load_settings()
        self._build_ui()
        self.root.after(100, self._poll_results)
        if self.project_var.get():
            self.refresh_status()

    def _load_settings(self):
        try:
            data = json.loads(settings_file().read_text(encoding="utf-8"))
            self.project_var.set(data.get("project", self.project_var.get()))
            self.remote_var.set(data.get("remote", ""))
            self.branch_var.set(data.get("branch", "main"))
        except (OSError, ValueError, TypeError):
            pass

    def _save_settings(self):
        path = settings_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "project": self.project_var.get().strip(),
                "remote": self.remote_var.get().strip(),
                "branch": self.branch_var.get().strip(),
            }, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=APP_NAME, font=("TkDefaultFont", 20, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Transfer committed Game-Downloader changes between your computers over your configured Git remote.",
            wraplength=850,
        ).pack(anchor="w", pady=(4, 16))

        repo_frame = ttk.LabelFrame(outer, text="Project", padding=10)
        repo_frame.pack(fill="x", pady=(0, 10))
        self.project_entry = ttk.Entry(repo_frame, textvariable=self.project_var)
        self.project_entry.pack(side="left", fill="x", expand=True)
        self.browse_button = ttk.Button(repo_frame, text="Choose folder…", command=self.choose_project)
        self.browse_button.pack(side="left", padx=(8, 0))

        remote_frame = ttk.Frame(outer)
        remote_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(remote_frame, text="Remote").pack(side="left")
        self.remote_combo = ttk.Combobox(remote_frame, textvariable=self.remote_var, state="readonly", width=22)
        self.remote_combo.pack(side="left", padx=(8, 16))
        ttk.Label(remote_frame, text="Branch").pack(side="left")
        self.branch_entry = ttk.Entry(remote_frame, textvariable=self.branch_var, width=18)
        self.branch_entry.pack(side="left", padx=(8, 16))
        self.refresh_button = ttk.Button(remote_frame, text="Check for changes", command=self.check_remote)
        self.refresh_button.pack(side="right")

        self.summary_label = ttk.Label(outer, textvariable=self.summary_var, wraplength=850)
        self.summary_label.pack(anchor="w", pady=(0, 8))

        list_frame = ttk.LabelFrame(outer, text="Review working-tree changes — select only files you want to commit", padding=8)
        list_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(list_frame, columns=("include", "state", "file"), show="headings", selectmode="browse")
        self.tree.heading("include", text="Include")
        self.tree.heading("state", text="Change")
        self.tree.heading("file", text="File")
        self.tree.column("include", width=75, anchor="center", stretch=False)
        self.tree.column("state", width=100, anchor="center", stretch=False)
        self.tree.column("file", width=560, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        yscroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        yscroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", lambda _event: self.preview_selected())

        row = ttk.Frame(outer)
        row.pack(fill="x", pady=(8, 4))
        self.select_all_button = ttk.Button(row, text="Select all", command=self.select_all)
        self.select_all_button.pack(side="left")
        self.clear_button = ttk.Button(row, text="Clear selection", command=self.clear_selection)
        self.clear_button.pack(side="left", padx=(6, 0))
        self.preview_button = ttk.Button(row, text="Preview selected", command=self.preview_selected)
        self.preview_button.pack(side="left", padx=(6, 0))

        ttk.Label(outer, text="Commit message (required when files are selected)").pack(anchor="w", pady=(10, 3))
        self.message_entry = ttk.Entry(outer)
        self.message_entry.insert(0, "Update Game-Downloader")
        self.message_entry.pack(fill="x")

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(12, 0))
        self.sync_button = ttk.Button(bottom, text="Commit selected & sync", command=self.sync_selected)
        self.sync_button.pack(side="right")
        self.status_label = ttk.Label(bottom, textvariable=self.status_var, wraplength=620)
        self.status_label.pack(side="left", fill="x", expand=True, padx=(0, 12))

        ttk.Label(
            outer,
            text="Safety: no automatic staging of unselected files, no stash/reset/rebase, and incoming updates use fast-forward only.",
            wraplength=850,
        ).pack(anchor="w", pady=(10, 0))

    def project(self) -> GitProject:
        path = self.project_var.get().strip()
        if not path:
            raise GitError("Choose the Game-Downloader repository folder first.")
        return GitProject(path)

    def choose_project(self):
        path = filedialog.askdirectory(title="Choose the Game-Downloader repository folder")
        if path:
            self.project_var.set(path)
            self.refresh_status()

    def _on_tree_click(self, event):
        if self.busy or self.tree.identify("column", event.x) != "#1":
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        record = self.change_rows[item]
        if item in self.selected:
            self.selected.remove(item)
        else:
            self.selected.add(item)
        self.tree.set(item, "include", "☑" if item in self.selected else "☐")
        return "break"

    def select_all(self):
        self.selected = set(self.change_rows)
        for item in self.change_rows:
            self.tree.set(item, "include", "☑")

    def clear_selection(self):
        self.selected.clear()
        for item in self.change_rows:
            self.tree.set(item, "include", "☐")

    def _run_async(self, work, callback):
        if self.busy:
            return
        self.busy = True
        self._set_controls(False)
        self.status_var.set("Working…")

        def runner():
            try:
                result = work()
            except Exception as exc:  # shown in the UI, never hidden
                result = exc
            self.results.put((callback, result))

        threading.Thread(target=runner, daemon=True).start()

    def _poll_results(self):
        try:
            while True:
                callback, result = self.results.get_nowait()
                self.busy = False
                self._set_controls(True)
                callback(result)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_results)

    def _set_controls(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for widget in (self.project_entry, self.browse_button, self.remote_combo, self.branch_entry,
                       self.refresh_button, self.select_all_button, self.clear_button,
                       self.preview_button, self.message_entry, self.sync_button):
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        if enabled:
            self.remote_combo.configure(state="readonly")

    def _load_project_state(self, fetch: bool):
        project = self.project()
        info = project.project_info()
        branch = self.branch_var.get().strip() or info["branch"]
        if not branch:
            raise GitError("The repository has no current branch. Open it in VS Code and create or check out a branch first.")
        remote = self.remote_var.get().strip()
        if remote not in info["remotes"]:
            remote = info["preferred_remote"] if info["preferred_remote"] in info["remotes"] else (info["remotes"][0] if info["remotes"] else "")
        if remote:
            self.remote_var.set(remote)
            if fetch:
                project.fetch(remote, branch)
        self.branch_var.set(branch)
        changes = project.changes()
        divergence = project.divergence(remote, branch) if remote else None
        return {"project": project, "info": info, "remote": remote, "branch": branch,
                "changes": changes, "divergence": divergence, "fetch": fetch}

    def refresh_status(self):
        self._save_settings()
        self._run_async(lambda: self._load_project_state(False), self._show_state)

    def check_remote(self):
        self._save_settings()
        self._run_async(lambda: self._load_project_state(True), self._show_state)

    def _show_state(self, result):
        if isinstance(result, Exception):
            self.status_var.set(f"Could not check repository: {result}")
            self.summary_var.set("")
            return
        info = result["info"]
        remotes = info["remotes"]
        self.remote_combo.configure(values=remotes)
        if result["remote"]:
            self.remote_var.set(result["remote"])
        self.branch_var.set(result["branch"])

        for item in self.tree.get_children():
            self.tree.delete(item)
        self.change_rows.clear()
        self.selected.clear()
        for index, change in enumerate(result["changes"]):
            code = change["code"]
            state = "Untracked" if code == "??" else code
            item = f"change-{index}"
            self.change_rows[item] = change
            self.tree.insert("", "end", iid=item, values=("☐", state, change["path"]))

        counts = result["divergence"]
        if counts is None:
            sync_text = f"{result['remote']}/{result['branch']} is not present yet." if result["remote"] else "No Git remote is configured."
        else:
            ahead, behind = counts
            sync_text = f"{ahead} outgoing commit(s), {behind} incoming commit(s) vs {result['remote']}/{result['branch']}."
        dirty = len(result["changes"])
        staged = result["project"].staged_changes_exist()
        staged_note = " A staged change exists; sync will pause until you handle it in VS Code." if staged else ""
        self.summary_var.set(f"Branch: {info['branch'] or '(detached)'}  •  {sync_text}  •  {dirty} working-tree change(s).{staged_note}")
        self.status_var.set("Checked remote." if result["fetch"] else "Repository status loaded.")
        self._save_settings()

    def preview_selected(self):
        selection = self.tree.selection()
        item = selection[0] if selection else (next(iter(self.selected)) if len(self.selected) == 1 else None)
        if item is None or item not in self.change_rows:
            messagebox.showinfo(APP_NAME, "Select one changed file in the list, then choose Preview selected.")
            return
        change = self.change_rows[item]
        self._run_async(lambda: self.project().diff_for(change), self._show_diff)

    def _show_diff(self, result):
        if isinstance(result, Exception):
            messagebox.showerror(APP_NAME, str(result))
            return
        window = tk.Toplevel(self.root)
        window.title("Change preview")
        window.geometry("850x600")
        body = ttk.Frame(window, padding=10)
        body.pack(fill="both", expand=True)
        text = tk.Text(body, wrap="none", undo=False)
        ybar = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        xbar = ttk.Scrollbar(window, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        text.pack(side="left", fill="both", expand=True)
        ybar.pack(side="right", fill="y")
        xbar.pack(fill="x")
        text.insert("1.0", result)
        text.configure(state="disabled")

    def sync_selected(self):
        if not self.remote_var.get().strip():
            messagebox.showerror(APP_NAME, "No remote is configured. Choose a repository with a remote first.")
            return
        selected_changes = [self.change_rows[item] for item in self.selected if item in self.change_rows]
        selected_paths = list(dict.fromkeys(path for change in selected_changes for path in change["paths"]))
        message = self.message_entry.get().strip()
        if selected_paths and not message:
            messagebox.showerror(APP_NAME, "Enter a commit message for the selected files.")
            return
        if selected_paths and not messagebox.askyesno(
            APP_NAME,
            f"Commit {len(selected_paths)} selected path(s) with this message?\n\n{message}\n\nThen check {self.remote_var.get()}/{self.branch_var.get()} for incoming/outgoing commits.",
        ):
            return
        self._save_settings()
        remote = self.remote_var.get().strip()
        branch = self.branch_var.get().strip()
        self._run_async(
            lambda: self.project().sync(remote, branch, selected_paths, message),
            self._sync_finished,
        )

    def _sync_finished(self, result):
        if isinstance(result, Exception):
            self.status_var.set(f"Sync stopped safely: {result}")
            self.refresh_status()
            return
        self.status_var.set(result)
        self.refresh_status()


def main():
    root = tk.Tk()
    SyncApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
