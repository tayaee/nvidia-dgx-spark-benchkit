"""Tests for benchkit.artifact — atomic writes and directory layout."""

from benchkit.artifact import (
    attempt_dir,
    atomic_write_bytes,
    atomic_write_text,
    ensure_attempt_layout,
    sha256_of_file,
)


class TestAtomicWrite:
    def test_writes_text_file(self, tmp_path):
        path = tmp_path / "hello.txt"
        atomic_write_text(path, "hello world")
        assert path.read_text() == "hello world"

    def test_writes_bytes_file(self, tmp_path):
        path = tmp_path / "blob.bin"
        atomic_write_bytes(path, b"\x00\x01\x02")
        assert path.read_bytes() == b"\x00\x01\x02"

    def test_no_temp_file_left(self, tmp_path):
        path = tmp_path / "x.txt"
        atomic_write_text(path, "hi")
        # any temp file would have a different name
        temps = [p for p in tmp_path.iterdir() if p.name != "x.txt"]
        assert temps == []

    def test_overwrites_existing(self, tmp_path):
        path = tmp_path / "x.txt"
        atomic_write_text(path, "first")
        atomic_write_text(path, "second")
        assert path.read_text() == "second"


class TestSha256:
    def test_known_hash(self, tmp_path):
        p = tmp_path / "x.bin"
        p.write_bytes(b"hello")
        # sha256("hello") = 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824
        assert sha256_of_file(p) == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


class TestAttemptLayout:
    def test_creates_required_subdirs(self, tmp_path):
        a = ensure_attempt_layout(tmp_path)
        assert (a / "raw").is_dir()
        assert (a / "canonical").is_dir()
        assert (a / "logs").is_dir()
        assert (a / "checkpoints").is_dir()

    def test_creates_files(self, tmp_path):
        a = ensure_attempt_layout(tmp_path)
        assert (a / "events.jsonl").exists()
        assert (a / "state.jsonl").exists()

    def test_attempt_dir_path(self):
        p = attempt_dir("/results/exp-1", "trial-001", "attempt-001")
        assert p == "/results/exp-1/trials/trial-001/attempts/attempt-001"