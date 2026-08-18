"""Tests for benchkit.events — append-only JSONL event log."""

from benchkit.events import append_event, read_events, EventLogError


class TestAppendRead:
    def test_single_event_round_trip(self, tmp_path):
        path = tmp_path / "events.jsonl"
        append_event(path, {"kind": "queued", "trial_id": "trial-001"})
        events = read_events(path)
        assert len(events) == 1
        assert events[0]["kind"] == "queued"
        assert "ts" in events[0]

    def test_multiple_events_preserve_order(self, tmp_path):
        path = tmp_path / "events.jsonl"
        for i in range(5):
            append_event(path, {"kind": "step", "i": i})
        events = read_events(path)
        assert [e["i"] for e in events] == [0, 1, 2, 3, 4]

    def test_each_event_gets_unique_ts(self, tmp_path):
        path = tmp_path / "events.jsonl"
        append_event(path, {"kind": "a"})
        append_event(path, {"kind": "b"})
        events = read_events(path)
        assert events[0]["ts"] != events[1]["ts"]

    def test_rejects_malformed_jsonl(self, tmp_path):
        path = tmp_path / "events.jsonl"
        path.write_text("not json\n")
        with __import__("pytest").raises(EventLogError):
            read_events(path)

    def test_atomic_write_does_not_leave_partial_file(self, tmp_path):
        path = tmp_path / "events.jsonl"
        append_event(path, {"kind": "queued"})
        # no temp files should be left behind
        temps = list(tmp_path.glob(".events*.tmp*"))
        assert temps == []