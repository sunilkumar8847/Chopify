"""Project administration (Phase 3): titles, rename, open folder, safe delete.

Deletion has two clearly separate modes:

* ``project``  - remove the project from Studio: every file in its folder
  EXCEPT the final clips (``clips/*.mp4``) and their ``.meta.json``. The clips
  stay where they are (``projects/<id>/clips``) with a short note explaining
  what happened.
* ``everything`` - also delete the final MP4s. Requires ``confirm_clips``
  equal to the exact number of final clips the user was shown.

Safety rules (both modes):
* The folder must be ``<projects root>/<12-hex id>``, a real directory - not a
  junction, symlink or other reparse point - and must resolve inside the root.
* Nothing is followed through a link: a symlink/junction inside the project
  is removed as a link only (its target is never entered or deleted), and a
  file is unlinked only when its resolved path is inside the project.
* Busy projects (queued/running/recording) and projects with uploads still
  waiting or in progress are refused.
* Upload history (in the config folder) is never touched.
"""
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import applog

ID = re.compile(r"^[0-9a-f]{12}$")
NOTE = "REMOVED_PROJECT.txt"
ACTIVE_UPLOADS = {"queued", "uploading", "quota_wait", "processing"}
MAX_TITLE = 120


class AdminError(ValueError):
    pass


# ----------------------------------------------------------------- titles

def _pretty(text):
    text = re.sub(r"[_\-.]+", " ", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:1].upper() + text[1:] if text else ""


def display_title(state):
    """A human-readable project name (never the internal id or a raw path)."""
    title = str(state.get("title") or "").strip()
    if title:
        return title
    meta = state.get("source_meta") or {}
    if meta.get("title"):
        return str(meta["title"]).strip()[:MAX_TITLE]
    source = str(state.get("source") or "")
    if re.match(r"^[a-z]+://", source, re.I):
        from urllib.parse import urlparse
        parsed = urlparse(source)
        host = (parsed.hostname or "link").removeprefix("www.")
        tail = Path(parsed.path).stem or parsed.query[:20]
        return f"{host} · {_pretty(tail) or 'video'}"[:MAX_TITLE]
    stem = Path(source).stem
    if stem.lower() in {"recording", "source", "video", "audio"}:
        stem = Path(source).parent.name + " " + stem
    return (_pretty(stem) or "Untitled project")[:MAX_TITLE]


def validate_title(title):
    if not isinstance(title, str):
        raise AdminError("The title must be text.")
    title = re.sub(r"\s+", " ", title).strip()
    if not 1 <= len(title) <= MAX_TITLE:
        raise AdminError(f"Use 1 to {MAX_TITLE} characters.")
    if any(ord(c) < 32 for c in title):
        raise AdminError("The title cannot contain control characters.")
    return title


# ----------------------------------------------------------------- safety

def is_reparse(path):
    """True for symlinks, junctions and any other Windows reparse point."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attrs = getattr(info, "st_file_attributes", 0)
    if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))


def project_folder(root, project_id):
    """The verified project folder (raises AdminError if anything is off)."""
    if not isinstance(project_id, str) or not ID.match(project_id):
        raise AdminError("Unknown project.")
    root = Path(root).resolve()
    folder = root / project_id
    if not folder.exists():
        raise AdminError("Project not found.")
    if is_reparse(folder):
        raise AdminError("This project folder is a link (junction or symlink); Chopify will "
                         "not delete through it. Remove it by hand if you are sure.")
    resolved = folder.resolve()
    if resolved.parent != root or not resolved.is_dir():
        raise AdminError("This project folder is not inside the projects folder.")
    return resolved


def _walk(folder):
    """(files, links, dirs) inside folder without following any link."""
    files, links, dirs = [], [], []
    stack = [folder]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            if is_reparse(path):
                links.append(path)
            elif entry.is_dir(follow_symlinks=False):
                dirs.append(path)
                stack.append(path)
            elif entry.is_file(follow_symlinks=False):
                files.append(path)
    return files, links, dirs


def _is_final(folder, file):
    rel = file.relative_to(folder)
    parts = rel.parts
    return (len(parts) == 2 and parts[0] == "clips" and not parts[1].startswith("_")
            and (file.suffix.lower() == ".mp4" or file.name.lower().endswith(".meta.json")))


def delete_plan(root, project_id, uploads=(), busy=False):
    """What each deletion mode would remove (nothing is deleted)."""
    folder = project_folder(root, project_id)
    files, links, _ = _walk(folder)
    final = [f for f in files if _is_final(folder, f) and f.suffix.lower() == ".mp4"]
    meta = [f for f in files if _is_final(folder, f) and f.suffix.lower() != ".mp4"]
    other = [f for f in files if not _is_final(folder, f)]
    pending = [u for u in uploads if u.get("project_id") == project_id
               and u.get("state") in ACTIVE_UPLOADS]
    blockers = []
    if busy:
        blockers.append("The project is queued, running or recording. Cancel it first.")
    if pending:
        blockers.append(f"{len(pending)} upload(s) for this project are still waiting or in "
                        "progress. Cancel them first.")

    def size(items):
        total = 0
        for item in items:
            try:
                total += item.stat().st_size
            except OSError:
                pass
        return total
    return {"project_id": project_id, "folder": str(folder), "blockers": blockers,
            "final_clips": [f.name for f in sorted(final)], "final_bytes": size(final),
            "other_files": len(other) + len(meta), "other_bytes": size(other) + size(meta),
            "links": [str(link.relative_to(folder)) for link in links],
            "uploaded": sum(1 for u in uploads if u.get("project_id") == project_id
                            and u.get("state") == "done")}


def delete_project(root, project_id, mode, uploads=(), busy=False, confirm=False,
                   confirm_clips=None, title=""):
    """Delete as planned. Returns a summary. Final clips survive mode 'project'."""
    if mode not in ("project", "everything"):
        raise AdminError("Choose what to delete.")
    if confirm is not True:
        raise AdminError("Deletion needs your confirmation.")
    plan = delete_plan(root, project_id, uploads, busy)
    if plan["blockers"]:
        raise AdminError(" ".join(plan["blockers"]))
    folder = Path(plan["folder"])
    if mode == "everything" and confirm_clips != len(plan["final_clips"]):
        raise AdminError(f"Type the number of final clips ({len(plan['final_clips'])}) to "
                         "confirm deleting them.")
    files, links, dirs = _walk(folder)
    removed, kept, freed, skipped = 0, 0, 0, []
    for link in links:
        try:
            # The link itself only; its target is never entered.
            try:
                os.rmdir(link)            # junction or directory symlink
            except OSError:
                os.unlink(link)           # file symlink
            removed += 1
        except OSError as exc:
            skipped.append(str(link.relative_to(folder)))
            applog.warning("project.delete", "Could not remove a link", exc=exc,
                           project_id=project_id)
    for file in files:
        if _is_final(folder, file) and mode == "project":
            kept += 1
            continue
        try:
            resolved = file.resolve()
            if folder not in resolved.parents:
                skipped.append(str(file.relative_to(folder)))
                continue
            size = file.stat().st_size
            os.unlink(file)
            removed += 1
            freed += size
        except OSError as exc:
            skipped.append(str(file.relative_to(folder)))
            applog.warning("project.delete", "Could not delete a file", exc=exc,
                           project_id=project_id)
    for directory in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()             # only empty folders
        except OSError:
            pass
    if mode == "project" and kept:
        (folder / NOTE).write_text(
            f"The Chopify project \"{title or project_id}\" was removed from Studio on "
            f"{time.strftime('%Y-%m-%d %H:%M')}.\nIts final clips were kept in the clips "
            "folder next to this note.\n", encoding="utf-8")
    else:
        try:
            folder.rmdir()
        except OSError:
            pass
    summary = {"project_id": project_id, "mode": mode, "files_removed": removed,
               "bytes_freed": freed, "final_clips_kept": kept, "skipped": skipped,
               "folder_kept": folder.exists()}
    applog.info("project.delete", f"Project deleted ({mode}): {removed} files removed, "
                f"{kept} final clips kept", project_id=project_id, mode=mode,
                files=removed, kept=kept, skipped=len(skipped) or None)
    return summary


def open_folder(root, project_id, opener=None):
    """Open the project folder in Explorer (this computer only)."""
    folder = project_folder(root, project_id)
    if opener is not None:
        opener(str(folder))
    elif sys.platform == "win32":
        os.startfile(str(folder))        # noqa: S606 - local folder, validated above
    else:
        subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", str(folder)])
    return {"opened": str(folder)}
