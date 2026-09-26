#!/usr/bin/env python3
"""
File Organizer
--------------
A small desktop app (Tkinter) that sorts the files in a folder into
subfolders by category (Pictures, Documents, ...), by extension, or by date.

Features
  * Preview (dry run) before anything is moved
  * Safe moves: name clashes get " (1)", " (2)", ... instead of overwriting
  * Duplicate detection: a file whose content already exists in its target
    folder (same size + same SHA-256 hash) can be moved to a "Duplicates"
    folder, left where it is, or kept anyway (renamed)
  * Sorting rules you can edit in the app (Edit rules...): folders by
    extension, plus filename-keyword rules such as "invoice" -> Finance
  * Remembers folder, sort mode, duplicate handling, theme, auto interval
    and window size between sessions
  * Theme: System / Light / Dark. Follows the OS setting live when set to
    System. Dark mode also works without sv-ttk (built-in fallback palette)
  * Undo the last run (a small hidden log is kept inside the folder)
  * Optional auto-organize mode that re-checks the folder every N seconds
  * Skips hidden files and unfinished downloads (.crdownload, .part, ...)

Settings and rules are stored in:
  Windows:  %APPDATA%\\FileOrganizer\\
  macOS:    ~/Library/Application Support/FileOrganizer/
  Linux:    ~/.config/file_organizer/

Requirements: Python 3.8+. For the Windows 11 look (optional but recommended):
                  pip install sv-ttk darkdetect
              Without them the app still works, including dark mode.
Run with:     python file_organizer.py
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox

try:
    import sv_ttk  # Windows 11 "Sun Valley" theme: pip install sv-ttk
except ImportError:
    sv_ttk = None
try:
    import darkdetect  # reads the OS light/dark setting: pip install darkdetect
except ImportError:
    darkdetect = None

APP_TITLE = "File Organizer"
IS_WINDOWS = sys.platform == "win32"
UNDO_LOG_NAME = ".file_organizer_undo.json"
MAX_LOGGED_RUNS = 20
AUTO_MIN_FILE_AGE_S = 5        # in auto mode, leave files alone that changed very recently
DUPLICATES_FOLDER = "Duplicates"
HASH_CHUNK = 1024 * 1024       # read files 1 MB at a time when hashing
SYSTEM_THEME_POLL_MS = 3000    # how often "System" theme re-checks the OS setting

# ---------------------------------------------------------------------------
# Default sorting rules (used until you save your own with "Edit rules...")
# Note: the original version listed "Documents" twice, so the first set was
# silently dropped and .tex/.epub ended up in "Others". Fixed here.
# ---------------------------------------------------------------------------
DEFAULT_CATEGORIES = {
    "Pictures": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg",
                 ".heic", ".tif", ".tiff", ".ico", ".raw"],
    "Documents": [".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".tex", ".epub"],
    "PDF": [".pdf"],
    "Spreadsheets": [".xls", ".xlsx", ".ods", ".csv"],
    "Presentations": [".ppt", ".pptx", ".odp", ".key"],
    "Music": [".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma"],
    "Videos": [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".flv"],
    "Compressed": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"],
    "Code": [".py", ".ipynb", ".js", ".ts", ".html", ".css", ".java", ".c",
             ".cpp", ".h", ".json", ".xml", ".yml", ".yaml", ".sh", ".m", ".r"],
    "Programs": [".exe", ".msi", ".dmg", ".deb", ".rpm", ".appimage", ".apk"],
}
DEFAULT_OTHER_FOLDER = "Others"
PARTIAL_EXTS = {".crdownload", ".part", ".partial", ".tmp", ".download"}

MODES = {
    "category": "Category",
    "extension": "Extension",
    "date": "Date modified",
}
DUP_MODES = {
    "move": "Move to Duplicates folder",
    "skip": "Leave in place",
    "keep": "Keep both (rename)",
}
THEMES = {"system": "System", "light": "Light", "dark": "Dark"}

DEFAULT_SETTINGS = {
    "folder": "",
    "mode": "category",
    "duplicates": "move",
    "theme": "system",
    "auto_interval": 10,
    "window_size": "900x660",
    "maximized": False,
}


# ---------------------------------------------------------------------------
# Config files (settings + rules)
# ---------------------------------------------------------------------------
def config_dir() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Roaming") / "FileOrganizer"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FileOrganizer"
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "file_organizer"


SETTINGS_FILE = config_dir() / "settings.json"
RULES_FILE = config_dir() / "rules.json"


def read_json(path: Path):
    """Return parsed JSON, or None if the file is missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, data):
    """Write JSON atomically so a crash mid-write can't corrupt the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_settings() -> dict:
    data = read_json(SETTINGS_FILE)
    s = dict(DEFAULT_SETTINGS)
    if not isinstance(data, dict):
        return s
    if isinstance(data.get("folder"), str):
        s["folder"] = data["folder"]
    if data.get("mode") in MODES:
        s["mode"] = data["mode"]
    if data.get("duplicates") in DUP_MODES:
        s["duplicates"] = data["duplicates"]
    if data.get("theme") in THEMES:
        s["theme"] = data["theme"]
    if isinstance(data.get("auto_interval"), int):
        s["auto_interval"] = min(3600, max(5, data["auto_interval"]))
    if isinstance(data.get("window_size"), str) and re.fullmatch(r"\d{3,5}x\d{3,5}", data["window_size"]):
        s["window_size"] = data["window_size"]
    s["maximized"] = bool(data.get("maximized", False))
    return s


# ---------------------------------------------------------------------------
# Sorting rules
# ---------------------------------------------------------------------------
INVALID_NAME_CHARS = set('<>:"/\\|?*')


def folder_name_error(name: str):
    """Return an error message if `name` can't be used as a folder name, else None."""
    if not name:
        return "Folder name can't be empty."
    if name in {".", ".."}:
        return f'"{name}" can\'t be used as a folder name.'
    if any(c in INVALID_NAME_CHARS or ord(c) < 32 for c in name):
        return f'"{name}" contains a character folders can\'t use:  < > : " / \\ | ? *'
    if name.endswith((" ", ".")):
        return f'"{name}" ends with a space or period, which Windows doesn\'t allow.'
    if name.lower() == DUPLICATES_FOLDER.lower():
        return f'"{DUPLICATES_FOLDER}" is reserved for duplicate files. Choose another name.'
    return None


def normalize_extensions(text: str) -> list:
    """'JPG, .png  gif' -> ['.jpg', '.png', '.gif'] (lowercase, unique, in order)."""
    out = []
    for part in re.split(r"[,\s;]+", text):
        part = part.strip().lower()
        if not part or part == ".":
            continue
        ext = part if part.startswith(".") else "." + part
        if ext not in out:
            out.append(ext)
    return out


class Rules:
    """Where each file goes in "category" mode.

    Keyword rules are checked first, in order (first match wins), against the
    file name without its extension. Then the extension is looked up. Anything
    unmatched goes to `other_folder`.
    """

    def __init__(self, categories: dict, keywords: list, other_folder: str):
        self.categories = {name: list(exts) for name, exts in categories.items()}
        self.keywords = [{"keyword": k["keyword"], "folder": k["folder"]} for k in keywords]
        self.other_folder = other_folder
        self.ext_map = {}
        for name, exts in self.categories.items():
            for ext in exts:
                self.ext_map.setdefault(ext, name)

    @classmethod
    def defaults(cls) -> "Rules":
        return cls(DEFAULT_CATEGORIES, [], DEFAULT_OTHER_FOLDER)

    @classmethod
    def from_dict(cls, data):
        """Build rules from saved JSON. Returns None if the data is unusable."""
        try:
            cats = {}
            for name, exts in (data.get("categories") or {}).items():
                text = " ".join(map(str, exts)) if isinstance(exts, list) else str(exts)
                cats[str(name).strip()] = normalize_extensions(text)
            kws = [{"keyword": str(k["keyword"]).strip(), "folder": str(k["folder"]).strip()}
                   for k in (data.get("keywords") or []) if isinstance(k, dict)]
            other = str(data.get("other_folder") or DEFAULT_OTHER_FOLDER).strip()
            rules = cls(cats, kws, other)
        except (AttributeError, KeyError, TypeError):
            return None
        return None if rules.errors() else rules

    def to_dict(self) -> dict:
        return {"categories": self.categories, "keywords": self.keywords,
                "other_folder": self.other_folder}

    def errors(self) -> list:
        errs = []
        for name in [*self.categories, *(k["folder"] for k in self.keywords), self.other_folder]:
            msg = folder_name_error(name)
            if msg and msg not in errs:
                errs.append(msg)
        owner = {}
        for name, exts in self.categories.items():
            if not exts:
                errs.append(f'"{name}" has no extensions.')
            for ext in exts:
                if ext in owner and owner[ext] != name:
                    errs.append(f'{ext} is listed under both "{owner[ext]}" and "{name}".')
                owner.setdefault(ext, name)
        for k in self.keywords:
            if not k["keyword"]:
                errs.append("A keyword rule has an empty keyword.")
        return errs

    def folder_for(self, path: Path) -> str:
        stem = path.stem.lower()
        for k in self.keywords:
            if k["keyword"].lower() in stem:
                return k["folder"]
        return self.ext_map.get(path.suffix.lower(), self.other_folder)


def load_rules():
    """Return (rules, warning). `warning` is set if a saved rules file was unusable."""
    if not RULES_FILE.exists():
        return Rules.defaults(), None
    data = read_json(RULES_FILE)
    rules = Rules.from_dict(data) if isinstance(data, dict) else None
    if rules is None:
        return Rules.defaults(), (f"Your rules file couldn't be read, so the default rules are in use:\n"
                                  f"{RULES_FILE}\n\nSaving rules in the app will replace it.")
    return rules, None


def save_rules(rules: Rules):
    write_json(RULES_FILE, rules.to_dict())


# ---------------------------------------------------------------------------
# Core logic (no GUI code here, so it can be reused or tested on its own)
# ---------------------------------------------------------------------------
def eligible_files(folder: Path, min_age_s: float = 0):
    """Yield top-level files in `folder` that are safe to move."""
    now = time.time()
    with os.scandir(folder) as it:
        for entry in it:
            if not entry.is_file(follow_symlinks=False):
                continue
            if entry.name.startswith("."):
                continue
            if Path(entry.name).suffix.lower() in PARTIAL_EXTS:
                continue
            if min_age_s and now - entry.stat().st_mtime < min_age_s:
                continue
            yield Path(entry.path)


def target_subfolder(path: Path, mode: str, rules: Rules) -> Path:
    ext = path.suffix.lower()
    if mode == "category":
        return Path(rules.folder_for(path))
    if mode == "extension":
        return Path(ext[1:].upper() if ext else "NO_EXTENSION")
    if mode == "date":
        dt = datetime.fromtimestamp(path.stat().st_mtime)
        return Path(f"{dt:%Y}") / f"{dt:%m}"
    raise ValueError(f"Unknown mode: {mode}")


# ---- duplicate detection ----
_hash_cache = {}
_hash_lock = threading.Lock()


def file_digest(path: Path) -> str:
    """SHA-256 of a file's content, cached by (path, size, modified time)."""
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    with _hash_lock:
        if key in _hash_cache:
            return _hash_cache[key]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK), b""):
            h.update(chunk)
    digest = h.hexdigest()
    with _hash_lock:
        if len(_hash_cache) > 5000:
            _hash_cache.clear()
        _hash_cache[key] = digest
    return digest


class DuplicateFinder:
    """Finds files whose content already exists in a destination folder.

    Candidates are narrowed by file size first, so only same-size files are
    ever hashed. Files planned earlier in the same run are included, so two
    identical loose files heading to the same folder are caught too.
    """

    def __init__(self):
        self._index = {}  # destination folder -> {size: [paths]}

    def _folder_index(self, folder: Path) -> dict:
        idx = self._index.get(folder)
        if idx is None:
            idx = {}
            try:
                with os.scandir(folder) as it:
                    for e in it:
                        if e.is_file(follow_symlinks=False) and not e.name.startswith("."):
                            idx.setdefault(e.stat().st_size, []).append(Path(e.path))
            except OSError:
                pass  # folder doesn't exist yet (or can't be read): nothing to compare
            self._index[folder] = idx
        return idx

    def find(self, src: Path, dest_folder: Path, size: int):
        candidates = self._folder_index(dest_folder).get(size, [])
        if not candidates:
            return None
        digest = file_digest(src)
        for c in candidates:
            try:
                if file_digest(c) == digest:
                    return c
            except OSError:
                continue
        return None

    def add(self, path: Path, dest_folder: Path, size: int):
        self._folder_index(dest_folder).setdefault(size, []).append(path)


@dataclass
class PlanItem:
    src: Path
    dest: Path     # intended destination; the final name is decided at move time
    action: str    # "move" or "skip"
    note: str = ""


def build_plan(folder: Path, mode: str, rules: Rules, dup_mode: str = "move",
               min_age_s: float = 0, progress_cb=None) -> list:
    """Decide where every eligible file goes, without moving anything."""
    files = sorted(eligible_files(folder, min_age_s), key=lambda p: p.name.lower())
    finder = DuplicateFinder() if dup_mode != "keep" else None
    plan = []
    for i, f in enumerate(files, 1):
        try:
            dest_folder = folder / target_subfolder(f, mode, rules)
            item = PlanItem(f, dest_folder / f.name, "move")
            if finder:
                size = f.stat().st_size
                # empty files are all "identical"; treating them as duplicates isn't useful
                original = finder.find(f, dest_folder, size) if size else None
                if original is not None:
                    try:
                        where = original.relative_to(folder).as_posix()
                    except ValueError:
                        where = original.name
                    note = f"Same content as {where}"
                    if dup_mode == "skip":
                        item = PlanItem(f, f, "skip", note)
                    else:
                        item = PlanItem(f, folder / DUPLICATES_FOLDER / f.name, "move", note)
                else:
                    finder.add(f, dest_folder, size)
            plan.append(item)
        except OSError:
            pass  # file vanished or unreadable; skip it
        if progress_cb:
            progress_cb(i, len(files))
    return plan


def unique_destination(dest: Path) -> Path:
    """Avoid overwriting: 'a.txt' -> 'a (1).txt', 'a (2).txt', ..."""
    if not dest.exists():
        return dest
    i = 1
    while True:
        candidate = dest.with_name(f"{dest.stem} ({i}){dest.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def execute_plan(plan, progress_cb=None):
    """Move files. Returns (moves, created_dirs, errors)."""
    moves, created_dirs, errors = [], [], []
    to_move = [it for it in plan if it.action == "move"]
    total = len(to_move)
    for i, item in enumerate(to_move, 1):
        src, dest = item.src, item.dest
        try:
            # remember which folders we create, so Undo can remove only those
            missing, p = [], dest.parent
            while not p.exists():
                missing.append(p)
                p = p.parent
            dest.parent.mkdir(parents=True, exist_ok=True)
            created_dirs.extend(str(m) for m in missing)

            final = unique_destination(dest)
            shutil.move(str(src), str(final))
            moves.append({"from": str(src), "to": str(final)})
        except Exception as exc:  # keep going if one file fails
            errors.append(f"{src.name}: {exc}")
        if progress_cb:
            progress_cb(i, total)
    return moves, created_dirs, errors


def load_log(folder: Path):
    data = read_json(folder / UNDO_LOG_NAME)
    return data if isinstance(data, list) else []


def save_log(folder: Path, runs):
    (folder / UNDO_LOG_NAME).write_text(
        json.dumps(runs[-MAX_LOGGED_RUNS:], indent=2), encoding="utf-8")


def organize(folder: Path, mode: str, plan, progress_cb=None) -> dict:
    moves, created, errors = execute_plan(plan, progress_cb)
    if moves:
        runs = load_log(folder)
        runs.append({"time": datetime.now().isoformat(timespec="seconds"),
                     "mode": mode, "moves": moves, "created_dirs": created})
        save_log(folder, runs)
    moved_srcs = {m["from"] for m in moves}
    return {
        "moved": len(moves),
        "duplicates": sum(1 for it in plan if it.action == "move" and it.note
                          and str(it.src) in moved_srcs),
        "skipped": sum(1 for it in plan if it.action == "skip"),
        "errors": errors,
    }


def undo_last_run(folder: Path, progress_cb=None):
    runs = load_log(folder)
    if not runs:
        return {"restored": 0, "errors": [], "nothing": True}
    run = runs.pop()
    restored, errors = 0, []
    moves = list(reversed(run["moves"]))
    for i, m in enumerate(moves, 1):
        current, original = Path(m["to"]), Path(m["from"])
        try:
            if not current.exists():
                errors.append(f"Not found (moved or deleted?): {current.name}")
            else:
                shutil.move(str(current), str(unique_destination(original)))
                restored += 1
        except Exception as exc:
            errors.append(f"{current.name}: {exc}")
        if progress_cb:
            progress_cb(i, len(moves))
    # remove folders this run created, deepest first, only if now empty
    for d in sorted(run.get("created_dirs", []), key=lambda s: s.count(os.sep), reverse=True):
        try:
            Path(d).rmdir()
        except OSError:
            pass
    save_log(folder, runs)
    return {"restored": restored, "errors": errors, "nothing": False}


# ---------------------------------------------------------------------------
# Theme: palettes, OS dark-mode detection, DPI, title bar
# ---------------------------------------------------------------------------
# Used for label colours in both paths, and for the whole look when sv-ttk
# isn't installed (built on ttk's "clam" theme, which accepts custom colours).
PALETTES = {
    "light": {
        "bg": "#f3f3f3", "surface": "#fbfbfb", "field": "#ffffff", "border": "#d1d1d1",
        "fg": "#1b1b1b", "muted": "#616161", "disabled": "#a0a0a0",
        "accent": "#005fb8", "accent_hover": "#196ebf", "accent_fg": "#ffffff",
        "select": "#cce4f7", "select_fg": "#1b1b1b", "warn": "#9d5d00",
    },
    "dark": {
        "bg": "#202020", "surface": "#2d2d2d", "field": "#272727", "border": "#3d3d3d",
        "fg": "#f3f3f3", "muted": "#9d9d9d", "disabled": "#6e6e6e",
        "accent": "#4cc2ff", "accent_hover": "#47b1e8", "accent_fg": "#000000",
        "select": "#264f78", "select_fg": "#ffffff", "warn": "#fce100",
    },
}


def system_theme() -> str:
    """'dark' or 'light', from darkdetect or (on Windows) the registry."""
    if darkdetect:
        try:
            return "dark" if (darkdetect.theme() or "").lower() == "dark" else "light"
        except Exception:
            pass
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if value else "dark"
        except OSError:
            pass
    return "light"


def enable_high_dpi():
    """Crisp text on high-DPI screens (must run before Tk() is created)."""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def set_title_bar_dark(win, dark: bool):
    """Dark/light title bar via the Windows DWM API (Windows 10 20H1+ / 11)."""
    if not IS_WINDOWS:
        return
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (19 on older Win10 builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
        # nudge Windows to repaint the title bar
        win.attributes("-alpha", 0.99)
        win.after(10, lambda: win.attributes("-alpha", 1.0))
    except Exception:
        pass


def pick_font(root, *candidates):
    available = set(tkfont.families(root))
    for name in candidates:
        if name in available:
            return name
    return tkfont.nametofont("TkDefaultFont").actual("family")


def configure_fallback_theme(style: ttk.Style, p: dict):
    """A complete light or dark look on ttk's 'clam' theme (no sv-ttk needed)."""
    style.theme_use("clam")
    style.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["field"],
                    bordercolor=p["border"], lightcolor=p["surface"], darkcolor=p["surface"],
                    troughcolor=p["surface"], focuscolor=p["accent"], insertcolor=p["fg"],
                    selectbackground=p["select"], selectforeground=p["select_fg"],
                    arrowcolor=p["fg"])
    style.map(".", foreground=[("disabled", p["disabled"])])

    style.configure("TButton", background=p["surface"], padding=(12, 5))
    style.map("TButton", background=[("disabled", p["bg"]), ("pressed", p["border"]),
                                     ("active", p["border"])])
    style.configure("Accent.TButton", background=p["accent"], foreground=p["accent_fg"],
                    bordercolor=p["accent"])
    style.map("Accent.TButton",
              background=[("disabled", p["surface"]), ("pressed", p["accent"]),
                          ("active", p["accent_hover"])],
              foreground=[("disabled", p["disabled"])])

    for w in ("TEntry", "TSpinbox"):
        style.configure(w, fieldbackground=p["field"], foreground=p["fg"],
                        background=p["surface"], insertcolor=p["fg"])
    for w in ("TCheckbutton", "TRadiobutton"):
        style.configure(w, background=p["bg"], indicatorbackground=p["field"],
                        indicatorforeground=p["accent_fg"], upperbordercolor=p["border"],
                        lowerbordercolor=p["border"])
        style.map(w, indicatorbackground=[("selected", p["accent"]), ("pressed", p["border"])],
                  background=[("active", p["bg"])])

    style.configure("Treeview", background=p["field"], fieldbackground=p["field"],
                    foreground=p["fg"], rowheight=26, bordercolor=p["border"])
    style.map("Treeview", background=[("selected", p["select"])],
              foreground=[("selected", p["select_fg"])])
    style.configure("Treeview.Heading", background=p["surface"], foreground=p["fg"],
                    relief="flat", padding=(6, 4))
    style.map("Treeview.Heading", background=[("active", p["border"])])

    style.configure("TNotebook", background=p["bg"], bordercolor=p["border"])
    style.configure("TNotebook.Tab", background=p["surface"], foreground=p["fg"], padding=(12, 4))
    style.map("TNotebook.Tab", background=[("selected", p["bg"])])

    style.configure("TScrollbar", background=p["surface"], troughcolor=p["bg"],
                    bordercolor=p["bg"], arrowcolor=p["fg"])
    style.map("TScrollbar", background=[("active", p["border"])])
    style.configure("TProgressbar", background=p["accent"], troughcolor=p["surface"],
                    bordercolor=p["border"])


# ---------------------------------------------------------------------------
# Rules editor window
# ---------------------------------------------------------------------------
class RulesEditor:
    def __init__(self, app: "OrganizerApp"):
        self.app = app
        rules = app.rules
        self.categories = [[name, list(exts)] for name, exts in rules.categories.items()]
        self.keywords = [dict(k) for k in rules.keywords]

        win = self.win = tk.Toplevel(app.root)
        win.title("Sorting rules")
        win.transient(app.root)
        win.geometry("680x520")
        win.minsize(560, 420)
        app.register_window(win)

        main = ttk.Frame(win, padding=(22, 18, 22, 16))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        ttk.Label(main, text="Sorting rules", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(main, style="Muted.TLabel", wraplength=620, justify="left",
                  text="Used when sorting by category. Keyword rules are checked first, top to "
                       "bottom, against the file name. Then the extension decides. Anything "
                       "unmatched goes to the fallback folder.").grid(row=1, column=0, sticky="w", pady=(2, 12))

        nb = ttk.Notebook(main)
        nb.grid(row=2, column=0, sticky="nsew")
        self.cat_tree = self._make_tab(nb, "By extension", ("folder", "exts"),
                                       ("Folder", "Extensions"), (150, 400),
                                       [("Add…", self.add_category), ("Edit…", self.edit_category),
                                        ("Remove", self.remove_category)])
        self.kw_tree = self._make_tab(nb, "By file name keyword", ("keyword", "folder"),
                                      ("File name contains", "Folder"), (260, 200),
                                      [("Add…", self.add_keyword), ("Edit…", self.edit_keyword),
                                       ("Remove", self.remove_keyword), ("Move up", lambda: self.move_keyword(-1)),
                                       ("Move down", lambda: self.move_keyword(1))])
        self.cat_tree.bind("<Double-1>", lambda e: self.edit_category())
        self.kw_tree.bind("<Double-1>", lambda e: self.edit_keyword())
        self.cat_tree.bind("<Delete>", lambda e: self.remove_category())
        self.kw_tree.bind("<Delete>", lambda e: self.remove_keyword())

        other = ttk.Frame(main)
        other.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        ttk.Label(other, text="Fallback folder").grid(row=0, column=0, padx=(0, 8))
        self.other_var = tk.StringVar(value=rules.other_folder)
        ttk.Entry(other, textvariable=self.other_var, width=24).grid(row=0, column=1)

        bottom = ttk.Frame(main)
        bottom.grid(row=4, column=0, sticky="ew", pady=(16, 0))
        bottom.columnconfigure(1, weight=1)
        ttk.Button(bottom, text="Reset to defaults", command=self.reset).grid(row=0, column=0)
        ttk.Button(bottom, text="Cancel", command=self.close).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(bottom, text="Save rules", style="Accent.TButton",
                   command=self.save).grid(row=0, column=3)

        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda e: self.close())
        self.refresh()
        center_over(win, app.root)
        win.grab_set()

    def _make_tab(self, nb, title, cols, headings, widths, buttons):
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text=title)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        tree = ttk.Treeview(tab, columns=cols, show="headings", selectmode="browse")
        for c, h, w in zip(cols, headings, widths):
            tree.heading(c, text=h, anchor="w")
            tree.column(c, width=w, anchor="w", stretch=True)
        sb = ttk.Scrollbar(tab, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        side = ttk.Frame(tab)
        side.grid(row=0, column=2, sticky="n", padx=(10, 0))
        for i, (text, cmd) in enumerate(buttons):
            ttk.Button(side, text=text, command=cmd, width=11).grid(row=i, column=0, pady=(0, 6), sticky="ew")
        return tree

    # ---- helpers ----
    def refresh(self, select_cat=None, select_kw=None):
        self.cat_tree.delete(*self.cat_tree.get_children())
        for i, (name, exts) in enumerate(self.categories):
            self.cat_tree.insert("", "end", iid=str(i), values=(name, ", ".join(exts)))
        self.kw_tree.delete(*self.kw_tree.get_children())
        for i, k in enumerate(self.keywords):
            self.kw_tree.insert("", "end", iid=str(i), values=(k["keyword"], k["folder"]))
        for tree, idx in ((self.cat_tree, select_cat), (self.kw_tree, select_kw)):
            if idx is not None and tree.exists(str(idx)):
                tree.selection_set(str(idx))
                tree.see(str(idx))

    @staticmethod
    def _selected(tree):
        sel = tree.selection()
        return int(sel[0]) if sel else None

    def _error(self, text):
        messagebox.showerror("Sorting rules", text, parent=self.win)

    # ---- extension rules ----
    def _category_error(self, name, exts, index):
        msg = folder_name_error(name)
        if msg:
            return msg
        for i, (other, _) in enumerate(self.categories):
            if i != index and other.lower() == name.lower():
                return f'There\'s already a rule for "{other}". Edit that one instead.'
        if not exts:
            return "Add at least one extension, for example:  .jpg, .png"
        clashes = [f"{e} (already in {other})" for i, (other, o_exts) in enumerate(self.categories)
                   if i != index for e in exts if e in o_exts]
        if clashes:
            return "Each extension can only belong to one folder:\n\n" + "\n".join(clashes)
        return None

    def _edit_category_at(self, index):
        name, exts = self.categories[index] if index is not None else ("", [])
        text = ", ".join(exts)
        while True:
            res = ask_two_fields(self.app, self.win, "Extension rule" if index is None else f"Edit “{name}”",
                                 "Folder name", name, "Extensions", text,
                                 "Separate extensions with commas or spaces, e.g.  .jpg, png, .webp")
            if res is None:
                return
            name, text = res
            exts = normalize_extensions(text)
            err = self._category_error(name, exts, index)
            if not err:
                break
            self._error(err)
        if index is None:
            self.categories.append([name, exts])
            index = len(self.categories) - 1
        else:
            self.categories[index] = [name, exts]
        self.refresh(select_cat=index)

    def add_category(self):
        self._edit_category_at(None)

    def edit_category(self):
        i = self._selected(self.cat_tree)
        if i is not None:
            self._edit_category_at(i)

    def remove_category(self):
        i = self._selected(self.cat_tree)
        if i is not None:
            del self.categories[i]
            self.refresh(select_cat=min(i, len(self.categories) - 1))

    # ---- keyword rules ----
    def _edit_keyword_at(self, index):
        k = self.keywords[index] if index is not None else {"keyword": "", "folder": ""}
        kw, folder = k["keyword"], k["folder"]
        while True:
            res = ask_two_fields(self.app, self.win, "Keyword rule",
                                 "File name contains", kw, "Folder name", folder,
                                 "Not case-sensitive. Example: “invoice” → Finance")
            if res is None:
                return
            kw, folder = res
            err = "Enter a keyword." if not kw else folder_name_error(folder)
            if not err:
                break
            self._error(err)
        if index is None:
            self.keywords.append({"keyword": kw, "folder": folder})
            index = len(self.keywords) - 1
        else:
            self.keywords[index] = {"keyword": kw, "folder": folder}
        self.refresh(select_kw=index)

    def add_keyword(self):
        self._edit_keyword_at(None)

    def edit_keyword(self):
        i = self._selected(self.kw_tree)
        if i is not None:
            self._edit_keyword_at(i)

    def remove_keyword(self):
        i = self._selected(self.kw_tree)
        if i is not None:
            del self.keywords[i]
            self.refresh(select_kw=min(i, len(self.keywords) - 1))

    def move_keyword(self, delta):
        i = self._selected(self.kw_tree)
        if i is None or not 0 <= i + delta < len(self.keywords):
            return
        self.keywords[i], self.keywords[i + delta] = self.keywords[i + delta], self.keywords[i]
        self.refresh(select_kw=i + delta)

    # ---- save / reset / close ----
    def reset(self):
        if not messagebox.askyesno("Sorting rules", "Replace all rules with the defaults?\n"
                                   "Nothing is saved until you select Save rules.", parent=self.win):
            return
        d = Rules.defaults()
        self.categories = [[n, list(e)] for n, e in d.categories.items()]
        self.keywords = []
        self.other_var.set(d.other_folder)
        self.refresh()

    def save(self):
        rules = Rules(dict((n, e) for n, e in self.categories), self.keywords,
                      self.other_var.get().strip())
        errs = rules.errors()
        if errs:
            self._error("Fix these before saving:\n\n" + "\n".join(errs))
            return
        try:
            save_rules(rules)
        except OSError as exc:
            self._error(f"Couldn't save the rules file:\n{exc}")
            return
        self.app.set_rules(rules)
        self.close()

    def close(self):
        self.win.grab_release()
        self.win.destroy()


def center_over(win, parent):
    win.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - win.winfo_width()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - win.winfo_height()) // 3
    win.geometry(f"+{max(x, 0)}+{max(y, 0)}")


def ask_two_fields(app, parent, title, label1, value1, label2, value2, hint=""):
    """Small themed dialog with two text fields. Returns (a, b) or None."""
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.transient(parent)
    dlg.resizable(False, False)
    app.register_window(dlg)
    frm = ttk.Frame(dlg, padding=(20, 16, 20, 16))
    frm.pack(fill="both", expand=True)
    v1, v2 = tk.StringVar(value=value1), tk.StringVar(value=value2)
    ttk.Label(frm, text=label1).grid(row=0, column=0, sticky="w")
    e1 = ttk.Entry(frm, textvariable=v1, width=46)
    e1.grid(row=1, column=0, sticky="ew", pady=(4, 10))
    ttk.Label(frm, text=label2).grid(row=2, column=0, sticky="w")
    ttk.Entry(frm, textvariable=v2, width=46).grid(row=3, column=0, sticky="ew", pady=(4, 4))
    if hint:
        ttk.Label(frm, text=hint, style="Muted.TLabel").grid(row=4, column=0, sticky="w")
    btns = ttk.Frame(frm)
    btns.grid(row=5, column=0, sticky="e", pady=(14, 0))
    result = {}

    def ok(_=None):
        result["v"] = (v1.get().strip(), v2.get().strip())
        dlg.destroy()

    ttk.Button(btns, text="Cancel", command=dlg.destroy).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(btns, text="OK", style="Accent.TButton", command=ok).grid(row=0, column=1)
    dlg.bind("<Return>", ok)
    dlg.bind("<Escape>", lambda e: dlg.destroy())
    center_over(dlg, parent)
    e1.focus_set()
    dlg.grab_set()
    dlg.wait_window()
    try:
        parent.grab_set()  # give the modal grab back to the rules editor
    except tk.TclError:
        pass
    return result.get("v")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class OrganizerApp:
    POLL_MS = 100

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.minsize(720, 540)

        self.settings = load_settings()
        self.rules, rules_warning = load_rules()

        self.style = ttk.Style(root)
        self.display_font = pick_font(root, "Segoe UI Variable Display", "Segoe UI", "Helvetica")
        self.text_font = pick_font(root, "Segoe UI Variable Text", "Segoe UI", "Helvetica")

        s = self.settings
        self.folder_var = tk.StringVar(value=s["folder"])
        self.mode_var = tk.StringVar(value=s["mode"])
        self.dup_var = tk.StringVar(value=s["duplicates"])
        self.theme_var = tk.StringVar(value=s["theme"])
        self.auto_var = tk.BooleanVar(value=False)  # auto mode always starts off, on purpose
        self.interval_var = tk.IntVar(value=s["auto_interval"])
        self.status_var = tk.StringVar(value="Choose a folder to get started.")

        self.busy = False
        self.ui_locked = False
        self.is_dark = False
        self.q = queue.Queue()
        self.auto_job = None
        self.save_job = None
        self.toplevels = []

        self._restore_window(s)
        self._build_ui()
        self.apply_theme()

        if s["folder"]:
            if Path(s["folder"]).expanduser().is_dir():
                self.status_var.set("Welcome back. Select Preview to see what would move.")
            else:
                self.status_var.set(f"Your last folder isn't available anymore: {s['folder']}")
        self._show_empty_state("Choose a folder, then select Preview to see where each file will go.")

        for var in (self.folder_var, self.mode_var, self.dup_var, self.theme_var, self.interval_var):
            var.trace_add("write", self._schedule_save)

        root.after(self.POLL_MS, self._process_queue)
        root.after(SYSTEM_THEME_POLL_MS, self._watch_system_theme)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        if rules_warning:
            root.after(300, lambda: messagebox.showwarning(APP_TITLE, rules_warning, parent=root))

    # ---------- settings ----------
    def _restore_window(self, s):
        self.root.geometry(s["window_size"])
        if s["maximized"]:
            try:
                self.root.state("zoomed")            # Windows, macOS
            except tk.TclError:
                try:
                    self.root.attributes("-zoomed", True)  # most Linux window managers
                except tk.TclError:
                    pass

    def _collect_settings(self) -> dict:
        s = dict(self.settings)
        s["folder"] = self.folder_var.get().strip()
        s["mode"] = self.mode_var.get()
        s["duplicates"] = self.dup_var.get()
        s["theme"] = self.theme_var.get()
        try:
            s["auto_interval"] = min(3600, max(5, int(self.interval_var.get())))
        except (tk.TclError, ValueError):
            pass  # half-typed number; keep the last good value
        return s

    def _schedule_save(self, *_):
        if self.save_job:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(600, self._save_settings)

    def _save_settings(self, include_window=False):
        self.save_job = None
        s = self._collect_settings()
        if include_window:
            try:
                zoomed = self.root.state() == "zoomed" or bool(self.root.attributes("-zoomed"))
            except tk.TclError:
                zoomed = self.root.state() == "zoomed"
            s["maximized"] = zoomed
            if not zoomed and self.root.state() == "normal":
                s["window_size"] = f"{self.root.winfo_width()}x{self.root.winfo_height()}"
        self.settings = s
        try:
            write_json(SETTINGS_FILE, s)
        except OSError:
            pass  # settings are a convenience; never block the app over them

    # ---------- theme ----------
    def apply_theme(self):
        choice = self.theme_var.get()
        dark = (system_theme() == "dark") if choice == "system" else (choice == "dark")
        self.is_dark = dark
        p = PALETTES["dark" if dark else "light"]
        if sv_ttk:
            sv_ttk.set_theme("dark" if dark else "light")
        else:
            configure_fallback_theme(self.style, p)

        self.style.configure("Title.TLabel", font=(self.display_font, 22, "bold"))
        self.style.configure("Heading.TLabel", font=(self.display_font, 15, "bold"))
        self.style.configure("Subtitle.TLabel", font=(self.text_font, 10), foreground=p["muted"])
        self.style.configure("Section.TLabel", font=(self.text_font, 10, "bold"))
        self.style.configure("Muted.TLabel", font=(self.text_font, 10), foreground=p["muted"])

        # plain tk windows don't follow ttk themes, so colour them by hand
        bg = self.style.lookup("TFrame", "background") or p["bg"]
        for w in [self.root, *self.toplevels]:
            try:
                w.configure(bg=bg)
                set_title_bar_dark(w, dark)
            except tk.TclError:
                pass
        self.tree.tag_configure("dup", foreground=p["warn"])
        self.tree.tag_configure("skip", foreground=p["muted"])

    def register_window(self, win):
        """Keep extra windows (dialogs) in step with the current theme."""
        self.toplevels.append(win)
        try:
            win.configure(bg=self.style.lookup("TFrame", "background"))
        except tk.TclError:
            pass
        win.after(10, lambda: set_title_bar_dark(win, self.is_dark))

        def forget(e):
            if e.widget is win and win in self.toplevels:
                self.toplevels.remove(win)
        win.bind("<Destroy>", forget, add="+")

    def _watch_system_theme(self):
        if self.theme_var.get() == "system" and (system_theme() == "dark") != self.is_dark:
            self.apply_theme()
        self.root.after(SYSTEM_THEME_POLL_MS, self._watch_system_theme)

    # ---------- layout ----------
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=(28, 22, 28, 18))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(7, weight=1)

        # header: title + theme picker
        header = ttk.Frame(main)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Sort a folder into tidy subfolders. Preview first, undo anytime.",
                  style="Subtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))
        theme_box = ttk.Frame(header)
        theme_box.grid(row=0, column=1, rowspan=2, sticky="e")
        ttk.Label(theme_box, text="Theme", style="Muted.TLabel").grid(row=0, column=0, padx=(0, 10))
        for col, (key, label) in enumerate(THEMES.items(), 1):
            ttk.Radiobutton(theme_box, text=label, value=key, variable=self.theme_var,
                            command=self.apply_theme).grid(row=0, column=col, padx=(0, 10))

        # folder
        ttk.Label(main, text="Folder", style="Section.TLabel").grid(row=1, column=0, sticky="w", pady=(22, 6))
        folder_row = ttk.Frame(main)
        folder_row.grid(row=2, column=0, sticky="ew")
        folder_row.columnconfigure(0, weight=1)
        ttk.Entry(folder_row, textvariable=self.folder_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(folder_row, text="Browse…", command=self.browse).grid(row=0, column=1)

        # sort mode
        ttk.Label(main, text="Sort by", style="Section.TLabel").grid(row=3, column=0, sticky="w", pady=(18, 6))
        sort_bar = ttk.Frame(main)
        sort_bar.grid(row=4, column=0, sticky="ew")
        sort_bar.columnconfigure(len(MODES), weight=1)
        for col, (key, label) in enumerate(MODES.items()):
            ttk.Radiobutton(sort_bar, text=label, value=key, variable=self.mode_var,
                            command=self._options_changed).grid(row=0, column=col, padx=(0, 18))
        ttk.Button(sort_bar, text="Edit rules…", command=self.edit_rules).grid(
            row=0, column=len(MODES) + 1, sticky="e")

        # duplicates
        ttk.Label(main, text="Identical files", style="Section.TLabel").grid(
            row=5, column=0, sticky="w", pady=(16, 6))
        dup_bar = ttk.Frame(main)
        dup_bar.grid(row=6, column=0, sticky="ew")
        for col, (key, label) in enumerate(DUP_MODES.items()):
            ttk.Radiobutton(dup_bar, text=label, value=key, variable=self.dup_var,
                            command=self._options_changed).grid(row=0, column=col, padx=(0, 18))

        # preview table (with an empty-state message in the same spot)
        table = ttk.Frame(main)
        table.grid(row=7, column=0, sticky="nsew", pady=(18, 12))
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=("file", "dest", "note"), show="headings")
        self.tree.heading("file", text="File", anchor="w")
        self.tree.heading("dest", text="Moves to", anchor="w")
        self.tree.heading("note", text="Note", anchor="w")
        self.tree.column("file", width=340, anchor="w")
        self.tree.column("dest", width=180, anchor="w")
        self.tree.column("note", width=240, anchor="w")
        sb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.empty_label = ttk.Label(table, style="Muted.TLabel", anchor="center", justify="center")
        self.empty_label.grid(row=0, column=0, sticky="nsew")

        # auto mode + status
        footer = ttk.Frame(main)
        footer.grid(row=8, column=0, sticky="ew")
        footer.columnconfigure(3, weight=1)
        ttk.Checkbutton(footer, text="Auto-organize every", variable=self.auto_var,
                        style="Switch.TCheckbutton", command=self.toggle_auto).grid(row=0, column=0)
        ttk.Spinbox(footer, from_=5, to=3600, width=5,
                    textvariable=self.interval_var).grid(row=0, column=1, padx=6)
        ttk.Label(footer, text="seconds").grid(row=0, column=2)
        ttk.Label(footer, textvariable=self.status_var, style="Muted.TLabel",
                  anchor="e").grid(row=0, column=3, sticky="e")

        # progress + actions
        actions = ttk.Frame(main)
        actions.grid(row=9, column=0, sticky="ew", pady=(14, 0))
        actions.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(actions, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 16))
        self.btn_preview = ttk.Button(actions, text="Preview", command=self.preview)
        self.btn_undo = ttk.Button(actions, text="Undo last run", command=self.undo)
        self.btn_organize = ttk.Button(actions, text="Organize now", style="Accent.TButton",
                                       command=self.organize_now)
        self.btn_preview.grid(row=0, column=1, padx=(0, 8))
        self.btn_undo.grid(row=0, column=2, padx=(0, 8))
        self.btn_organize.grid(row=0, column=3)

    # ---------- helpers ----------
    def _show_empty_state(self, text):
        self.empty_label.configure(text=text)
        self.empty_label.lift()

    def _show_table(self):
        self.tree.lift()

    def _options_changed(self):
        if self.tree.get_children():
            self.preview()  # keep the preview in sync with the chosen options

    def _get_folder(self, silent=False):
        text = self.folder_var.get().strip()
        folder = Path(text).expanduser().resolve() if text else None
        if not folder or not folder.is_dir():
            if not silent:
                messagebox.showwarning(APP_TITLE, "That folder doesn't exist. Choose a folder with Browse.",
                                       parent=self.root)
            return None
        return folder

    def _set_buttons(self, enabled: bool):
        for b in (self.btn_preview, self.btn_organize, self.btn_undo):
            b.state(["!disabled"] if enabled else ["disabled"])

    def _check_idle(self) -> bool:
        if self.busy:
            self.status_var.set("Still working. Try again in a moment.")
            return False
        return True

    def _clear_tree(self):
        self.tree.delete(*self.tree.get_children())

    def _run_background(self, job, on_done, lock_ui=True):
        """Run `job` in a worker thread so the window never freezes."""
        self.busy = True
        self.ui_locked = lock_ui
        if lock_ui:
            self._set_buttons(False)
            self.progress["value"] = 0

        def worker():
            try:
                self.q.put(("done", on_done, job(), None))
            except Exception as exc:
                self.q.put(("done", on_done, None, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _progress_cb(self, i, n):
        self.q.put(("progress", i, n))

    def _process_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "progress":
                    _, i, n = msg
                    self.progress["maximum"] = max(n, 1)
                    self.progress["value"] = i
                elif msg[0] == "done":
                    _, on_done, result, err = msg
                    self.busy = False
                    if self.ui_locked:
                        self._set_buttons(True)
                    on_done(result, err)
        except queue.Empty:
            pass
        self.root.after(self.POLL_MS, self._process_queue)

    @staticmethod
    def _plan_summary(plan) -> str:
        moves = sum(1 for it in plan if it.action == "move")
        dups = sum(1 for it in plan if it.note)
        text = f"{moves} file(s) ready to move"
        if dups:
            text += f", {dups} identical"
        return text + ". Nothing has changed yet."

    # ---------- actions ----------
    def browse(self):
        path = filedialog.askdirectory(title="Choose a folder to organize", parent=self.root)
        if path:
            self.folder_var.set(path)
            self._clear_tree()
            self._show_empty_state("Select Preview to see where each file will go.")
            self.status_var.set("Folder selected.")

    def edit_rules(self):
        RulesEditor(self)

    def set_rules(self, rules: Rules):
        self.rules = rules
        self.status_var.set("Rules saved.")
        if self.tree.get_children():
            self.preview()

    def preview(self):
        folder = self._get_folder()
        if not folder or not self._check_idle():
            return
        mode, dup, rules = self.mode_var.get(), self.dup_var.get(), self.rules
        self.status_var.set("Checking files…")
        self._run_background(
            lambda: build_plan(folder, mode, rules, dup, progress_cb=self._progress_cb),
            lambda plan, err: self._show_plan(folder, plan, err))

    def _show_plan(self, folder, plan, err):
        self._clear_tree()
        if err:
            self.status_var.set(f"Couldn't read the folder: {err}")
            return
        if not plan:
            self._show_empty_state("This folder is already tidy. There are no loose files to move.")
            self.status_var.set("Nothing to move.")
            return
        for it in plan:
            if it.action == "skip":
                dest, tags = "Stays here", ("skip",)
            else:
                dest, tags = str(it.dest.parent.relative_to(folder)), (("dup",) if it.note else ())
            self.tree.insert("", "end", values=(it.src.name, dest, it.note), tags=tags)
        self._show_table()
        self.status_var.set(self._plan_summary(plan))

    def organize_now(self):
        folder = self._get_folder()
        if not folder or not self._check_idle():
            return
        mode, dup, rules = self.mode_var.get(), self.dup_var.get(), self.rules
        self.status_var.set("Checking files…")
        self._run_background(
            lambda: build_plan(folder, mode, rules, dup, progress_cb=self._progress_cb),
            lambda plan, err: self._confirm_and_organize(folder, mode, plan, err))

    def _confirm_and_organize(self, folder, mode, plan, err):
        if err:
            self.status_var.set(f"Couldn't read the folder: {err}")
            messagebox.showerror(APP_TITLE, str(err), parent=self.root)
            return
        moves = [it for it in plan if it.action == "move"]
        skipped = len(plan) - len(moves)
        if not moves:
            text = "There are no loose files to organize in this folder."
            if skipped:
                text = f"Nothing to move. {skipped} identical file(s) are being left in place."
            self.status_var.set("Nothing to move.")
            messagebox.showinfo(APP_TITLE, text, parent=self.root)
            return
        dups = sum(1 for it in moves if it.note)
        detail = ""
        if dups:
            detail += f"\n{dups} identical file(s) will go to “{DUPLICATES_FOLDER}”."
        if skipped:
            detail += f"\n{skipped} identical file(s) will be left in place."
        if not messagebox.askyesno(APP_TITLE, f"Move {len(moves)} file(s) in\n{folder}?\n{detail}\n\n"
                                              "You can reverse this with Undo last run.", parent=self.root):
            self.status_var.set("Cancelled. Nothing was moved.")
            return
        self.status_var.set("Organizing…")
        self._run_background(lambda: organize(folder, mode, plan, self._progress_cb),
                             lambda r, e: self._after_organize(r, e, silent=False))

    def _after_organize(self, result, err, silent):
        self._clear_tree()
        if err:
            self.status_var.set(f"Couldn't organize: {err}")
            if not silent:
                messagebox.showerror(APP_TITLE, str(err), parent=self.root)
            return
        stamp = datetime.now().strftime("%H:%M")
        msg = f"Organized {result['moved']} file(s) at {stamp}."
        if result["duplicates"]:
            msg += f" {result['duplicates']} went to {DUPLICATES_FOLDER}."
        if result["skipped"]:
            msg += f" {result['skipped']} identical left in place."
        if result["errors"]:
            msg += f" {len(result['errors'])} couldn't be moved."
            if not silent:
                messagebox.showwarning(APP_TITLE, "These files couldn't be moved "
                                       "(they may be open in another app):\n\n"
                                       + "\n".join(result["errors"][:15]), parent=self.root)
        self._show_empty_state(f"Organized {result['moved']} file(s). Select Undo last run to put them back.")
        self.status_var.set(msg)

    def undo(self):
        folder = self._get_folder()
        if not folder or not self._check_idle():
            return
        runs = load_log(folder)
        if not runs:
            messagebox.showinfo(APP_TITLE, "There's nothing to undo in this folder.", parent=self.root)
            return
        last = runs[-1]
        if not messagebox.askyesno(APP_TITLE, f"Put back the {len(last['moves'])} file(s) "
                                              f"moved at {last['time'].replace('T', ' ')}?", parent=self.root):
            return
        self.status_var.set("Undoing…")
        self._run_background(lambda: undo_last_run(folder, self._progress_cb), self._after_undo)

    def _after_undo(self, result, err):
        self._clear_tree()
        if err:
            self.status_var.set(f"Couldn't undo: {err}")
            messagebox.showerror(APP_TITLE, str(err), parent=self.root)
            return
        msg = f"Restored {result['restored']} file(s)."
        if result["errors"]:
            msg += f" {len(result['errors'])} couldn't be restored."
            messagebox.showwarning(APP_TITLE, "These files couldn't be restored:\n\n"
                                   + "\n".join(result["errors"][:15]), parent=self.root)
        self._show_empty_state(f"Restored {result['restored']} file(s) to their original place.")
        self.status_var.set(msg)

    # ---------- auto mode ----------
    def toggle_auto(self):
        if self.auto_var.get():
            if not self._get_folder():
                self.auto_var.set(False)
                return
            self.status_var.set("Auto-organize is on.")
            self._auto_tick()
        else:
            if self.auto_job:
                self.root.after_cancel(self.auto_job)
                self.auto_job = None
            self.status_var.set("Auto-organize is off.")

    def _auto_tick(self):
        if not self.auto_var.get():
            return
        folder = self._get_folder(silent=True)
        if folder and not self.busy:
            mode, dup, rules = self.mode_var.get(), self.dup_var.get(), self.rules

            def job():
                plan = build_plan(folder, mode, rules, dup, min_age_s=AUTO_MIN_FILE_AGE_S)
                if not any(it.action == "move" for it in plan):
                    return None  # nothing to do this round
                return organize(folder, mode, plan, self._progress_cb)

            def done(result, err):
                if result is None and err is None:
                    return
                if isinstance(err, OSError):
                    self.status_var.set(f"Can't read the folder: {err}")
                    return
                self._after_organize(result, err, silent=True)

            # don't lock the buttons for background checks, so they don't flicker
            self._run_background(job, done, lock_ui=False)
        try:
            interval = max(5, int(self.interval_var.get()))
        except (tk.TclError, ValueError):
            interval = 10
        self.auto_job = self.root.after(interval * 1000, self._auto_tick)

    def _on_close(self):
        if self.busy and not messagebox.askyesno(
                APP_TITLE, "Files are still being moved. Quit anyway?", parent=self.root):
            return
        if self.save_job:
            self.root.after_cancel(self.save_job)
        self._save_settings(include_window=True)
        self.root.destroy()


def main():
    enable_high_dpi()
    root = tk.Tk()
    OrganizerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()