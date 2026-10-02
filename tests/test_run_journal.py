import json
import os
import time
from pathlib import Path

import api.run_journal as run_journal
from api.run_journal import (
    REPLAY_SKIPPED_SSE_EVENTS,
    RUN_JOURNAL_DIR_NAME,
    RunJournalWriter,
    append_run_event,
    find_run_summary,
    journal_replay_visible,
    latest_run_summary,
    read_run_events,
    read_session_run_events,
    stale_interrupted_event,
)


def test_run_journal_appends_monotonic_seq_and_reads_after_cursor(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)

    first = writer.append_sse_event("token", {"text": "hello"})
    second = writer.append_sse_event("done", {"session": {"session_id": "session_1"}})

    assert first["seq"] == 1
    assert first["event_id"] == "run_1:1"
    assert first["terminal"] is False
    assert second["seq"] == 2
    assert second["terminal"] is True
    assert second["terminal_state"] == "completed"

    journal = read_run_events("session_1", "run_1", after_seq=1, session_dir=tmp_path)
    assert [event["event"] for event in journal["events"]] == ["done"]


def test_writer_skips_metering_and_keeps_remaining_seqs_contiguous(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)

    first = writer.append_sse_event("token", {"text": "hello"})
    skipped = writer.append_sse_event("metering", {"tps": 47.3})
    second = writer.append_sse_event("done", {"session": {}})

    assert skipped is None
    assert first["seq"] == 1
    # Skipped metering must NOT consume a seq, so visible rows stay contiguous —
    # the offline-gap coverage and replay-cursor contiguity checks depend on it.
    assert second["seq"] == 2

    journal = read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert [event["event"] for event in journal["events"]] == ["token", "done"]


def test_writer_skip_is_payload_independent(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)

    assert writer.append_sse_event("metering", None) is None
    assert writer.append_sse_event("metering", {"tps": 1.0, "active": 1}) is None

    journal = read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert journal["events"] == []


def test_journal_replay_visible_predicate():
    assert REPLAY_SKIPPED_SSE_EVENTS == {"metering"}
    assert journal_replay_visible({"event": "token", "payload": {"text": "x"}}) is True
    assert journal_replay_visible({"event": "metering", "payload": {"tps": 1.0}}) is False
    # `type` fallback: metering rows are invisible under either field.
    assert journal_replay_visible({"type": "metering"}) is False
    # Unknown shapes must pass through (visible) so they can never swallow output.
    assert journal_replay_visible("not-a-dict") is True


def test_readers_keep_metering_rows(tmp_path):
    # Writers skip metering, but readers deliberately do NOT filter: cursor math
    # (the ``cursor_event_missing`` bound) and the offline-gap coverage check
    # count journal seqs and must keep seeing every row.
    append_run_event("session_1", "run_1", "token", {"text": "hello"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "metering", {"tps": 47.3}, session_dir=tmp_path)

    journal = read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert [event["event"] for event in journal["events"]] == ["token", "metering"]

    replay = read_session_run_events("session_1", after_event_id="run_1:1", session_dir=tmp_path)
    assert replay["status"] == "ok"
    assert [event["event"] for event in replay["events"]] == ["metering"]


def test_run_journal_reads_bounded_replay_window(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)

    writer.append_sse_event("token", {"text": "one"})
    writer.append_sse_event("token", {"text": "two"})
    writer.append_sse_event("token", {"text": "three"})
    writer.append_sse_event("token", {"text": "four"})

    journal = read_run_events(
        "session_1",
        "run_1",
        after_seq=1,
        max_seq=3,
        session_dir=tmp_path,
    )

    assert [event["seq"] for event in journal["events"]] == [2, 3]
    assert [event["payload"]["text"] for event in journal["events"]] == ["two", "three"]


def test_run_journal_default_fsyncs_terminal_events_only(tmp_path, monkeypatch):
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    path.parent.mkdir(parents=True)
    path.touch()
    fsync_calls = []
    monkeypatch.delenv("HERMES_WEBUI_RUN_JOURNAL_FSYNC", raising=False)
    monkeypatch.setattr("api.run_journal.os.fsync", lambda fd: fsync_calls.append(fd))

    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)

    assert fsync_calls == []

    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)

    assert len(fsync_calls) == 1


def test_run_journal_eager_fsync_mode_fsyncs_non_terminal_events(tmp_path, monkeypatch):
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    path.parent.mkdir(parents=True)
    path.touch()
    fsync_calls = []
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_FSYNC", "eager")
    monkeypatch.setattr("api.run_journal.os.fsync", lambda fd: fsync_calls.append(fd))

    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)

    assert len(fsync_calls) == 1


def test_run_journal_tolerates_malformed_lines(tmp_path):
    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json}\n")
        fh.write(json.dumps(["wrong-shape"]) + "\n")

    journal = read_run_events("session_1", "run_1", session_dir=tmp_path)

    assert len(journal["events"]) == 1
    assert len(journal["malformed"]) == 2


def test_latest_summary_and_find_run_summary_classify_terminal_state(tmp_path):
    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "cancel", {"message": "Cancelled by user"}, session_dir=tmp_path)

    summary = latest_run_summary("session_1", "run_1", session_dir=tmp_path)
    found = find_run_summary("run_1", session_dir=tmp_path)

    assert summary["terminal"] is True
    assert summary["terminal_state"] == "interrupted-by-user"
    assert summary["last_seq"] == 2
    assert found["session_id"] == "session_1"
    assert found["terminal_state"] == "interrupted-by-user"


def test_latest_summary_reuses_unchanged_journal_summary_without_reparsing(tmp_path, monkeypatch):
    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)

    first = latest_run_summary("session_1", "run_1", session_dir=tmp_path)

    monkeypatch.setattr(
        "api.run_journal._read_jsonl",
        lambda _path: (_ for _ in ()).throw(AssertionError("unchanged journal was reparsed")),
    )
    repeated = latest_run_summary("session_1", "run_1", session_dir=tmp_path)

    assert repeated == first


def test_summary_cache_invalidates_on_same_size_rewrite_with_restored_mtime(tmp_path, monkeypatch):
    # A same-inode, same-size rewrite that restores the original mtime_ns (e.g. an
    # atomic replace, or a tool that preserves mtime) must still invalidate the
    # cached summary. The signature includes st_ctime_ns — which advances on any
    # content/metadata change and cannot be forged back — so device/inode/size/
    # mtime collisions alone can never serve a stale summary. Proven at the
    # signature level (the enforced TOCTOU precondition for the cache) with a
    # deterministic stat where ONLY ctime differs.
    import api.run_journal as run_journal

    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    path = run_journal._run_path("session_1", "run_1", session_dir=tmp_path)
    real = path.stat()

    class _Stat:
        st_dev = real.st_dev
        st_ino = real.st_ino
        st_size = real.st_size
        st_mtime_ns = real.st_mtime_ns
        st_ctime_ns = real.st_ctime_ns  # overwritten per-call below

    seq = {"ctime": real.st_ctime_ns}

    def fake_stat(self, *a, **k):
        s = _Stat()
        s.st_ctime_ns = seq["ctime"]
        return s

    monkeypatch.setattr(Path, "stat", fake_stat)
    sig_before = run_journal._summary_cache_signature(path)
    # Same dev/inode/size/mtime, but a same-size in-place rewrite advanced ctime.
    seq["ctime"] = real.st_ctime_ns + 1
    sig_after = run_journal._summary_cache_signature(path)

    assert sig_after is not None and sig_before is not None
    assert sig_after != sig_before, "signature must change when only ctime advances"
    assert sig_before[:4] == sig_after[:4], "dev/inode/size/mtime_ns unexpectedly changed"


def test_summary_cache_does_not_store_result_when_journal_changes_during_read(tmp_path, monkeypatch):
    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)

    import api.run_journal as run_journal

    original_read = run_journal._read_jsonl

    def append_after_read(path):
        events, malformed = original_read(path)
        append_run_event(
            "session_1",
            "run_1",
            "cancel",
            {"message": "Cancelled by user"},
            session_dir=tmp_path,
        )
        return events, malformed

    monkeypatch.setattr(run_journal, "_read_jsonl", append_after_read)

    first = latest_run_summary("session_1", "run_1", session_dir=tmp_path)
    second = latest_run_summary("session_1", "run_1", session_dir=tmp_path)

    assert first["terminal_state"] == "completed"
    assert second["terminal_state"] == "interrupted-by-user"



def test_summary_cache_rejects_first_append_that_races_missing_journal_read(tmp_path, monkeypatch):
    import api.run_journal as run_journal

    original_read = run_journal._read_jsonl
    appended = False

    def append_after_missing_read(path):
        nonlocal appended
        events, malformed = original_read(path)
        if not appended:
            appended = True
            append_run_event(
                "session_1",
                "run_first_append",
                "done",
                {"session": {}},
                session_dir=tmp_path,
            )
        return events, malformed

    monkeypatch.setattr(run_journal, "_read_jsonl", append_after_missing_read)

    raced = latest_run_summary("session_1", "run_first_append", session_dir=tmp_path)
    refreshed = latest_run_summary("session_1", "run_first_append", session_dir=tmp_path)

    assert raced["terminal_state"] == "unknown"
    assert refreshed["terminal_state"] == "completed"
    assert refreshed["last_seq"] == 1
    assert refreshed["last_event_id"] == "run_first_append:1"


def test_terminal_state_classification_distinguishes_crash_from_user_cancel(tmp_path):
    append_run_event("session_1", "run_cancelled", "cancel", {"message": "Cancelled by user"}, session_dir=tmp_path)
    append_run_event("session_1", "run_crashed", "apperror", {"type": "interrupted"}, session_dir=tmp_path)
    append_run_event("session_1", "run_failed", "apperror", {"type": "auth_mismatch"}, session_dir=tmp_path)
    append_run_event("session_1", "run_tool_limit", "apperror", {"type": "tool_limit_reached"}, session_dir=tmp_path)
    append_run_event("session_1", "run_tool_limit_done", "done", {"terminal_state": "tool_limit_reached"}, session_dir=tmp_path)
    append_run_event("session_1", "run_unknown_done", "done", {"terminal_state": "future_unknown_state"}, session_dir=tmp_path)
    append_run_event("session_1", "run_done", "done", {"session": {}}, session_dir=tmp_path)

    assert latest_run_summary("session_1", "run_cancelled", session_dir=tmp_path)["terminal_state"] == "interrupted-by-user"
    assert latest_run_summary("session_1", "run_crashed", session_dir=tmp_path)["terminal_state"] == "interrupted-by-crash"
    assert latest_run_summary("session_1", "run_failed", session_dir=tmp_path)["terminal_state"] == "errored"
    assert latest_run_summary("session_1", "run_tool_limit", session_dir=tmp_path)["terminal_state"] == "tool_limit_reached"
    assert latest_run_summary("session_1", "run_tool_limit_done", session_dir=tmp_path)["terminal_state"] == "tool_limit_reached"
    assert latest_run_summary("session_1", "run_unknown_done", session_dir=tmp_path)["terminal_state"] == "completed"
    assert latest_run_summary("session_1", "run_done", session_dir=tmp_path)["terminal_state"] == "completed"


def test_summary_keeps_logical_terminal_state_when_stream_end_follows(tmp_path):
    append_run_event("session_1", "run_1", "apperror", {"type": "auth_mismatch"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "stream_end", {"session_id": "session_1"}, session_dir=tmp_path)

    summary = latest_run_summary("session_1", "run_1", session_dir=tmp_path)

    assert summary["terminal"] is True
    assert summary["last_event"] == "stream_end"
    assert summary["terminal_state"] == "errored"


def test_stale_interrupted_event_reports_non_terminal_journal(tmp_path, monkeypatch):
    append_run_event("session_1", "run_1", "token", {"text": "partial"}, session_dir=tmp_path)

    monkeypatch.setattr("api.run_journal._default_session_dir", lambda: tmp_path)
    event = stale_interrupted_event("session_1", "run_1")
    assert event is not None

    assert event["event"] == "apperror"
    assert event["seq"] == 2
    assert event["terminal_state"] == "lost-worker-bookkeeping"
    assert event["payload"]["type"] == "interrupted"
    assert "last journaled event" in event["payload"]["hint"]
    assert "process restarted" not in event["payload"]["message"]
    assert "lost the live worker" not in event["payload"]["message"]
    assert "live worker stopped" in event["payload"]["message"]


def test_stale_interrupted_event_skips_terminal_journal(tmp_path, monkeypatch):
    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)

    monkeypatch.setattr("api.run_journal._default_session_dir", lambda: tmp_path)

    assert stale_interrupted_event("session_1", "run_1") is None


# ── Run-journal retention ────────────────────────────────────────────────────
# Journals are debugging artifacts and used to be reclaimed only when the owning
# session was deleted, so a long-lived session grew without bound. These cover the
# three independent caps (age / count / bytes) and the two invariants that matter:
# the newest run is never removed, and pruning is throttled per session.


def _make_journal(session_root, name, *, age_seconds, size=10):
    path = session_root / name
    path.write_bytes(b"x" * size)
    mtime = time.time() - age_seconds
    os.utime(path, (mtime, mtime))
    return path


def _journal_root(tmp_path, session_id):
    root = tmp_path / "sessions" / RUN_JOURNAL_DIR_NAME / session_id
    root.mkdir(parents=True)
    return root


def test_retention_removes_aged_runs_and_keeps_fresh_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: tmp_path / "sessions")
    run_journal._RUN_JOURNAL_PRUNE_STATE.clear()
    root = _journal_root(tmp_path, "s1")

    old_a = _make_journal(root, "old_a.jsonl", age_seconds=30 * 86400)
    old_b = _make_journal(root, "old_b.jsonl", age_seconds=30 * 86400)
    fresh = _make_journal(root, "fresh.jsonl", age_seconds=0)

    removed = run_journal._prune_run_journal("s1", session_dir=tmp_path / "sessions")

    assert removed == 2
    assert not old_a.exists() and not old_b.exists()
    assert fresh.exists()


def test_retention_never_removes_the_newest_run(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: tmp_path / "sessions")
    run_journal._RUN_JOURNAL_PRUNE_STATE.clear()
    monkeypatch.setattr(run_journal, "_RUN_JOURNAL_MAX_BYTES_PER_SESSION", 100)
    monkeypatch.setattr(run_journal, "_RUN_JOURNAL_MAX_RUNS_PER_SESSION", 999)
    root = _journal_root(tmp_path, "s2")

    # Every file is over the byte cap on its own; the newest must still survive.
    for i in range(4):
        _make_journal(root, f"r{i}.jsonl", age_seconds=(4 - i) * 60, size=500)
    newest = root / "r3.jsonl"

    run_journal._prune_run_journal("s2", session_dir=tmp_path / "sessions")

    assert newest.exists(), "the run currently being written must never be pruned"
    assert sum(p.stat().st_size for p in root.glob("*.jsonl")) <= 100 * 2


def test_retention_enforces_a_run_count_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: tmp_path / "sessions")
    run_journal._RUN_JOURNAL_PRUNE_STATE.clear()
    monkeypatch.setattr(run_journal, "_RUN_JOURNAL_MAX_RUNS_PER_SESSION", 3)
    monkeypatch.setattr(run_journal, "_RUN_JOURNAL_MAX_BYTES_PER_SESSION", 10**9)
    monkeypatch.setattr(run_journal, "_RUN_JOURNAL_RETENTION_SECONDS", 10**9)
    root = _journal_root(tmp_path, "s3")

    for i in range(6):
        _make_journal(root, f"c{i}.jsonl", age_seconds=(6 - i) * 60)

    run_journal._prune_run_journal("s3", session_dir=tmp_path / "sessions")

    assert sorted(p.name for p in root.glob("*.jsonl")) == ["c3.jsonl", "c4.jsonl", "c5.jsonl"]


def test_retention_is_throttled_per_session(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: tmp_path / "sessions")
    run_journal._RUN_JOURNAL_PRUNE_STATE.clear()
    monkeypatch.setattr(run_journal, "_RUN_JOURNAL_MAX_RUNS_PER_SESSION", 1)
    root = _journal_root(tmp_path, "s4")
    for i in range(4):
        _make_journal(root, f"t{i}.jsonl", age_seconds=(4 - i) * 60)

    sessions = tmp_path / "sessions"
    assert run_journal._prune_run_journal("s4", session_dir=sessions) > 0
    # Immediately after, the throttle must short-circuit rather than rescan.
    assert run_journal._prune_run_journal("s4", session_dir=sessions) == 0


def test_retention_is_best_effort_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: tmp_path / "sessions")
    run_journal._RUN_JOURNAL_PRUNE_STATE.clear()
    monkeypatch.setattr(
        run_journal.Path, "glob", lambda self, pattern: (_ for _ in ()).throw(OSError("boom")),
    )

    assert run_journal._prune_run_journal("s5", session_dir=tmp_path / "sessions") == 0


def test_appending_a_new_run_triggers_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: tmp_path / "sessions")
    run_journal._RUN_JOURNAL_PRUNE_STATE.clear()
    monkeypatch.setattr(run_journal, "_RUN_JOURNAL_RETENTION_SECONDS", 3600)
    root = _journal_root(tmp_path, "s6")
    _make_journal(root, "stale.jsonl", age_seconds=30 * 86400)

    append_run_event("s6", "run_new", "token", {"text": "hi"}, session_dir=tmp_path / "sessions")

    assert not (root / "stale.jsonl").exists(), "append should shed aged journals"
    assert (root / "run_new.jsonl").exists()
