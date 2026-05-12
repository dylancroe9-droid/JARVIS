"""
Study Mode — continuously transcribes audio and builds a running summary.
"""
from __future__ import annotations
import threading
from datetime import datetime
from pathlib import Path

STUDY_DIR = Path.home() / "JARVIS" / "study_notes"
STUDY_DIR.mkdir(exist_ok=True)

class StudySession:
    def __init__(self):
        self._lock = threading.Lock()
        self._chunks: list[str] = []   # raw transcript chunks
        self._summaries: list[str] = []
        self._start = datetime.now()
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> str:
        with self._lock:
            self._active = True
            self._chunks = []
            self._summaries = []
            self._start = datetime.now()
        return "Study mode started. I'm listening — talk to your teacher."

    def stop(self) -> str:
        with self._lock:
            self._active = False
        return self._get_full_transcript()

    def add_chunk(self, text: str) -> None:
        """Called every time the STT engine produces a transcript chunk."""
        with self._lock:
            if self._active and text.strip():
                self._chunks.append(text.strip())

    def get_transcript(self) -> str:
        with self._lock:
            return " ".join(self._chunks) if self._chunks else ""

    def get_chunk_count(self) -> int:
        with self._lock:
            return len(self._chunks)

    def clear_chunks(self) -> list[str]:
        """Pop all current chunks (used when building a summary)."""
        with self._lock:
            chunks = list(self._chunks)
            self._chunks = []
            return chunks

    def add_summary(self, summary: str) -> None:
        with self._lock:
            self._summaries.append(summary)

    def save_notes(self, final_summary: str) -> str:
        """Save the full session transcript + summary to a file."""
        timestamp = self._start.strftime("%Y-%m-%d_%H-%M")
        path = STUDY_DIR / f"session_{timestamp}.md"
        with self._lock:
            full_transcript = " ".join(self._chunks)
        content = f"# Study Session — {self._start.strftime('%B %d, %Y at %I:%M %p')}\n\n"
        content += f"## Summary\n{final_summary}\n\n"
        if self._summaries:
            content += "## Section Summaries\n"
            for i, s in enumerate(self._summaries, 1):
                content += f"### Part {i}\n{s}\n\n"
        if full_transcript:
            content += f"## Full Transcript\n{full_transcript}\n"
        path.write_text(content)
        return str(path)


# Global singleton
_session = StudySession()

def get_session() -> StudySession:
    return _session
