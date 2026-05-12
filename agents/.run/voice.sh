#!/usr/bin/env bash
export PATH="$HOME/.npm-global/bin:$PATH"
clear
echo '=== voice ==='
echo
cat '/Users/dylanroe/JARVIS/agents/tasks/voice.md'
echo
echo '--- starting claude (role: voice, workspace: /Users/dylanroe/JARVIS/agents/workspaces/voice, +access: /Users/dylanroe/JARVIS) ---'
cd '/Users/dylanroe/JARVIS/agents/workspaces/voice'
exec claude --add-dir '/Users/dylanroe/JARVIS' --append-system-prompt "$(cat '/Users/dylanroe/JARVIS/agents/configs/voice.md')"
