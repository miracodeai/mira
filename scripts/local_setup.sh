#!/bin/bash
set -e

echo "=== Mira Local Setup ==="
echo ""

# 1. Check dependencies
echo "Checking dependencies..."
command -v docker >/dev/null 2>&1 || { echo "Docker required. Install from https://docker.com"; exit 1; }
command -v ngrok >/dev/null 2>&1 || { echo "ngrok required. Install with: brew install ngrok"; exit 1; }

# 2. Start Postgres
echo ""
echo "Starting PostgreSQL..."
docker rm -f mira-postgres 2>/dev/null || true
docker run -d --name mira-postgres \
  -e POSTGRES_USER=mira \
  -e POSTGRES_PASSWORD=mira \
  -e POSTGRES_DB=mira \
  -p 5432:5432 \
  postgres:16-alpine

echo "Waiting for Postgres to be ready..."
sleep 3
until docker exec mira-postgres pg_isready -U mira >/dev/null 2>&1; do
  sleep 1
done
echo "PostgreSQL ready on localhost:5432"

# 3. Print next steps
echo ""
echo "=== PostgreSQL is running ==="
echo ""
echo "Next steps:"
echo ""
echo "1. Create a GitHub App at https://github.com/settings/apps/new"
echo "   - App name: Mira Code Reviewer"
echo "   - Homepage URL: http://localhost:8100"
echo "   - Webhook URL: (start ngrok first, see step 2)"
echo "   - Webhook secret: generate one with: openssl rand -hex 20"
echo ""
echo "   Permissions needed:"
echo "   - Repository > Contents: Read"
echo "   - Repository > Pull requests: Read & Write"
echo "   - Repository > Issues: Read & Write"
echo ""
echo "   Subscribe to events:"
echo "   - Pull request"
echo "   - Issue comment"
echo "   - Push"
echo ""
echo "   After creating:"
echo "   - Note the App ID (shown on the app page)"
echo "   - Generate a private key (.pem file)"
echo "   - Install the app on your account/repos"
echo ""
echo "2. Start ngrok in a separate terminal:"
echo "   ngrok http 8100"
echo "   Then update the GitHub App webhook URL to the ngrok https URL + /webhook"
echo "   e.g., https://abc123.ngrok.io/webhook"
echo ""
echo "3. Create a .env file:"
echo "   cp .env.example .env"
echo "   # Then fill in your values"
echo ""
echo "4. Start Mira:"
echo "   source .env && ./scripts/start_local.sh"
echo ""
