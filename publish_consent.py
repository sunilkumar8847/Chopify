"""Consent-based automatic publishing (Phase 7).

A *consent* lets Chopify upload clips from one project or one live session
without asking again for every clip. It is created only by an explicit user
action and records every choice the user made:

* ``channel_id`` / ``channel_title`` - the destination; it must still be the
  connected channel when each upload is queued, otherwise nothing is queued;
* ``visibility`` - private, unlisted or public. There is no default: the user
  must choose. Public additionally needs ``public_confirmed`` (the user typed
  PUBLIC in Studio);
* ``made_for_kids`` - must be given explicitly (true/false), never assumed;
* ``category_id`` - "20" (Gaming) by default, validated as assignable for the
  user's ``region_code`` through the official ``videoCategories.list``;
* ``title_template`` / ``description_template`` - previewed and approved by
  the user; rendered per clip, then cleaned (Roman script, YouTube limits);
* ``daily_limit`` - uploads per day under this consent (the upload queue's
  own daily safety limit and API quota still apply);
* ``min_score`` - only clips the AI judged at least this good.

Revoking a consent stops it immediately: clips it queued that have not
started uploading go back to draft (approval withdrawn). Uploads already
finished stay on YouTube and in the upload history.

Chopify never claims to change YouTube's own decisions: age restrictions,
"made for kids" reviews and Shorts classification are YouTube's.
Every finished upload is verified with ``videos.list`` (official API).
"""
import json
import re
import secrets
import threading
import time
from pathlib import Path

import applog
import publish_meta
import youtube_auth

FILE = "publish_consents.json"
VISIBILITIES = ("private", "unlisted", "public")
PLACEHOLDERS = ("title", "hook", "game", "stream", "date", "score")
MAX_DAILY = 20
AGE_NOTE = ("YouTube decides age restrictions and 'made for kids' reviews; Chopify cannot "
            "remove or override them.")


class ConsentError(ValueError):
    pass


def render_template(template, values):
    def sub(match):
        return str(values.get(match.group(1), "")).strip()
    text = re.sub(r"\{(" + "|".join(PLACEHOLDERS) + r")\}", sub, template or "")
    return re.sub(r"\s+\n", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def check_category(data_client, category_id, region):
    """Is the category assignable in this region? Asks videoCategories.list."""
    data = data_client._get("videoCategories", {"part": "snippet", "id": category_id,
                                                "regionCode": region})
    items = data.get("items") or []
    if not items:
        raise ConsentError(f"Category {category_id} does not exist in region {region}.")
    snippet = items[0].get("snippet") or {}
    if not snippet.get("assignable"):
        raise ConsentError(f"Category {category_id} ({snippet.get('title')}) cannot be "
                           f"assigned to videos in region {region}.")
    return {"id": category_id, "title": snippet.get("title"), "region": region,
            "checked_at": time.time()}


class ConsentStore:
    def __init__(self, directory=None, clock=time.time):
        self.dir = Path(directory or youtube_auth.config_dir())
        self.path = self.dir / FILE
        self.clock = clock
        self.lock = threading.RLock()

    def all(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, items):
        self.dir.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(items, indent=1), encoding="utf-8")
        temp.replace(self.path)

    def active_for(self, project_id):
        for item in reversed(self.all()):
            if item["project_id"] == project_id and not item.get("revoked_at"):
                return item
        return None

    def create(self, project_id, choices, channel, category_check=None):
        """Record an explicit consent. ``channel`` = the connected channel info."""
        if not isinstance(choices, dict):
            raise ConsentError("Consent details are missing.")
        if choices.get("confirm") is not True:
            raise ConsentError("Tick the consent box to allow automatic uploads.")
        visibility = choices.get("visibility")
        if visibility not in VISIBILITIES:
            raise ConsentError("Choose the visibility (private, unlisted or public). "
                               "There is no default.")
        if visibility == "public" and choices.get("public_confirmed") is not True:
            raise ConsentError("Public uploads need the extra confirmation (type PUBLIC).")
        kids = choices.get("made_for_kids")
        if not isinstance(kids, bool):
            raise ConsentError("Say whether these videos are made for kids (yes or no). "
                               "This is a legal declaration and is never assumed.")
        if not channel or not channel.get("id"):
            raise ConsentError("Connect the YouTube channel to publish to first.")
        if choices.get("channel_id") != channel["id"]:
            raise ConsentError("Select the connected channel as the destination.")
        title = str(choices.get("title_template") or "").strip()
        description = str(choices.get("description_template") or "").strip()
        if not title or len(title) > 200:
            raise ConsentError("Write a title template (up to 200 characters).")
        if len(description) > 4000:
            raise ConsentError("The description template is too long.")
        unknown = set(re.findall(r"\{(\w+)\}", title + description)) - set(PLACEHOLDERS)
        if unknown:
            raise ConsentError(f"Unknown placeholders: {', '.join(sorted(unknown))}.")
        limit = choices.get("daily_limit")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_DAILY:
            raise ConsentError(f"Daily limit must be 1 to {MAX_DAILY}.")
        score = choices.get("min_score", 7)
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 10:
            raise ConsentError("Minimum score must be 0 to 10.")
        region = str(choices.get("region_code") or "").upper()
        if not re.fullmatch(r"[A-Z]{2}", region):
            raise ConsentError("Enter your two-letter region code (for example IN or US).")
        game = str(choices.get("game") or "").strip()[:60]
        category_id = str(choices.get("category_id") or "20")
        if category_id not in publish_meta.CATEGORIES:
            raise ConsentError("Unknown YouTube category.")
        checked = category_check(category_id, region) if category_check else None
        item = {"id": secrets.token_hex(6), "project_id": project_id,
                "scope": choices.get("scope", "project"), "channel_id": channel["id"],
                "channel_title": channel.get("title"), "visibility": visibility,
                "public_confirmed": visibility == "public",
                "made_for_kids": kids, "category_id": category_id, "region_code": region,
                "category_check": checked, "title_template": title,
                "description_template": description, "daily_limit": limit,
                "min_score": float(score), "game": game, "created_at": self.clock(),
                "revoked_at": None,
                "note": AGE_NOTE}
        with self.lock:
            items = [i for i in self.all()]
            for other in items:
                if other["project_id"] == project_id and not other.get("revoked_at"):
                    other["revoked_at"] = self.clock()
                    other["revoked_reason"] = "replaced"
            items.append(item)
            self._save(items)
        applog.info("publish.consent", f"Auto-publish consent given ({visibility})",
                    project_id=project_id, consent=item["id"], channel=channel["id"])
        return item

    def revoke(self, project_id):
        with self.lock:
            items = self.all()
            found = None
            for item in items:
                if item["project_id"] == project_id and not item.get("revoked_at"):
                    item["revoked_at"] = self.clock()
                    item["revoked_reason"] = "user"
                    found = item
            self._save(items)
        if not found:
            raise ConsentError("There is no active consent for this project.")
        applog.info("publish.consent", "Auto-publish consent revoked", project_id=project_id,
                    consent=found["id"])
        return found


class AutoPublisher:
    """Turns finished clips into approved uploads under an active consent."""

    def __init__(self, store, uploads, channel, clock=time.time):
        self.store = store
        self.uploads = uploads
        self.channel = channel            # () -> connected channel {id, title} or None
        self.clock = clock
        self.lock = threading.Lock()

    def preview(self, consent_or_choices, values):
        return {"title": render_template(consent_or_choices.get("title_template"), values),
                "description": render_template(consent_or_choices.get("description_template"),
                                               values)}

    def _today(self, consent):
        day = youtube_upload_day(self.clock())
        return sum(1 for e in self.uploads.entries()
                   if e.get("consent_id") == consent["id"] and e.get("approved_at")
                   and youtube_upload_day(e["approved_at"]) == day)

    def offer(self, project_id, clip_path, values, score=None):
        """A clip is ready. Returns the queued entry, or (None, reason)."""
        consent = self.store.active_for(project_id)
        if consent is None:
            return None, "no consent"
        if score is not None and score < consent["min_score"]:
            return None, f"score {score:.1f} below {consent['min_score']:.1f}"
        values = {"game": consent.get("game", ""), **values}
        channel = self.channel()
        if not channel or channel.get("id") != consent["channel_id"]:
            applog.warning("publish.auto", "Connected channel differs from the consented one; "
                           "nothing queued", project_id=project_id)
            return None, "connected channel is not the consented channel"
        with self.lock:
            if self._today(consent) >= consent["daily_limit"]:
                return None, f"daily limit of {consent['daily_limit']} reached"
            meta = publish_meta.clean({
                "title": render_template(consent["title_template"], values),
                "description": render_template(consent["description_template"], values),
                "privacy": consent["visibility"], "category_id": consent["category_id"],
                "made_for_kids": consent["made_for_kids"],
                "tags": [values.get("game")] if values.get("game") else []},
                self.uploads.settings())
            entry = self.uploads.draft(project_id, clip_path, {**meta, "source": "consent"},
                                       {"consent_id": consent["id"]})
            if entry["state"] in ("queued", "uploading", "uploaded"):
                return entry, "already queued or uploaded"
            if entry.get("consent_id") != consent["id"]:
                self.uploads._mutate(entry["id"], lambda e: e.update(consent_id=consent["id"]))
            entry = self.uploads.approve(entry["id"], meta, confirm=True)
            applog.info("publish.auto", f"Queued under consent ({consent['visibility']})",
                        project_id=project_id, consent=consent["id"], entry=entry["id"])
            return entry, "queued"

    def revoke(self, project_id):
        consent = self.store.revoke(project_id)
        withdrawn = 0
        for entry in self.uploads.entries():
            if entry.get("consent_id") == consent["id"] and entry["state"] in ("queued",
                                                                               "quota_wait"):
                self.uploads._mutate(entry["id"], lambda e: e.update(
                    state="draft", approved_at=None, error="Consent revoked."))
                withdrawn += 1
        return {"consent": consent, "withdrawn": withdrawn}


def youtube_upload_day(ts):
    import youtube_upload
    return youtube_upload.quota_day(ts)
