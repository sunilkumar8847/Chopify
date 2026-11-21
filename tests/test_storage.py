"""Storage cleanup: final clips are kept, supporting files go only when safe."""
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import dashboard
import projects
import storage
import youtube_upload

GOOD_PROBE = lambda path: (1080, 1920, 30.0)  # noqa: E731

SUPPORTING = ["audio.webm", "source_full.f299.mp4.part", "transcript.json",
              "transcript.part000.json", "candidates.json", "captions.en-orig.json3",
              "highlights_cache.json", "segments.json", "selection.json",
              "audio_features.npz", "previews/001-ab12-hook.mp4", "sections/section001.mp4",
              "audio_parts/part000.wav", "clips/001-ab12-hook.srt",
              "clips/001-ab12-hook.png", "clips/_caption-001-ab12.ass"]
KEPT = ["project.json", "job.log", "clips/001-ab12-hook.mp4",
        "clips/001-ab12-hook.meta.json", "clips/002-cd34-other.mp4",
        "clips/002-cd34-other.meta.json"]


def make_project(root, status="complete", auto=True):
    state = projects.create_project(root, "https://youtu.be/aaaaaaaaaaa", {})
    path = projects.project_path(root, state["id"])
    for rel in SUPPORTING + KEPT:
        if rel == "project.json":
            continue
        file = path / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"x" * (2048 if rel.endswith(".mp4") else 100))
    projects.update_project(path, lambda s: s.update(
        status=status, storage={"auto_cleanup": auto}))
    return path, state["id"]


def uploaded(path, project_id, names=("001-ab12-hook.mp4", "002-cd34-other.mp4"),
             state="uploaded", youtube_status="processed"):
    return [{"id": f"e{i}", "project_id": project_id, "file": str(path / "clips" / n),
             "name": n, "state": state, "video_id": f"vid{i}" if state == "uploaded" else None,
             "youtube_status": youtube_status, "url": f"https://youtube.com/shorts/vid{i}"}
            for i, n in enumerate(names)]


def existing(path):
    return sorted(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file())


# ------------------------------------------------------------ policy core

def test_classification_protects_final_clips_metadata_and_history():
    for rel in KEPT:
        assert storage.protected(rel), rel
    for rel in SUPPORTING:
        assert not storage.protected(rel), rel
    assert storage.classify("audio.webm") == "source_media"
    assert storage.classify("previews/x.mp4") == "previews"
    assert storage.classify("transcript.part003.json") == "transcripts_ai"
    assert not storage.is_final_clip("previews/001.mp4")
    assert not storage.is_final_clip("clips/_caption-001.mp4")


def test_successful_verified_upload_cleans_supporting_files_and_keeps_clips(tmp_path):
    path, pid = make_project(tmp_path)
    state = projects.read_project(path)
    plan = storage.plan(path, state, uploaded(path, pid), probe=GOOD_PROBE)
    assert plan["eligible"], plan["reasons"]
    assert {f["path"] for f in plan["delete"]} == set(SUPPORTING)
    summary = storage.run_cleanup(path, plan)
    assert summary["files_deleted"] == len(SUPPORTING) and summary["bytes_freed"] > 0
    assert existing(path) == sorted(KEPT + [storage.JOURNAL])
    assert projects.read_project(path)["status"] == "complete"


@pytest.mark.parametrize("entries_kind,why", [
    ("failed", "upload failed"),
    ("processing", "YouTube status is uploaded"),
    ("unverified", "not verified with YouTube yet"),
    ("queued", "upload is queued"),
    ("one_missing", "not uploaded yet"),
])
def test_unsafe_upload_states_keep_everything(tmp_path, entries_kind, why):
    path, pid = make_project(tmp_path)
    if entries_kind == "failed":
        entries = uploaded(path, pid, state="failed")
    elif entries_kind == "processing":
        entries = uploaded(path, pid, youtube_status="uploaded")
    elif entries_kind == "unverified":
        entries = uploaded(path, pid, youtube_status=None)
    elif entries_kind == "queued":
        entries = uploaded(path, pid, state="queued")
    else:
        entries = uploaded(path, pid, names=("001-ab12-hook.mp4",))
    before = existing(path)
    plan = storage.plan(path, projects.read_project(path), entries, probe=GOOD_PROBE)
    assert not plan["eligible"] and any(why in r for r in plan["reasons"]), plan["reasons"]
    with pytest.raises(storage.StorageError):
        storage.run_cleanup(path, plan)
    assert existing(path) == before


def test_busy_failed_and_clipless_projects_are_not_cleaned(tmp_path):
    path, pid = make_project(tmp_path)
    entries = uploaded(path, pid)
    state = projects.read_project(path)
    assert not storage.plan(path, state, entries, busy=True, probe=GOOD_PROBE)["eligible"]
    assert not storage.plan(path, {**state, "status": "failed"}, entries, probe=GOOD_PROBE)["eligible"]
    assert not storage.plan(path, {**state, "render_errors": [{"clip": "003"}]}, entries,
                            probe=GOOD_PROBE)["eligible"]
    for clip in (path / "clips").glob("*.mp4"):
        clip.unlink()
    plan = storage.plan(path, state, [], probe=GOOD_PROBE)
    assert "there are no final clips" in plan["reasons"]


def test_missing_or_corrupted_final_clips_block_cleanup(tmp_path):
    path, pid = make_project(tmp_path)
    (path / "clips" / "002-cd34-other.mp4").write_bytes(b"")

    def probe(file):
        if "001" in str(file):
            raise RuntimeError("moov atom not found")
        return 1080, 1920, 30.0
    plan = storage.plan(path, projects.read_project(path), uploaded(path, pid), probe=probe)
    assert not plan["eligible"]
    text = "; ".join(plan["reasons"])
    assert "001-ab12-hook.mp4: not a readable video" in text and "002-cd34-other.mp4: empty file" in text


def test_interrupted_cleanup_resumes_and_repeats_are_harmless(tmp_path, monkeypatch):
    path, pid = make_project(tmp_path)
    plan = storage.plan(path, projects.read_project(path), uploaded(path, pid), probe=GOOD_PROBE)
    real_unlink = Path.unlink
    calls = {"n": 0}

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 5:
            raise KeyboardInterrupt("power cut")
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", flaky)
    with pytest.raises(KeyboardInterrupt):
        storage.run_cleanup(path, plan)
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert storage.pending(path)["state"] == "running"
    assert all((path / rel).exists() for rel in KEPT if rel != "project.json")
    summary = storage.run_cleanup(path, {})        # resume: no new plan needed
    assert summary["state"] == "done"
    assert existing(path) == sorted(KEPT + [storage.JOURNAL])
    assert summary["total_files_deleted"] == len(SUPPORTING)
    again = storage.run_cleanup(path, storage.plan(path, projects.read_project(path),
                                                   uploaded(path, pid), probe=GOOD_PROBE))
    assert again["files_deleted"] == 0 and existing(path) == sorted(KEPT + [storage.JOURNAL])


def test_a_tampered_journal_can_never_delete_clips(tmp_path):
    path, pid = make_project(tmp_path)
    (path / storage.JOURNAL).write_text(json.dumps({
        "state": "running", "started": 1, "files": [
            "clips/001-ab12-hook.mp4", "project.json", "../outside.txt", "audio.webm"]}),
        encoding="utf-8")
    (tmp_path / "outside.txt").write_text("keep me")
    summary = storage.run_cleanup(path, {})
    assert (path / "clips" / "001-ab12-hook.mp4").exists() and (path / "project.json").exists()
    assert (tmp_path / "outside.txt").exists() and not (path / "audio.webm").exists()
    assert len(summary["skipped"]) == 3


def test_local_sources_outside_the_project_are_never_touched(tmp_path):
    video = tmp_path / "my_video.mp4"
    video.write_bytes(b"v" * 100)
    state = projects.create_project(tmp_path / "projects", str(video), {})
    path = projects.project_path(tmp_path / "projects", state["id"])
    plan = storage.plan(path, projects.read_project(path), [], probe=GOOD_PROBE)
    assert plan["outside"] == [str(video.resolve())]
    assert all("my_video" not in f["path"] for f in plan["delete"])


# --------------------------------------------------------- manual delete

def test_manual_clip_deletion_needs_the_confirmed_list(tmp_path):
    path, pid = make_project(tmp_path)
    entries = uploaded(path, pid, names=("001-ab12-hook.mp4",))
    preview = storage.delete_clips(path, pid, ["001-ab12-hook.mp4", "002-cd34-other.mp4"], entries)
    assert preview["deleted"] is False and preview["uploaded"] == 1 and preview["not_uploaded"] == 1
    assert (path / "clips" / "001-ab12-hook.mp4").exists()
    with pytest.raises(storage.StorageError, match="does not match"):
        storage.delete_clips(path, pid, ["001-ab12-hook.mp4", "002-cd34-other.mp4"], entries,
                             confirm=True, confirmed_files=["001-ab12-hook.mp4"])
    with pytest.raises(storage.StorageError):
        storage.delete_clips(path, pid, ["../project.json"], entries)
    result = storage.delete_clips(path, pid, ["001-ab12-hook.mp4"], entries, confirm=True,
                                  confirmed_files=["001-ab12-hook.mp4"])
    assert result["deleted"] and set(result["removed"]) >= {"001-ab12-hook.mp4",
                                                            "001-ab12-hook.meta.json"}
    assert (path / "clips" / "002-cd34-other.mp4").exists(), "unselected clips stay"


def test_clips_with_an_active_upload_cannot_be_deleted(tmp_path):
    path, pid = make_project(tmp_path)
    entries = uploaded(path, pid, state="uploading")
    with pytest.raises(storage.StorageError, match="Cancel the upload"):
        storage.delete_clips(path, pid, ["001-ab12-hook.mp4"], entries, confirm=True,
                             confirmed_files=["001-ab12-hook.mp4"])


# ------------------------------------------------------ YouTube verify

class FakeAccount:
    def __init__(self, items=None, status=200, fail=False):
        self.items, self.status, self.fail = items or [], status, fail
        self.calls = []
        self.dir = None
        self.quota_hook = None

    def authorized(self, method, url, headers=None, body=None, timeout=120):
        self.calls.append((method, url))
        if self.fail:
            raise OSError("network down")
        return self.status, {}, json.dumps({"items": self.items}).encode()


def queue_with(tmp_path, account, entries):
    queue = youtube_upload.UploadQueue(account, directory=tmp_path)
    queue._save(entries)
    return queue


def test_verify_asks_youtube_once_per_50_videos_and_records_status(tmp_path):
    account = FakeAccount(items=[{"id": "vid0", "status": {"uploadStatus": "processed"}},
                                 {"id": "vid1", "status": {"uploadStatus": "uploaded"}}])
    entries = uploaded(tmp_path, "p", names=("a.mp4", "b.mp4", "c.mp4"), youtube_status=None)
    queue = queue_with(tmp_path, account, entries)
    result = queue.verify_uploaded(["e0", "e1", "e2"])
    assert result == {"e0": "processed", "e1": "uploaded", "e2": "deleted"}
    assert len(account.calls) == 1 and "videos?part=status&id=vid0%2Cvid1%2Cvid2" in account.calls[0][1]
    assert queue.get("e0")["youtube_status"] == "processed" and queue.get("e0")["verified_at"]


@pytest.mark.parametrize("account", [FakeAccount(status=403), FakeAccount(fail=True)])
def test_unavailable_api_means_uncertain(tmp_path, account):
    queue = queue_with(tmp_path, account, uploaded(tmp_path, "p", names=("a.mp4",),
                                                   youtube_status=None))
    with pytest.raises(youtube_upload.UploadError):
        queue.verify_uploaded(["e0"])
    assert queue.get("e0")["youtube_status"] is None


# ------------------------------------------------------------- Studio

@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "check_clip", lambda file, probe: None if Path(file).stat().st_size
                        else "empty file")
    app = dashboard.Studio(tmp_path / "projects")
    app.uploads = queue_with(tmp_path / "yt", FakeAccount(), [])
    yield app
    app.close()


def test_studio_verifies_then_cleans_and_keeps_listing_clips(studio, monkeypatch):
    path, pid = make_project(studio.root)
    entries = uploaded(path, pid, youtube_status=None)
    studio.uploads._save(entries)
    studio.uploads.account.items = [{"id": "vid0", "status": {"uploadStatus": "processed"}},
                                    {"id": "vid1", "status": {"uploadStatus": "processed"}}]
    result = studio.cleanup_project(pid)
    assert result["cleaned"] and result["files_deleted"] == len(SUPPORTING)
    detail = dashboard.project_detail(studio.root, pid)
    assert [f["name"] for f in detail["files"] if f["name"].endswith(".mp4")] == [
        "001-ab12-hook.mp4", "002-cd34-other.mp4"]
    assert detail["project"]["status"] == "complete"
    assert detail["pipeline"]["transcribe"] == "done", "not shown as missing work"
    assert detail["project"]["storage"]["bytes_freed"] > 0
    with pytest.raises(ValueError, match="cleaned up after its clips were uploaded"):
        studio.enqueue(pid, "render", preview=True, pick="1")


def test_studio_auto_cleanup_waits_for_processing_and_skips_old_projects(studio):
    path, pid = make_project(studio.root)
    old_path, old_pid = make_project(studio.root, auto=False)      # existing project
    studio.uploads._save(uploaded(path, pid, youtube_status=None)
                         + [dict(e, id="o" + e["id"]) for e in uploaded(old_path, old_pid)])
    studio.uploads.account.items = [{"id": "vid0", "status": {"uploadStatus": "uploaded"}},
                                    {"id": "vid1", "status": {"uploadStatus": "processed"}}]
    assert studio.auto_cleanup() is True, "still processing: check again later"
    assert (path / "audio.webm").exists() and (old_path / "audio.webm").exists()
    studio.uploads.account.items[0]["status"]["uploadStatus"] = "processed"
    assert studio.auto_cleanup() is False
    assert not (path / "audio.webm").exists() and (path / "clips" / "001-ab12-hook.mp4").exists()
    assert (old_path / "audio.webm").exists(), "existing projects need explicit approval"
    studio.storage_timer and studio.storage_timer.cancel()


def test_api_unavailable_prevents_studio_cleanup(studio):
    path, pid = make_project(studio.root)
    studio.uploads._save(uploaded(path, pid, youtube_status=None))
    studio.uploads.account.fail = True
    result = studio.cleanup_project(pid)
    assert not result["cleaned"] and any("uncertain" in r for r in result["reasons"])
    assert (path / "audio.webm").exists()


def test_storage_overview_and_http_endpoints(studio):
    path, pid = make_project(studio.root)
    studio.uploads._save(uploaded(path, pid))
    httpd = dashboard.make_server(studio.root, port=0)
    httpd.app.close()
    httpd.app = studio
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        overview = json.load(urllib.request.urlopen(base + "/api/storage"))
        assert overview["eligible"] == [pid] and overview["totals"]["final_clips"] == 4096
        assert overview["reclaimable_bytes"] > 0

        def post(url, data):
            req = urllib.request.Request(base + url, data=json.dumps(data).encode(), headers={
                "Content-Type": "application/json", "X-Chopify-Token": studio.token})
            try:
                return json.load(urllib.request.urlopen(req))
            except urllib.error.HTTPError as exc:
                return {"status": exc.code, **json.load(exc)}
        preview = post(f"/api/projects/{pid}/delete-clips", {"files": ["001-ab12-hook.mp4"]})
        assert preview["deleted"] is False and (path / "clips" / "001-ab12-hook.mp4").exists()
        cleaned = post("/api/storage/cleanup", {})
        assert cleaned["projects_cleaned"] == 1 and cleaned["files_deleted"] == len(SUPPORTING)
        again = post("/api/storage/cleanup", {"project_id": pid})
        assert again["cleaned"] is True and again["files_deleted"] == 0
        bad = post(f"/api/projects/{pid}/delete-clips", {"files": ["001-ab12-hook.mp4"],
                                                         "confirm": True, "confirmed": []})
        assert bad["status"] == 400 and (path / "clips" / "001-ab12-hook.mp4").exists()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()
