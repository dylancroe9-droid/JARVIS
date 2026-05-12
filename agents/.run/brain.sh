#!/usr/bin/env bash
export PATH="$HOME/.npm-global/bin:$PATH"
clear
echo '=== brain ==='
echo
cat '/Users/dylanroe/JARVIS/agents/tasks/brain.md'
echo
echo '--- starting claude (role: brain, workspace: /Users/dylanroe/JARVIS/agents/workspaces/brain, +access: /Users/dylanroe/JARVIS) ---'
cd '/Users/dylanroe/JARVIS/agents/workspaces/brain'
exec claude --add-dir '/Users/dylanroe/JARVIS' --append-system-prompt "$(cat '/Users/dylanroe/JARVIS/agents/configs/brain.md')"
