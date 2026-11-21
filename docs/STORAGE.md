# Storage management

Chopify keeps **final clips** and deletes the files used to make them once
those clips are safely on YouTube. Final clips are never deleted
automatically.

## What is kept

| Kept until you delete it | Where |
|---|---|
| Final rendered clips | `projects/<id>/clips/*.mp4` |
| Clip title/description | `projects/<id>/clips/*.meta.json` |
| Project settings, status and history | `projects/<id>/project.json`, `job*.log` |
| Cleanup record | `projects/<id>/storage_cleanup.json` |
| Upload history, video ids, metadata | `uploads.json` in the YouTube config folder (never touched) |

A local source video outside the project folder is never deleted.

## What cleanup removes

Everything else inside the project folder: downloaded video/audio
(`audio.*`, `source*`, `recording*`, partial `.part` downloads, `sections/`,
`audio_parts/`), previews (`previews/`), transcripts and chunks
(`transcript*.json`), caption caches (`captions.*`), AI and scoring caches
(`candidates*`, `highlights_cache.json`, `segments.json`, `selection.json`,
`audio_features.npz`), clip SRT/poster/caption temporary files, and other
intermediate files.

## When cleanup runs

A project is cleaned only when **all** of these hold:

1. The project is complete (status complete, or review after watching a
   preview), with no failed clip exports.
2. It has final clips, and each one exists, is not empty and is a readable
   video (ffprobe: video stream, duration above zero).
3. Every final clip has an upload in state *uploaded* with a YouTube video id.
4. YouTube itself, asked with `videos.list?part=status` (1 quota unit per 50
   videos), reports every video as `processed`.
5. No job, recording or upload for the project is queued or running.

Failed, queued, uploading, still-processing, unverified or unreachable
uploads keep every file. If YouTube cannot be asked (no connection, API
error) the status is "uncertain" and nothing is deleted.

**Automatic:** projects created from now on clean up by themselves. After
an upload finishes Studio checks again after a minute, then every 15
minutes while YouTube is still processing. **Existing projects** are never
cleaned automatically: use the Storage panel after reviewing them.

Cleanup writes its file list to `storage_cleanup.json` before deleting
anything and records progress after each file, so an interrupted cleanup
resumes with the same list (on the next check or Studio start). Running it
again is harmless. The deletion step refuses final clips, metadata,
`project.json`, logs and anything outside the project folder, whatever the
journal says.

After cleanup the project keeps its status and its clips play in Studio as
before. It cannot export or preview again (the sources are gone); Find clips
starts it over.

## Deleting final clips

Only by hand, in Studio:

* Project page, **Rendered clips**: tick clips and press **Delete selected
  clips**, or **Delete all clips**.
* Dashboard, **Storage**: **Delete clips…** for a project.

The confirmation lists every file, its size and whether the clip was
uploaded or **not uploaded**. Nothing is deleted until you tick "I
understand" and press **Delete permanently**; the server then deletes exactly
the confirmed list. Clips with an upload still queued or running cannot be
deleted (cancel the upload first). Videos on YouTube are not changed.

## Storage panel (Dashboard)

Shows disk used by Chopify, final clips, source media, temporary files and
previews, transcripts and AI caches, what can be reclaimed now, free space,
and a row per project with the reason it is not eligible. Buttons: **Refresh
storage**, **Clean up** (one project), **Clean up all eligible**, **Delete
clips…**. Results show the files deleted and space recovered.

## Command line dry run

`GET /api/storage` (Studio) returns the same report. The report never
deletes anything.
