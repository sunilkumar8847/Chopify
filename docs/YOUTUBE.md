# Publishing to YouTube Shorts

Chopify can upload your exported vertical clips to your own YouTube channel.
It never uploads anything you have not approved, clip by clip.

## One-time setup (about 10 minutes)

Google requires every app that uploads videos to use its own OAuth client.
Chopify does not ship one, so you create a free one in your Google account:

1. Open <https://console.cloud.google.com/> and create a project (any name).
2. **APIs & Services → Library**: enable **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen**: choose *External*, fill in the
   app name and your email, and add yourself under **Test users**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   application type **Desktop app**. Download the JSON file
   (`client_secret_….json`).
5. In Studio open a project, find the **YouTube** panel, paste the path of
   that file and press **Add client file**. Then press **Connect YouTube
   channel** and sign in with the Google account that owns the channel.

From the command line instead:

```
python youtube_auth.py client D:\path\client_secret_XXXX.json
python youtube_auth.py connect
python youtube_auth.py status
```

While the consent screen is in *Testing* mode Google expires the sign-in
after 7 days; Chopify then asks you to connect again. Publishing the consent
screen (or Google's verification for a public app) removes that limit.

Since the YouTube Data API revision of **November 2, 2026**, videos uploaded
from unverified API projects are **no longer restricted to private viewing
mode** (this reverses the policy announced on July 28, 2020). Chopify still
uploads as **private** unless you choose otherwise for a clip: it always sends
the privacy setting explicitly, because YouTube makes a video *public* when an
upload does not specify one.

## Publishing a clip

1. Export the project in **9:16** (Shorts must be vertical or square and at
   most 3 minutes; Chopify checks both).
2. On an exported clip press **Publish…**. Chopify drafts a title,
   description, hashtags and tags with the local AI model (or a simple draft
   if the model is off). Edit anything; **Save draft** uploads nothing.
3. Press **Upload to YouTube…** and confirm. Only then is the clip queued.
   New uploads are **private** by default; Chopify never makes a video public
   on its own. Change the default or a single clip's privacy yourself.
4. Progress, success (with a link) or the reason for a failure appear on the
   clip. **Retry** resumes a failed upload; **Withdraw** removes one that has
   not started uploading.

**Thumbnails.** Since the **November 3, 2026** revision, YouTube's
`thumbnails.set` method accepts custom thumbnails for Shorts once the video
has finished processing (50 quota units each, from the general bucket).
**Chopify does not set custom thumbnails yet**: YouTube picks one, and you can
change it in YouTube Studio. (Chopify still saves a `.png` poster next to each
clip that you can upload there.)

## Quota and limits

YouTube uses a granular quota system (since June 2026). Each Google Cloud
project gets, by default, per day:

| Bucket | Default per day | Cost | Used by Chopify for |
|---|---|---|---|
| `videos.insert` (uploads) | 100 calls | 1 per call | starting each upload |
| `search.list` | 100 calls | 1 per call | not used |
| everything else | 10,000 units | per method (`channels.list` = 1) | showing the connected channel |

All buckets reset at **midnight Pacific Time**. Every request counts, also
failed ones and retries, so Chopify counts each `videos.insert` attempt in
the upload bucket *before* sending it. Sending the video bytes to the upload
session and resuming an interrupted upload are part of that single call and
are not counted again.

Separately, Chopify has its **own safety limit of 6 uploads per day**
(change it in the YouTube panel). It counts uploads that actually started
and is independent of YouTube's quota: with the defaults the safety limit,
not YouTube, decides how many clips go up per day.

The YouTube panel shows all three counters and when they reset. Chopify never
starts an upload that would exceed any of them; the clip shows "Waiting for
YouTube quota" and continues automatically after the reset. If YouTube itself
reports an exhausted quota, or that the channel reached its daily upload
limit (`uploadLimitExceeded`), the clip waits and continues automatically
(after the next reset, or after 24 hours for the channel limit). If Google
granted your project a higher quota, set the new numbers in the YouTube
panel.

Official references:
[quota costs](https://developers.google.com/youtube/data/determine_quota_cost),
[videos.insert](https://developers.google.com/youtube/data/reference/rest/v3/videos/insert),
[revision history](https://developers.google.com/youtube/data/revision_history).

## Safety

* Credentials are never stored in the repository. Chopify keeps the client
  file and the sign-in **encrypted with Windows DPAPI** (readable only by your
  Windows user) in `%APPDATA%\Chopify`, or in `$CHOPIFY_CONFIG` if set.
* Only two permissions are requested: upload videos, and read your channel
  name.
* **Disconnect** revokes Chopify's access at Google and deletes the sign-in.
* Uploads are resumable: after a crash or a network drop Chopify asks YouTube
  how much arrived and continues. Each clip is fingerprinted, so the same
  video is never uploaded twice.
