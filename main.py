#!/usr/bin/env python3
"""Sheldinner Practice — cross-platform osu! beatmap splitter.

A single-file app: parses .osu files, splits them by difficulty (rosu-pp-py
strain peaks) or by equal length, packages the results into .osz archives
and imports them into osu! (stable or lazer) via the game's native import
path. The optional "current running beatmap" preview is provided by tosu
(https://github.com/tosuapp/tosu), a cross-platform memory reader for both
osu!stable and osu!lazer; a file dialog is the always-available fallback.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import rosu_pp_py as rosu

try:
    import requests
    _HAS_REQUESTS = True
except Exception:
    requests = None
    _HAS_REQUESTS = False


TOSU_BASE = "http://127.0.0.1:24050"
TOSU_TIMEOUT = 1.0


# --------------------------------------------------------------------------- #
# Resource path resolution (frozen vs. script)
# --------------------------------------------------------------------------- #

if getattr(sys, "frozen", False):
    RESOURCES = Path(sys._MEIPASS) / "resources"
else:
    RESOURCES = Path(__file__).resolve().parent / "resources"


# --------------------------------------------------------------------------- #
# .osu parsing
# --------------------------------------------------------------------------- #

@dataclass
class OsuMap:
    """Parsed lightweight view of a .osu file."""
    path: str | None
    lines: list[str]
    header_end: int            # index of the line *after* the [HitObjects] tag
    hitobjects: list[str]      # lines after [HitObjects]
    audio_filename: str | None
    bg_filename: str | None
    mode: int
    artist: str
    title: str
    creator: str
    version: str


def _find_section_end(lines: list[str], tag: str) -> int | None:
    for i, line in enumerate(lines):
        if line.strip() == tag:
            return i
    return None


def _kv(lines: list[str], section: str, key: str) -> str | None:
    in_section = False
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_section = s == f"[{section}]"
            continue
        if in_section and s.startswith(f"{key}:"):
            return s.split(":", 1)[1].strip()
    return None


def _parse_bg(lines: list[str]) -> str | None:
    in_events = False
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_events = s == "[Events]"
            continue
        if not in_events:
            continue
        # Background line: 0,0,"file.jpg",x,y  or  0,0,file.jpg,x,y
        if s.startswith("0,0,"):
            rest = s[4:]
            if rest.startswith('"'):
                end = rest.find('"', 1)
                if end != -1:
                    return rest[1:end]
            return rest.split(",", 1)[0].strip()
    return None


def parse_osu(path: str | None = None, content: str | None = None,
              encoding: str = "utf-8-sig") -> OsuMap:
    if content is None:
        if path is None:
            raise ValueError("parse_osu needs either path or content")
        with open(path, "r", encoding=encoding, errors="replace") as f:
            content = f.read()
    lines = content.splitlines(keepends=True)

    ho_idx = _find_section_end(lines, "[HitObjects]")
    if ho_idx is None:
        header_end = len(lines)
        hitobjects: list[str] = []
    else:
        header_end = ho_idx + 1
        hitobjects = lines[ho_idx + 1:]

    mode_raw = _kv(lines, "General", "Mode")
    mode = int(mode_raw) if mode_raw and mode_raw.isdigit() else 0

    return OsuMap(
        path=path,
        lines=lines,
        header_end=header_end,
        hitobjects=hitobjects,
        audio_filename=_kv(lines, "General", "AudioFilename"),
        bg_filename=_parse_bg(lines),
        mode=mode,
        artist=_kv(lines, "Metadata", "Artist") or "",
        title=_kv(lines, "Metadata", "Title") or "",
        creator=_kv(lines, "Metadata", "Creator") or "",
        version=_kv(lines, "Metadata", "Version") or "",
    )


def _hitobject_time(line: str) -> int | None:
    parts = line.split(",")
    if len(parts) < 3:
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


def _build_split_osu(base: OsuMap, version: str, hitobjects: list[str],
                     strip_beatmap_id: bool = False, strip_set_id: bool = False,
                     set_id_value: int = 0,
                     title_suffix: str = "") -> str:
    """Rebuild a .osu text from `base` with a new Version and hitobjects.

    - `strip_beatmap_id` / `strip_set_id`: blank the online IDs so the import
      is treated as a fresh local difficulty/set rather than an update of the
      original (prevents the client from *replacing* the source beatmap).
    - `set_id_value`: the value written for `BeatmapSetID` when stripped
      (0 for lazer; -1 for a stable duplicate, per the osu! wiki BSS guide).
    - `title_suffix`: text appended to Title(/TitleUnicode) so the produced
      set is visibly distinct (e.g. " (Split Difficulty)").
    """
    out: list[str] = []
    for line in base.lines[:base.header_end]:
        s = line.strip()
        if s.startswith("Version:"):
            out.append(f"Version:{version}\n")
        elif strip_beatmap_id and s.startswith("BeatmapID:"):
            out.append("BeatmapID:0\n")
        elif strip_set_id and s.startswith("BeatmapSetID:"):
            out.append(f"BeatmapSetID:{set_id_value}\n")
        elif title_suffix and (s.startswith("Title:")
                               or s.startswith("TitleUnicode:")):
            key, val = line.split(":", 1)
            out.append(f"{key}:{val.strip()} {title_suffix}\n")
        else:
            out.append(line)
    out.extend(hitobjects)
    return "".join(out)


def _safe_name(s: str, fallback: str = "map") -> str:
    s = re.sub(r'[\\/:*?"<>|]', "_", s).strip()
    return s or fallback


# --------------------------------------------------------------------------- #
# Map sources (local file vs. tosu current beatmap)
# --------------------------------------------------------------------------- #

@dataclass
class MapSource:
    """Provides the .osu file path and asset bytes for packaging."""
    osu_path: str
    parsed: OsuMap
    kind: str  # "local" | "tosu"

    def asset_bytes(self, name: str | None) -> bytes | None:
        raise NotImplementedError


@dataclass
class LocalSource(MapSource):
    folder: str = ""

    def __post_init__(self):
        if not self.folder and self.osu_path:
            self.folder = os.path.dirname(self.osu_path)

    def asset_bytes(self, name: str | None) -> bytes | None:
        if not name:
            return None
        candidate = os.path.join(self.folder, name)
        if os.path.isfile(candidate):
            try:
                with open(candidate, "rb") as f:
                    return f.read()
            except OSError:
                return None
        return None

    def iter_assets(self, exclude_osu: bool = True) -> Iterable[tuple[str, bytes]]:
        """Yield (basename, bytes) for all sibling files (audio, bg, ...)."""
        if not self.folder or not os.path.isdir(self.folder):
            return
        for entry in os.listdir(self.folder):
            full = os.path.join(self.folder, entry)
            if not os.path.isfile(full):
                continue
            if exclude_osu and entry.lower().endswith(".osu"):
                continue
            try:
                with open(full, "rb") as f:
                    yield entry, f.read()
            except OSError:
                continue


@dataclass
class TosuSource(MapSource):
    """Source backed by tosu's HTTP file endpoints."""
    audio_bytes: bytes | None = None
    bg_bytes: bytes | None = None
    tosu_state: dict = field(default_factory=dict)

    def asset_bytes(self, name: str | None) -> bytes | None:
        if not name:
            return None
        if self.parsed.audio_filename and name == self.parsed.audio_filename:
            return self.audio_bytes
        if self.parsed.bg_filename and name == self.parsed.bg_filename:
            return self.bg_bytes
        # Try generic endpoint for other files (storyboard/video).
        return tosu_fetch_bytes(f"/files/beatmap/{name}")


# --------------------------------------------------------------------------- #
# tosu client (optional)
# --------------------------------------------------------------------------- #

def tosu_available() -> bool:
    if not _HAS_REQUESTS:
        return False
    try:
        r = requests.get(f"{TOSU_BASE}/json/v2", timeout=TOSU_TIMEOUT)
        return r.status_code == 200
    except Exception:
        return False


def tosu_status() -> dict | None:
    if not _HAS_REQUESTS:
        return None
    try:
        r = requests.get(f"{TOSU_BASE}/json/v2", timeout=TOSU_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def tosu_fetch_bytes(endpoint: str) -> bytes | None:
    if not _HAS_REQUESTS:
        return None
    try:
        r = requests.get(f"{TOSU_BASE}{endpoint}", timeout=3.0)
        if r.status_code == 200:
            return r.content
    except Exception:
        pass
    return None


def tosu_current_source() -> TosuSource | None:
    """Build a TosuSource from tosu's current beatmap, or None if unavailable."""
    st = tosu_status()
    if not st:
        return None
    beatmap = st.get("beatmap") or {}
    if not beatmap.get("title") and not st.get("files", {}).get("beatmap"):
        return None
    osu_bytes = tosu_fetch_bytes("/files/beatmap/file")
    if not osu_bytes:
        return None
    tmp = tempfile.NamedTemporaryFile(
        suffix=".osu", delete=False, mode="wb", prefix="sheldinner_tosu_")
    tmp.write(osu_bytes)
    tmp.close()
    parsed = parse_osu(path=tmp.name, content=osu_bytes.decode(
        "utf-8-sig", errors="replace"))
    audio = tosu_fetch_bytes("/files/beatmap/audio")
    bg = tosu_fetch_bytes("/files/beatmap/background")
    return TosuSource(
        osu_path=tmp.name,
        parsed=parsed,
        kind="tosu",
        audio_bytes=audio if audio else None,
        bg_bytes=bg if bg else None,
        tosu_state=st,
    )


def tosu_current_meta() -> dict | None:
    """Return a compact dict for UI display, or None."""
    st = tosu_status()
    if not st:
        return None
    b = st.get("beatmap") or {}
    if not b:
        return None
    files = st.get("files") or {}
    state = (st.get("state") or {}).get("name") or ""
    return {
        "artist": b.get("artist", ""),
        "title": b.get("title", ""),
        "version": b.get("version", ""),
        "mapper": b.get("mapper", ""),
        "state": state,
        "has_osu": bool(files.get("beatmap")),
    }


def tosu_songs_root() -> str | None:
    """The stable Songs root folder reported by tosu, if it exists on disk."""
    st = tosu_status()
    if not st:
        return None
    songs = ((st.get("folders") or {}).get("songs") or "").strip()
    return songs if songs and os.path.isdir(songs) else None


# --------------------------------------------------------------------------- #
# Splitting
# --------------------------------------------------------------------------- #

@dataclass
class Section:
    start_ms: int
    end_ms: int
    peak_ms: int = 0
    peak_strain: float = 0.0


@dataclass
class SplitResult:
    sections: list[Section]
    strains: list[float] = field(default_factory=list)
    section_length: float = 400.0


def split_by_difficulty(source: MapSource, mods: int = 0,
                        threshold_pct: float = 0.4,
                        max_sections: int = 12) -> SplitResult:
    """Detect difficulty spikes via rosu-pp strain peaks and return sections.

    Finds local maxima in the aim curve, expands each to a contiguous region
    above a *relative* fraction of that peak, then merges overlaps. Always
    returns at least one section when the map has any strain at all.
    """
    parsed = source.parsed
    if parsed.mode != 0:
        # difficulty-based split only meaningful for osu! standard aim skill
        raise ValueError(
            "Difficulty-based split is only supported for osu!standard maps.")

    beatmap = rosu.Beatmap(path=parsed.path) if parsed.path else \
        rosu.Beatmap(content="".join(parsed.lines))
    strains = rosu.Difficulty(mods=mods).strains(beatmap)
    aim = strains.aim
    if not aim:
        # fall back to any available skill curve
        for fallback in (strains.speed, strains.flashlight):
            if fallback:
                aim = fallback
                break
    if not aim:
        return SplitResult(sections=[], strains=[], section_length=400.0)

    section_len = float(strains.section_length) or 400.0
    n = len(aim)
    if n == 0:
        return SplitResult(sections=[], strains=list(aim),
                           section_length=section_len)
    if max(aim) <= 0:
        return SplitResult(sections=[], strains=list(aim),
                           section_length=section_len)

    # Local maxima (skip duplicates on plateaus to one representative).
    peaks: list[tuple[int, float]] = []
    for i in range(n):
        left = aim[i - 1] if i > 0 else float("-inf")
        right = aim[i + 1] if i < n - 1 else float("-inf")
        if aim[i] >= left and aim[i] >= right and aim[i] > 0:
            if i > 0 and aim[i] == aim[i - 1]:
                continue
            peaks.append((i, aim[i]))
    peaks.sort(key=lambda x: x[1], reverse=True)

    sections: list[Section] = []
    used: set[int] = set()
    for pidx, pval in peaks:
        if pidx in used:
            continue
        thresh = pval * threshold_pct
        s = pidx
        while s > 0 and aim[s - 1] >= thresh:
            s -= 1
        e = pidx
        while e < n - 1 and aim[e + 1] >= thresh:
            e += 1
        sections.append(Section(
            start_ms=int(s * section_len),
            end_ms=int((e + 1) * section_len),
            peak_ms=int(pidx * section_len),
            peak_strain=float(pval),
        ))
        for k in range(s, e + 1):
            used.add(k)

    sections.sort(key=lambda s: s.start_ms)

    # Merge overlapping / adjacent sections.
    merged: list[Section] = []
    for s in sections:
        if merged and s.start_ms <= merged[-1].end_ms:
            merged[-1].end_ms = max(merged[-1].end_ms, s.end_ms)
            if s.peak_strain > merged[-1].peak_strain:
                merged[-1].peak_strain = s.peak_strain
                merged[-1].peak_ms = s.peak_ms
        else:
            merged.append(s)

    if not merged:
        merged = [Section(0, int(n * section_len))]

    # Cap to the most prominent sections if there are too many.
    if len(merged) > max_sections:
        merged.sort(key=lambda s: s.peak_strain, reverse=True)
        merged = merged[:max_sections]
        merged.sort(key=lambda s: s.start_ms)

    return SplitResult(sections=merged, strains=list(aim),
                       section_length=section_len)


def split_by_length(source: MapSource, parts: int) -> SplitResult:
    """Split the map into `parts` equal-length time sections."""
    parsed = source.parsed
    times: list[int] = []
    for line in parsed.hitobjects:
        t = _hitobject_time(line)
        if t is not None:
            times.append(t)
    if not times:
        return SplitResult(sections=[])
    first_ms = min(times)
    last_ms = max(times)
    span = last_ms - first_ms
    if parts < 1:
        parts = 1
    section_len = span / parts if parts else 400.0

    sections: list[Section] = []
    for rank in range(1, parts + 1):
        start_ms = int(first_ms + (rank - 1) * span / parts)
        end_ms = int(first_ms + rank * span / parts) if rank < parts \
            else last_ms
        sections.append(Section(start_ms=start_ms, end_ms=end_ms))
    return SplitResult(sections=sections, strains=[], section_length=section_len)


# --------------------------------------------------------------------------- #
# Packaging + importing
# --------------------------------------------------------------------------- #

def _section_hitobjects(base: OsuMap, section: Section) -> list[str]:
    out = []
    for line in base.hitobjects:
        t = _hitobject_time(line)
        if t is not None and section.start_ms <= t <= section.end_ms:
            out.append(line)
    return out


def _section_osu(base: OsuMap, section: Section, rank: int,
                 base_name: str, label: str, *,
                 strip_beatmap_id: bool = False, strip_set_id: bool = False,
                 set_id_value: int = 0,
                 title_suffix: str = "") -> tuple[str, str]:
    version = (f"{base_name} ({label}) Section {rank} "
               f"({section.start_ms}ms-{section.end_ms}ms)")
    inside_name = f"{base_name} ({label}) Section {rank}.osu"
    text = _build_split_osu(base, version, _section_hitobjects(base, section),
                            strip_beatmap_id=strip_beatmap_id,
                            strip_set_id=strip_set_id,
                            set_id_value=set_id_value,
                            title_suffix=title_suffix)
    return inside_name, text


def package_osz(source: MapSource, sections: list[Section],
                out_dir: str, label: str) -> str:
    """Package all sections as difficulties into a single .osz archive.

    Online IDs are stripped and the Title is suffixed with `label` so lazer
    imports it as a *new* local set (instead of replacing the original by ID).
    Returns the path to the written .osz file.
    """
    parsed = source.parsed
    base_name = _safe_name(parsed.version or Path(parsed.osu_path or "map").stem
                           or "map")
    set_name = _safe_name(
        f"{parsed.artist} - {parsed.title} ({parsed.creator}) {label}"
        or "sheldinner-split")

    os.makedirs(out_dir, exist_ok=True)
    osz_path = os.path.join(out_dir, f"{set_name}.osz")

    with zipfile.ZipFile(osz_path, "w", zipfile.ZIP_DEFLATED) as z:
        for rank, section in enumerate(sections, start=1):
            inside, text = _section_osu(
                parsed, section, rank, base_name, label,
                strip_beatmap_id=True, strip_set_id=True,
                title_suffix=f"({label})")
            z.writestr(inside, text)

        # Assets: include everything the beatmap references/contains.
        if isinstance(source, LocalSource):
            for name, data in source.iter_assets(exclude_osu=True):
                z.writestr(name, data)
        else:
            # tosu: include audio + background (essential for playability).
            if source.audio_bytes and parsed.audio_filename:
                z.writestr(parsed.audio_filename, source.audio_bytes)
            if source.bg_bytes and parsed.bg_filename:
                z.writestr(parsed.bg_filename, source.bg_bytes)

    return osz_path


def write_split_as_new_set(source: MapSource, sections: list[Section],
                            songs_root: str, label: str) -> dict:
    """Duplicate the beatmap as a *new* stable set containing the split diffs.

    Following the osu! wiki BSS guide to avoid colliding with the original:

      * The new folder lives directly under the Songs root and its name does
        NOT begin with digits (stable keys sets by folder name; a leading
        numeric prefix like "{BeatmapSetID} ..." would be treated as the
        original online set and replaced, not duplicated).
      * `BeatmapID: 0` and `BeatmapSetID: -1` ("unsubmitted / local set") in
        every split .osu.
      * All non-.osu assets (audio, background, storyboard, hitsounds, video)
        are copied into the new folder so the duplicate is fully playable.

    Returns {"folder": new_folder, "paths": [...]}.
    """
    parsed = source.parsed
    base_name = _safe_name(parsed.version or Path(parsed.osu_path or "map").stem
                           or "map")
    # Non-numeric prefix guarantees stable treats this as a fresh local set.
    set_folder_name = _safe_name(
        f"Sheldinner {parsed.artist} - {parsed.title} ({label})")
    if set_folder_name and set_folder_name[0].isdigit():
        set_folder_name = "_" + set_folder_name

    new_folder = os.path.join(songs_root, set_folder_name)
    os.makedirs(new_folder, exist_ok=True)

    # Copy assets so the duplicate plays standalone.
    if isinstance(source, LocalSource):
        src_folder = source.folder
        if src_folder and os.path.isdir(src_folder):
            for entry in os.listdir(src_folder):
                full = os.path.join(src_folder, entry)
                if not os.path.isfile(full):
                    continue
                if entry.lower().endswith(".osu"):
                    continue  # only the new split diffs go in
                try:
                    shutil.copyfile(full, os.path.join(new_folder, entry))
                except OSError:
                    continue
    else:  # TosuSource: write the assets we fetched (audio + background).
        if source.audio_bytes and parsed.audio_filename:
            with open(os.path.join(new_folder, parsed.audio_filename),
                      "wb") as f:
                f.write(source.audio_bytes)
        if source.bg_bytes and parsed.bg_filename:
            with open(os.path.join(new_folder, parsed.bg_filename), "wb") as f:
                f.write(source.bg_bytes)

    # Split .osu files: blank BeatmapID, mark as unsubmitted local set (-1).
    written: list[str] = []
    for rank, section in enumerate(sections, start=1):
        inside, text = _section_osu(
            parsed, section, rank, base_name, label,
            strip_beatmap_id=True, strip_set_id=True, set_id_value=-1)
        out_path = os.path.join(new_folder, inside)
        with open(out_path, "w", encoding="utf-8-sig") as f:
            f.write(text)
        written.append(out_path)
    return {"folder": new_folder, "paths": written}


# --------------------------------------------------------------------------- #
# osu! binary detection + import
# --------------------------------------------------------------------------- #

def _is_executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def find_osu_binary() -> str | None:
    """Locate the osu! (lazer or stable) binary for the current platform."""
    system = platform.system()

    if system == "Windows":
        candidates = []
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates += [
                os.path.join(local, "osulazer", "osu!.exe"),
                os.path.join(local, "osulazer", "current", "osu!.exe"),
            ]
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        candidates.append(os.path.join(program_files, "osulazer", "osu!.exe"))
        # osu!stable via registry
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\osu!")
            install, _ = winreg.QueryValueEx(key, "InstallLocation")
            winreg.CloseKey(key)
            candidates.append(os.path.join(install, "osu!.exe"))
        except Exception:
            pass
        for c in candidates:
            if os.path.isfile(c):
                return c

    elif system == "Linux":
        home = os.path.expanduser("~")
        glob_dirs = [
            os.path.join(home, "Applications"),
            os.path.join(home, ".local", "bin"),
            "/opt",
            "/usr/local/bin",
            "/usr/bin",
        ]
        for d in glob_dirs:
            if not os.path.isdir(d):
                continue
            for entry in os.listdir(d):
                low = entry.lower()
                if "osu" in low and (low.endswith(".appimage")
                                     or low.startswith("osu")):
                    full = os.path.join(d, entry)
                    if _is_executable(full):
                        return full
        for name in ("osu!", "osu-lazer", "osu"):
            p = shutil.which(name)
            if p:
                return p

    elif system == "Darwin":
        app = "/Applications/osu!.app"
        if os.path.isdir(app):
            return app

    return None


def import_osz(osz_path: str, binary: str | None = None) -> bool:
    """Import a .osz into a running osu! instance by launching its binary.

    Lazer (and stable) forward the file argument to the already-running
    process and import it natively. Returns True if a launch was attempted.
    """
    if not os.path.isfile(osz_path):
        return False
    binary = binary or find_osu_binary()

    try:
        if binary and platform.system() == "Darwin":
            # .app bundle: use `open` which forwards to running instance.
            subprocess.Popen(["open", "-a", binary, osz_path],
                             close_fds=True)
            return True
        if binary:
            subprocess.Popen([binary, osz_path], close_fds=True)
            return True
    except OSError:
        return False

    # No binary found: reveal the file so the user can drag-drop manually.
    reveal_in_file_manager(osz_path)
    return False


def reveal_in_file_manager(path: str) -> None:
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(["explorer", f"/select,{path}"], close_fds=True)
        elif system == "Darwin":
            subprocess.Popen(["open", "-R", path], close_fds=True)
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) or "."],
                             close_fds=True)
    except OSError:
        pass


def default_output_dir() -> str:
    base = os.path.join(tempfile.gettempdir(), "sheldinner-output")
    os.makedirs(base, exist_ok=True)
    return base


# =========================================================================== #
# GUI
# =========================================================================== #
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QProgressBar, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

QSS = """
* { color: #e6e6e6; font-family: 'Segoe UI', 'Noto Sans', sans-serif; }
#root { background: #161616; }
QFrame#card { background: #1f1f1f; border-radius: 10px; }
QLabel#titlebar { color: #b0b0b0; font-size: 12px; }
QLabel#heading { color: #ffffff; font-size: 14px; font-weight: 600; }
QLabel#muted { color: #8a8a8a; font-size: 11px; }
QLabel#maptitle { color: #ffffff; font-size: 13px; font-weight: 600; }
QLabel#mapsub { color: #9a9a9a; font-size: 11px; }

QPushButton {
    background: #2a2a2a; border: 1px solid #333; border-radius: 8px;
    padding: 8px 12px; font-size: 12px;
}
QPushButton:hover { background: #333; border-color: #444; }
QPushButton:pressed { background: #222; }
QPushButton:disabled { color: #666; background: #202020; border-color: #262626; }
QPushButton#iconbtn { background: transparent; border: none; }

QLineEdit, QSpinBox {
    background: #262626; border: 1px solid #333; border-radius: 6px;
    padding: 6px; font-size: 12px;
}
QLineEdit:focus, QSpinBox:focus { border-color: #ff66aa; }

QProgressBar {
    background: #262626; border: none; border-radius: 6px; height: 8px;
    text-align: center;
}
QProgressBar::chunk { background: #ff66aa; border-radius: 6px; }

QCheckBox { color: #cfcfcf; font-size: 12px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px;
    background: #262626; border: 1px solid #333; }
QCheckBox::indicator:checked { background: #ff66aa; border-color: #ff66aa; }
"""


class TitleBar(QWidget):
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            win = self.window().windowHandle()
            if win:
                win.startSystemMove()
            event.accept()


class App(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle("Sheldinner Practice")
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)

        screen = QApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.w = max(420, int(geo.width() * 0.22))
        self.h = max(560, int(geo.height() * 0.52))
        self.setFixedSize(self.w, self.h)

        self._source: MapSource | None = None
        self._tosu_ok = tosu_available()
        self._stable_dir_manual = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_titlebar())
        root.addWidget(self._build_map_card())
        root.addWidget(self._build_actions())
        root.addStretch()

        # Poll tosu for the in-game map (cheap; re-probes availability).
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_in_game)
        self._timer.start(2000)
        self._refresh_in_game()

    # --- widgets -----------------------------------------------------------
    def _build_titlebar(self) -> QWidget:
        bar = TitleBar()
        bar.setFixedHeight(32)
        bar.setStyleSheet("background: #111;")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 0, 0, 0)
        title = QLabel("Sheldinner Practice")
        title.setObjectName("titlebar")
        title.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay.addWidget(title)
        lay.addStretch()
        lay.addWidget(self._icon_btn("—", self.showMinimized, "#3a3a3a"))
        lay.addWidget(self._icon_btn("✕", self.close, "#c42b1c"))
        return bar

    def _icon_btn(self, text, slot, hover) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("iconbtn")
        b.setFixedSize(32, 32)
        b.setStyleSheet(
            f"QPushButton {{ color:#ccc; font-size:14px; }}"
            f"QPushButton:hover {{ background:{hover}; }}")
        b.clicked.connect(slot)
        return b

    def _build_map_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("card")
        card.setContentsMargins(0, 0, 0, 0)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.bg_label = QLabel()
        self.bg_label.setFixedHeight(self.w // 3)
        self.bg_label.setAlignment(Qt.AlignCenter)
        self.bg_label.setStyleSheet("background: #111;")
        self.bg_label.setText("Select a beatmap to begin")
        self.bg_label.setObjectName("muted")
        lay.addWidget(self.bg_label)

        info = QVBoxLayout()
        info.setContentsMargins(14, 10, 14, 12)
        info.setSpacing(2)
        self.map_title = QLabel("No map selected")
        self.map_title.setObjectName("maptitle")
        self.map_title.setWordWrap(True)
        self.map_sub = QLabel("Use the buttons below to choose a .osu file, "
                              "or run tosu to detect the current map.")
        self.map_sub.setObjectName("mapsub")
        self.map_sub.setWordWrap(True)
        info.addWidget(self.map_title)
        info.addWidget(self.map_sub)
        self.ingame_label = QLabel("In game: —")
        self.ingame_label.setObjectName("muted")
        self.ingame_label.setWordWrap(True)
        info.addSpacing(4)
        info.addWidget(self.ingame_label)
        lay.addLayout(info)
        return card

    def _build_actions(self) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(14, 8, 14, 14)
        lay.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.browse_btn = QPushButton("Browse .osu…")
        self.browse_btn.clicked.connect(self._browse)
        self.use_current_btn = QPushButton("Use current map")
        self.use_current_btn.setEnabled(self._tosu_ok)
        self.use_current_btn.clicked.connect(self._use_current)
        row.addWidget(self.browse_btn)
        row.addWidget(self.use_current_btn)
        lay.addLayout(row)

        heading = QLabel("Split")
        heading.setObjectName("heading")
        lay.addWidget(heading)

        self.diff_btn = QPushButton("Split by Difficulty")
        self.diff_btn.clicked.connect(self._split_difficulty)
        lay.addWidget(self.diff_btn)

        len_row = QHBoxLayout()
        self.len_btn = QPushButton("Split by Length")
        self.len_btn.clicked.connect(self._split_length)
        self.parts_spin = QSpinBox()
        self.parts_spin.setRange(2, 99)
        self.parts_spin.setValue(4)
        len_row.addWidget(self.len_btn, 1)
        len_row.addWidget(QLabel("parts:"))
        len_row.addWidget(self.parts_spin, 0)
        lay.addLayout(len_row)

        out_row = QHBoxLayout()
        self.out_edit = QLineEdit(default_output_dir())
        out_btn = QPushButton("…")
        out_btn.setFixedWidth(34)
        out_btn.clicked.connect(self._pick_output)
        out_row.addWidget(QLabel("Output:"))
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(out_btn)
        lay.addLayout(out_row)

        self.lazer_check = QCheckBox("Lazer import")
        self.lazer_check.setChecked(True)
        self.stable_check = QCheckBox("Stable import")
        self.stable_check.setChecked(False)
        lay.addWidget(self.lazer_check)
        lay.addWidget(self.stable_check)

        stable_row = QHBoxLayout()
        self.stable_dir_edit = QLineEdit("")
        self.stable_dir_edit.setPlaceholderText("stable Songs folder…")
        sd_btn = QPushButton("…")
        sd_btn.setFixedWidth(34)
        sd_btn.clicked.connect(self._pick_stable_dir)
        stable_row.addWidget(QLabel("Songs folder:"))
        stable_row.addWidget(self.stable_dir_edit, 1)
        stable_row.addWidget(sd_btn)
        lay.addLayout(stable_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setVisible(False)
        lay.addWidget(self.progress)

        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        return wrap

    # --- behaviour ---------------------------------------------------------
    def _set_status(self, text: str):
        self.status.setText(text)

    def _render_card(self, title: str, sub: str, bg_bytes: bytes | None):
        self.map_title.setText(title)
        self.map_sub.setText(sub)
        pm = QPixmap()
        if bg_bytes:
            pm.loadFromData(bg_bytes)
        if pm.isNull():
            pm = QPixmap(str(RESOURCES / "plsopenosu.png"))
        if not pm.isNull():
            self.bg_label.setPixmap(self._fit(pm))
            self.bg_label.setText("")
        else:
            self.bg_label.setPixmap(QPixmap())
            self.bg_label.setText("No background")

    def _source_name(self, source: MapSource) -> str:
        p = source.parsed
        if p.title:
            return f"{p.artist} - {p.title} [{p.version}]"
        return os.path.basename(source.osu_path)

    def _source_bg(self, source: MapSource) -> bytes | None:
        if source.kind == "local" and source.parsed.bg_filename:
            return source.asset_bytes(source.parsed.bg_filename)
        return getattr(source, "bg_bytes", None)

    def _set_source(self, source: MapSource | None):
        self._source = source
        if source is None:
            self._render_card(
                "No map selected",
                "Browse a .osu file or use the current map.", None)
        else:
            p = source.parsed
            self._render_card(
                self._source_name(source),
                f"Mapper: {p.creator}  ·  source: {source.kind}",
                self._source_bg(source))
        self._autofill_stable_dir()

    def _fit(self, pm: QPixmap) -> QPixmap:
        scaled = pm.scaledToWidth(self.w, Qt.SmoothTransformation)
        max_h = self.w // 3
        if scaled.height() > max_h:
            y = (scaled.height() - max_h) // 2
            return scaled.copy(0, y, scaled.width(), max_h)
        return scaled

    def _refresh_in_game(self):
        # Re-probe tosu each tick: it may start after the app, or the initial
        # check may have failed (slow startup / network hiccup on Windows).
        if not tosu_available():
            self._tosu_ok = False
            self.ingame_label.setText("In game: — (tosu not running)")
            self.use_current_btn.setEnabled(False)
            return
        if not self._tosu_ok:
            self._tosu_ok = True  # recovered
        meta = tosu_current_meta()
        if not meta:
            self.use_current_btn.setEnabled(False)
            self.ingame_label.setText("In game: —")
            return
        self.use_current_btn.setEnabled(bool(meta.get("has_osu")))
        if meta.get("title"):
            self.ingame_label.setText(
                f"In game: {meta['artist']} - {meta['title']} "
                f"[{meta['version']}]  ({meta['state']})")
        else:
            self.ingame_label.setText("In game: —")
        self._autofill_stable_dir()

    def _autofill_stable_dir(self):
        if self._stable_dir_manual:
            return
        # Prefer tosu's reported Songs root; fall back to the active source's
        # "Songs" ancestor when browsing a local .osu from inside Songs.
        cand = tosu_songs_root()
        if not cand and isinstance(self._source, LocalSource):
            folder = self._source.folder
            if folder:
                parts = Path(folder).resolve().parts
                if "Songs" in parts:
                    idx = parts.index("Songs")
                    cand = str(Path(*parts[: idx + 1]))
        if cand:
            self.stable_dir_edit.setText(cand)

    def _pick_stable_dir(self):
        d = QFileDialog.getExistingDirectory(
            self, "Choose stable Songs folder",
            self.stable_dir_edit.text() or "")
        if d:
            self._stable_dir_manual = True
            self.stable_dir_edit.setText(d)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a .osu beatmap", "", "osu! beatmap (*.osu)")
        if not path:
            return
        try:
            parsed = parse_osu(path=path)
        except Exception as e:
            QMessageBox.critical(self, "Parse error", f"Could not read map:\n{e}")
            return
        self._set_source(LocalSource(osu_path=path, parsed=parsed, kind="local"))

    def _use_current(self):
        src = tosu_current_source()
        if src is None:
            QMessageBox.information(
                self, "No current map",
                "tosu is running but no beatmap is currently selected.")
            return
        self._set_source(src)

    def _pick_output(self):
        d = QFileDialog.getExistingDirectory(
            self, "Choose output folder", self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def _require_source(self) -> MapSource | None:
        if self._source is None:
            QMessageBox.information(
                self, "No map selected",
                "Pick a .osu file or use the current map first.")
            return None
        return self._source

    def _finalize(self, sections: list[Section], label: str):
        if not sections:
            self._set_status("No sections produced.")
            return
        out_dir = self.out_edit.text() or default_output_dir()
        source = self._source
        do_lazer = self.lazer_check.isChecked()
        do_stable = self.stable_check.isChecked()

        messages: list[str] = [f"Split into {len(sections)} section(s)."]

        # No import target: just save a .osz so the user has the file.
        if not do_lazer and not do_stable:
            try:
                osz = package_osz(source, sections, out_dir, label)
            except Exception as e:
                QMessageBox.critical(self, "Packaging failed", str(e))
                return
            reveal_in_file_manager(osz)
            self._set_status(f"Saved {osz}")
            QMessageBox.information(self, "Done", "\n".join(messages) +
                                    f"\nSaved to:\n{osz}")
            return

        if do_lazer:
            try:
                osz = package_osz(source, sections, out_dir, label)
                ok = import_osz(osz)
            except Exception as e:
                QMessageBox.critical(self, "Lazer import failed", str(e))
                return
            if ok:
                messages.append(f"Lazer: imported {osz}")
                self._set_status(f"Lazer import: {osz}")
            else:
                reveal_in_file_manager(osz)
                messages.append(f"Lazer: saved {osz} (no binary found)")
                self._set_status(f"Lazer: saved {osz} (no binary found)")

        if do_stable:
            songs_root = self.stable_dir_edit.text().strip()
            if not songs_root or not os.path.isdir(songs_root):
                QMessageBox.critical(
                    self, "Stable import needs a Songs folder",
                    "Pick a valid stable Songs folder first (the "
                    "'Songs folder' field — the Songs root, not a single "
                    "beatmap folder).")
                return
            try:
                result = write_split_as_new_set(
                    source, sections, songs_root, label)
            except Exception as e:
                QMessageBox.critical(self, "Stable import failed", str(e))
                return
            folder = result["folder"]
            paths = result["paths"]
            messages.append(
                f"Stable: duplicated as a new set ({len(paths)} diff(s)) in\n"
                f"{folder}\nRefresh song select (F5) to see it.")
            self._set_status(
                f"Stable import: new set '{os.path.basename(folder)}' "
                f"({len(paths)} diffs)")

        QMessageBox.information(self, "Done", "\n\n".join(messages))

    def _split_difficulty(self):
        src = self._require_source()
        if src is None:
            return
        try:
            res = split_by_difficulty(src)
        except ValueError as e:
            QMessageBox.information(self, "Unsupported", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Strain error", str(e))
            return
        self._finalize(res.sections, "Difficulty Split")

    def _split_length(self):
        src = self._require_source()
        if src is None:
            return
        parts = self.parts_spin.value()
        res = split_by_length(src, parts)
        self._finalize(res.sections, f"{parts}-Part Split")


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    app.setWindowIcon(QIcon(str(RESOURCES / "icon.png")))
    win = App()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
