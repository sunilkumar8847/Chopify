"""Storage management: keep final clips, clean up everything else safely.

Policy
------
* Final rendered clips (``clips/*.mp4``) are never deleted automatically -
  not after upload, not by age, not under disk pressure. Only
  ``delete_clips(..., confirm=True)`` removes them, for the exact files the
  user confirmed.
* Supporting files (source media, downloads, previews, transcripts, caption
  and AI caches, intermediate files) are deleted only when ``plan()`` finds
  the project eligible: every final clip exists and passes media checks,
  every final clip was uploaded and YouTube itself reports the video
  ``processed``, and nothing is running or waiting for the project.
* Kept forever: ``project.json`` (project, options, status), the clips'
  ``.meta.json`` (title/description), ``job*.log`` (job history) and the
  cleanup journal. Upload history lives in the YouTube config folder and is
  never touched.
* Only files inside the project folder are considered. A local source video
  elsewhere on disk is never deleted.

Cleanup is journaled (``storage_cleanup.json``): the file list is written
before anything is deleted, so an interrupted cleanup resumes with the same
list, and running it twice is harmless.
"""
import json
import os
import time
from pathlib import Path

import applog

JOURNAL = "storage_cleanup.json"
CLIPS = "clips"

CATEGORIES = ("final_clips", "kept_metadata", "source_media", "previews",
              "transcripts_ai", "temporary")
LABELS = {"final_clips": "Final clips", "kept_metadata": "Project metadata and history",
          "source_media": "Source media and downloads", "previews": "Previews",
          "transcripts_ai": "Transcripts, captions and AI caches",
          "temporary": "Temporary and intermediate files"}
_SOURCE_PREFIXES = ("audio.", "source", "recording", "discovery", "download-")
_SOURCE_DIRS = {"sections", "recording_parts", "parts", "audio_parts"}
_AI_PREFIXES = ("transcript", "candidates", "captions.", "highlights", "segments",
                "selection", "embeddings", "semantic", "hinglish", "visual",
                "audio_features", "evidence", "scenes")
_MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".wav", ".opus", ".ts",
               ".mov", ".aac", ".flac", ".part"}


class StorageError(ValueError):
    pass


# ------------------------------------------------------------ classification

def is_final_clip(rel):
    parts = Path(rel).parts
    return len(parts) == 2 and parts[0] == CLIPS and Path(rel).suffix.lower() == ".mp4" \
        and not parts[1].startswith("_")


def classify(rel):
    """Category of one file, given its path relative to the project folder."""
    rel = Path(rel)
    parts, name = rel.parts, rel.name.lower()
    if is_final_clip(rel):
        return "final_clips"
    if len(parts) == 1 and (name == "project.json" or name == JOURNAL
                            or (name.startswith("job") and name.endswith(".log"))):
        return "kept_metadata"
    if len(parts) == 2 and parts[0] == CLIPS and name.endswith(".meta.json"):
        return "kept_metadata"
    if parts[0] == "previews":
        return "previews"
    if parts[0] in _SOURCE_DIRS or (len(parts) == 1 and name.startswith(_SOURCE_PREFIXES)
                                    and rel.suffix.lower() in _MEDIA_EXTS | {".json"}):
        return "source_media"
    if len(parts) == 1 and name.startswith(_AI_PREFIXES):
        return "transcripts_ai"
    return "temporary"


def protected(rel):
    """Files cleanup must never delete, whatever a plan or journal says."""
    return classify(rel) in {"final_clips", "kept_metadata"}


def scan(path):
    """Every file in a project folder with its size and category."""
    path = Path(path)
    files = []
    for file in sorted(path.rglob("*")):
        if not file.is_file() or file.is_symlink():
            continue
        rel = file.relative_to(path).as_posix()
        try:
            size = file.stat().st_size
        except OSError:
            continue
        files.append({"path": rel, "size": size, "category": classify(rel)})
    totals = {c: 0 for c in CATEGORIES}
    for f in files:
        totals[f["category"]] += f["size"]
    return {"files": files, "totals": totals, "bytes": sum(totals.values())}


# ----------------------------------------------------------------- checks

def check_clip(file, probe):
    """None when the clip looks like a playable video, else the problem."""
    try:
        size = Path(file).stat().st_size
    except OSError:
        return "missing"
    if size <= 0:
        return "empty file"
    try:
        width, height, duration = probe(file)
    except Exception as exc:                    # ffprobe failed: corrupt file
        return f"not a readable video ({type(exc).__name__})"
    if not width or not height or duration <= 0:
        return "no video stream or zero duration"
    return None


def clip_uploads(path, project_id, entries):
    """{clip name: best upload entry} for this project's final clips."""
    by_clip = {}
    rank = {"uploaded": 3, "uploading": 2, "queued": 2, "quota_wait": 2,
            "failed": 1, "draft": 0, "cancelled": -1}
    for entry in entries:
        if entry.get("project_id") != project_id:
            continue
        file = Path(entry.get("file") or "")
        try:
            inside = file.resolve().parent == (Path(path) / CLIPS).resolve()
        except OSError:
            inside = False
        if not inside:
            continue
        name = file.name
        if rank.get(entry.get("state"), -1) > rank.get((by_clip.get(name) or {}).get("state"), -2):
            by_clip[name] = entry
    return by_clip


ACTIVE_UPLOAD = {"queued", "uploading", "quota_wait"}


def plan(path, state, entries, busy=False, probe=None, require_verified=True):
    """Whether the project's supporting files may be deleted, and which.

    ``entries`` are the upload queue entries (with ``youtube_status`` from
    ``verify_uploads``). Returns a dict with ``eligible``, ``reasons`` (why
    not), ``delete``/``keep`` lists, ``bytes`` and per-clip status.
    """
    from youtube_upload import probe_clip
    probe = probe or probe_clip
    path = Path(path)
    project_id = state.get("id") or path.name
    inventory = scan(path)
    reasons = []
    clips = [f for f in inventory["files"] if f["category"] == "final_clips"]
    uploads = clip_uploads(path, project_id, entries)
    clip_status = []
    for clip in clips:
        name = Path(clip["path"]).name
        entry = uploads.get(name) or {}
        problem = check_clip(path / clip["path"], probe)
        clip_status.append({
            "name": name, "path": clip["path"], "size": clip["size"],
            "problem": problem, "upload_state": entry.get("state") or "not uploaded",
            "video_id": entry.get("video_id"), "youtube_status": entry.get("youtube_status"),
            "verified_at": entry.get("verified_at"), "url": entry.get("url")})
    if busy:
        reasons.append("a job is running or waiting for this project")
    # complete = exported; review = a preview was watched after exporting
    # (clips exist). Failed, running or interrupted projects keep everything.
    if state.get("status") not in {"complete", "review"}:
        reasons.append(f"the project is {state.get('status')}, not complete "
                       "(finish exporting first)")
    if state.get("render_errors"):
        reasons.append("some clips failed to export (files kept for a retry)")
    recording = state.get("recording")
    if isinstance(recording, dict) and recording.get("state") not in (None, "recorded"):
        reasons.append("a recording is still in progress")
    if not clips:
        reasons.append("there are no final clips")
    for clip in clip_status:
        if clip["problem"]:
            reasons.append(f"{clip['name']}: {clip['problem']}")
        state_ = clip["upload_state"]
        if state_ in ACTIVE_UPLOAD:
            reasons.append(f"{clip['name']}: upload is {state_}")
        elif state_ == "failed":
            reasons.append(f"{clip['name']}: upload failed (files kept for retry)")
        elif state_ != "uploaded":
            reasons.append(f"{clip['name']}: not uploaded yet")
        elif not clip["video_id"]:
            reasons.append(f"{clip['name']}: uploaded without a video id")
        elif require_verified and clip["youtube_status"] != "processed":
            status = clip["youtube_status"] or "not verified with YouTube yet"
            reasons.append(f"{clip['name']}: YouTube status is {status}")
    if any(e.get("project_id") == project_id and e.get("state") in ACTIVE_UPLOAD
           for e in entries):
        reasons.append("an upload for this project is still queued or running")
    delete = [f for f in inventory["files"] if not protected(f["path"])]
    keep = [f for f in inventory["files"] if protected(f["path"])]
    return {"project_id": project_id, "eligible": not reasons, "reasons": reasons,
            "delete": delete, "keep": keep, "clips": clip_status,
            "bytes": sum(f["size"] for f in delete), "totals": inventory["totals"],
            "total_bytes": inventory["bytes"], "outside": _outside_sources(path, state)}


def _outside_sources(path, state):
    """Source files referenced outside the project folder (never deleted)."""
    found = []
    for value in (state.get("source"), state.get("video")):
        try:
            file = Path(str(value or ""))
            if file.is_file() and Path(path).resolve() not in file.resolve().parents:
                found.append(str(file))
        except (OSError, ValueError):
            continue
    return sorted(set(found))


# ---------------------------------------------------------------- cleanup

def _journal(path):
    return Path(path) / JOURNAL


def _write(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, path)


def pending(path):
    """An unfinished cleanup journal, or None."""
    try:
        data = json.loads(_journal(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if data.get("state") == "running" else None


def run_cleanup(path, plan_result, reason="manual", clock=time.time):
    """Delete the planned supporting files; resumable and idempotent."""
    path = Path(path).resolve()
    journal = _journal(path)
    existing = pending(path)
    if existing:
        files = existing["files"]
        started = existing["started"]
    else:
        if not plan_result.get("eligible"):
            raise StorageError("Not eligible for cleanup: " + "; ".join(plan_result["reasons"]))
        files = [f["path"] for f in plan_result["delete"]]
        started = clock()
    before = (existing or {}).get("before")
    if before is None:
        done = last_cleanup(path) or {}
        before = {"total_files_deleted": int(done.get("total_files_deleted") or 0),
                  "total_bytes_freed": int(done.get("total_bytes_freed") or 0)}
    progress = {"files": int((existing or {}).get("done_files") or 0),
                "bytes": int((existing or {}).get("done_bytes") or 0)}
    running = {"state": "running", "reason": reason, "started": started,
               "files": files, "project_id": path.name, "before": before,
               "done_files": progress["files"], "done_bytes": progress["bytes"]}
    _write(journal, running)
    applog.info("storage.cleanup", f"Cleanup started: {len(files)} supporting files",
                project_id=path.name, event="started", reason=reason, files=len(files))
    deleted, freed, skipped = 0, 0, []
    for rel in files:
        target = (path / rel).resolve()
        if path not in target.parents or protected(rel):
            skipped.append(rel)                 # a corrupted journal cannot hurt clips
            continue
        try:
            size = target.stat().st_size
        except OSError:
            continue                             # already gone (resumed run)
        try:
            target.unlink()
            deleted += 1
            freed += size
            # progress survives an interruption, so totals stay exact
            running.update(done_files=progress["files"] + deleted,
                           done_bytes=progress["bytes"] + freed)
            _write(journal, running)
        except OSError as exc:
            skipped.append(rel)
            applog.warning("storage.cleanup", f"Could not delete {rel}", exc=exc,
                           project_id=path.name, file=str(target))
    for directory in sorted((p for p in path.rglob("*") if p.is_dir()),
                            key=lambda p: len(p.parts), reverse=True):
        if directory.name != CLIPS:
            try:
                directory.rmdir()               # only empty folders
            except OSError:
                pass
    summary = {"state": "done", "reason": reason, "started": started, "finished": clock(),
               "files_deleted": deleted, "bytes_freed": freed, "skipped": skipped,
               "project_id": path.name}
    summary["total_files_deleted"] = before["total_files_deleted"] + progress["files"] + deleted
    summary["total_bytes_freed"] = before["total_bytes_freed"] + progress["bytes"] + freed
    _write(journal, summary)
    applog.info("storage.cleanup", f"Cleanup finished: {deleted} files, {freed} bytes freed",
                project_id=path.name, event="completed", files=deleted, bytes=freed,
                skipped=len(skipped) or None)
    return summary


def last_cleanup(path):
    try:
        data = json.loads(_journal(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data


# ----------------------------------------------------- manual clip delete

def clip_files(path, names):
    """The clip MP4s and their sidecar files for the given clip names."""
    clips = Path(path) / CLIPS
    selected = []
    for name in names:
        if not isinstance(name, str) or "/" in name or "\\" in name or not name.endswith(".mp4"):
            raise StorageError(f"Not a clip file: {name}")
        mp4 = clips / name
        if not mp4.is_file():
            raise StorageError(f"Clip not found: {name}")
        stem = name[:-4]
        group = [mp4] + [p for p in clips.glob(stem + ".*") if p != mp4]
        group += list(clips.glob(f"_caption-{stem.split('-')[0]}-*"))
        selected.append((name, sorted(set(group))))
    return selected


def delete_plan(path, project_id, names, entries):
    """What a manual delete would remove, with sizes and upload state."""
    uploads = clip_uploads(path, project_id, entries)
    items = []
    for name, group in clip_files(path, names):
        entry = uploads.get(name) or {}
        items.append({"name": name, "files": [p.name for p in group],
                      "bytes": sum(p.stat().st_size for p in group),
                      "upload_state": entry.get("state") or "not uploaded",
                      "video_id": entry.get("video_id"), "url": entry.get("url")})
    blocked = [i["name"] for i in items if i["upload_state"] in ACTIVE_UPLOAD]
    return {"project_id": project_id, "clips": items,
            "bytes": sum(i["bytes"] for i in items),
            "uploaded": sum(1 for i in items if i["upload_state"] == "uploaded"),
            "not_uploaded": sum(1 for i in items if i["upload_state"] != "uploaded"),
            "blocked": blocked}


def delete_clips(path, project_id, names, entries, confirm=False, confirmed_files=None):
    """Permanently delete chosen final clips, only with explicit confirmation.

    ``confirmed_files`` must equal the clip names the user saw in the
    confirmation dialog, so a stale or tampered request cannot delete more.
    """
    preview = delete_plan(path, project_id, names, entries)
    if confirm is not True:
        return {**preview, "deleted": False}
    if sorted(confirmed_files or []) != sorted(names):
        raise StorageError("The confirmed clip list does not match the request.")
    if preview["blocked"]:
        raise StorageError("Cancel the upload first: " + ", ".join(preview["blocked"]))
    deleted, freed = [], 0
    for name, group in clip_files(path, names):
        for file in group:
            size = file.stat().st_size
            file.unlink()
            freed += size
            deleted.append(file.name)
        applog.info("storage.delete_clip", f"Clip deleted by the user: {name}",
                    project_id=project_id, clip=name, files=len(group))
    return {**preview, "deleted": True, "files_deleted": len(deleted),
            "bytes_freed": freed, "removed": deleted}
