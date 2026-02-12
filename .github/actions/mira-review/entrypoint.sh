#!/bin/bash
set -e

# Extract PR URL from GitHub event
PR_URL=$(python3 -c "
import json, os
event_path = os.environ.get('GITHUB_EVENT_PATH', '')
if event_path:
    with open(event_path) as f:
        event = json.load(f)
    pr = event.get('pull_request', {})
    print(pr.get('html_url', ''))
")

if [ -z "$PR_URL" ]; then
    echo "Error: Could not extract PR URL from event. Is this running on a pull_request event?"
    exit 1
fi

echo "Reviewing PR: $PR_URL"

# Build mira command
CMD="mira review --pr $PR_URL"

if [ -n "$MIRA_CONFIG_PATH" ]; then
    CMD="$CMD --config $MIRA_CONFIG_PATH"
fi

# Run the review
exec $CMD
