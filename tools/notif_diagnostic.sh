#!/bin/bash
# Diagnostic: watch ALL notification-related events for 60 seconds.
# Run this, then trigger a Snapchat notification on your Mac. We'll see
# exactly which process/subsystem Snap uses so we can capture it.

OUT="/tmp/jarvis_notif_diag.log"
echo "Capturing notification events for 60 seconds..."
echo "Trigger a Snapchat notification NOW (and any other apps you want to test)."
echo "Output will be saved to: $OUT"
echo ""

/usr/bin/log stream \
  --predicate '(process == "usernoted") OR (process == "NotificationCenter") OR (process == "apsd") OR (process == "Snapchat") OR (subsystem CONTAINS[c] "notif")' \
  --info --style ndjson > "$OUT" 2>&1 &

LOGPID=$!
sleep 60
kill $LOGPID 2>/dev/null
wait 2>/dev/null

echo ""
echo "=== UNIQUE PROCESSES THAT EMITTED EVENTS ==="
python3 -c "
import json, sys
seen = {}
with open('$OUT') as f:
    for line in f:
        try:
            ev = json.loads(line)
            proc = ev.get('processImagePath', '').split('/')[-1] or '?'
            sub  = ev.get('subsystem') or '?'
            key = f'{proc} | subsystem={sub}'
            seen[key] = seen.get(key, 0) + 1
        except: pass
for k, v in sorted(seen.items(), key=lambda x: -x[1])[:30]:
    print(f'  {v:5d} × {k}')
"

echo ""
echo "=== EVENTS MENTIONING SNAP / SNAPCHAT ==="
grep -i 'snap' "$OUT" | head -10

echo ""
echo "=== ALL BUNDLE IDs SEEN IN DELIVERY EVENTS ==="
python3 -c "
import json, re
bundles = {}
PAT = re.compile(r'app:\"([\w.\-]+)\"')
with open('$OUT') as f:
    for line in f:
        try:
            ev = json.loads(line)
            msg = ev.get('eventMessage', '')
            m = PAT.search(msg)
            if m:
                bundles[m.group(1)] = bundles.get(m.group(1), 0) + 1
        except: pass
for k, v in sorted(bundles.items(), key=lambda x: -x[1]):
    print(f'  {v:3d} × {k}')
"
