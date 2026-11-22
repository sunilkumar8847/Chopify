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
