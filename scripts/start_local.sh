#!/bin/bash
set -e

# cd to project root (parent of scripts/)
cd "$(dirname "$0")/.."

# Load .env — keep set -a on so all vars are exported to child processes
set -a
if [ -f .env ]; then
  source .env
  echo "Loaded .env"
else
  echo "WARNING: No .env file found"
fi

# Kill anything on our ports first
lsof -ti:8100 2>/dev/null | xargs kill -9 2>/dev/null || true
lsof -ti:5173 2>/dev/null | xargs kill -9 2>/dev/null || true
sleep 1

echo "=== Starting Mira ==="

# Load private key from file if path is set
if [ -n "$MIRA_GITHUB_PRIVATE_KEY_PATH" ]; then
  KEY_PATH="$MIRA_GITHUB_PRIVATE_KEY_PATH"
  # Try with .pem extension if file not found
  [ ! -f "$KEY_PATH" ] && [ -f "${KEY_PATH}.pem" ] && KEY_PATH="${KEY_PATH}.pem"
  if [ -f "$KEY_PATH" ]; then
    export MIRA_GITHUB_PRIVATE_KEY=$(cat "$KEY_PATH")
    echo "Loaded GitHub private key from $KEY_PATH"
  else
    echo "WARNING: Private key file not found: $MIRA_GITHUB_PRIVATE_KEY_PATH"
  fi
fi

# Ensure index directory exists
mkdir -p "$MIRA_INDEX_DIR"

# Set defaults only if not already set by .env
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
export MIRA_MODEL="${MIRA_MODEL:-anthropic/claude-sonnet-4-6}"
export MIRA_INDEX_DIR="${MIRA_INDEX_DIR:-./data/indexes}"

# Debug
echo "  MIRA_GITHUB_APP_ID=${MIRA_GITHUB_APP_ID:-(not set)}"
echo "  DATABASE_URL=${DATABASE_URL:-(not set)}"
echo "  MIRA_INDEX_DIR=${MIRA_INDEX_DIR}"

# Start single server (dashboard API + webhooks on same port)
echo "Starting Mira server on port 8100..."
.venv/bin/python -c "
import os, uvicorn
from mira.dashboard.api import app

# Mount webhook routes if GitHub App is configured
app_id = os.environ.get('MIRA_GITHUB_APP_ID')
private_key = os.environ.get('MIRA_GITHUB_PRIVATE_KEY')

if app_id and private_key:
    from mira.github_app.auth import GitHubAppAuth
    from mira.github_app.webhooks import create_app as create_webhook_app
    auth = GitHubAppAuth(app_id=app_id, private_key=private_key)
    webhook_app = create_webhook_app(
        app_auth=auth,
        webhook_secret=os.environ.get('MIRA_WEBHOOK_SECRET', ''),
        bot_name=os.environ.get('MIRA_BOT_NAME', 'miracodeai'),
    )
    app.mount('/github', webhook_app)
    print('GitHub webhooks enabled at /github/webhook')
else:
    print('No MIRA_GITHUB_APP_ID set — webhooks disabled')

uvicorn.run(app, host='0.0.0.0', port=8100)
" &
SERVER_PID=$!

sleep 2

# Start frontend
echo "Starting frontend on port 5173..."
cd UI/mira
VITE_API_URL=http://localhost:8100 npm run dev &
UI_PID=$!
cd ../..

sleep 2
echo ""
echo "=== Mira is running ==="
echo "  Dashboard:  http://localhost:5173"
echo "  API:        http://localhost:8100"
if [ -n "$MIRA_GITHUB_APP_ID" ]; then
  echo "  Webhook:    http://localhost:8100/github/webhook"
  echo "  Point ngrok at: http://localhost:8100"
  echo "  Set GitHub App webhook URL to: https://<ngrok-url>/github/webhook"
fi
echo ""
echo "  Login with: admin / ${ADMIN_PASSWORD}"
echo ""
echo "Press Ctrl+C to stop"

trap "kill $SERVER_PID $UI_PID 2>/dev/null" EXIT
wait
