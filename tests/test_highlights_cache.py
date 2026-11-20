"""Incremental, atomic, concurrency-safe AI judgment cache (Phase 2)."""
import json
import multiprocessing
import threading

import pytest

import highlights
from test_highlights import FakeProvider, cand, run_stage, scores


class Interrupted(BaseException):
    """Stands in for the process being killed (not an Exception subclass)."""


def forty_clips():
    candidates = [cand(i * 17, 4 + (i * 37 % 50) / 10) for i in range(200)]
    picked = highlights.shortlist(candidates, highlights.shortlist_size(len(candidates)), 3600)
    assert len(picked) == 40
    return candidates


def counting_provider(stop_after=None):
    provider = FakeProvider()
    provider.judged = 0
    inner = provider.chat_json

    def chat_json(system, user, schema=None):
        if system == highlights.SYSTEM_PROMPT:
            if stop_after is not None and provider.judged >= stop_after:
                raise Interrupted()
            provider.judged += 1
        return inner(system, user, schema)
    provider.chat_json = chat_json
    return provider


def test_interrupt_after_10_of_40_then_restart_judges_only_the_other_30(tmp_path):
    candidates = forty_clips()
    first = counting_provider(stop_after=10)
    with pytest.raises(Interrupted):
        run_stage(tmp_path, candidates, first)
    assert first.judged == 10
    assert len(highlights.load_cache(tmp_path)) == 10, "each judgment saved at once"

    second = counting_provider()
    records, info = run_stage(tmp_path, candidates, second)
    assert second.judged == 30, "only the remaining clips are judged"
    assert info["from_cache"] == 10 and info["judged"] == 40 and info["status"] == "ok"
    assert len(highlights.load_cache(tmp_path)) == 40

    third = counting_provider()
    _, info = run_stage(tmp_path, candidates, third)
    assert third.judged == 0 and info["from_cache"] == 40


def test_restart_gives_the_same_scores_as_an_uninterrupted_run(tmp_path):
    candidates = forty_clips()
    with pytest.raises(Interrupted):
        run_stage(tmp_path / "a", candidates, counting_provider(stop_after=10))
    resumed, _ = run_stage(tmp_path / "a", candidates, counting_provider())
    straight, _ = run_stage(tmp_path / "b", candidates, counting_provider())
    assert [(r["start"], r["overall"]) for r in resumed] == \
           [(r["start"], r["overall"]) for r in straight]


@pytest.fixture(autouse=True)
def _dirs(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()


def test_existing_valid_judgments_are_preserved(tmp_path):
    other = {"k-other": {**scores(7), "summary": "other video clip"}}
    highlights.update_cache(tmp_path, other)
    run_stage(tmp_path, [cand(0, 6), cand(100, 7)], FakeProvider())
    cache = highlights.load_cache(tmp_path)
    assert cache["k-other"]["summary"] == "other video clip"
    assert len(cache) == 3


def test_invalid_judgments_are_never_cached(tmp_path):
    bad = {"a": {**scores(7), "hook": 11}, "b": {"verdict": "keep"},
           "c": "not a dict", "d": {**scores(float("nan"))}}
    highlights.update_cache(tmp_path, bad)
    assert highlights.load_cache(tmp_path) == {}
    replies = [{"hook": "high"}, {"clips": []}] * 2     # malformed both tries
    _, info = run_stage(tmp_path, [cand(0, 6)], FakeProvider(replies=replies))
    assert info["judged"] == 0 and highlights.load_cache(tmp_path) == {}


def test_degenerate_answers_are_removed_even_if_interrupted(tmp_path):
    candidates = [cand(i * 100, 5 + i * 0.3) for i in range(8)]
    provider = FakeProvider(judge=lambda text: scores(0, verdict="skip"))
    calls = {"n": 0}
    inner = provider.chat_json

    def chat_json(system, user, schema=None):
        calls["n"] += 1
        if calls["n"] > 6:
            raise Interrupted()
        return inner(system, user, schema)
    provider.chat_json = chat_json
    with pytest.raises(Interrupted):
        run_stage(tmp_path, candidates, provider)
    assert highlights.load_cache(tmp_path) == {}, "identical answers are not kept"


def test_a_crash_while_writing_leaves_the_previous_cache_intact(tmp_path, monkeypatch):
    highlights.update_cache(tmp_path, {"k1": scores(6)})
    real_dump = json.dump

    def dying_dump(obj, handle, **kw):
        handle.write('{"version": 1, "entries": {"k1": {"ho')   # half written
        raise OSError("disk unplugged")
    monkeypatch.setattr(highlights.json, "dump", dying_dump)
    assert highlights.update_cache(tmp_path, {"k2": scores(7)}) is None
    monkeypatch.setattr(highlights.json, "dump", real_dump)
    assert set(highlights.load_cache(tmp_path)) == {"k1"}
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob("*.lock"))


def test_threads_never_lose_each_others_updates(tmp_path):
    def writer(n):
        for i in range(15):
            highlights.update_cache(tmp_path, {f"t{n}-{i}": scores(i % 10)})
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(highlights.load_cache(tmp_path)) == 90


def _process_writer(path, n):
    import highlights as h
    for i in range(10):
        assert h.update_cache(path, {f"p{n}-{i}": scores(i % 10)}) is not None


def test_processes_never_lose_each_others_updates(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_process_writer, args=(str(tmp_path), n)) for n in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    assert all(p.exitcode == 0 for p in procs)
    assert len(highlights.load_cache(tmp_path)) == 40


def test_a_stale_lock_from_a_crashed_process_is_recovered(tmp_path, monkeypatch):
    import os, time
    lock = tmp_path / (highlights.CACHE_FILE + ".lock")
    lock.write_text("12345")
    old = time.time() - highlights.CACHE_LOCK_STALE - 5
    os.utime(lock, (old, old))
    assert highlights.update_cache(tmp_path, {"k": scores(5)}) is not None
    assert not lock.exists()


def test_a_live_lock_times_out_without_breaking_judging(tmp_path, monkeypatch):
    monkeypatch.setattr(highlights, "CACHE_LOCK_TIMEOUT", 0.2)
    (tmp_path / (highlights.CACHE_FILE + ".lock")).write_text("other process")
    records, info = run_stage(tmp_path, [cand(0, 6), cand(100, 7)], FakeProvider())
    assert info["status"] == "ok" and info["judged"] == 2   # still judged
    assert highlights.load_cache(tmp_path) == {}
