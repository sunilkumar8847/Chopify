"""Phase 3: titles and safe project deletion (junctions, symlinks, outside targets)."""
import os
import sys

import pytest

import project_admin
import projects
from project_admin import AdminError


def junction(link, target):
    if sys.platform != "win32":
        os.symlink(target, link, target_is_directory=True)
        return
    import _winapi
    _winapi.CreateJunction(str(target), str(link))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "projects"
    video = tmp_path / "Night_stream-01.mkv"
    video.write_bytes(b"\0" * 10)
    state = projects.create_project(root, str(video), {})
    folder = projects.project_path(root, state["id"])
    (folder / "clips").mkdir()
    (folder / "clips" / "clip_01.mp4").write_bytes(b"final one")
    (folder / "clips" / "clip_01.meta.json").write_text("{}", encoding="utf-8")
    (folder / "audio.webm").write_bytes(b"supporting")
    (folder / "previews").mkdir()
    (folder / "previews" / "p.mp4").write_bytes(b"preview")
    return root, state["id"], folder, video


def test_display_titles_are_human_readable():
    assert project_admin.display_title({"source": r"D:\OBS\GTA_night-stream.mkv"}) == \
        "GTA night stream"
    assert project_admin.display_title({"title": "  Custom  ", "source": "x"}) == "Custom"
    assert project_admin.display_title({"source": r"C:\p\abc\recording.mp4"}) == \
        "Abc recording"
    assert project_admin.display_title({"source": "https://cdn.example.com/v/clip_7.mp4"}) == \
        "cdn.example.com · Clip 7"
    assert project_admin.display_title({"source_meta": {"title": "Live!"}, "source": "u"}) == \
        "Live!"
    with pytest.raises(AdminError):
        project_admin.validate_title("   ")
    with pytest.raises(AdminError):
        project_admin.validate_title("x" * 121)


def test_project_mode_keeps_final_clips_and_the_source_video(project):
    root, pid, folder, video = project
    plan = project_admin.delete_plan(root, pid)
    assert plan["final_clips"] == ["clip_01.mp4"] and not plan["blockers"]
    with pytest.raises(AdminError, match="confirmation"):
        project_admin.delete_project(root, pid, "project")
    result = project_admin.delete_project(root, pid, "project", confirm=True)
    assert result["final_clips_kept"] == 2         # mp4 + its .meta.json
    assert (folder / "clips" / "clip_01.mp4").read_bytes() == b"final one"
    assert not (folder / "audio.webm").exists() and not (folder / "previews").exists()
    assert not (folder / "project.json").exists()
    assert video.exists(), "a local source outside the project is never touched"


def test_everything_mode_needs_the_exact_clip_count(project):
    root, pid, folder, video = project
    with pytest.raises(AdminError, match="Type the number"):
        project_admin.delete_project(root, pid, "everything", confirm=True, confirm_clips=0)
    assert (folder / "clips" / "clip_01.mp4").exists()
    project_admin.delete_project(root, pid, "everything", confirm=True, confirm_clips=1)
    assert not folder.exists() and video.exists()


def test_links_inside_a_project_are_removed_as_links_only(project, tmp_path):
    root, pid, folder, _ = project
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.mp4").write_bytes(b"not chopify's")
    junction(folder / "linked_dir", outside)
    plan = project_admin.delete_plan(root, pid)
    assert plan["links"] == ["linked_dir"]
    project_admin.delete_project(root, pid, "everything", confirm=True, confirm_clips=1)
    assert (outside / "keep.mp4").read_bytes() == b"not chopify's", "target untouched"
    assert not folder.exists()


def test_a_project_folder_that_is_a_junction_is_refused(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "clip.mp4").write_bytes(b"x")
    junction(root / "0123456789ab", target)
    with pytest.raises(AdminError, match="link"):
        project_admin.delete_plan(root, "0123456789ab")
    with pytest.raises(AdminError):
        project_admin.delete_project(root, "0123456789ab", "everything", confirm=True,
                                     confirm_clips=1)
    assert (target / "clip.mp4").exists()


@pytest.mark.parametrize("bad", ["..", "../x", "ABC", "0123456789abc", "", None, "a/b"])
def test_only_real_project_ids_are_accepted(tmp_path, bad):
    with pytest.raises(AdminError):
        project_admin.project_folder(tmp_path, bad)


def test_busy_projects_and_pending_uploads_block_deletion(project):
    root, pid, folder, _ = project
    with pytest.raises(AdminError, match="Cancel it first"):
        project_admin.delete_project(root, pid, "project", busy=True, confirm=True)
    uploads = [{"project_id": pid, "state": "queued"}, {"project_id": pid, "state": "done"}]
    plan = project_admin.delete_plan(root, pid, uploads)
    assert plan["blockers"] and plan["uploaded"] == 1
    with pytest.raises(AdminError, match="upload"):
        project_admin.delete_project(root, pid, "project", uploads, confirm=True)
    assert (folder / "project.json").exists()


def test_upload_history_is_not_touched(project, tmp_path):
    import youtube_auth
    root, pid, folder, _ = project
    history = youtube_auth.config_dir() / "uploads.json"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text('[{"project_id": "%s", "state": "done"}]' % pid, encoding="utf-8")
    project_admin.delete_project(root, pid, "project", [{"project_id": pid, "state": "done"}],
                                 confirm=True)
    assert pid in history.read_text(encoding="utf-8")


def test_open_folder_only_opens_validated_project_folders(project):
    root, pid, folder, _ = project
    opened = []
    assert project_admin.open_folder(root, pid, opener=opened.append)["opened"] == str(folder)
    with pytest.raises(AdminError):
        project_admin.open_folder(root, "..", opener=opened.append)
    assert opened == [str(folder)]
