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
Unverified projects may also have uploads restricted to *private*: keep the
default privacy until your project is verified.

## Publishing a clip

1. Export the project in **9:16** (Shorts must be vertical or square and at
   most 3 minutes; Chopify checks both).
2. On an exported clip press **Publish…**. Chopify drafts a title,
   description, hashtags and tags with the local AI model (or a simple draft
   if the model is off). Edit anything; **Save draft** uploads nothing.
3. Press **Upload to YouTube…** and confirm. Only then is the clip queued.
   New uploads are **private** by default.
4. Progress, success (with a link) or the reason for a failure appear on the
   clip. **Retry** resumes a failed upload; **Withdraw** removes one that has
   not started uploading.

## Quota and limits

YouTube gives each Google Cloud project 10,000 API units per day by default,
and one upload costs 1,600 units: about **6 uploads a day**. The day resets at
midnight Pacific Time. Chopify counts its own use, never starts an upload
that would exceed the limits set in the YouTube panel, and parks clips as
"Waiting for YouTube quota" until the next day. If YouTube itself reports the
quota or the channel's daily upload limit, the clip waits and continues
automatically.

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
