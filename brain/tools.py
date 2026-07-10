"""Tool definitions — kept short to minimize token usage."""


def _to_openai(tools: list) -> list:
    return [
        {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
        for t in tools
    ]


def get_openai_tools() -> list:
    """All tools — used by the full 70b model."""
    return _to_openai(TOOL_DEFINITIONS)


# Core tools for the fast 8b fallback — kept small and SAFE.
# research_topic and open_tabs are intentionally excluded: too aggressive for 8b.
# Scout model (llama-4-scout): medium-size tool set — richer coverage without payload limits.
_CORE_TOOL_NAMES = {
    "open_application", "play_music", "pause_music", "next_track",
    "web_search", "youtube_search", "take_screenshot", "read_chrome_tab", "remember_fact",
    "quick_note", "add_reminder", "get_reminders", "set_timer",
    "set_dark_mode", "send_imessage",
    "play_playlist", "add_to_playlist", "list_playlists",
    "get_weather", "get_news", "translate_text", "convert_units", "define_word",
    "set_focus_mode", "web_search_results", "get_battery_status",
    "get_unread_emails", "get_recent_emails", "send_email", "draft_email",
    "get_daily_briefing", "get_location",
    "get_calendar_events", "create_calendar_event",
    "deep_research", "generate_image",
    "get_forecast",
    "show_overlay",
    "read_camera",
    "get_wikipedia_summary",
    # Computer control — screen vision + mouse/keyboard
    "click_screen", "type_text", "press_key", "scroll_screen", "move_mouse",
    # Coding tools
    "read_file", "write_file", "edit_file", "grep_code",
    "run_command", "list_directory", "search_files",
    # File management
    "create_spreadsheet", "manage_files",
    # Homework auto-answer loop
    "start_homework_loop", "stop_homework_loop",
    # AR holographic builder
    "ar_build",
    # Self-inspection — read own code, logs, history
    "read_my_code", "search_my_code", "list_my_files",
    "read_my_logs", "read_my_history",
    "get_last_diagnostic_findings",
}

# 8b model (llama-3.1-8b-instant): SMALL tool set to avoid Groq 413 payload-too-large errors.
# Keep this to ~18 tools max. The 8b model is the last resort — cover most common voice commands.
_8B_TOOL_NAMES = {
    "open_application", "play_music", "pause_music", "next_track",
    "web_search", "youtube_search", "take_screenshot", "remember_fact",
    "quick_note", "add_reminder", "get_reminders", "set_timer",
    "send_imessage", "get_weather", "get_calendar_events",
    "set_dark_mode", "get_battery_status",
    # NOTE: create_calendar_event intentionally omitted — 8b model cannot reliably
    # extract title/start params. 70b and scout handle this in normal use.
}

# research_topic / open_tabs stay 70b-only — they open many tabs at once
# and should never fire on an ambiguous 8b interpretation.

# Minimal tools for the last-ditch retry when 8b tool JSON fails.
# Fewer tools = simpler JSON = higher success rate.
# Covers the most common voice commands that must NEVER fall through.
# NOTE: show_overlay is DELIBERATELY excluded — its schema is ~1,450 tokens
# (half the whole minimal payload) and, combined with the system prompt, it
# pushed the 8b request over Groq's 6,000 TPM limit (the 413 errors). The
# emergency path is for basic commands; overlays are handled by the bigger
# models. read_camera is likewise dropped to keep the payload lean.
_MINIMAL_TOOL_NAMES = {
    "play_music", "open_application", "web_search", "youtube_search",
    "take_screenshot", "remember_fact", "get_weather", "get_forecast",
    # Task management — must be in minimal set or "remind me" falls through to quick_note
    "quick_note", "add_reminder", "get_reminders", "set_timer",
    # Music controls — required because tool_choice can force these by name.
    # Missing them caused Groq 400 errors that triggered runaway TTS fallback.
    "pause_music", "next_track", "previous_track",
}


def get_core_tools() -> list:
    """Scout-model tool set — medium coverage for meta-llama/llama-4-scout."""
    return _to_openai([t for t in TOOL_DEFINITIONS if t["name"] in _CORE_TOOL_NAMES])


def get_8b_tools() -> list:
    """Compact tool set for llama-3.1-8b-instant — avoids Groq 413 payload errors."""
    return _to_openai([t for t in TOOL_DEFINITIONS if t["name"] in _8B_TOOL_NAMES])


def get_minimal_tools() -> list:
    """5-tool emergency set — used when 8b tool JSON generation fails."""
    return _to_openai([t for t in TOOL_DEFINITIONS if t["name"] in _MINIMAL_TOOL_NAMES])


TOOL_DEFINITIONS = [
    # ── Calendar ────────────────────────────────────────────────────────
    {"name": "get_calendar_events", "description": "Get upcoming calendar events from Apple Calendar via EventKit. Only sees calendars Dylan has synced to Apple Calendar.app. If he uses Google Calendar or Outlook without syncing, this returns 'nothing scheduled' even when events exist — in that case call read_calendar_visually instead.",
     "input_schema": {"type": "object", "properties": {"days": {"type": "integer", "default": 7}}, "required": []}},

    {"name": "read_calendar_visually",
     "description": (
         "Open Apple Calendar.app, take a screenshot, and use vision to read whatever events "
         "are actually displayed. Use this when get_calendar_events returns nothing but Dylan "
         "insists he has events, OR for any 'what's coming up / what's my next event' question. "
         "Sees all calendars displayed (Google, Outlook, iCloud, school portals) — anything "
         "the user can see in Calendar.app, JARVIS can read."
     ),
     "input_schema": {"type": "object", "properties": {
         "question": {"type": "string", "description": "Optional specific question about the calendar — defaults to 'list every event you can see this week'."}
     }, "required": []}},

    {"name": "create_calendar_event",
     "description": (
         "Create a new calendar event. "
         "Extract the event name as 'title' and the time as 'start' from the user's message. "
         "Example: 'add lunch with Jake tomorrow at noon' → title='Lunch with Jake', start='tomorrow at 12pm'. "
         "Example: 'schedule dentist for Monday at 3pm' → title='Dentist', start='Monday at 3pm'. "
         "ALWAYS provide both title and start — they are required."
     ),
     "input_schema": {"type": "object", "properties": {
         "title":    {"type": "string", "description": "Event name extracted from the user's message"},
         "start":    {"type": "string", "description": "Start date/time in natural language, e.g. 'tomorrow at 12pm'"},
         "end":      {"type": "string", "default": "", "description": "End date/time (optional)"},
         "calendar": {"type": "string", "default": "", "description": "Calendar name (optional)"},
         "notes":    {"type": "string", "default": ""},
     }, "required": ["title", "start"]}},

    # ── Shell / files ────────────────────────────────────────────────────
    {"name": "run_command", "description": "Run a shell command. Use for syntax checking (node --check), restarting services, running tests, installing packages.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "working_directory": {"type": "string"}, "timeout": {"type": "integer", "default": 30}}, "required": ["command"]}},

    {"name": "read_file", "description": "Read a file. ALWAYS read a file before editing it. Use offset/limit for large files.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "description": "Line to start from (0-based)"}, "limit": {"type": "integer", "description": "Number of lines to read"}}, "required": ["path"]}},

    {"name": "write_file", "description": "Write a complete file. Use for new files only. For existing files, prefer edit_file to avoid overwriting other code.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "append": {"type": "boolean", "default": False}}, "required": ["path", "content"]}},

    {"name": "edit_file", "description": "Surgically replace an exact string in a file. ALWAYS read the file first to get the exact text. old_string must be unique in the file — include enough surrounding context. Safer than write_file for code changes.",
     "input_schema": {"type": "object", "properties": {
         "path":       {"type": "string", "description": "Absolute path to file"},
         "old_string": {"type": "string", "description": "Exact text to replace — must be unique in file"},
         "new_string": {"type": "string", "description": "Text to replace it with"},
     }, "required": ["path", "old_string", "new_string"]}},

    {"name": "grep_code", "description": "Search for a pattern inside files — like grep. Use to find function definitions, variable names, where something is used. Returns matching lines with line numbers.",
     "input_schema": {"type": "object", "properties": {
         "pattern":   {"type": "string", "description": "Search string or regex"},
         "path":      {"type": "string", "description": "File or directory to search in"},
         "max_lines": {"type": "integer", "default": 40, "description": "Max matching lines to return"},
     }, "required": ["pattern", "path"]}},

    {"name": "list_directory", "description": "List files in a directory.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "show_hidden": {"type": "boolean", "default": False}}, "required": []}},

    {"name": "search_files", "description": "Find files by glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "directory": {"type": "string"}, "max_results": {"type": "integer", "default": 50}}, "required": ["pattern"]}},

    {"name": "create_project", "description": "Scaffold a new project directory.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "type": {"type": "string", "enum": ["python", "node", "web", "general"]}, "description": {"type": "string"}, "directory": {"type": "string"}}, "required": ["name", "type"]}},

    {"name": "create_spreadsheet",
     "description": (
         "Create a formatted spreadsheet (.xlsx) and open it in Numbers. "
         "Use this whenever Dylan asks to 'make a spreadsheet', 'build a budget', 'track expenses', "
         "'create a table', 'make a roster', or any request involving structured data in rows/columns. "
         "Generate realistic, complete data — never leave rows empty. "
         "For budgets: include category, amount, notes columns. For schedules: date, time, task, status."
     ),
     "input_schema": {"type": "object", "properties": {
         "filename":    {"type": "string", "description": "Filename without extension, e.g. 'Q3_Budget'"},
         "headers":     {"type": "array",  "items": {"type": "string"}, "description": "Column header names"},
         "rows":        {"type": "array",  "items": {"type": "array"},  "description": "Data rows — each row is an array of values matching the headers"},
         "sheet_name":  {"type": "string", "default": "Sheet1"},
         "destination": {"type": "string", "default": "~/Desktop", "description": "Where to save — default ~/Desktop"},
         "open_after":  {"type": "boolean", "default": True},
     }, "required": ["filename", "headers", "rows"]}},

    {"name": "manage_files",
     "description": (
         "Find, count, list, or delete files by pattern and/or age. "
         "Use this for: 'delete my screenshots from today', 'delete screenshots from the last 24 hours', "
         "'clean up my Downloads folder', 'remove old temp files', 'list all PDFs on my Desktop', "
         "'delete all .tmp files', 'how many screenshots did I take today'. "
         "macOS screenshots are PNG files named 'Screenshot...' saved to ~/Desktop by default. "
         "ALWAYS use action='list' first to confirm what will be deleted, then action='delete'."
     ),
     "input_schema": {"type": "object", "properties": {
         "action":           {"type": "string", "enum": ["list", "count", "delete"], "description": "What to do"},
         "directory":        {"type": "string", "default": "~/Desktop", "description": "Folder to search in"},
         "pattern":          {"type": "string", "default": "*", "description": "Glob pattern, e.g. 'Screenshot*.png', '*.tmp', '*.pdf'"},
         "newer_than_hours": {"type": "number", "default": 0, "description": "Only match files created in the last N hours (0=any)"},
         "age_hours":        {"type": "number", "default": 0, "description": "Only match files OLDER than N hours (0=any)"},
         "recursive":        {"type": "boolean", "default": False},
     }, "required": ["action"]}},

    # ── System ───────────────────────────────────────────────────────────
    {"name": "open_application", "description": "Open an app, file path, or URL.",
     "input_schema": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}},

    {"name": "app_control", "description": "Quit, focus, hide, or restart an app.",
     "input_schema": {"type": "object", "properties": {
         "app":    {"type": "string"},
         "action": {"type": "string", "enum": ["quit", "focus", "hide", "restart"]}
     }, "required": ["app", "action"]}},

    {"name": "list_running_apps", "description": "List running applications.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "get_system_info", "description": "Battery, CPU, memory, disk, uptime.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    # ── Browser (excluded from core — rarely used by voice) ──────────────
    {"name": "browser_navigate", "description": "Open a URL in Playwright browser.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},

    {"name": "browser_screenshot", "description": "Screenshot the browser page.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "browser_click", "description": "Click an element in the browser.",
     "input_schema": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}},

    {"name": "browser_type", "description": "Type into a browser input field.",
     "input_schema": {"type": "object", "properties": {"selector": {"type": "string"}, "text": {"type": "string"}, "clear_first": {"type": "boolean", "default": True}}, "required": ["selector", "text"]}},

    {"name": "browser_press_key", "description": "Press a key in the browser.",
     "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},

    {"name": "browser_get_text", "description": "Get text from the browser page.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "browser_scroll", "description": "Scroll the browser page.",
     "input_schema": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down"], "default": "down"}, "amount": {"type": "integer", "default": 300}}, "required": []}},

    {"name": "browser_go_back", "description": "Go back in browser history.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "browser_wait", "description": "Wait for a page to load.",
     "input_schema": {"type": "object", "properties": {"seconds": {"type": "number", "default": 1.5}}, "required": []}},

    {"name": "browser_close", "description": "Close the Playwright browser.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    # ── Web / search ─────────────────────────────────────────────────────
    {"name": "get_weather", "description": "Get current weather and 3-day forecast.",
     "input_schema": {"type": "object", "properties": {"location": {"type": "string", "description": "City or location, e.g. 'Miami' or 'Tokyo'. Leave empty for Atlanta."}}, "required": []}},

    {"name": "get_forecast",
     "description": "Get a detailed multi-day weather forecast for a destination — optimised for trip packing. Use when Dylan asks what to pack, what the weather will be like on a trip, or planning travel.",
     "input_schema": {"type": "object", "properties": {
         "location": {"type": "string", "description": "Destination city, e.g. 'Miami Florida' or 'Tokyo Japan'"},
         "days": {"type": "integer", "default": 3, "description": "Number of days (max 3)"}
     }, "required": ["location"]}},

    {"name": "web_search", "description": "Open a Google search in Chrome.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},

    {"name": "youtube_search", "description": "Open YouTube and search for videos. Use this whenever the user says 'pull up a video', 'show me a video', 'find a video', 'YouTube', 'watch', or wants to see video content.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},

    {"name": "open_url", "description": "Open a URL in the default browser.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},

    # ── Music ────────────────────────────────────────────────────────────
    {"name": "play_music", "description": "Play music in Apple Music. Use for 'play music', 'play [artist/song]', 'put on some music', 'play something'.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": []}},

    {"name": "pause_music", "description": "Pause Apple Music. Use for 'pause', 'stop music', 'pause the music', 'stop playing'.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "next_track", "description": "Skip to the next track in Apple Music. Use for 'skip', 'next', 'next track', 'next song', 'skip this'.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "previous_track", "description": "Go back to the previous track in Apple Music. Use for 'back', 'previous', 'previous track', 'go back', 'last song'.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "get_now_playing", "description": "Get the current song playing in Apple Music. Use when Dylan asks 'what's playing', 'what song is this', 'what track is this', 'what are you playing', 'what's the song', 'what music is on'. NOT for screen questions — use take_screenshot for those.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "set_volume", "description": "Set system volume 0–100.",
     "input_schema": {"type": "object", "properties": {"level": {"type": "integer"}}, "required": ["level"]}},

    {"name": "mute_volume", "description": "Mute system audio.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    # ── Timers & productivity ─────────────────────────────────────────────
    {"name": "set_timer",
     "description": (
         "Set a countdown timer. "
         "Use 'seconds' for short timers (under 2 min): e.g. 'set a 30 second timer' → seconds=30. "
         "Use 'minutes' for longer timers: e.g. 'set a 5 minute timer' → minutes=5. "
         "You MUST provide either seconds or minutes (not both)."
     ),
     "input_schema": {"type": "object", "properties": {
         "minutes": {"type": "number", "description": "Duration in minutes (for timers 2+ minutes long)"},
         "seconds": {"type": "number", "description": "Duration in seconds (for timers under 2 minutes)"},
         "label":   {"type": "string",  "default": "Timer", "description": "Timer label shown in HUD"}
     }, "required": []}},

    {"name": "set_pomodoro", "description": "Start a Pomodoro session.",
     "input_schema": {"type": "object", "properties": {"work_minutes": {"type": "integer", "default": 25}, "break_minutes": {"type": "integer", "default": 5}}, "required": []}},

    # ── Reminders & notes ─────────────────────────────────────────────────
    {"name": "add_reminder", "description": "Add a task to macOS Reminders. Use for 'remind me to X', 'add reminder', 'don't let me forget to X'.",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}, "notes": {"type": "string"}}, "required": ["text"]}},

    {"name": "get_reminders", "description": "List pending reminders.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "quick_note", "description": "Save a note to Apple Notes. Use when Dylan says 'quick note:', 'note:', 'jot this down', 'save a note', 'write this down'. The note text is saved verbatim — do NOT take any action described inside the note (e.g. if the note says 'remember to email Coach', ONLY save the note, do NOT send any message).",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}, "title": {"type": "string", "default": "JARVIS Note"}}, "required": ["text"]}},

    # ── Memory ───────────────────────────────────────────────────────────
    {"name": "remember_fact", "description": "Save a fact about Dylan.",
     "input_schema": {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]}},

    # ── Research / study ──────────────────────────────────────────────────
    {"name": "open_tabs", "description": "Open multiple URLs in Chrome.",
     "input_schema": {"type": "object", "properties": {
         "urls": {"type": "array", "items": {"type": "string"}}
     }, "required": ["urls"]}},

    {"name": "research_topic", "description": "Open study resources in Chrome for a subject.",
     "input_schema": {"type": "object", "properties": {
         "subject": {"type": "string"},
         "subtopics": {"type": "array", "items": {"type": "string"}},
         "include_videos": {"type": "boolean", "default": True},
         "include_guides": {"type": "boolean", "default": True}
     }, "required": ["subject"]}},

    # ── Screen vision ─────────────────────────────────────────────────────
    {"name": "read_chrome_tab",
     "description": (
         "Read the URL, title, and full text content of the user's current Chrome "
         "(or Arc/Brave/Safari) tab. Use this when the user says 'what am I looking at', "
         "'can you see my screen', 'what's in Chrome', 'look at this', 'check this page', "
         "or similar. No Screen Recording permission needed — always works."
     ),
     "input_schema": {"type": "object", "properties": {
         "question": {"type": "string", "description": "Optional question about the page content.", "default": ""}
     }, "required": []}},

    {"name": "take_screenshot",
     "description": (
         "Capture the full screen. "
         "Set for_control=true when you need to click/type next — returns EXACT pixel coordinates matching the screen. "
         "Set for_control=false for general vision questions. "
         "Use read_chrome_tab instead when the user wants to know about a web page — faster and more accurate."
     ),
     "input_schema": {"type": "object", "properties": {
         "question": {"type": "string", "default": "Describe everything on screen. For each input field, button, or clickable element — report its CENTER x,y coordinates."},
         "for_control": {"type": "boolean", "default": False,
                         "description": "true = resize to exact screen pixels so coordinates work for click_screen"}
     }, "required": []}},

    # ── Computer control ──────────────────────────────────────────────────
    {"name": "click_screen",
     "description": "Click at screen pixel coordinates. ALWAYS call take_screenshot(for_control=true) first to get accurate coordinates.",
     "input_schema": {"type": "object", "properties": {
         "x": {"type": "integer", "description": "Horizontal pixels from left"},
         "y": {"type": "integer", "description": "Vertical pixels from top"},
         "button": {"type": "string", "default": "left", "enum": ["left", "right", "middle"]},
         "double": {"type": "boolean", "default": False, "description": "Double-click"}
     }, "required": ["x", "y"]}},

    {"name": "type_text",
     "description": "Type text at current cursor position (uses clipboard for full unicode support). Click the target field first.",
     "input_schema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "Text to type — can include newlines"}
     }, "required": ["text"]}},

    {"name": "press_key",
     "description": "Press a keyboard key or shortcut. Examples: 'return', 'escape', 'tab', 'cmd+c', 'cmd+a', 'cmd+v', 'cmd+shift+t', 'space'.",
     "input_schema": {"type": "object", "properties": {
         "keys": {"type": "string", "description": "Key or combo, e.g. 'return', 'cmd+a', 'tab'"}
     }, "required": ["keys"]}},

    {"name": "scroll_screen",
     "description": "Scroll up or down on screen.",
     "input_schema": {"type": "object", "properties": {
         "direction": {"type": "string", "default": "down", "enum": ["up", "down"]},
         "amount": {"type": "integer", "default": 3, "description": "Scroll ticks — 3 = moderate, 10 = a lot"}
     }, "required": []}},

    {"name": "move_mouse",
     "description": "Move the mouse cursor to coordinates without clicking (for hover effects).",
     "input_schema": {"type": "object", "properties": {
         "x": {"type": "integer"}, "y": {"type": "integer"}
     }, "required": ["x", "y"]}},

    # ── Math & clipboard ──────────────────────────────────────────────────
    {"name": "calculate", "description": "Evaluate a math expression.",
     "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}},

    {"name": "get_clipboard", "description": "Read current clipboard.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "set_clipboard", "description": "Copy text to clipboard.",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},

    # ── System controls ───────────────────────────────────────────────────
    {"name": "set_dark_mode", "description": "Toggle macOS dark or light mode.",
     "input_schema": {"type": "object", "properties": {"enabled": {"type": "boolean"}}, "required": ["enabled"]}},

    {"name": "set_brightness", "description": "Increase or decrease screen brightness.",
     "input_schema": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": ["up", "down"]},
         "steps": {"type": "integer", "default": 3, "description": "Number of brightness steps 1-16"}
     }, "required": ["direction"]}},

    {"name": "lock_screen", "description": "Lock the Mac screen.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "show_notification", "description": "Show a macOS notification banner.",
     "input_schema": {"type": "object", "properties": {
         "title": {"type": "string"},
         "message": {"type": "string"},
         "sound": {"type": "boolean", "default": True}
     }, "required": ["title", "message"]}},

    {"name": "get_wifi_status", "description": "Get current Wi-Fi network info.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "toggle_wifi", "description": "Turn Wi-Fi on or off.",
     "input_schema": {"type": "object", "properties": {"enabled": {"type": "boolean"}}, "required": ["enabled"]}},

    {"name": "read_url", "description": "Fetch and return text content from a URL.",
     "input_schema": {"type": "object", "properties": {
         "url": {"type": "string"},
         "max_chars": {"type": "integer", "default": 4000}
     }, "required": ["url"]}},

    # ── Playlists ─────────────────────────────────────────────────────────
    {"name": "list_playlists", "description": "List all of Dylan's Apple Music playlists.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "play_playlist", "description": "Play one of Dylan's Apple Music playlists by name. Fuzzy matches.",
     "input_schema": {"type": "object", "properties": {
         "name":    {"type": "string", "description": "Playlist name or partial name"},
         "shuffle": {"type": "boolean", "default": True}
     }, "required": ["name"]}},

    {"name": "add_to_playlist", "description": "Add a song to one of Dylan's Apple Music playlists. Leave song empty to add whatever is currently playing.",
     "input_schema": {"type": "object", "properties": {
         "playlist_name": {"type": "string"},
         "song":          {"type": "string", "default": "", "description": "Song name to search for. Leave empty to add currently playing track."}
     }, "required": ["playlist_name"]}},

    {"name": "create_playlist", "description": "Create a new Apple Music playlist.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}
     }, "required": ["name"]}},

    {"name": "remove_from_playlist", "description": "Remove a song from one of Dylan's Apple Music playlists.",
     "input_schema": {"type": "object", "properties": {
         "playlist_name": {"type": "string"},
         "song":          {"type": "string", "default": "", "description": "Song to remove. Leave empty to remove currently playing track."}
     }, "required": ["playlist_name"]}},

    # ── Email ─────────────────────────────────────────────────────────────
    {"name": "get_unread_emails",
     "description": "Get unread emails from Mail.app. Shows sender, subject, and preview.",
     "input_schema": {"type": "object", "properties": {
         "count": {"type": "integer", "default": 10}
     }, "required": []}},

    {"name": "get_recent_emails",
     "description": "Get the most recent emails from inbox, read or unread.",
     "input_schema": {"type": "object", "properties": {
         "count":   {"type": "integer", "default": 10},
         "mailbox": {"type": "string",  "default": "inbox"},
     }, "required": []}},

    {"name": "read_email",
     "description": "Read the full content of a specific email by subject keyword.",
     "input_schema": {"type": "object", "properties": {
         "subject_keyword": {"type": "string"}
     }, "required": ["subject_keyword"]}},

    {"name": "send_email",
     "description": "Send an email via Mail.app.",
     "input_schema": {"type": "object", "properties": {
         "to":      {"type": "string"},
         "subject": {"type": "string"},
         "body":    {"type": "string"},
     }, "required": ["to", "subject", "body"]}},

    {"name": "draft_email",
     "description": "Open a draft email in Mail.app for review before sending.",
     "input_schema": {"type": "object", "properties": {
         "to":      {"type": "string"},
         "subject": {"type": "string"},
         "body":    {"type": "string"},
     }, "required": ["to", "subject", "body"]}},

    # ── Briefing & location ────────────────────────────────────────────────
    {"name": "get_daily_briefing",
     "description": "Get a full morning briefing: weather, today's calendar, unread emails, and top news in one shot. Use when Dylan asks what his day looks like, morning update, etc.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "get_location",
     "description": "Get Dylan's current approximate location (city/region or GPS coordinates).",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    # ── Messages ──────────────────────────────────────────────────────────
    {"name": "send_imessage", "description": "Send an iMessage or SMS to a contact. ONLY use when Dylan explicitly says 'text [person]', 'message [person]', 'send [person] a message', 'iMessage [person]'. Do NOT use just because a note or reminder mentions a person's name.",
     "input_schema": {"type": "object", "properties": {
         "contact": {"type": "string", "description": "Phone number, email, or contact name"},
         "message": {"type": "string"}
     }, "required": ["contact", "message"]}},

    {"name": "get_contacts", "description": "Search macOS Contacts by name.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},

    # ── Study mode ────────────────────────────────────────────────────────
    {"name": "get_study_transcript", "description": "Get the current study session transcript.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "save_study_notes", "description": "Save study session notes to a file.",
     "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}},

    # ── Smart utilities ───────────────────────────────────────────────────
    {"name": "translate_text", "description": "Translate text to another language.",
     "input_schema": {"type": "object", "properties": {
         "text": {"type": "string"},
         "target_language": {"type": "string", "default": "Spanish", "description": "Language name e.g. Spanish, French, Japanese"}
     }, "required": ["text"]}},

    {"name": "convert_units", "description": "Convert between units (length, weight, temp, volume, speed).",
     "input_schema": {"type": "object", "properties": {
         "value": {"type": "number"},
         "from_unit": {"type": "string"},
         "to_unit": {"type": "string"}
     }, "required": ["value", "from_unit", "to_unit"]}},

    {"name": "define_word", "description": "Get the definition of a word.",
     "input_schema": {"type": "object", "properties": {
         "word": {"type": "string"}
     }, "required": ["word"]}},

    {"name": "get_news", "description": "Get recent news headlines, optionally on a specific topic.",
     "input_schema": {"type": "object", "properties": {
         "topic": {"type": "string", "default": ""},
         "count": {"type": "integer", "default": 5}
     }, "required": []}},

    {"name": "set_focus_mode", "description": "Turn macOS Focus/Do Not Disturb on or off.",
     "input_schema": {"type": "object", "properties": {
         "enabled": {"type": "boolean"},
         "minutes": {"type": "integer", "default": 0}
     }, "required": ["enabled"]}},

    {"name": "random_number", "description": "Generate a random number between min and max.",
     "input_schema": {"type": "object", "properties": {
         "min_val": {"type": "integer", "default": 1},
         "max_val": {"type": "integer", "default": 100}
     }, "required": []}},

    {"name": "coin_flip", "description": "Flip a coin — heads or tails.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "ping_site", "description": "Check if a website is up and responding.",
     "input_schema": {"type": "object", "properties": {
         "url": {"type": "string"}
     }, "required": ["url"]}},

    {"name": "web_search_results", "description": "Search the web and return actual results (not just open Chrome).",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "max_results": {"type": "integer", "default": 5}
     }, "required": ["query"]}},

    {"name": "deep_research",
     "description": "Multi-step research — fetches multiple web sources and returns synthesized content. Use for 'research X', 'find out about X', 'look into X', or any complex factual question.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string"}
     }, "required": ["query"]}},

    {"name": "generate_image",
     "description": "Generate an image from a text description and open it. Use when Dylan asks to create, generate, draw, or make an image.",
     "input_schema": {"type": "object", "properties": {
         "prompt": {"type": "string", "description": "Detailed description of the image to generate"}
     }, "required": ["prompt"]}},

    # ── Battery & system extras ────────────────────────────────────────────
    {"name": "get_battery_status", "description": "Get MacBook battery level and charging status.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "start_study_mode", "description": "Start study mode — continuously listens and transcribes teacher speech.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "stop_study_mode", "description": "Stop study mode and summarize what was captured.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    # ── Camera vision ─────────────────────────────────────────────────────
    {"name": "read_camera",
     "description": (
         "Capture a live frame from Dylan's Mac camera and analyze what it sees. "
         "Use when Dylan holds something up to the camera, says 'what is this', "
         "'what am I holding', 'can you see this', 'look at this' — anything where "
         "a physical object is being shown to the camera. "
         "NOT for screen content (use take_screenshot or read_chrome_tab for that)."
     ),
     "input_schema": {
         "type": "object",
         "properties": {
             "question": {
                 "type": "string",
                 "description": "What to identify or analyze in the camera image",
                 "default": "What do you see? Describe it specifically."
             }
         },
         "required": []
     }},

    # ── AR Visual Overlays ────────────────────────────────────────────────
    {"name": "show_overlay",
     "description": (
         "Display an interactive visual overlay directly on Dylan's camera view. "
         "Use for timelines, concept maps, flashcard decks, blueprints, comparisons, or any "
         "visual content. Call this whenever Dylan asks for a visual, timeline, diagram, or "
         "blueprint. IMPORTANT: For historical/factual timelines, FIRST call "
         "get_wikipedia_summary to get accurate dates and events, then populate items with "
         "that REAL data — specific dates, real names, accurate facts. "
         "For NON-blueprint overlays: generate 5-7 items. label=year or short ID, title=max 5 words, "
         "detail=2-3 sentences with specific facts spoken when selected. "
         "For BLUEPRINT overlays: generate 20-24 items organized into sections using the category field. "
         "VEHICLE sections: ENGINE, PERFORMANCE, DRIVETRAIN, DIMENSIONS, WEIGHT, SUSPENSION, AERO, PRODUCTION. "
         "TECH (phone/laptop): CHIP, DISPLAY, CAMERA, BATTERY, CONNECTIVITY, STORAGE, BUILD, PRICE. "
         "ANATOMY (heart/brain): TYPE, SYSTEM, SIZE, WEIGHT, FUNCTION, key anatomical sections. "
         "SPACE (planet/star): TYPE, DIAMETER, MASS, GRAVITY, ORBIT, ATMOSPHERE, MOONS, COMPOSITION. "
         "ANIMAL: CLASS, HABITAT, SIZE, WEIGHT, SPEED, DIET, LIFESPAN, BEHAVIOR, PREDATORS, CONSERVATION. "
         "label=short spec name ALL CAPS, title=exact value with units, detail=2-3 technical sentences."
     ),
     "input_schema": {
         "type": "object",
         "properties": {
             "overlay_type": {
                 "type": "string",
                 "enum": ["timeline", "concepts", "flashcards", "comparison", "mindmap", "blueprint"],
                 "description": "Layout type: timeline=horizontal chronological, blueprint=tech specs grid, concepts=topic chips"
             },
             "title": {
                 "type": "string",
                 "description": "Title shown at top (e.g. 'World War II Timeline' or 'Bugatti Chiron — Technical Blueprint')"
             },
             "items": {
                 "type": "array",
                 "description": "For blueprints: 20-24 items across multiple categories. For other types: 5-7 items.",
                 "items": {
                     "type": "object",
                     "properties": {
                         "label":  {"type": "string", "description": "For blueprints: short spec name in CAPS (e.g. 'POWER', '0-60 MPH', 'WHEELBASE'). For others: year, letter, or key term."},
                         "title":  {"type": "string", "description": "For blueprints: exact value with units (e.g. '1,479 hp @ 6,700 rpm'). For others: event/concept name, max 5 words."},
                         "detail": {"type": "string", "description": "2-3 sentences of specific technical facts. This is spoken aloud when Dylan clicks the item."},
                         "color":  {"type": "string", "enum": ["cyan", "amber", "green", "purple", "red"],
                                    "description": "cyan=default spec, amber=key performance figure, red=extreme/record stat, purple=production/price, green=efficiency/positive"},
                         "category": {"type": "string",
                                      "description": (
                                          "Blueprint section header — group related specs under the same header. "
                                          "CRITICAL: Use categories that match the subject, NOT car categories for non-vehicles. "
                                          "VEHICLE: ENGINE, PERFORMANCE, DRIVETRAIN, DIMENSIONS, WEIGHT, SUSPENSION, AERO, PRODUCTION. "
                                          "TECH (phone/laptop/tablet): CHIP, DISPLAY, CAMERA, BATTERY, CONNECTIVITY, STORAGE, BUILD, PRICE. "
                                          "ANATOMY (heart/brain/organ): TYPE, COMPOSITION, DIMENSIONS, STRUCTURE, PHYSIOLOGY, PATHOLOGY, FACTS. "
                                          "SPACE (planet/star/moon): TYPE, DIMENSIONS, MASS, ORBIT, ATMOSPHERE, GEOLOGY, MOONS, FACTS. "
                                          "ANIMAL: CLASS, HABITAT, SIZE, SPEED, DIET, LIFESPAN, BEHAVIOR, CONSERVATION. "
                                          "Leave empty or omit for non-blueprint overlays."
                                      )}
                     },
                     "required": ["label", "title", "detail"]
                 }
             },
             "theme": {
                 "type": "string",
                 "enum": ["cyan", "amber", "green", "purple"],
                 "description": "Overall color theme",
                 "default": "cyan"
             },
             "model_type": {
                 "type": "string",
                 "enum": [
                     "bugatti", "ferrari", "lambo", "porsche", "mclaren", "koenigsegg", "mustang",
                     "supercar", "muscle_car", "car", "suv", "pickup",
                     "plane", "rocket", "helicopter", "motorcycle", "ship",
                     "phone", "tablet", "laptop", "computer", "headphones", "tv", "watch",
                     "camera", "drone", "robot", "gaming",
                     "heart", "brain", "skull", "lungs", "dna", "atom",
                     "planet", "moon", "solar_system", "galaxy", "black_hole",
                     "lion", "tiger", "wolf", "shark", "whale", "eagle", "dinosaur",
                     "horse", "elephant", "gorilla",
                     "human", "building", "house", "bridge", "stadium",
                     "weapon", "shoe", "tree", "mountain", "crystal",
                     "iron_man", "lightsaber", "batmobile", "millennium_falcon", "death_star",
                     "object", "default"
                 ],
                 "description": (
                     "3D wireframe model type — pick the MOST specific match. "
                     "VEHICLES: bugatti=Bugatti/Pagani hypercar. ferrari=Ferrari mid-engine. lambo=Lamborghini angular. "
                     "porsche=Porsche 911 fastback. mclaren=McLaren capsule. koenigsegg=Koenigsegg flat-deck. "
                     "mustang=muscle car (Mustang/Camaro/Challenger). supercar=other exotic. muscle_car=other muscle. "
                     "car=sedan/Tesla/BMW. suv=SUV/crossover. pickup=truck. plane/rocket/helicopter/motorcycle/ship. "
                     "TECH: phone=iPhone/Android/smartphone. tablet=iPad/tablet. laptop=MacBook/laptop. "
                     "computer=desktop/tower. headphones=AirPods/headphones. tv=television. watch=smartwatch. "
                     "camera=DSLR/camera. drone=UAV/drone. robot=robot/android. gaming=console/controller. "
                     "ANATOMY: heart=human heart (4 chambers). brain=brain (hemispheres). skull=skull/cranium. "
                     "lungs=lungs (bronchial tree). dna=DNA double helix. atom=atomic model (nucleus+shells). "
                     "SPACE: planet=planet (sphere+rings/atmosphere). moon=lunar surface. solar_system=orbits. "
                     "galaxy=spiral/elliptical. black_hole=event horizon+accretion disk. "
                     "ANIMALS: lion/tiger/wolf/shark/whale/eagle/dinosaur/horse/elephant/gorilla. "
                     "STRUCTURES: building=office/skyscraper. house=residential. bridge=arch/suspension. stadium. "
                     "OTHER: weapon/shoe/tree/mountain/crystal. "
                     "FICTIONAL: iron_man/lightsaber/batmobile/millennium_falcon/death_star. "
                     "human=human body/person. object=generic fallback. default=abstract wireframe. "
                     "CRITICAL: Always use the most specific type. Phone requests = phone. Heart requests = heart."
                 )
             }
         },
         "required": ["overlay_type", "title", "items"]
     }},

    {"name": "get_wikipedia_summary",
     "description": (
         "Fetch accurate, detailed information from Wikipedia for ANY topic. "
         "Call this BEFORE show_overlay when creating timelines, blueprints, or visual "
         "breakdowns of historical, scientific, technical, or educational topics. "
         "Returns real data with specific dates, names, and facts. "
         "Also call for blueprints: search '[car model] specifications' to get real specs."
     ),
     "input_schema": {
         "type": "object",
         "properties": {
             "query": {
                 "type": "string",
                 "description": "Topic to look up. Be specific: 'World War 2 timeline major events', 'Ford Mustang GT500 specifications', 'French Revolution causes and events'"
             },
         },
         "required": ["query"]
     }},

    # ── Homework / Quiz auto-answer loop ────────────────────────────────────
    {"name": "start_homework_loop",
     "description": (
         "Start the autonomous homework/quiz auto-answer loop. "
         "JARVIS will continuously screenshot the screen, read each multiple-choice question, "
         "determine the correct answer, click the right option, then move to the next question — "
         "repeating until the user says stop. "
         "Use this when the user says anything like: 'answer my homework', 'start answering questions', "
         "'do my quiz for me', 'keep answering questions', 'auto answer', 'answer all these questions'. "
         "The loop runs in the background and the user can stop it at any time by saying 'stop'."
     ),
     "input_schema": {
         "type": "object",
         "properties": {},
         "required": []
     }},

    {"name": "stop_homework_loop",
     "description": (
         "Stop the autonomous homework/quiz auto-answer loop if it is currently running. "
         "Use this when the user says 'stop', 'stop answering', 'stop the loop', 'cancel', etc. "
         "while the loop is active."
     ),
     "input_schema": {
         "type": "object",
         "properties": {},
         "required": []
     }},

    # ── AR Holographic Shape Builder ──────────────────────────────────────────
    {"name": "ar_build",
     "description": (
         "Build or modify shapes in the holographic AR display. "
         "Use when the user asks to create, add, move, resize, recolor, or clear shapes. "
         "Supports: square, circle, triangle, rectangle, hexagon, pentagon, cube, sphere, line. "
         "Coordinate system: (0,0)=center, negative Y=higher on screen, positive Y=lower. "
         "'On top of' shape A → set B.y = A.y - A.size/2 - B.size/2 - 10. "
         "'Twice the size' → size * 2. Always reference existing shape IDs from scene context."
     ),
     "input_schema": {
         "type": "object",
         "required": ["ops"],
         "properties": {
             "ops": {
                 "type": "array",
                 "description": "List of operations to apply in order",
                 "items": {
                     "type": "object",
                     "required": ["action"],
                     "properties": {
                         "action": {
                             "type": "string",
                             "enum": ["add", "modify", "remove", "clear"],
                             "description": "add=new shape, modify=change existing by id, remove=delete by id, clear=wipe all"
                         },
                         "id": {"type": "string", "description": "Shape id for modify/remove"},
                         "shape": {
                             "type": "object",
                             "description": "Shape data (for add or modify)",
                             "properties": {
                                 "id":        {"type": "string", "description": "Optional — auto-assigned if omitted on add"},
                                 "type":      {"type": "string", "enum": ["square","circle","triangle","rectangle","hexagon","pentagon","cube","sphere","line"]},
                                 "size":      {"type": "number", "description": "Edge/diameter in pixels. Default 100."},
                                 "width":     {"type": "number", "description": "Width override for rectangles"},
                                 "height":    {"type": "number", "description": "Height override for rectangles"},
                                 "x":         {"type": "number", "description": "Horizontal offset from canvas center. Default 0."},
                                 "y":         {"type": "number", "description": "Vertical offset from canvas center. Negative = up. Default 0."},
                                 "z":         {"type": "number", "description": "Depth (for future 3D). Default 0."},
                                 "color":     {"type": "string", "description": "Hex color. Default #00d4ff (cyan)."},
                                 "rotation":  {"type": "number", "description": "Degrees clockwise. Default 0."},
                                 "opacity":   {"type": "number", "description": "0–1. Default 1."},
                                 "thickness": {"type": "number", "description": "Line/border thickness px. Default 2."},
                                 "label":     {"type": "string", "description": "Optional text label shown below shape."},
                                 "length":    {"type": "number", "description": "Length for line type."}
                             }
                         }
                     }
                 }
             }
         }
     }},

    # ── Self-inspection ───────────────────────────────────────────────────
    # These tools let JARVIS investigate his OWN code, logs, and state when
    # something isn't working. Use them when Dylan asks "why did you do X",
    # "what does your Y look like", "look at your own code", or when you
    # need to explain a failure honestly instead of guessing. NOT for
    # generic file ops on Dylan's machine — use read_file / grep_code for
    # files outside the JARVIS project directory.

    {"name": "read_my_code",
     "description": (
         "Read a JARVIS source file by its path relative to the JARVIS project root "
         "(e.g. 'server.py', 'tools/diagnostics.py', 'brain/jarvis.py'). "
         "Use this when investigating your OWN behavior. "
         "Secrets (.env), binaries, and __pycache__ are blocked."
     ),
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Path relative to JARVIS root, e.g. 'brain/jarvis.py'"},
     }, "required": ["path"]}},

    {"name": "search_my_code",
     "description": (
         "Regex search across JARVIS's own source. Use to find where a function is "
         "defined, where a phrase is matched, or where a config lives. Returns "
         "matching lines in 'path:line: snippet' format."
     ),
     "input_schema": {"type": "object", "properties": {
         "pattern":     {"type": "string", "description": "Regex pattern, e.g. '_MUSIC_SKIP_WORDS'"},
         "file_glob":   {"type": "string", "default": "**/*.py", "description": "Glob to limit files, e.g. 'brain/*.py' or '**/*.js'"},
         "max_results": {"type": "integer", "default": 30},
     }, "required": ["pattern"]}},

    {"name": "list_my_files",
     "description": (
         "List files in a JARVIS directory (e.g. '.', 'tools', 'brain', "
         "'jarvis-app/renderer'). Use to navigate your own codebase."
     ),
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string", "default": ".", "description": "Directory relative to JARVIS root"},
     }, "required": []}},

    {"name": "read_my_logs",
     "description": (
         "Tail your own captured server log to see what actually happened recently "
         "— music gates, brain calls, errors, broadcasts. Use this when Dylan asks "
         "'why did you do X just now' so you answer from real evidence, not guesses."
     ),
     "input_schema": {"type": "object", "properties": {
         "lines": {"type": "integer", "default": 100, "description": "How many trailing lines to return (max 500)"},
     }, "required": []}},

    {"name": "read_my_history",
     "description": (
         "Read your own recent conversation history with Dylan. Use this when "
         "answering 'why did you say X' or 'what did we just talk about' so you "
         "ground the answer in what actually happened."
     ),
     "input_schema": {"type": "object", "properties": {
         "max_turns": {"type": "integer", "default": 20, "description": "How many recent turns to read (default 20)"},
     }, "required": []}},

    {"name": "get_last_diagnostic_findings",
     "description": (
         "Get the auto-investigation results from the most recent diagnostic. "
         "When the diagnostic detected failures, this returns the related log "
         "lines and source-file pointers that were captured automatically. "
         "Use when Dylan asks 'why did X fail', 'what's wrong with X', or "
         "'explain that failure' after he just ran a system check."
     ),
     "input_schema": {"type": "object", "properties": {
         "check_name": {"type": "string", "description": "Optional — specific check (e.g. 'cloud_brain', 'screen_watcher'). Omit to get all findings."},
     }, "required": []}},
]
