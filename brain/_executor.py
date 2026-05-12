"""
Routes tool calls from Claude to the correct implementation.
"""

from __future__ import annotations

from rich.console import Console

from config import JARVIS_DIR

console = Console()


class ToolsExecutor:
    """Dispatches tool calls by name and returns string results."""

    def __init__(self):
        # Set by server.py so show_overlay can broadcast to the Electron frontend
        self.broadcast_callback = None
        # Set by server.py: callable(question) → base64 JPEG string | None
        self.camera_capture_callback = None
        # Set by server.py: callable(action, detail) → None  (live computer-agent HUD)
        self.computer_agent_callback = None
        # Set to True when a media action runs (YouTube, open_url, open_application).
        # server.py checks this and uses detecting mode so video audio isn't captured.
        self.media_opened = False

    def _ca(self, action: str, detail: str = "") -> None:
        """Broadcast a computer-agent action event to the HUD (best-effort)."""
        if self.computer_agent_callback:
            try:
                self.computer_agent_callback(action, detail)
            except Exception:
                pass

    def execute(self, name: str, args: dict) -> str:
        try:
            return self._dispatch(name, args)
        except Exception as exc:
            return f"[Tool error — {name}]: {exc}"

    def _dispatch(self, name: str, args: dict) -> str:  # noqa: C901
        if name == "get_calendar_events":
            try:
                from tools.calendar_tool import get_calendar_events
            except ImportError as exc:
                return f"Calendar tool unavailable: {exc}"
            return get_calendar_events(days=args.get("days", 7))

        elif name == "create_calendar_event":
            try:
                from tools.calendar_tool import create_calendar_event
            except ImportError as exc:
                return f"Calendar tool unavailable: {exc}"
            return create_calendar_event(
                title=args["title"],
                start=args["start"],
                end=args.get("end", ""),
                calendar=args.get("calendar", ""),
                notes=args.get("notes", ""),
            )

        elif name == "run_command":
            try:
                from tools.shell_tool import run_command
            except ImportError as exc:
                return f"Shell tool unavailable: {exc}"
            return run_command(
                command=args["command"],
                working_directory=args.get("working_directory"),
                timeout=args.get("timeout", 30),
            )

        elif name == "read_file":
            try:
                from tools.file_tools import read_file
            except ImportError as exc:
                return f"File tool unavailable: {exc}"
            path   = args["path"]
            offset = args.get("offset")
            limit  = args.get("limit")
            if offset is not None or limit is not None:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        all_lines = f.readlines()
                    start = int(offset or 0)
                    end   = start + int(limit) if limit else len(all_lines)
                    selected = all_lines[start:end]
                    numbered = "".join(f"{start+i+1}\t{l}" for i, l in enumerate(selected))
                    return numbered or "(empty range)"
                except Exception as exc:
                    return f"read_file error: {exc}"
            return read_file(path=path)

        elif name == "write_file":
            try:
                from tools.file_tools import write_file
            except ImportError as exc:
                return f"File tool unavailable: {exc}"
            return write_file(
                path=args["path"],
                content=args["content"],
                append=args.get("append", False),
            )

        elif name == "edit_file":
            path       = args["path"]
            old_string = args["old_string"]
            new_string = args["new_string"]
            try:
                with open(path, "r", encoding="utf-8") as f:
                    src = f.read()
                if old_string not in src:
                    return (
                        f"edit_file failed: old_string not found in {path}.\n"
                        "Read the file first to get the exact text, then retry."
                    )
                count = src.count(old_string)
                if count > 1:
                    return (
                        f"edit_file failed: old_string appears {count} times in {path}. "
                        "Include more surrounding context to make it unique."
                    )
                new_src = src.replace(old_string, new_string, 1)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_src)
                lines_changed = new_string.count("\n") - old_string.count("\n")
                return f"edit_file OK — replaced 1 occurrence in {path}. Line delta: {lines_changed:+d}"
            except Exception as exc:
                return f"edit_file error: {exc}"

        elif name == "grep_code":
            import subprocess, shlex
            pattern   = args["pattern"]
            path      = args.get("path", JARVIS_DIR)
            max_lines = int(args.get("max_lines", 40))
            try:
                cmd = ["grep", "-rn", "--include=*.py", "--include=*.js",
                       "--include=*.html", "--include=*.css", "--include=*.ts",
                       "-m", str(max_lines), pattern, path]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                out = (result.stdout or "").strip()
                if not out:
                    return f"No matches for {pattern!r} in {path}"
                lines = out.splitlines()
                if len(lines) > max_lines:
                    lines = lines[:max_lines]
                    lines.append(f"… (truncated to {max_lines} lines)")
                return "\n".join(lines)
            except Exception as exc:
                return f"grep_code error: {exc}"

        elif name == "list_directory":
            try:
                from tools.file_tools import list_directory
            except ImportError as exc:
                return f"File tool unavailable: {exc}"
            return list_directory(
                path=args.get("path"),
                show_hidden=args.get("show_hidden", False),
            )

        elif name == "search_files":
            try:
                from tools.file_tools import search_files
            except ImportError as exc:
                return f"File tool unavailable: {exc}"
            return search_files(
                pattern=args["pattern"],
                directory=args.get("directory"),
                max_results=args.get("max_results", 50),
            )

        elif name == "create_project":
            try:
                from tools.file_tools import create_project
            except ImportError as exc:
                return f"File tool unavailable: {exc}"
            return create_project(
                name=args["name"],
                project_type=args["type"],
                description=args.get("description", ""),
                directory=args.get("directory"),
            )

        elif name == "create_spreadsheet":
            try:
                from tools.file_tools import create_spreadsheet
            except ImportError as exc:
                return f"File tool unavailable: {exc}"
            return create_spreadsheet(
                filename=args["filename"],
                headers=args["headers"],
                rows=args["rows"],
                sheet_name=args.get("sheet_name", "Sheet1"),
                destination=args.get("destination", "~/Desktop"),
                open_after=args.get("open_after", True),
            )

        elif name == "manage_files":
            try:
                from tools.file_tools import manage_files
            except ImportError as exc:
                return f"File tool unavailable: {exc}"
            return manage_files(
                action=args["action"],
                directory=args.get("directory", "~/Desktop"),
                pattern=args.get("pattern", "*"),
                age_hours=float(args.get("age_hours", 0)),
                newer_than_hours=float(args.get("newer_than_hours", 0)),
                recursive=args.get("recursive", False),
            )

        elif name == "open_application":
            try:
                from tools.system_tools import open_application
            except ImportError as exc:
                return f"System tool unavailable: {exc}"
            self.media_opened = True   # flag: mic → detecting after response
            return open_application(target=args["target"])

        elif name == "app_control":
            try:
                from tools.system_tools import app_control
            except ImportError as exc:
                return f"System tool unavailable: {exc}"
            return app_control(app=args["app"], action=args["action"])

        elif name == "list_running_apps":
            try:
                from tools.system_tools import list_running_apps
            except ImportError as exc:
                return f"System tool unavailable: {exc}"
            return list_running_apps()

        elif name == "get_system_info":
            try:
                from tools.system_tools import get_system_info
            except ImportError as exc:
                return f"System tool unavailable: {exc}"
            return get_system_info()

        # ------------------------------------------------------------------ #
        # Browser tools                                                        #
        # ------------------------------------------------------------------ #

        elif name == "browser_navigate":
            try:
                from tools.browser_tool import browser_navigate
            except ImportError as exc:
                return f"Browser tool unavailable: {exc}"
            return browser_navigate(url=args["url"])

        elif name == "browser_screenshot":
            try:
                from tools.browser_tool import browser_screenshot
            except ImportError as exc:
                return f"Browser tool unavailable: {exc}"
            return browser_screenshot()   # returns list of content blocks

        elif name == "browser_click":
            try:
                from tools.browser_tool import browser_click
            except ImportError as exc:
                return f"Browser tool unavailable: {exc}"
            return browser_click(target=args["target"])

        elif name == "browser_type":
            try:
                from tools.browser_tool import browser_type
            except ImportError as exc:
                return f"Browser tool unavailable: {exc}"
            return browser_type(
                selector=args["selector"],
                text=args["text"],
                clear_first=args.get("clear_first", True),
            )

        elif name == "browser_scroll":
            try:
                from tools.browser_tool import browser_scroll
            except ImportError as exc:
                return f"Browser tool unavailable: {exc}"
            return browser_scroll(
                direction=args.get("direction", "down"),
                amount=args.get("amount", 300),
            )

        elif name == "browser_get_text":
            try:
                from tools.browser_tool import browser_get_text
            except ImportError as exc:
                return f"Browser tool unavailable: {exc}"
            return browser_get_text()

        elif name == "browser_press_key":
            try:
                from tools.browser_tool import browser_press_key
            except ImportError as exc:
                return f"Browser tool unavailable: {exc}"
            return browser_press_key(key=args["key"])

        elif name == "browser_wait":
            try:
                from tools.browser_tool import browser_wait
            except ImportError as exc:
                return f"Browser tool unavailable: {exc}"
            return browser_wait(seconds=args.get("seconds", 1.5))

        elif name == "browser_go_back":
            try:
                from tools.browser_tool import browser_go_back
            except ImportError as exc:
                return f"Browser tool unavailable: {exc}"
            return browser_go_back()

        elif name == "browser_close":
            try:
                from tools.browser_tool import close_browser
            except ImportError as exc:
                return f"Browser tool unavailable: {exc}"
            return close_browser()

        # ── Playlists ────────────────────────────────────────────────────
        elif name == "list_playlists":
            try:
                from tools.playlist_tool import list_playlists
            except ImportError as exc:
                return f"Playlist tool unavailable: {exc}"
            return list_playlists()

        elif name == "play_playlist":
            try:
                from tools.playlist_tool import play_playlist
            except ImportError as exc:
                return f"Playlist tool unavailable: {exc}"
            return play_playlist(
                name=args["name"],
                shuffle=args.get("shuffle", True),
            )

        elif name == "add_to_playlist":
            try:
                from tools.playlist_tool import add_to_playlist
            except ImportError as exc:
                return f"Playlist tool unavailable: {exc}"
            return add_to_playlist(
                playlist_name=args["playlist_name"],
                song=args.get("song", ""),
            )

        elif name == "create_playlist":
            try:
                from tools.playlist_tool import create_playlist
            except ImportError as exc:
                return f"Playlist tool unavailable: {exc}"
            return create_playlist(name=args["name"])

        elif name == "remove_from_playlist":
            try:
                from tools.playlist_tool import remove_from_playlist
            except ImportError as exc:
                return f"Playlist tool unavailable: {exc}"
            return remove_from_playlist(
                playlist_name=args["playlist_name"],
                song=args.get("song", ""),
            )

        # ── Web / search ────────────────────────────────────────────────
        elif name == "get_weather":
            try:
                from tools.web_tools import get_weather
            except ImportError as exc:
                return f"Web tool unavailable: {exc}"
            return get_weather(location=args.get("location", ""))

        elif name == "get_forecast":
            try:
                from tools.web_tools import get_forecast
            except ImportError as exc:
                return f"Web tool unavailable: {exc}"
            return get_forecast(location=args.get("location", ""), days=args.get("days", 3))

        elif name == "web_search":
            try:
                from tools.web_tools import web_search
            except ImportError as exc:
                return f"Web tool unavailable: {exc}"
            return web_search(query=args["query"])

        elif name == "youtube_search":
            try:
                from tools.web_tools import youtube_search
            except ImportError as exc:
                return f"Web tool unavailable: {exc}"
            self.media_opened = True   # flag: mic → detecting after response
            return youtube_search(query=args["query"])

        elif name == "open_url":
            try:
                from tools.web_tools import open_url
            except ImportError as exc:
                return f"Web tool unavailable: {exc}"
            self.media_opened = True   # flag: mic → detecting after response
            return open_url(url=args["url"])

        # ── Music ────────────────────────────────────────────────────────
        elif name == "play_music":
            try:
                from tools.music_tools import play_music
            except ImportError as exc:
                return f"Music tool unavailable: {exc}"
            # play_music handles all timing/polling internally and returns
            # the now-playing string when successful — no extra sleep needed.
            return play_music(query=args.get("query", ""))

        elif name == "pause_music":
            try:
                from tools.music_tools import pause_music
            except ImportError as exc:
                return f"Music tool unavailable: {exc}"
            return pause_music()

        elif name == "next_track":
            try:
                from tools.music_tools import next_track
            except ImportError as exc:
                return f"Music tool unavailable: {exc}"
            return next_track()

        elif name == "previous_track":
            try:
                from tools.music_tools import previous_track
            except ImportError as exc:
                return f"Music tool unavailable: {exc}"
            return previous_track()

        elif name == "get_now_playing":
            try:
                from tools.music_tools import get_now_playing
            except ImportError as exc:
                return f"Music tool unavailable: {exc}"
            return get_now_playing()

        elif name == "set_volume":
            try:
                from tools.music_tools import set_volume
            except ImportError as exc:
                return f"Music tool unavailable: {exc}"
            return set_volume(level=args["level"])

        elif name == "mute_volume":
            try:
                from tools.music_tools import mute_volume
            except ImportError as exc:
                return f"Music tool unavailable: {exc}"
            return mute_volume()

        # ── Timers & productivity ────────────────────────────────────────
        elif name == "set_timer":
            try:
                from tools.extras_tool import set_timer
            except ImportError as exc:
                return f"Extras tool unavailable: {exc}"
            # Accept either 'minutes' or 'seconds' (model chooses the natural one)
            minutes_arg = float(args.get("minutes") or 0)
            seconds_arg = float(args.get("seconds") or 0)
            if seconds_arg > 0:
                minutes = seconds_arg / 60
            elif minutes_arg > 0:
                minutes = minutes_arg
            else:
                minutes = 1 / 60  # default: 1 second fallback
            label   = args.get("label", "Timer")
            # Kick off HUD countdown broadcast in background thread
            try:
                import sys as _sys, uuid, threading as _th
                # server.py runs as __main__ — grab via sys.modules to avoid reimport
                _srv = _sys.modules.get("server") or _sys.modules.get("__main__")
                if _srv and hasattr(_srv, "_broadcast_timer_countdown"):
                    timer_id = str(uuid.uuid4())[:8]
                    total_secs = max(1, int(round(minutes * 60)))
                    _th.Thread(
                        target=_srv._broadcast_timer_countdown,
                        args=(timer_id, total_secs, label),
                        daemon=True
                    ).start()
                    print(f"[timer] started {total_secs}s HUD countdown id={timer_id}")
                else:
                    print(f"[timer] server module not found in sys.modules (keys with 'server': {[k for k in _sys.modules if 'server' in k.lower()]})")
            except Exception as _te:
                print(f"[timer] broadcast setup error: {_te}")
            return set_timer(minutes=minutes, label=label)

        elif name == "set_pomodoro":
            try:
                from tools.extras_tool import set_pomodoro
            except ImportError as exc:
                return f"Extras tool unavailable: {exc}"
            return set_pomodoro(
                work_minutes=args.get("work_minutes", 25),
                break_minutes=args.get("break_minutes", 5),
            )

        # ── Reminders & notes ────────────────────────────────────────────
        elif name == "add_reminder":
            try:
                from tools.extras_tool import add_reminder
            except ImportError as exc:
                return f"Extras tool unavailable: {exc}"
            return add_reminder(text=args["text"], notes=args.get("notes", ""))

        elif name == "get_reminders":
            try:
                from tools.extras_tool import get_reminders
            except ImportError as exc:
                return f"Extras tool unavailable: {exc}"
            return get_reminders()

        elif name == "quick_note":
            try:
                from tools.extras_tool import quick_note
            except ImportError as exc:
                return f"Extras tool unavailable: {exc}"
            return quick_note(text=args["text"], title=args.get("title", "JARVIS Note"))

        # ── Memory ───────────────────────────────────────────────────────
        elif name == "remember_fact":
            try:
                from brain.memory import save_fact
            except ImportError as exc:
                return f"Memory module unavailable: {exc}"
            return save_fact(fact=args["fact"])

        # ── Research / study ─────────────────────────────────────────────
        elif name == "open_tabs":
            try:
                from tools.research_tool import open_chrome_tabs
            except ImportError as exc:
                return f"Research tool unavailable: {exc}"
            return open_chrome_tabs(urls=args["urls"])

        elif name == "research_topic":
            try:
                from tools.research_tool import research_topic
            except ImportError as exc:
                return f"Research tool unavailable: {exc}"
            return research_topic(
                subject=args["subject"],
                subtopics=args.get("subtopics"),
                include_videos=args.get("include_videos", True),
                include_guides=args.get("include_guides", True),
            )

        # ── Screen vision ────────────────────────────────────────────────
        elif name == "read_chrome_tab":
            try:
                from tools.screen_tool import read_chrome_tab
            except ImportError as exc:
                return f"Screen tool unavailable: {exc}"
            return read_chrome_tab(question=args.get("question", ""))

        elif name == "take_screenshot":
            if args.get("for_control"):
                self._ca("screenshot", "Scanning screen…")
            try:
                from tools.screen_tool import take_screenshot
            except ImportError as exc:
                return f"Screen tool unavailable: {exc}"
            return take_screenshot(
                question=args.get("question", ""),
                for_control=args.get("for_control", False),
            )

        # ── Computer control ─────────────────────────────────────────────
        elif name == "click_screen":
            try:
                from tools.control_tools import click_screen
            except ImportError as exc:
                return f"Control tool unavailable: {exc}"
            x, y = args["x"], args["y"]
            btn = args.get("button", "left")
            dbl = args.get("double", False)
            self._ca("click", f"{'Double-click' if dbl else 'Click'} ({x}, {y})")
            return click_screen(x=x, y=y, button=btn, double=dbl)

        elif name == "type_text":
            try:
                from tools.control_tools import type_text
            except ImportError as exc:
                return f"Control tool unavailable: {exc}"
            preview = args["text"][:60] + ("…" if len(args["text"]) > 60 else "")
            self._ca("type", f'Typing: "{preview}"')
            return type_text(text=args["text"])

        elif name == "press_key":
            try:
                from tools.control_tools import press_key
            except ImportError as exc:
                return f"Control tool unavailable: {exc}"
            self._ca("key", f"Key: {args['keys']}")
            return press_key(keys=args["keys"])

        elif name == "scroll_screen":
            try:
                from tools.control_tools import scroll_screen
            except ImportError as exc:
                return f"Control tool unavailable: {exc}"
            direction = args.get("direction", "down")
            amount = args.get("amount", 3)
            self._ca("scroll", f"Scroll {direction} ×{amount}")
            return scroll_screen(direction=direction, amount=amount)

        elif name == "move_mouse":
            try:
                from tools.control_tools import move_mouse
            except ImportError as exc:
                return f"Control tool unavailable: {exc}"
            self._ca("move", f"Mouse → ({args['x']}, {args['y']})")
            return move_mouse(x=args["x"], y=args["y"])

        # ── Math & clipboard ─────────────────────────────────────────────
        elif name == "calculate":
            try:
                from tools.extras_tool import calculate
            except ImportError as exc:
                return f"Extras tool unavailable: {exc}"
            return calculate(expression=args["expression"])

        elif name == "get_clipboard":
            try:
                from tools.extras_tool import get_clipboard
            except ImportError as exc:
                return f"Extras tool unavailable: {exc}"
            return get_clipboard()

        elif name == "set_clipboard":
            try:
                from tools.extras_tool import set_clipboard
            except ImportError as exc:
                return f"Extras tool unavailable: {exc}"
            return set_clipboard(text=args["text"])

        elif name == "set_dark_mode":
            try:
                from tools.system_controls import set_dark_mode
            except ImportError as exc:
                return f"System controls unavailable: {exc}"
            return set_dark_mode(**args)
        elif name == "set_brightness":
            try:
                from tools.system_controls import set_brightness
            except ImportError as exc:
                return f"System controls unavailable: {exc}"
            return set_brightness(**args)
        elif name == "lock_screen":
            try:
                from tools.system_controls import lock_screen
            except ImportError as exc:
                return f"System controls unavailable: {exc}"
            return lock_screen()
        elif name == "show_notification":
            try:
                from tools.system_controls import show_notification
            except ImportError as exc:
                return f"System controls unavailable: {exc}"
            return show_notification(**args)
        elif name == "get_wifi_status":
            try:
                from tools.system_controls import get_wifi_status
            except ImportError as exc:
                return f"System controls unavailable: {exc}"
            return get_wifi_status()
        elif name == "toggle_wifi":
            try:
                from tools.system_controls import toggle_wifi
            except ImportError as exc:
                return f"System controls unavailable: {exc}"
            return toggle_wifi(**args)
        elif name == "read_url":
            try:
                from tools.system_controls import read_url
            except ImportError as exc:
                return f"System controls unavailable: {exc}"
            return read_url(**args)
        # ── Email ────────────────────────────────────────────────────────
        elif name == "get_unread_emails":
            try:
                from tools.email_tools import get_unread_emails
            except ImportError as exc:
                return f"Email tool unavailable: {exc}"
            return get_unread_emails(count=args.get("count", 10))

        elif name == "get_recent_emails":
            try:
                from tools.email_tools import get_recent_emails
            except ImportError as exc:
                return f"Email tool unavailable: {exc}"
            return get_recent_emails(count=args.get("count", 10), mailbox=args.get("mailbox", "inbox"))

        elif name == "read_email":
            try:
                from tools.email_tools import read_email
            except ImportError as exc:
                return f"Email tool unavailable: {exc}"
            return read_email(subject_keyword=args["subject_keyword"])

        elif name == "send_email":
            try:
                from tools.email_tools import send_email
            except ImportError as exc:
                return f"Email tool unavailable: {exc}"
            return send_email(to=args["to"], subject=args["subject"], body=args["body"])

        elif name == "draft_email":
            try:
                from tools.email_tools import draft_email
            except ImportError as exc:
                return f"Email tool unavailable: {exc}"
            return draft_email(to=args["to"], subject=args["subject"], body=args["body"])

        # ── Briefing & location ───────────────────────────────────────────
        elif name == "get_daily_briefing":
            try:
                from tools.briefing_tool import get_daily_briefing
            except ImportError as exc:
                return f"Briefing tool unavailable: {exc}"
            return get_daily_briefing()

        elif name == "get_location":
            try:
                from tools.briefing_tool import get_location
            except ImportError as exc:
                return f"Location tool unavailable: {exc}"
            return get_location()

        elif name == "send_imessage":
            try:
                from tools.messages_tool import send_imessage
            except ImportError as exc:
                return f"Messages tool unavailable: {exc}"
            return send_imessage(**args)
        elif name == "get_contacts":
            try:
                from tools.messages_tool import get_contacts
            except ImportError as exc:
                return f"Messages tool unavailable: {exc}"
            return get_contacts(**args)

        elif name == "get_study_transcript":
            try:
                from tools.study_tool import get_session
            except ImportError as exc:
                return f"Study tool unavailable: {exc}"
            t = get_session().get_transcript()
            return t if t else "No transcript yet."

        elif name == "save_study_notes":
            try:
                from tools.study_tool import get_session
            except ImportError as exc:
                return f"Study tool unavailable: {exc}"
            path = get_session().save_notes(args.get("summary", ""))
            return f"Notes saved to {path}"

        # ── Smart utilities ───────────────────────────────────────────────
        elif name == "translate_text":
            from tools.smart_tools import translate_text
            return translate_text(text=args["text"], target_language=args.get("target_language", "Spanish"))

        elif name == "convert_units":
            from tools.smart_tools import convert_units
            return convert_units(value=args["value"], from_unit=args["from_unit"], to_unit=args["to_unit"])

        elif name == "define_word":
            from tools.smart_tools import define_word
            return define_word(word=args["word"])

        elif name == "get_news":
            from tools.smart_tools import get_news
            return get_news(topic=args.get("topic", ""), count=args.get("count", 5))

        elif name == "set_focus_mode":
            from tools.smart_tools import set_focus_mode
            return set_focus_mode(enabled=args["enabled"], minutes=args.get("minutes", 0))

        elif name == "random_number":
            from tools.smart_tools import random_number
            return random_number(min_val=args.get("min_val", 1), max_val=args.get("max_val", 100))

        elif name == "coin_flip":
            from tools.smart_tools import coin_flip
            return coin_flip()

        elif name == "ping_site":
            from tools.smart_tools import ping_site
            return ping_site(url=args["url"])

        elif name == "web_search_results":
            from tools.web_tools import web_search_results
            return web_search_results(query=args["query"], max_results=args.get("max_results", 5))

        elif name == "deep_research":
            from tools.web_tools import deep_research
            return deep_research(query=args["query"])

        elif name == "get_wikipedia_summary":
            from tools.web_tools import get_wikipedia_summary
            return get_wikipedia_summary(query=args["query"])

        elif name == "generate_image":
            from tools.image_tool import generate_image
            return generate_image(prompt=args["prompt"])

        elif name == "get_battery_status":
            try:
                from tools.extras_tool import get_battery_status
            except ImportError as exc:
                return f"Extras tool unavailable: {exc}"
            return get_battery_status()

        # ── Camera vision ──────────────────────────────────────────────────
        elif name == "read_camera":
            question = args.get("question", "What do you see? Describe it specifically.")
            if not self.camera_capture_callback:
                return (
                    "Camera capture unavailable — JARVIS isn't connected to the camera feed. "
                    "Make sure the JARVIS Electron app is running and the camera is active."
                )
            image_data = self.camera_capture_callback()
            if not image_data:
                return (
                    "Camera frame timed out. The camera may not be active or permissions "
                    "weren't granted. Try saying this again in a moment."
                )
            # Strip data URL prefix if present (e.g. "data:image/jpeg;base64,")
            if "," in image_data:
                b64 = image_data.split(",", 1)[1]
            else:
                b64 = image_data

            # Try Claude vision (best quality)
            try:
                from config import ANTHROPIC_API_KEY
                if ANTHROPIC_API_KEY.startswith("sk-ant-"):
                    import anthropic
                    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
                    resp = client.messages.create(
                        model="claude-opus-4-5",
                        max_tokens=512,
                        messages=[{
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/jpeg",
                                        "data": b64,
                                    },
                                },
                                {"type": "text", "text": question},
                            ],
                        }],
                    )
                    return resp.content[0].text
            except Exception:
                pass

            # Groq vision fallback
            try:
                from openai import OpenAI
                from config import GROQ_API_KEY
                client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
                resp = client.chat.completions.create(
                    model="meta-llama/llama-4-scout-17b-16e-instruct",
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "image_url",
                             "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                            {"type": "text", "text": question},
                        ],
                    }],
                    max_tokens=512,
                )
                return resp.choices[0].message.content or "Vision model returned no text."
            except Exception as exc:
                return f"Vision unavailable: {exc}"

        # ── Homework auto-answer loop ──────────────────────────────────────
        elif name == "start_homework_loop":
            try:
                from tools.homework_loop import start_loop, is_running
            except ImportError as exc:
                return f"Homework loop unavailable: {exc}"
            if is_running():
                return "Auto-answer loop is already running. Say 'stop' to end it."
            return start_loop(broadcast_fn=self.broadcast_callback)

        elif name == "stop_homework_loop":
            try:
                from tools.homework_loop import stop_loop, is_running
            except ImportError as exc:
                return f"Homework loop unavailable: {exc}"
            if not is_running():
                return "No homework loop is currently running."
            return stop_loop()

        # ── AR Visual Overlays ─────────────────────────────────────────────
        elif name == "show_overlay":
            data = {
                "overlay_type": args.get("overlay_type", "timeline"),
                "title":        args.get("title", "Visual"),
                "items":        args.get("items", []),
                "theme":        args.get("theme", "cyan"),
                "model_type":   args.get("model_type", "default"),
            }
            if self.broadcast_callback:
                self.broadcast_callback({"type": "overlay", "data": data})
            count = len(data["items"])
            title = data["title"]
            if data["overlay_type"] == "blueprint":
                return (
                    f"Blueprint rendered: {title}. "
                    f"{count} spec entries — drag the 3D model to rotate it, "
                    f"click any spec on the right panel for details."
                )
            return (
                f"Here's your {title}. {count} cards — look at any one and "
                f"pinch your fingers to select it."
            )

        else:
            return f"Unknown tool: {name}"
