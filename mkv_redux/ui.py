"""Windows-first desktop interface for MKV Redux."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk

from .core import (
    MKVMERGE_NAME,
    Cancellation,
    Cancelled,
    Decision,
    Language,
    Plan,
    ReduxError,
    discover_mkv_files,
    find_mkvmerge,
    inspect_file,
    output_path,
    remux_file,
)

BG = "#101826"
CARD = "#1A2638"
FIELD = "#131E2E"
BORDER = "#30425A"
INK = "#F1F5FB"
MUTED = "#B8C8DC"
ACCENT = "#63E6BB"
WARNING = "#FFD888"
ERROR = "#FFB6B9"
TOOL_URL = "https://mkvtoolnix.download/downloads.html#windows"


@dataclass(frozen=True)
class Job:
    path: Path
    status: str = "Waiting"
    detail: str = "Waiting for audio inspection."
    plan: Plan | None = None
    output: Path | None = None

    @property
    def ready(self) -> bool:
        return self.output is None and self.plan is not None and self.plan.decision == Decision.READY


@dataclass(frozen=True)
class Update:
    key: str
    job: Job


@dataclass(frozen=True)
class Progress:
    text: str
    value: float


@dataclass(frozen=True)
class Finished:
    text: str
    complete: bool


@dataclass(frozen=True)
class FolderScan:
    paths: list[Path]
    unreadable: int = 0
    error: str = ""


Event = Update | Progress | Finished | FolderScan


def scan_folder(folder: Path, cancel: Cancellation, events: queue.SimpleQueue[Event]) -> None:
    try:
        paths, unreadable = discover_mkv_files(folder, cancel)
    except Cancelled:
        events.put(FolderScan([]))
    except OSError as exc:
        events.put(FolderScan([], error=f"Could not scan folder: {exc}"))
    else:
        events.put(FolderScan(paths, unreadable))


def run_batch(
    tool: Path,
    jobs: list[tuple[str, Job]],
    folder: Path | None,
    cancel: Cancellation,
    events: queue.SimpleQueue[Event],
    mux: bool,
    note: str = "",
    *,
    replace_original: bool = False,
) -> None:
    """Run one disk-intensive job at a time; never call Tk from this thread."""
    total = len(jobs)
    succeeded = failed = warnings = 0
    interrupted = ""
    action = ("Replacing originals" if replace_original else "Creating copies") if mux else "Inspecting audio"
    try:
        for index, (key, job) in enumerate(jobs):
            cancel.check()
            headline = f"{action}  ·  {index + 1} of {total}"
            events.put(Progress(f"{headline}  ·  {job.path.name}", index / total * 100))
            try:
                if mux:
                    if not job.ready or job.plan is None:
                        raise ReduxError("This file must be inspected before creating a copy.")
                    dest = job.plan.source if replace_original else output_path(job.path, folder)

                    def progress(percent: int, *, title=headline, name=job.path.name, i=index) -> None:
                        events.put(
                            Progress(f"{title}  ·  {percent}%  ·  {name}", (i * 100 + percent) / total)
                        )

                    warning = remux_file(
                        tool, job.plan, dest, cancel, progress, replace_original=replace_original
                    )
                    warnings += bool(warning)
                    if replace_original:
                        result = replace(
                            job, output=dest, status="Replaced", detail="Original replaced using the same name."
                        )
                    else:
                        result = replace(
                            job,
                            output=dest,
                            status="Saved with warning" if warning else "Saved",
                            detail=f"Copy saved. Check it before using it. {warning}"
                            if warning
                            else "Filtered copy saved. The original is unchanged.",
                        )
                else:
                    plan = inspect_file(tool, job.path, cancel)
                    result = Job(job.path, plan.decision.value, plan.summary, plan)
                succeeded += 1
            except (ReduxError, OSError) as exc:
                cancel.check()
                failed += 1
                if mux:
                    status = "Not replaced" if replace_original else "Not saved"
                else:
                    status = "Cannot inspect"
                result = Job(job.path, status, str(exc), job.plan if mux else None)
            events.put(Update(key, result))
    except Cancelled:
        interrupted = "Stopped. "
    except Exception as exc:  # noqa: BLE001 — release the UI even if the worker fails unexpectedly.
        interrupted = f"Stopped unexpectedly ({exc}). "
    outcome = ("replaced" if replace_original else "saved") if mux else "inspected"
    summary = f"{succeeded} {outcome}"
    if warnings:
        summary += f" ({warnings} with warnings)"
    if failed:
        failure = f"not {outcome}" if mux else "could not be inspected"
        summary += f"  ·  {failed} {failure}"
    events.put(Finished(f"{interrupted}{summary}.{note}", not interrupted))


class ReduxWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.jobs: dict[str, Job] = {}
        self.next_id = 0
        self.folder: Path | None = None
        self.events: queue.SimpleQueue[Event] = queue.SimpleQueue()
        self.cancel: Cancellation | None = None
        self.closing = False
        self.portable = bool(getattr(sys, "frozen", False))
        config = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".config")
        self.preference = config / "MKV Redux" / "mkvmerge.txt"
        self.tool = self._load_tool()

        root.title("MKV Redux · English audio cleanup")
        root.geometry("1040x700")
        root.minsize(860, 620)
        root.configure(bg=BG)
        self.family = "Segoe UI" if os.name == "nt" else font.nametofont("TkDefaultFont").actual("family")
        self._styles()
        icon = tk.PhotoImage(width=32, height=32)
        icon.put(CARD, to=(0, 0, 32, 32))
        for y, right in ((7, 26), (14, 21), (21, 16)):
            icon.put(ACCENT, to=(6, y, right, y + 4))
        root.iconphoto(True, icon)

        self.count = tk.StringVar(root)
        self.activity = tk.StringVar(root, "Add MKV files or a folder to get started.")
        self.destination = tk.StringVar(root)
        self.replace_originals = tk.BooleanVar(root, value=False)
        self.selected_path = tk.StringVar(root)
        self._layout()
        self._refresh()
        self._preview()
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.bind("<Control-o>", lambda _event: self._add_files())
        root.after(60, self._poll)

    def _styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=(self.family, 10), background=CARD, foreground=INK)
        style.configure(
            "TButton",
            padding=(12, 7),
            background=BORDER,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            focuscolor=INK,
        )
        style.map(
            "TButton",
            background=[("disabled", CARD), ("pressed", "#3F5771"), ("active", "#405570")],
            foreground=[("disabled", "#8E9EB3")],
        )
        style.configure(
            "Action.TButton",
            font=(self.family, 11, "bold"),
            padding=(17, 9),
            background=ACCENT,
            foreground=BG,
            bordercolor=ACCENT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            focuscolor=BG,
        )
        style.map(
            "Action.TButton",
            background=[("disabled", BORDER), ("pressed", "#46C9A0"), ("active", "#8DF3D2")],
            foreground=[("disabled", MUTED)],
        )
        style.configure(
            "Replace.Action.TButton", background=WARNING, bordercolor=WARNING, lightcolor=WARNING, darkcolor=WARNING
        )
        style.map(
            "Replace.Action.TButton",
            background=[("disabled", BORDER), ("pressed", "#F0BC63"), ("active", "#FFE4AC")],
            foreground=[("disabled", MUTED)],
        )
        style.configure(
            "Treeview",
            background=FIELD,
            fieldbackground=FIELD,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            rowheight=max(28, font.Font(family=self.family, size=10).metrics("linespace") + 11),
        )
        style.map("Treeview", background=[("selected", "#324B65")], foreground=[("selected", INK)])
        style.configure(
            "Treeview.Heading",
            background=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            foreground=INK,
            font=(self.family, 9, "bold"),
            padding=(6, 5),
        )
        style.map("Treeview.Heading", background=[("active", BORDER)])
        style.configure(
            "Vertical.TScrollbar", background=BORDER, troughcolor=FIELD, bordercolor=FIELD, arrowcolor=MUTED
        )
        style.configure(
            "Horizontal.TProgressbar",
            background=ACCENT,
            troughcolor=BORDER,
            bordercolor=BORDER,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=5,
        )
        style.configure(
            "Path.TEntry",
            fieldbackground=FIELD,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=5,
            foreground=MUTED,
        )
        style.map("Path.TEntry", fieldbackground=[("readonly", FIELD)], bordercolor=[("focus", ACCENT)])

    def _label(self, parent, text="", *, fg=INK, bg=CARD, size=10, bold=False, **extra) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            anchor="w",
            fg=fg,
            bg=bg,
            font=(self.family, size, "bold" if bold else "normal"),
            **extra,
        )

    def _card(self, parent) -> tk.Frame:
        return tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER)

    def _tree(self, parent, columns: tuple[tuple[str, str, int, bool], ...]) -> ttk.Treeview:
        frame = tk.Frame(parent, bg=CARD)
        frame.pack(fill="both", expand=True, padx=15, pady=(0, 15))
        keys = tuple(key for key, _, _, _ in columns)
        tree = ttk.Treeview(frame, columns=keys, show="headings", selectmode="browse", height=5)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        for key, heading, width, stretch in columns:
            tree.heading(key, text=heading, anchor="w")
            tree.column(key, width=width, minwidth=width if not stretch else 120, stretch=stretch)
        return tree

    def _layout(self) -> None:
        page = tk.Frame(self.root, bg=BG)
        page.pack(fill="both", expand=True, padx=24, pady=20)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1)

        hero = tk.Frame(page, bg=BG)
        hero.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        self._label(hero, "  MKV REDUX  ", bg=ACCENT, fg=BG, size=9, bold=True, pady=3).pack(anchor="w")
        self._label(hero, "English audio. Same video.", bg=BG, size=27, bold=True).pack(
            anchor="w", pady=(3, 0)
        )
        self._label(
            hero,
            "Remove audio tagged in other languages without re-encoding.",
            bg=BG,
            fg=MUTED,
            size=11,
        ).pack(anchor="w")

        setup = self._card(page)
        setup.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.tool_label = self._label(setup, bold=True)
        self.tool_label.pack(side="left", padx=15, pady=11)
        self.locate: ttk.Button | None = None
        if self.portable:
            if self.tool is not None:
                ttk.Button(setup, text="Licenses…", command=self._open_licenses).pack(
                    side="right", padx=11, pady=7
                )
        else:
            self.locate = ttk.Button(setup, text=f"Locate {MKVMERGE_NAME}…", command=self._locate_tool)
            self.locate.pack(side="right", padx=(8, 11), pady=7)
            ttk.Button(setup, text="Get MKVToolNix", command=lambda: webbrowser.open(TOOL_URL)).pack(
                side="right", pady=7
            )

        middle = tk.Frame(page, bg=BG)
        middle.grid(row=2, column=0, sticky="nsew", pady=(0, 12))
        middle.rowconfigure(0, weight=1)
        middle.columnconfigure((0, 1), weight=1, uniform="panels")
        files = self._card(middle)
        files.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        review = self._card(middle)
        review.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        files_top = tk.Frame(files, bg=CARD)
        files_top.pack(fill="x", padx=15, pady=(9, 4))
        self._label(files_top, "1   Add videos", size=14, bold=True).pack(side="left")
        self.rescan = ttk.Button(files_top, text="Rescan", command=self._scan_unsaved)
        self.rescan.pack(side="right")
        self.remove = ttk.Button(files_top, text="Remove", command=self._remove_file)
        self.remove.pack(side="right", padx=6)
        self._label(files, fg=MUTED, textvariable=self.count).pack(fill="x", padx=15, pady=(0, 8))
        file_actions = tk.Frame(files, bg=CARD)
        file_actions.pack(fill="x", padx=15, pady=(0, 10))
        self.add = ttk.Button(file_actions, text="Add MKV files…", command=self._add_files)
        self.add.pack(side="left")
        self.add_folder = ttk.Button(file_actions, text="Add folder…", command=self._add_folder)
        self.add_folder.pack(side="left", padx=6)
        self.file_tree = self._tree(files, (("file", "File", 200, True), ("status", "Status", 132, False)))
        for status, color in (
            ("Ready", ACCENT),
            ("Saved", ACCENT),
            ("Replaced", ACCENT),
            ("Needs review", WARNING),
            ("Saved with warning", WARNING),
            ("Cannot inspect", ERROR),
            ("Not saved", ERROR),
            ("Not replaced", ERROR),
        ):
            self.file_tree.tag_configure(status, foreground=color)
        self.file_tree.bind("<<TreeviewSelect>>", lambda _event: self._preview())
        self.file_tree.bind("<Delete>", lambda _event: self._remove_file())

        review_top = tk.Frame(review, bg=CARD)
        review_top.pack(fill="x", padx=15, pady=(9, 4))
        self._label(review_top, "2   Review audio", size=14, bold=True).pack(side="left")
        self.reveal = ttk.Button(review_top, text="Open output folder", command=self._open_output)
        self.reveal.pack(side="right")
        self.path_field = ttk.Entry(
            review, textvariable=self.selected_path, style="Path.TEntry", state="readonly", width=1
        )
        self.path_field.pack(fill="x", padx=15, pady=(0, 6))
        self.detail = tk.Text(
            review,
            height=3,
            width=1,
            wrap="word",
            state="disabled",
            background=CARD,
            foreground=MUTED,
            font=(self.family, 10),
            borderwidth=0,
            relief="flat",
            padx=0,
            pady=0,
            highlightthickness=0,
            takefocus=False,
        )
        self.detail.pack(fill="x", padx=15, pady=(0, 5))
        self.track_tree = self._tree(
            review,
            (
                ("track", "Track", 132, True),
                ("tag", "Language tag", 100, False),
                ("action", "Action", 90, False),
            ),
        )
        self.track_tree.tag_configure("Keep", foreground=ACCENT)
        self.track_tree.tag_configure("Keep as-is", foreground=ACCENT)
        self.track_tree.tag_configure("Remove", foreground=WARNING)
        self.track_tree.tag_configure("Unknown", foreground=WARNING)
        self.track_tree.tag_configure("Kept", foreground=ACCENT)

        save = self._card(page)
        save.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        self.reset = ttk.Button(save, text="Reset", command=self._reset_folder)
        self.reset.pack(side="right", padx=(0, 11), pady=8)
        self.choose = ttk.Button(save, text="Choose folder…", command=self._choose_folder)
        self.choose.pack(side="right", padx=8, pady=8)
        self._label(save, "3   Output", size=12, bold=True).pack(side="left", padx=15, pady=8)
        self.replace_toggle = ttk.Checkbutton(
            save, text="Replace originals", variable=self.replace_originals, command=self._refresh
        )
        self.replace_toggle.pack(side="left", padx=(0, 12), pady=8)
        self.destination_label = self._label(save, fg=MUTED, width=1, textvariable=self.destination)
        self.destination_label.pack(side="left", fill="x", expand=True)

        footer = tk.Frame(page, bg=BG)
        footer.grid(row=4, column=0, sticky="ew")
        self.create = ttk.Button(
            footer, style="Action.TButton", text="Create English copies", command=self._process_ready
        )
        self.create.pack(side="right")
        self.stop = ttk.Button(footer, text="Cancel", command=self._cancel_work)
        self.stop.pack(side="right", padx=10)
        self._label(footer, bg=BG, fg=MUTED, width=1, textvariable=self.activity).pack(fill="x", pady=(0, 5))
        self.bar = ttk.Progressbar(footer, mode="determinate")
        self.bar.pack(fill="x")

    def _load_tool(self) -> Path | None:
        if self.portable:
            return find_mkvmerge()
        try:
            saved = Path(self.preference.read_text(encoding="utf-8").strip())
            if saved.is_absolute() and saved.is_file():
                return saved
        except (OSError, ValueError):
            pass
        return find_mkvmerge()

    def _open_licenses(self) -> None:
        if os.name == "nt" and self.tool is not None:
            try:
                os.startfile(self.tool.parent / "licenses")
            except OSError as exc:
                messagebox.showerror("Could not open licenses", str(exc), parent=self.root)

    def _locate_tool(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root, title=f"Locate {MKVMERGE_NAME}", filetypes=[(MKVMERGE_NAME, MKVMERGE_NAME)]
        )
        if not selected:
            return
        chosen = Path(selected).absolute()
        if chosen.name.casefold() != MKVMERGE_NAME or not chosen.is_file():
            messagebox.showerror(
                "Select MKVToolNix",
                f"Choose {MKVMERGE_NAME} from the MKVToolNix installation folder.",
                parent=self.root,
            )
            return
        self.tool = chosen
        try:
            self.preference.parent.mkdir(parents=True, exist_ok=True)
            self.preference.write_text(str(chosen), encoding="utf-8")
        except OSError:
            messagebox.showwarning(
                "MKVToolNix location",
                "The location will work for this session, but could not be remembered.",
                parent=self.root,
            )
        self.activity.set("MKVToolNix located. Add MKV files or a folder to get started.")
        self._refresh()
        self._scan_unsaved()

    def _can_add(self) -> bool:
        return self.cancel is None and (not self.portable or self.tool is not None)

    def _add_files(self) -> None:
        if not self._can_add():
            return
        paths = filedialog.askopenfilenames(
            parent=self.root, title="Choose MKV videos", filetypes=[("Matroska videos", "*.mkv")]
        )
        if paths:
            self._queue_files([Path(name) for name in paths])

    def _add_folder(self) -> None:
        if not self._can_add():
            return
        chosen = filedialog.askdirectory(
            parent=self.root, title="Choose a folder to scan, including subfolders", mustexist=True
        )
        if not chosen:
            return
        folder = Path(chosen)
        self.cancel = Cancellation()
        self.activity.set(f"Searching folder and subfolders  ·  {folder}")
        self.bar.configure(mode="indeterminate", value=0)
        self.bar.start()
        self._refresh()
        threading.Thread(target=scan_folder, args=(folder, self.cancel, self.events), daemon=True).start()

    def _queue_files(self, paths: list[Path], *, note: str = "") -> None:
        existing = {job.path for job in self.jobs.values()}
        added = []
        for candidate in paths:
            path = candidate.absolute()
            if path in existing or not path.name.casefold().endswith(".mkv"):
                continue
            key = str(self.next_id)
            self.next_id += 1
            existing.add(path)
            added.append(key)
            self.jobs[key] = Job(path)
            self.file_tree.insert("", "end", iid=key, values=(path.name, "Waiting"))
        if added:
            self.file_tree.selection_set(added[0])
            self.file_tree.focus(added[0])
            self.file_tree.see(added[0])
            self._preview()
            if self.tool is None:
                self.activity.set(f"Install or locate MKVToolNix to inspect these files.{note}")
            else:
                self._start(added, mux=False, note=note)
        else:
            message = "No new .mkv files were added." if paths else "No .mkv files found."
            self.activity.set(f"{message}{note}")

    def _selected(self) -> str | None:
        selection = self.file_tree.selection()
        return selection[0] if selection else None

    def _selected_job(self) -> Job | None:
        key = self._selected()
        return self.jobs.get(key) if key is not None else None

    def _remove_file(self) -> None:
        if self.cancel is None and (key := self._selected()) is not None:
            next_key = self.file_tree.next(key) or self.file_tree.prev(key)
            self.file_tree.delete(key)
            del self.jobs[key]
            if next_key:
                self.file_tree.selection_set(next_key)
                self.file_tree.focus(next_key)
            if not self.jobs:
                self.activity.set("Add MKV files or a folder to get started.")
                self.bar["value"] = 0
            self._preview()

    def _scan_unsaved(self) -> None:
        self._start([key for key, job in self.jobs.items() if job.output is None], mux=False)

    def _process_ready(self) -> None:
        keys = [key for key, job in self.jobs.items() if job.ready]
        if self.cancel is not None or self.tool is None or not keys:
            return
        replacing = self.replace_originals.get()
        if replacing:
            count = len(keys)
            files = "file" if count == 1 else "files"
            if not messagebox.askyesno(
                "Replace originals?",
                f"Replace {count} original {files} with English-audio versions using the same names?\n\n"
                "Removed audio tracks cannot be recovered from the new files. No backup will be kept.",
                default=messagebox.NO,
                icon=messagebox.WARNING,
                parent=self.root,
            ):
                return
        self._start(keys, mux=True, replace_original=replacing)

    def _choose_folder(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self.root,
            title="Choose where to save English copies",
            initialdir=str(self.folder or ""),
            mustexist=True,
        )
        if chosen:
            self.folder = Path(chosen).absolute()
            self._refresh()

    def _reset_folder(self) -> None:
        self.folder = None
        self._refresh()

    def _open_output(self) -> None:
        job = self._selected_job()
        if job is None or job.output is None:
            return
        try:
            if os.name == "nt":
                os.startfile(job.output.parent)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(job.output.parent)])
        except OSError as exc:
            messagebox.showerror("Could not open output folder", str(exc), parent=self.root)

    def _preview(self) -> None:
        job = self._selected_job()
        self.track_tree.delete(*self.track_tree.get_children())
        self.selected_path.set(str(job.output or job.path) if job else "No video selected")
        self.path_field.xview_moveto(0)
        text = (
            job.detail
            if job
            else "Select a video to preview which tracks would be kept. The app reads language tags, not spoken audio."
        )
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")
        if job and job.plan:
            decision = job.plan.decision
            for track in job.plan.audio:
                label = " ".join((track.name or track.codec or "Audio").split())
                if decision != Decision.READY:
                    action = "Unknown" if track.language == Language.UNKNOWN else "Unchanged"
                elif track.language == Language.UNDETERMINED:
                    action = "Keep as-is"
                elif job.output is not None:
                    action = "Kept" if track.language == Language.ENGLISH else "Removed"
                else:
                    action = "Keep" if track.language == Language.ENGLISH else "Remove"
                self.track_tree.insert(
                    "", "end", values=(f"#{track.id}  {label}", track.tag or "—", action), tags=(action,)
                )
        self._refresh()

    def _refresh(self) -> None:
        busy = self.cancel is not None
        can_stop = self.cancel is not None and not self.cancel.requested
        selected = self._selected_job()
        ready = sum(job.ready for job in self.jobs.values())
        replacing = self.replace_originals.get()
        if replacing:
            self.destination.set("Same filenames · no backup")
        else:
            self.destination.set(str(self.folder) if self.folder else "Next to each original  ·  filename.english.mkv")
        self.destination_label.configure(fg=WARNING if replacing else MUTED)
        self.create.configure(
            text="Replace originals" if replacing else "Create English copies",
            style="Replace.Action.TButton" if replacing else "Action.TButton",
        )
        self.count.set(
            f"{len(self.jobs)} in queue  ·  {ready} ready"
            if self.jobs
            else "Choose files or search a folder and its subfolders."
        )
        if self.portable:
            status = "Built-in remux engine ready" if self.tool else "Built-in remux engine missing. Replace this copy of MKV Redux."
        else:
            status = "MKVToolNix located" if self.tool else "MKVToolNix is required to process files"
        self.tool_label.configure(text=status, fg=ACCENT if self.tool else WARNING)
        if self.locate is not None:
            self.locate.state(["disabled" if busy else "!disabled"])
        for button, enabled in (
            (self.add, self._can_add()),
            (self.add_folder, self._can_add()),
            (self.remove, not busy and selected is not None),
            (
                self.rescan,
                not busy and self.tool is not None and any(job.output is None for job in self.jobs.values()),
            ),
            (self.replace_toggle, not busy),
            (self.choose, not busy and not replacing),
            (self.reset, not busy and not replacing and self.folder is not None),
            (self.reveal, selected is not None and selected.output is not None),
            (self.create, not busy and self.tool is not None and ready > 0),
            (self.stop, can_stop),
        ):
            button.state(["!disabled" if enabled else "disabled"])

    def _start(self, keys: list[str], *, mux: bool, note: str = "", replace_original: bool = False) -> None:
        if self.cancel is not None or self.tool is None or not keys:
            return
        self.cancel = Cancellation()
        self.bar["value"] = 0
        self.activity.set(
            ("Preparing replacements…" if replace_original else "Preparing copies…") if mux else "Reading audio tags…"
        )
        self._refresh()
        work = [(key, self.jobs[key]) for key in keys]
        threading.Thread(
            target=run_batch,
            args=(self.tool, work, self.folder, self.cancel, self.events, mux, note),
            kwargs={"replace_original": replace_original},
            daemon=True,
        ).start()

    def _poll(self) -> None:
        refresh = preview = False
        for _ in range(200):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, Update):
                job = event.job
                self.jobs[event.key] = job
                self.file_tree.item(event.key, values=(job.path.name, job.status), tags=(job.status,))
                refresh = True
                preview |= self._selected() == event.key
            elif isinstance(event, Progress):
                if self.cancel is not None and not self.cancel.requested:
                    self.activity.set(event.text)
                    self.bar["value"] = event.value
            else:
                canceled = self.cancel is not None and self.cancel.requested
                self.cancel = None
                if self.closing:
                    self.root.destroy()
                    return
                if isinstance(event, FolderScan):
                    self.bar.stop()
                    self.bar.configure(mode="determinate", value=0)
                    if canceled or event.error:
                        self.activity.set("Folder scan canceled." if canceled else event.error)
                    else:
                        note = f" Skipped {event.unreadable} unreadable entries." if event.unreadable else ""
                        self._queue_files(event.paths, note=note)
                else:
                    self.activity.set(event.text)
                    if event.complete:
                        self.bar["value"] = 100
                refresh = True
        if preview:
            self._preview()
        elif refresh:
            self._refresh()
        self.root.after(60, self._poll)

    def _cancel_work(self) -> None:
        if self.cancel is not None:
            self.cancel.cancel()
            self.activity.set("Stopping… Completed changes won’t be undone.")
            self._refresh()

    def _close(self) -> None:
        if self.cancel is None:
            self.root.destroy()
        elif not self.closing and messagebox.askyesno(
            "Stop and close?",
            "Stop the current batch and close MKV Redux? Completed changes won’t be undone.",
            parent=self.root,
        ):
            self.closing = True
            self._cancel_work()


def main() -> None:
    root = tk.Tk()
    ReduxWindow(root)
    root.mainloop()
