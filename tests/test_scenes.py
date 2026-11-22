"""Phase 4: gameplay-only scene classification (observation mode)."""
import json

import pytest

import features
import projects
import scene_eval
import scenes

PLAN = [("starting_soon", 25), ("gameplay", 45), ("menu", 15), ("gameplay", 35),
        ("brb", 25), ("gameplay", 30), ("outro", 20)]


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    folder = tmp_path_factory.mktemp("scenes")
    video = folder / "stream.mp4"
    labels = scene_eval.make_sample(video, PLAN)
    return video, labels


@pytest.fixture(scope="module")
def analysed(sample):
    video, labels = sample
    return scenes.analyze(video), labels


def test_synthetic_stream_meets_the_acceptance_targets(analysed):
    result, labels = analysed
    report = scene_eval.score(result["scenes"], labels)
    assert report["gameplay_kept"] >= 0.95
    assert report["breaks_identified"] >= 0.90
    assert report["meets_targets"]
    found = {s["label"] for s in result["scenes"]}
    assert {"gameplay", "starting_soon", "menu", "brb", "outro"} <= found
    assert result["mode"] == "observation"


def test_without_ocr_breaks_are_still_not_gameplay(sample):
    video, labels = sample
    result = scenes.analyze(video, use_ocr=False)
    report = scene_eval.score(result["scenes"], labels)
    assert report["gameplay_kept"] >= 0.95, "gameplay is never lost"
    assert report["confusion"]["brb"].get("gameplay", 0) == 0
    first, last = result["scenes"][0], result["scenes"][-1]
    assert first["label"] in ("starting_soon", "intro") and last["label"] == "outro"


def test_vision_is_asked_rarely_and_never_about_gameplay(sample):
    video, _ = sample
    asked = []

    def vision(path, at):
        asked.append(at)
        return "brb"
    result = scenes.analyze(video, use_ocr=False, vision=vision)
    assert 0 < len(asked) <= scenes.MAX_VISION
    for at in asked:
        assert scenes.label_at([s for s in result["scenes"]], at) != "gameplay"


def test_keyword_rules():
    assert scenes.keyword_label("STREAM STARTING SOON") == "starting_soon"
    assert scenes.keyword_label("brb - grabbing food") == "brb"
    assert scenes.keyword_label("Thanks for watching!") == "outro"
    assert scenes.keyword_label("Loading... please wait") == "loading"
    assert scenes.keyword_label("PLAY SETTINGS QUIT") == "menu"
    assert scenes.keyword_label("press play") is None, "one menu word is not a menu"
    assert scenes.keyword_label("HP 100 AMMO 30") is None


def test_score_counts_false_positives_and_negatives():
    labels = [{"start": 0, "end": 10, "label": "gameplay"}, {"start": 10, "end": 20, "label": "brb"}]
    predicted = [{"start": 0, "end": 15, "label": "gameplay"}, {"start": 15, "end": 20, "label": "brb"}]
    report = scene_eval.score(predicted, labels)
    assert report["gameplay_kept"] == 1.0 and report["breaks_identified"] == 0.5
    assert report["false_positive_gameplay_seconds"] == 5 and not report["meets_targets"]


def test_label_page_is_self_contained(sample, tmp_path):
    video, _ = sample
    page = scene_eval.label_page(video, every=30, out=tmp_path / "labels.html")
    text = page.read_text(encoding="utf-8")
    assert text.count("<figure") >= 6 and "data:image/jpeg;base64," in text
    assert "Download labels" in text


def test_project_records_scenes_without_cutting_anything(sample, tmp_path):
    video, _ = sample
    state = projects.create_project(tmp_path / "projects", str(video), {})
    path = projects.project_path(tmp_path / "projects", state["id"])
    (path / "transcript.json").write_text(json.dumps({"video": str(video)}), encoding="utf-8")
    segments = [{"start": 30.0, "end": 60.0}, {"start": 115.0, "end": 140.0}]
    (path / "segments.json").write_text(json.dumps(segments), encoding="utf-8")
    state = projects.read_project(path)
    summary = projects.observe_scenes(path, state)
    assert summary["mode"] == "observation" and 0.5 < summary["gameplay_share"] < 0.9
    assert json.loads((path / "segments.json").read_text()) == segments, "nothing cut"
    import dashboard
    detail = dashboard.project_detail(tmp_path / "projects", state["id"])
    assert [c["scene"] for c in detail["candidates"]] == ["gameplay", "brb"]
    assert projects.observe_scenes(path, projects.read_project(path)) == summary, "cached"


def test_filtering_stays_off_until_targets_are_met_on_real_labels():
    assert features.enabled("scene_detection") is True
    assert features.enabled("scene_filtering") is False
