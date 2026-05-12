#!/usr/bin/env python3
"""
JARVIS — always-on AI assistant.

    python app.py           voice + text (default)
    python app.py --text    text-only mode

  ⌘⇧J    show / hide window
  Ctrl-C  clean shutdown (also works while running)
  ✕       hide window   |   right-click ✕ → quit completely
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from typing import Optional

from rich.console import Console

console = Console()

# Ensure imports resolve from JARVIS root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import ANTHROPIC_API_KEY, GROQ_API_KEY

if not (ANTHROPIC_API_KEY or GROQ_API_KEY):
    print(
        "No brain key found. Edit ~/JARVIS/.env and add one of:\n"
        "  ANTHROPIC_API_KEY=...   (preferred — best quality)\n"
        "  GROQ_API_KEY=...        (free fallback)"
    )
    sys.exit(1)

TEXT_MODE = "--text" in sys.argv or "-t" in sys.argv

from brain.jarvis import Jarvis
from voice.speaker import Speaker
from ui.window import JARVISWindow

if not TEXT_MODE:
    from voice.audio_engine import AudioEngine, check_wake_word


# ── Sounds ───────────────────────────────────────────────────────────────────

def _beep_on() -> None:
    subprocess.Popen(
        ["afplay", "/System/Library/Sounds/Tink.aiff"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

def _beep_off() -> None:
    subprocess.Popen(
        ["afplay", "/System/Library/Sounds/Pop.aiff"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ── App ───────────────────────────────────────────────────────────────────────

class JARVISApp:

    def __init__(self) -> None:
        self.jarvis  = Jarvis()
        self.speaker: Optional[Speaker]      = Speaker() if not TEXT_MODE else None
        self.audio:   Optional[AudioEngine]  = None

        self._processing    = False
        self._interrupted   = threading.Event()
        self._shutting_down = False

        # Window is created first so safe_call is available everywhere
        self.window = JARVISWindow(
            on_input=self._handle_text_input,
            on_close=self._window_close,
            on_quit=self.shutdown,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        # Wire Ctrl-C so it works even inside Tk's mainloop
        signal.signal(signal.SIGINT, lambda *_: self._schedule_shutdown())
        self._tk_sigint_poller()

        self._start_hotkey_daemon()

        if not TEXT_MODE:
            self.audio = AudioEngine(
                on_transcription=self._on_transcription,
                on_barge_in=self._on_barge_in,
            )

        console.print("JARVIS running.  ⌘⇧J toggle  |  Ctrl-C or right-click ✕ to quit.")
        self.window.after(200, self._show_and_greet)
        self.window.mainloop()

    def _tk_sigint_poller(self) -> None:
        """Tk's event loop swallows Python signals. Poll every 150 ms to fix that."""
        def _poll():
            if not self._shutting_down:
                self.window.after(150, _poll)
        self.window.after(150, _poll)

    def _schedule_shutdown(self) -> None:
        """Thread-safe: queue shutdown on the Tk main thread."""
        self.window.after(0, self.shutdown)

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        console.print("[dim]Shutting down…[/dim]")
        if self.speaker:
            self.speaker.stop()
        if self.audio:
            self.audio.close()
        try:
            self.window.quit()
            self.window.destroy()
        except Exception:
            pass
        sys.exit(0)

    def _window_close(self) -> None:
        """✕ left-click — hide the window, keep running."""
        self.window.hide()

    # ── Hotkey daemon (pynput in isolated subprocess) ─────────────────────────

    def _start_hotkey_daemon(self) -> None:
        daemon = os.path.join(os.path.dirname(__file__), "ui", "hotkey_daemon.py")
        if not os.path.exists(daemon):
            return

        def _watch() -> None:
            import time
            while not self._shutting_down:
                try:
                    proc = subprocess.Popen(
                        [sys.executable, daemon],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                    )
                    for line in (proc.stdout or []):
                        if "TOGGLE" in line:
                            self.window.safe_call(self.window.toggle_visibility)
                    proc.wait()
                except Exception:
                    pass
                time.sleep(1)

        threading.Thread(target=_watch, daemon=True, name="hotkey-daemon").start()

    # ── Text input (from UI entry box) ────────────────────────────────────────

    def _handle_text_input(self, text: str) -> None:
        self._interrupt()
        if not self._processing:
            threading.Thread(
                target=self._process,
                args=(text,),
                daemon=True,
                name="process-text",
            ).start()

    # ── Voice callbacks ───────────────────────────────────────────────────────

    def _on_transcription(self, kind: str, text: str) -> None:
        """Called from the STT thread."""
        if kind == "wake":
            triggered, inline_cmd = check_wake_word(text)
            if not triggered:
                return
            self._interrupt()
            _beep_on()
            self.window.safe_call(self.window.set_state, "listening")
            if inline_cmd:
                self._submit(inline_cmd)
            else:
                # Switch to command recording mode
                if self.audio:
                    self.audio.start_recording()

        elif kind == "command":
            if text.strip():
                self._submit(text)
            else:
                _beep_off()
                self.window.safe_call(self.window.set_state, "idle")
                if self.audio:
                    self.audio.start_detecting()

    def _on_barge_in(self) -> None:
        """Called from audio callback when user speaks loudly during JARVIS speech."""
        console.print("[dim]Barge-in.[/dim]")
        self._interrupt()          # stops speaker + waits for _process to finish
        _beep_on()
        self.window.safe_call(self.window.set_state, "listening")
        if self.audio:
            self.audio.start_recording()    # _interrupt ensures we arrive here after finally

    # ── Processing ────────────────────────────────────────────────────────────

    def _submit(self, text: str) -> None:
        threading.Thread(
            target=self._process,
            args=(text,),
            daemon=True,
            name="process",
        ).start()

    def _process(self, text: str) -> None:
        if self._processing:
            return
        self._processing  = True
        self._interrupted.clear()
        if self.speaker:
            self.speaker.resume()

        try:
            self.window.add_user_message(text)
            self.window.safe_call(self.window.set_state, "thinking")
            self.window.safe_call(self.window.start_jarvis_message)

            gen = self.jarvis.chat(text)

            if self.speaker:
                # _window_gen enables barge-in right before speech starts
                self.speaker.stream_speak(self._window_gen(gen))
            else:
                for chunk in gen:
                    if self._interrupted.is_set():
                        break
                    self.window.safe_call(self.window.add_jarvis_chunk, chunk)

            self.window.safe_call(self.window.set_state, "idle")

        except Exception as exc:
            console.print(f"[yellow]Error: {exc}[/yellow]")
            self.window.safe_call(self.window.add_jarvis_chunk, f"\n[Error: {exc}]")
            self.window.safe_call(self.window.set_state, "idle")

        finally:
            self._processing = False
            # Return to passive wake-word detection.
            # If barge-in fired, _on_barge_in() will call start_recording()
            # AFTER _interrupt() sees _processing=False here — so that call
            # runs after us and is the final state (RECORDING wins).
            if self.audio and self.audio._state != AudioEngine.RECORDING:
                self.audio.start_detecting()

    def _window_gen(self, gen):
        """
        Wraps the LLM generator: pumps chunks to the window.
        Audio stays in DETECTING during speech so saying "JARVIS"
        interrupts without the mic picking up speaker output.
        """
        self.window.safe_call(self.window.set_state, "speaking")
        for chunk in gen:
            if self._interrupted.is_set():
                break
            self.window.safe_call(self.window.add_jarvis_chunk, chunk)
            yield chunk

    def _interrupt(self) -> None:
        """
        Stop current speech immediately and wait for _process to exit.
        Safe to call from any thread.
        """
        if self.speaker:
            self.speaker.stop()
        self._interrupted.set()
        # Busy-wait up to 400 ms for the processing thread to notice
        for _ in range(20):
            if not self._processing:
                break
            import time; time.sleep(0.02)
        self._interrupted.clear()

    # ── Greeting ──────────────────────────────────────────────────────────────

    def _show_and_greet(self) -> None:
        self.window.show()
        if self.audio:
            self.audio.start_detecting()
        threading.Thread(target=self._greet, daemon=True, name="greet").start()

    def _greet(self) -> None:
        self._processing = True
        self._interrupted.clear()
        if self.speaker:
            self.speaker.resume()
        try:
            self.window.safe_call(self.window.set_state, "thinking")
            self.window.safe_call(self.window.start_jarvis_message)
            prompt = (
                "Greet me in one sentence. Include today's date. "
                "Mention I can speak or type."
            )
            gen = self.jarvis.chat(prompt)
            if self.speaker:
                self.speaker.stream_speak(self._window_gen(gen))
            else:
                for chunk in gen:
                    if self._interrupted.is_set():
                        break
                    self.window.safe_call(self.window.add_jarvis_chunk, chunk)
        except Exception as exc:
            console.print(f"[yellow]Greeting failed: {exc}[/yellow]")
        finally:
            self._processing = False
            if self.audio and self.audio._state != AudioEngine.RECORDING:
                self.audio.start_detecting()
            self.window.safe_call(
                self.window.set_state,
                "idle" if TEXT_MODE else "listening",
            )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    JARVISApp().run()
