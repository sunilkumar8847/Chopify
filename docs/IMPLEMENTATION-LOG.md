# Implementation log (upgrade/chopify)

One entry per phase: what was built, how it was tested, what is still open.

## Phase 1 validation - daa7a93
Live Groq Free run on synthetic TTS speech: turbo and v3 WER 0.043, JSON
judgement valid, $0, ledger correct. Real Hindi/Hinglish cloud accuracy
pending an authorized recording.

## Phase 2 - bb8fc8a
`scheduler.py` + Studio integration. 15 engine tests, 5 Studio tests
(5 active + sixth waiting, cancel keeps clips, restart resume once). 948
passing.

## Phase 3
`project_admin.py`, dialogs, menu. 15 unit tests (real junctions), 4 new
browser tests, native dialogs removed (test asserts none). 967 passing.

## Phase 4
Synthetic stream (8 scenes): gameplay kept 100 %, breaks identified 99 %
(OCR names every break screen; without OCR breaks are still never labelled
gameplay). Real IRL recording (3490104f40dd, 178 s): analysed in 6.8 s, all
labelled "gameplay" - motion cannot tell an IRL camera from a game, so
filtering stays off; real labelled gaming recordings are needed to evaluate.
8 tests.

## Phase 5
14 ingest tests with real ffmpeg streams: OBS folder follow + file switch,
SRT listener with encoder restart (reconnect), five simultaneous OBS
sessions with the sixth refused, Studio-restart resume without duplicate
segments, ring/pin trimming, extract, disk watchdog. Browser test for the
Live panel (incl. YouTube refusal). 990 passing.

## Phase 6
Simulated 95 s OBS feed with two loud bursts: two candidates on the bursts,
two simulated quota errors retried, slow AI (4 s) while ingestion kept
going, one accepted clip rendered for real (1080x1920, <=120 s, context
before and after the moment, ready well under 5 min), one rejected, pins
released, clip kept after stopping. Real Groq used only in Phase 9.
992 passing.

## Phase 7
14 consent tests against a mocked YouTube API (explicit choices, category
check per region, upload with chosen visibility/kids/category, verification
via videos.list, duplicate prevention, score/daily-limit/channel checks,
public confirmation, revocation), Studio hook test, consent browser test.
No real uploads were made. A timing-sensitive cancel browser test now
asserts the cancel request instead of a transient notice. 1008 passing.

## Phase 8
10 discovery tests (official parameters, YouTube order, dedupe, cache,
daily limit, popular chart, link-only add, 25/29-day refresh/delete,
forget-all, no download code) + browser test. Found and fixed a Windows
file-replace race and the empty "View storage" dialog. 1019 passing. No
real API calls were made in tests.

## Phase 9
Root cause of the stalled smoke run: this connection uploads at about
36-91 KB/s (raw curl to Cloudflare), and Chopify let Studio and every worker
upload in parallel (per-process limit). Fixed with machine-wide AI slots;
workers surviving a killed Studio fixed with a Job Object.
* Real Groq Free (synthetic TTS speech, live-clip code path): 65 s audio
  transcribed in 13.0 s (140 words, en), judged in 2.0 s (7.4/10), $0.
* Crash recovery (real Studio process, killed during ingestion+analysis and
  during rendering): 16/16 checks passed (recovery-20261123-222611).
* Five-source stress, simulated AI 8 s/call: 40-minute run
  (stress-20261123-223103): 5 live sessions, sixth refused, VOD queued,
  71 candidates, 68 live clips, render never above 1, no ingestion stalls,
  no sleep, no leftovers; CPU avg 39.6 % (max 91), free RAM 4.0-5.1 GB of
  16 GB, VRAM max 414 MB, disk free 24.5 -> 16.0 GB (8.7 GB written).
  It found a real defect (a clip being rendered when Studio stopped stayed as
  a truncated final-named MP4) and two harness defects (simulated transcript
  without punctuation; cancel check sampled too slowly). After fixes, the
  12-minute targeted run (stress-20261123-232107) passed 13/13 checks: queued
  VOD completed after a slot freed, a running VOD was cancelled, all 20 clips
  valid 1080x1920 <= 120 s, clip latency 73-128 s.
* Startup check (normal `dashboard.py` command, isolated folders): 15/15.
* Full suite: 1026 passed, 0 failed, 0 skipped.
* Scene filtering stays off: no authorized real gameplay recording with labels.

## YouTube downloading (2026-11-25, owner's request)
Removed the Phase 0 switch; YouTube links download with yt-dlp (+ Node.js
runtime and yt-dlp-ejs; yt-dlp 2026.11.23). Real end-to-end run
(youtube-20261125-110903): standard watch link through download, Groq
transcription (404 words) and judging to two 1080x1920 Shorts (37.4 s,
43.8 s, decode cleanly); youtu.be and /shorts/ links downloaded and
transcribed (350 words); NASA ISS live stream ingested as a live session
(29 segments, 175 s, 720p, no reconnects). Found and fixed: Studio projects
ignored the AI routes; API Find clips defaulted to 16:9; YouTube live needs
separate video+audio HLS inputs. Tests encoding "downloads blocked" were
rewritten for the new requirement.

## Story mode, consent fix, caption fix (2026-11-25)
GTA 6 project (91d70d8bcb85, 30 min) had 0 clips: its caption transcript
had every word twice (styled caption track), and the fixed 8.0 minimum
rejected all 28 candidates (1.7-7.1). On a copy (real project read only):
13 distinct stories, scores 7.28-9.52, adaptive threshold 7.02, 4 selected
and rendered (1080x1920, 32.7-119.3 s, decode clean). Groq problems found
and fixed on the way: strict/plain JSON answers empty without low reasoning;
gpt-oss wrote sentence numbers into score fields (renamed *_score, out of
range rejected); cache keys depended on earlier parts. Groq free TPD for
gpt-oss-20b is 200,000 tokens; a 30-minute story pass uses ~40,000.
Auto-publish: real videoCategories.list (regionCode=IN) returned Gaming as
assignable after the fix.
