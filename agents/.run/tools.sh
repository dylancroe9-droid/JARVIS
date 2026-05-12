#!/usr/bin/env bash
export PATH="$HOME/.npm-global/bin:$PATH"
clear
echo '=== tools ==='
echo
cat '/Users/dylanroe/JARVIS/agents/tasks/tools.md'
echo
echo '--- starting claude (role: tools, workspace: /Users/dylanroe/JARVIS/agents/workspaces/tools, +access: /Users/dylanroe/JARVIS) ---'
cd '/Users/dylanroe/JARVIS/agents/workspaces/tools'
exec claude --add-dir '/Users/dylanroe/JARVIS' --append-system-prompt "$(cat '/Users/dylanroe/JARVIS/agents/configs/tools.md')"
