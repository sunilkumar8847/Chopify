"""Phase 7: consent-based automatic publishing (YouTube API mocked)."""
import json

import pytest

import publish_consent
from publish_consent import AutoPublisher, ConsentError, ConsentStore
from test_youtube import API, Clock, FakeGoogle, make_clip, queue_for, sign_in  # noqa: F401
from youtube_auth import YouTubeAccount
import youtube_data


class Google(FakeGoogle):
    """Adds videoCategories.list and videos.list (status) to the fake API."""

    assignable = True
    deleted = set()

    def __call__(self, method, url, headers=None, body=None, timeout=60):
        if url.startswith(f"{API}/videoCategories"):
            self.calls.append((method, url))
            # Like the real API: exactly one of id / regionCode / hl.
            filters = [f for f in ("id=", "regionCode=", "hl=") if f"&{f}" in url or f"?{f}" in url]
            if "id=" in url and "regionCode=" in url:
                return 400, {}, json.dumps({"error": {"code": 400, "message": (
                    "Incompatible parameters specified in the request: id, regionCode"),
                    "errors": [{"reason": "incompatibleParameters"}]}}).encode()
            assert len(filters) == 1, url
            return 200, {}, json.dumps({"items": [
                {"id": "1", "snippet": {"title": "Film & Animation", "assignable": True}},
                {"id": "20", "snippet": {"title": "Gaming", "assignable": self.assignable}},
                {"id": "22", "snippet": {"title": "People & Blogs", "assignable": True}}]}).encode()
        if url.startswith(f"{API}/videos?"):
            self.calls.append((method, url))
            ids = url.split("id=")[1].split("&")[0].split("%2C")
            return 200, {}, json.dumps({"items": [
                {"id": i, "status": {"uploadStatus": "processed"}} for i in ids
                if i not in self.deleted]}).encode()
        return super().__call__(method, url, headers, body, timeout)


@pytest.fixture
def setup(tmp_path):
    from test_youtube import CLIENT
    clock = Clock()
    google = Google(clock)
    client = tmp_path / "client_secret_test.json"
    client.write_text(json.dumps(CLIENT))
    account = YouTubeAccount(tmp_path / "config", http=google, clock=clock)
    account.import_client(client)
    sign_in(account, google)
    queue = queue_for(account, clock)
    store = ConsentStore(tmp_path / "config", clock=clock)
    channel = lambda: account.status()["channel"]  # noqa: E731
    publisher = AutoPublisher(store, queue, channel, clock=clock)
    data = youtube_data.DataClient(account)
    return {"clock": clock, "google": google, "account": account, "queue": queue,
            "store": store, "publisher": publisher, "data": data, "tmp": tmp_path,
            "channel": channel()}


def choices(channel, **changes):
    base = {"channel_id": channel["id"], "visibility": "unlisted", "made_for_kids": False,
            "region_code": "IN", "game": "GTA 6", "title_template": "{hook} | {game}",
            "description_template": "From {stream} on {date}. #{game}", "daily_limit": 2,
            "min_score": 7, "confirm": True}
    return {**base, **changes}


def check(setup):
    return lambda cid, region: publish_consent.check_category(setup["data"], cid, region)


@pytest.mark.parametrize("change, message", [
    ({"visibility": None}, "no default"),
    ({"visibility": "public"}, "PUBLIC"),
    ({"made_for_kids": None}, "made for kids"),
    ({"made_for_kids": "no"}, "made for kids"),
    ({"confirm": False}, "consent box"),
    ({"channel_id": "UC-other"}, "connected channel"),
    ({"daily_limit": 0}, "Daily limit"),
    ({"region_code": "India"}, "region"),
    ({"title_template": "{hook} {secret}"}, "Unknown placeholders"),
])
def test_every_choice_must_be_explicit(setup, change, message):
    with pytest.raises(ConsentError, match=message):
        setup["store"].create("proj1", choices(setup["channel"], **change), setup["channel"],
                              check(setup))
    assert setup["store"].active_for("proj1") is None


def test_gaming_category_is_checked_for_the_region(setup):
    setup["google"].assignable = False
    with pytest.raises(ConsentError, match="cannot be assigned"):
        setup["store"].create("proj1", choices(setup["channel"]), setup["channel"],
                              check(setup))
    setup["google"].assignable = True
    consent = setup["store"].create("proj1", choices(setup["channel"]), setup["channel"],
                                    check(setup))
    assert consent["category_check"]["title"] == "Gaming" and consent["region_code"] == "IN"
    assert any("videoCategories" in url and "regionCode=IN" in url and "id=" not in url
               for _, url in setup["google"].calls), "one filter only (no id + regionCode)"


def test_unknown_category_in_a_region_is_explained(setup):
    with pytest.raises(ConsentError, match="does not exist in region IN"):
        publish_consent.check_category(setup["data"], "99", "IN")


def test_consented_clips_upload_with_the_chosen_settings_and_are_verified(setup):
    setup["store"].create("proj1", choices(setup["channel"]), setup["channel"], check(setup))
    clip = make_clip(setup["tmp"], "001-live.mp4")
    entry, reason = setup["publisher"].offer("proj1", clip, {"hook": "that clutch", "stream":
                                                             "Night stream", "date": "22 Nov"},
                                             score=8.5)
    assert reason == "queued" and entry["state"] == "queued" and entry["consent_id"]
    assert entry["metadata"]["title"] == "that clutch | GTA 6"
    setup["queue"].process()
    sent = setup["google"].inserts[0]
    assert sent["status"]["privacyStatus"] == "unlisted", "the user's choice, never public"
    assert sent["status"]["selfDeclaredMadeForKids"] is False
    assert sent["snippet"]["categoryId"] == "20"
    done = setup["queue"].get(entry["id"])
    assert done["state"] == "uploaded"
    assert setup["queue"].verify_uploaded([done["id"]]) == {done["id"]: "processed"}
    # The same clip is never uploaded twice.
    again, why = setup["publisher"].offer("proj1", clip, {"hook": "x"}, score=9)
    assert why == "already queued or uploaded" and len(setup["google"].inserts) == 1


def test_low_scores_daily_limit_and_other_channels_are_respected(setup):
    setup["store"].create("proj1", choices(setup["channel"], daily_limit=1), setup["channel"],
                          check(setup))
    low, why = setup["publisher"].offer("proj1", make_clip(setup["tmp"], "a.mp4", 40_001), {},
                                        score=5.0)
    assert low is None and "below" in why
    first, _ = setup["publisher"].offer("proj1", make_clip(setup["tmp"], "b.mp4", 40_002), {},
                                        score=9.0)
    second, why = setup["publisher"].offer("proj1", make_clip(setup["tmp"], "c.mp4", 40_003),
                                           {}, score=9.0)
    assert first and second is None and "daily limit" in why
    none, why = setup["publisher"].offer("other-project", make_clip(setup["tmp"], "d.mp4",
                                                                    40_004), {}, score=9.0)
    assert none is None and why == "no consent"
    setup["publisher"].channel = lambda: {"id": "UC-someone-else"}
    setup["clock"].now += 86400
    moved, why = setup["publisher"].offer("proj1", make_clip(setup["tmp"], "e.mp4", 40_005),
                                          {}, score=9.0)
    assert moved is None and "consented channel" in why


def test_public_needs_the_typed_confirmation(setup):
    consent = setup["store"].create("proj1", choices(setup["channel"], visibility="public",
                                                     public_confirmed=True),
                                    setup["channel"], check(setup))
    assert consent["visibility"] == "public" and consent["public_confirmed"]
    entry, _ = setup["publisher"].offer("proj1", make_clip(setup["tmp"]), {}, score=9)
    assert entry["metadata"]["privacy"] == "public"


def test_revoking_withdraws_queued_uploads_and_keeps_history(setup):
    setup["store"].create("proj1", choices(setup["channel"], daily_limit=3), setup["channel"],
                          check(setup))
    done, _ = setup["publisher"].offer("proj1", make_clip(setup["tmp"], "a.mp4", 40_010), {},
                                       score=9)
    setup["queue"].process()
    waiting, _ = setup["publisher"].offer("proj1", make_clip(setup["tmp"], "b.mp4", 40_011), {},
                                          score=9)
    result = setup["publisher"].revoke("proj1")
    assert result["withdrawn"] == 1
    assert setup["queue"].get(waiting["id"])["state"] == "draft"
    assert setup["queue"].get(done["id"])["state"] == "uploaded", "history kept"
    assert setup["store"].active_for("proj1") is None
    none, why = setup["publisher"].offer("proj1", make_clip(setup["tmp"], "c.mp4", 40_012), {},
                                         score=9)
    assert none is None and why == "no consent"
    assert setup["store"].all()[0]["revoked_reason"] == "user"
