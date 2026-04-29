# Data Privacy and Security


## Security Vulnerability Reporting Guidelines

We value the security community's role in protecting our systems and users. To report a security vulnerability:

- File a private vulnerability report on GitHub: [Report a vulnerability](https://github.com/miracodeai/mira/security/advisories/new)
- Or email **support@miracode.ai**
- Include steps to reproduce the issue
- Provide any relevant additional information (affected versions, suggested mitigations)

We commit to acknowledging your report within 3 business days, providing a status update within 7 business days, and crediting you in the advisory and CHANGELOG unless you prefer to remain anonymous.

### Vulnerability Categories

We classify vulnerabilities into the following categories:

**P0: Supply Chain Attacks**

Attacks that compromise our CI/CD pipeline, allowing a malicious actor to point our PyPI package or Docker images (GHCR or Docker Hub) to vulnerable or tampered artifacts.

**P1: Unauthenticated Access**

Application-level attacks where an unauthenticated user gains access to a self-hosted Mira instance — for example, bypassing webhook signature verification to trigger arbitrary actions, or reading dashboard data without a valid session.

**P2: Authenticated Malicious Actions**

Application-level attacks where an authenticated user performs actions beyond their intended permissions, such as privilege escalation, unauthorized data access, or LLM prompt-injection that leaks indexed code outside the bot's intended responses.

### Known Non-Issues

- Attacks that require a misconfiguration on setup (e.g. not setting `MIRA_WEBHOOK_SECRET`, leaving `ADMIN_PASSWORD` at the default `admin`, exposing the dashboard to the public internet without auth) are **explicitly not in scope** and are not considered vulnerabilities.
- Vulnerabilities in third-party dependencies that don't affect Mira's actual usage — please report those upstream.
- Issues requiring physical access to the host running Mira.

### Bug Bounty

We're a small open-source project and currently **do not offer a paid bug bounty**. Reporters are credited in the advisory and CHANGELOG. We deeply appreciate responsible disclosure regardless.

## Security Measures

### Mira GitHub

- All commits run through CI (lint, type-check, full test suite) before merge.
- Dependency updates are reviewed before being merged.

### Self-hosted Mira

Mira is **self-hosted only**. There is no Mira-managed cloud service.

- **No data or telemetry leaves your infrastructure when you self-host.** We don't run any phone-home, usage tracking, or analytics service.
- **Code never touches a Mira-controlled server.** Mira uses your own LLM provider (BYO-LLM via LiteLLM) — your code goes from your repo, through your LLM API key, and back to your Mira instance. We are not in the path.
- **Indexes are stored locally** in your SQLite file or your Postgres database. Mira has no central index store.
- **Webhook signatures verified** with the per-installation `MIRA_WEBHOOK_SECRET` you configure on the GitHub App. Invalid signatures are rejected at the edge.
- **GitHub App tokens cached in-process only**, with a 55-minute TTL, and never written to disk. Tokens for installations you've removed expire automatically.
- **License keys, paywalls, and phone-home licensing have all been removed.** Mira is fully open source — see [`FEATURES.md`](FEATURES.md).

### Hardening checklist for production

If you're operating Mira in production, we recommend:

1. Set `MIRA_WEBHOOK_SECRET` to a long random value (32+ bytes).
2. Change `ADMIN_PASSWORD` from the default before exposing the dashboard.
3. Restrict ingress to Mira's webhook endpoint to GitHub's published webhook source IPs, or front the service with a reverse proxy that does.
4. Store `MIRA_GITHUB_PRIVATE_KEY`, LLM API keys, and `DATABASE_URL` in a secret manager — never commit them.
5. Run Mira behind TLS. The Docker image does not terminate TLS itself.
6. If running with `DATABASE_URL=postgres://…`, ensure the connection requires TLS (`?sslmode=require`).

For security inquiries, please contact us at **support@miracode.ai**.
