#!/usr/bin/env bash
export PATH="$HOME/.npm-global/bin:$PATH"
clear
echo '=== app ==='
echo
cat '/Users/dylanroe/JARVIS/agents/tasks/app.md'
echo
echo '--- starting claude (role: app, workspace: /Users/dylanroe/JARVIS/agents/workspaces/app, +access: /Users/dylanroe/JARVIS) ---'
cd '/Users/dylanroe/JARVIS/agents/workspaces/app'
exec claude --add-dir '/Users/dylanroe/JARVIS' --append-system-prompt "$(cat '/Users/dylanroe/JARVIS/agents/configs/app.md')"
