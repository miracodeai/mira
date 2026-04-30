"""Seed demo data for the Mira dashboard UI."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mira.index.store import DirectorySummary, ExternalRef, FileSummary, IndexStore, SymbolInfo

INDEX_DIR = os.environ.get("MIRA_INDEX_DIR", "/tmp/mira-demo-indexes")


def seed():
    org = "acme-corp"
    org_dir = os.path.join(INDEX_DIR, org)
    os.makedirs(org_dir, exist_ok=True)

    # ── payments-service ──
    s = IndexStore(os.path.join(org_dir, "payments-service.db"))
    s.upsert_summary(
        FileSummary(
            path="cmd/server/main.go",
            language="go",
            summary="Payment service entry point, configures HTTP server and middleware.",
            symbols=[
                SymbolInfo("main", "function", "func main()", "Server entry point"),
                SymbolInfo(
                    "setupRoutes",
                    "function",
                    "func setupRoutes(r *mux.Router)",
                    "Register HTTP routes",
                ),
            ],
            imports=[
                "internal/handler/payment.go",
                "internal/handler/webhook.go",
                "pkg/config/config.go",
            ],
            external_refs=[
                ExternalRef(
                    "cmd/server/main.go",
                    "go_import",
                    "github.com/acme-corp/shared-lib/pkg/auth",
                    "JWT auth middleware",
                ),
                ExternalRef(
                    "cmd/server/main.go",
                    "go_import",
                    "github.com/acme-corp/shared-lib/pkg/logging",
                    "Structured logging",
                ),
            ],
        )
    )
    s.upsert_summary(
        FileSummary(
            path="internal/handler/payment.go",
            language="go",
            summary="HTTP handlers for payment processing: charge, refund, and status check.",
            symbols=[
                SymbolInfo(
                    "HandleCharge", "function", "func HandleCharge(w, r)", "Process a card charge"
                ),
                SymbolInfo("HandleRefund", "function", "func HandleRefund(w, r)", "Issue a refund"),
                SymbolInfo(
                    "HandleStatus", "function", "func HandleStatus(w, r)", "Check payment status"
                ),
            ],
            imports=["internal/service/stripe.go", "pkg/models/payment.go"],
        )
    )
    s.upsert_summary(
        FileSummary(
            path="internal/handler/webhook.go",
            language="go",
            summary="Stripe webhook handler for payment event notifications.",
            symbols=[
                SymbolInfo(
                    "HandleWebhook",
                    "function",
                    "func HandleWebhook(w, r)",
                    "Process Stripe webhooks",
                ),
            ],
            imports=["internal/service/stripe.go"],
        )
    )
    s.upsert_summary(
        FileSummary(
            path="internal/service/stripe.go",
            language="go",
            summary="Stripe API client wrapper for charges, refunds, and payment intents.",
            symbols=[
                SymbolInfo(
                    "ChargeCard",
                    "function",
                    "func ChargeCard(amount, token) (*Charge, error)",
                    "Create a Stripe charge",
                ),
                SymbolInfo(
                    "RefundCharge",
                    "function",
                    "func RefundCharge(chargeID) (*Refund, error)",
                    "Refund a charge",
                ),
                SymbolInfo(
                    "CreateIntent",
                    "function",
                    "func CreateIntent(amount) (*Intent, error)",
                    "Create payment intent",
                ),
            ],
            imports=["pkg/config/config.go"],
            external_refs=[
                ExternalRef(
                    "internal/service/stripe.go",
                    "go_import",
                    "github.com/stripe/stripe-go/v76",
                    "Stripe SDK",
                ),
            ],
        )
    )
    s.upsert_summary(
        FileSummary(
            path="pkg/models/payment.go",
            language="go",
            summary="Payment domain models and types.",
            symbols=[
                SymbolInfo("Payment", "struct", "type Payment struct", "Payment record"),
                SymbolInfo(
                    "PaymentStatus", "type", "type PaymentStatus string", "Payment status enum"
                ),
            ],
        )
    )
    s.upsert_summary(
        FileSummary(
            path="pkg/config/config.go",
            language="go",
            summary="Application configuration loaded from environment variables.",
            symbols=[
                SymbolInfo("Config", "struct", "type Config struct", "App configuration"),
                SymbolInfo(
                    "Load", "function", "func Load() (*Config, error)", "Load config from env"
                ),
            ],
        )
    )
    s.upsert_summary(
        FileSummary(
            path="docker-compose.yml",
            language="yaml",
            summary="Local dev environment with payments-worker, Redis, and PostgreSQL.",
            external_refs=[
                ExternalRef(
                    "docker-compose.yml",
                    "docker_image",
                    "ghcr.io/acme-corp/payments-worker",
                    "Background job processor",
                ),
                ExternalRef(
                    "docker-compose.yml", "docker_image", "redis:7-alpine", "Cache and queue"
                ),
                ExternalRef("docker-compose.yml", "docker_image", "postgres:16", "Database"),
            ],
        )
    )
    s.upsert_summary(
        FileSummary(
            path="deploy/main.tf",
            language="hcl",
            summary="Terraform infrastructure for ECS Fargate deployment.",
            external_refs=[
                ExternalRef(
                    "deploy/main.tf",
                    "terraform_module",
                    "github.com/acme-corp/infra-modules//modules/ecs-service",
                    "ECS deployment module",
                ),
            ],
        )
    )
    s.upsert_directory(
        DirectorySummary(
            path="cmd/server", summary="Payment service HTTP server entry point.", file_count=1
        )
    )
    s.upsert_directory(
        DirectorySummary(
            path="internal/handler",
            summary="HTTP request handlers for payments and webhooks.",
            file_count=2,
        )
    )
    s.upsert_directory(
        DirectorySummary(
            path="internal/service", summary="External service integrations (Stripe).", file_count=1
        )
    )
    s.upsert_directory(
        DirectorySummary(path="pkg/models", summary="Domain models and types.", file_count=1)
    )
    s.upsert_directory(
        DirectorySummary(path="pkg/config", summary="Application configuration.", file_count=1)
    )
    s.close()

    # ── payments-worker ──
    s = IndexStore(os.path.join(org_dir, "payments-worker.db"))
    s.upsert_summary(
        FileSummary(
            path="cmd/worker/main.go",
            language="go",
            summary="Background worker for payment reconciliation and retry processing.",
            symbols=[
                SymbolInfo("main", "function", "func main()", "Worker entry point"),
                SymbolInfo(
                    "RunWorker",
                    "function",
                    "func RunWorker(ctx context.Context)",
                    "Main worker loop",
                ),
            ],
            imports=["internal/jobs/reconcile.go", "internal/jobs/retry.go"],
            external_refs=[
                ExternalRef(
                    "cmd/worker/main.go",
                    "go_import",
                    "github.com/acme-corp/shared-lib/pkg/auth",
                    "Auth middleware",
                ),
                ExternalRef(
                    "cmd/worker/main.go",
                    "go_import",
                    "github.com/acme-corp/payments-service/pkg/models",
                    "Payment models",
                ),
            ],
        )
    )
    s.upsert_summary(
        FileSummary(
            path="internal/jobs/reconcile.go",
            language="go",
            summary="Daily payment reconciliation job comparing Stripe records with database.",
            symbols=[
                SymbolInfo(
                    "Reconcile",
                    "function",
                    "func Reconcile(ctx context.Context) error",
                    "Run reconciliation",
                ),
            ],
        )
    )
    s.upsert_summary(
        FileSummary(
            path="internal/jobs/retry.go",
            language="go",
            summary="Retry failed payment attempts with exponential backoff.",
            symbols=[
                SymbolInfo(
                    "RetryFailed",
                    "function",
                    "func RetryFailed(ctx context.Context) error",
                    "Retry failed payments",
                ),
            ],
        )
    )
    s.close()

    # ── checkout-flow ──
    s = IndexStore(os.path.join(org_dir, "checkout-flow.db"))
    s.upsert_summary(
        FileSummary(
            path="src/checkout.ts",
            language="typescript",
            summary="Checkout flow orchestrating cart validation, payment, and order creation.",
            symbols=[
                SymbolInfo(
                    "processCheckout",
                    "function",
                    "async function processCheckout(cart: Cart)",
                    "Execute checkout",
                ),
                SymbolInfo(
                    "validateCart",
                    "function",
                    "function validateCart(cart: Cart): ValidationResult",
                    "Validate cart items",
                ),
            ],
            imports=["src/api/payments.ts", "src/api/orders.ts"],
            external_refs=[
                ExternalRef(
                    "src/checkout.ts",
                    "go_import",
                    "github.com/acme-corp/payments-service/pkg/models",
                    "Payment types",
                ),
            ],
        )
    )
    s.upsert_summary(
        FileSummary(
            path="src/api/payments.ts",
            language="typescript",
            summary="API client for the payments service.",
            symbols=[
                SymbolInfo(
                    "createPayment",
                    "function",
                    "async function createPayment(amount: number)",
                    "Create payment",
                ),
            ],
            external_refs=[
                ExternalRef(
                    "src/api/payments.ts",
                    "api_endpoint",
                    "https://api.acme.com/v1/payments",
                    "Payments API",
                ),
            ],
        )
    )
    s.upsert_summary(
        FileSummary(
            path="src/api/orders.ts",
            language="typescript",
            summary="API client for the orders service.",
            symbols=[
                SymbolInfo(
                    "createOrder",
                    "function",
                    "async function createOrder(items: Item[])",
                    "Create order",
                ),
            ],
        )
    )
    s.close()

    # ── shared-lib ──
    s = IndexStore(os.path.join(org_dir, "shared-lib.db"))
    s.upsert_summary(
        FileSummary(
            path="pkg/auth/auth.go",
            language="go",
            summary="Shared JWT authentication middleware for all microservices.",
            symbols=[
                SymbolInfo(
                    "Authenticate",
                    "function",
                    "func Authenticate(next http.Handler) http.Handler",
                    "JWT middleware",
                ),
                SymbolInfo(
                    "ParseToken",
                    "function",
                    "func ParseToken(token string) (*Claims, error)",
                    "Parse JWT token",
                ),
            ],
        )
    )
    s.upsert_summary(
        FileSummary(
            path="pkg/logging/logger.go",
            language="go",
            summary="Structured logging with correlation ID propagation.",
            symbols=[
                SymbolInfo(
                    "NewLogger",
                    "function",
                    "func NewLogger(service string) *Logger",
                    "Create logger",
                ),
                SymbolInfo(
                    "WithCorrelationID",
                    "function",
                    "func WithCorrelationID(ctx context.Context) *Logger",
                    "Logger with correlation ID",
                ),
            ],
        )
    )
    s.upsert_summary(
        FileSummary(
            path="pkg/errors/errors.go",
            language="go",
            summary="Standardized error types and wrapping utilities.",
            symbols=[
                SymbolInfo("AppError", "struct", "type AppError struct", "Application error type"),
                SymbolInfo(
                    "Wrap",
                    "function",
                    "func Wrap(err error, msg string) *AppError",
                    "Wrap error with context",
                ),
            ],
        )
    )
    s.close()

    # ── infra-modules ──
    s = IndexStore(os.path.join(org_dir, "infra-modules.db"))
    s.upsert_summary(
        FileSummary(
            path="modules/ecs-service/main.tf",
            language="hcl",
            summary="Terraform module for ECS Fargate service deployment with ALB and auto-scaling.",
            symbols=[
                SymbolInfo(
                    "ecs_service", "resource", "resource aws_ecs_service", "ECS service definition"
                ),
                SymbolInfo(
                    "alb_target_group",
                    "resource",
                    "resource aws_lb_target_group",
                    "ALB target group",
                ),
            ],
        )
    )
    s.upsert_summary(
        FileSummary(
            path="modules/rds/main.tf",
            language="hcl",
            summary="Terraform module for RDS PostgreSQL with multi-AZ and automated backups.",
            symbols=[
                SymbolInfo("db_instance", "resource", "resource aws_db_instance", "RDS instance"),
            ],
        )
    )
    s.close()

    # ── solar-monitoring.api ──
    s = IndexStore(os.path.join(org_dir, "solar-monitoring.api.db"))
    s.upsert_summary(
        FileSummary(
            path="src/api/metrics.go",
            language="go",
            summary="REST API for querying solar panel performance metrics and historical data.",
            symbols=[
                SymbolInfo(
                    "GetMetrics", "function", "func GetMetrics(w, r)", "Query panel metrics"
                ),
                SymbolInfo(
                    "GetHistory", "function", "func GetHistory(w, r)", "Get historical data"
                ),
            ],
            external_refs=[
                ExternalRef(
                    "src/api/metrics.go",
                    "go_import",
                    "github.com/acme-corp/solar-monitoring.ingest/queue",
                    "Ingest queue client",
                ),
            ],
        )
    )
    s.upsert_summary(
        FileSummary(
            path="src/api/alerts.go",
            language="go",
            summary="Alert management API for solar panel fault detection and notification.",
            symbols=[
                SymbolInfo(
                    "CreateAlert", "function", "func CreateAlert(w, r)", "Create alert rule"
                ),
                SymbolInfo("ListAlerts", "function", "func ListAlerts(w, r)", "List active alerts"),
            ],
        )
    )
    s.close()

    # ── solar-monitoring.ingest ──
    s = IndexStore(os.path.join(org_dir, "solar-monitoring.ingest.db"))
    s.upsert_summary(
        FileSummary(
            path="src/ingest/pipeline.go",
            language="go",
            summary="Data ingestion pipeline for solar panel sensor readings and performance metrics.",
            symbols=[
                SymbolInfo(
                    "IngestBatch",
                    "function",
                    "func IngestBatch(readings []Reading) error",
                    "Ingest sensor batch",
                ),
                SymbolInfo(
                    "ProcessStream",
                    "function",
                    "func ProcessStream(ctx context.Context) error",
                    "Process sensor stream",
                ),
            ],
            external_refs=[
                ExternalRef(
                    "src/ingest/pipeline.go",
                    "go_import",
                    "github.com/acme-corp/shared-lib/pkg/logging",
                    "Structured logging",
                ),
            ],
        )
    )
    s.upsert_summary(
        FileSummary(
            path="src/ingest/queue.go",
            language="go",
            summary="Redis-backed message queue for buffering sensor data before processing.",
            symbols=[
                SymbolInfo(
                    "Enqueue", "function", "func Enqueue(msg Message) error", "Add message to queue"
                ),
                SymbolInfo(
                    "Dequeue",
                    "function",
                    "func Dequeue(ctx context.Context) (*Message, error)",
                    "Read from queue",
                ),
            ],
        )
    )
    s.close()

    # ── docs-site ──
    s = IndexStore(os.path.join(org_dir, "docs-site.db"))
    s.upsert_summary(
        FileSummary(
            path="content/getting-started.md",
            language="markdown",
            summary="Getting started guide for new developers.",
        )
    )
    s.upsert_summary(
        FileSummary(
            path="content/api-reference.md",
            language="markdown",
            summary="API reference documentation for all services.",
        )
    )
    s.close()

    # ── Seed review events ──
    import random
    import time

    review_repos = ["payments-service", "payments-worker", "checkout-flow", "solar-monitoring.api"]
    pr_titles = [
        "Add retry logic to payment processing",
        "Fix race condition in order creation",
        "Update Stripe webhook handler",
        "Refactor auth middleware",
        "Add rate limiting to checkout endpoint",
        "Fix SQL injection in search query",
        "Update Docker base image",
        "Add payment refund endpoint",
        "Fix memory leak in worker loop",
        "Migrate to new auth token format",
        "Add input validation for order items",
        "Fix CORS headers for API",
        "Update Terraform ECS module",
        "Add health check endpoint",
        "Fix timezone handling in reports",
        "Bump dependencies for security patches",
        "Add webhook signature verification",
        "Fix null pointer in metrics handler",
        "Refactor database connection pooling",
        "Add structured logging to ingestion pipeline",
    ]

    all_categories = [
        "bug",
        "security",
        "performance",
        "error-handling",
        "race-condition",
        "resource-leak",
        "maintainability",
        "clarity",
        "configuration",
    ]

    now = time.time()
    for repo_name in review_repos:
        s = IndexStore(os.path.join(org_dir, f"{repo_name}.db"))
        n_reviews = random.randint(20, 45)
        for _ in range(n_reviews):
            pr_num = random.randint(10, 300)
            title = random.choice(pr_titles)
            comments = random.randint(0, 8)
            blockers = random.randint(0, min(2, comments))
            warnings = random.randint(0, min(3, comments - blockers))
            suggestions = max(0, comments - blockers - warnings)
            files = random.randint(1, 15)
            lines = random.randint(20, 800)
            tokens = random.randint(2000, 20000)
            duration = random.randint(3000, 30000)
            cats = (
                ",".join(random.sample(all_categories, min(comments, random.randint(1, 4))))
                if comments > 0
                else ""
            )
            # Spread over last 60 days
            event_time = now - random.uniform(0, 60 * 86400)
            s.record_review(
                pr_number=pr_num,
                pr_title=title,
                pr_url=f"https://github.com/acme-corp/{repo_name}/pull/{pr_num}",
                comments_posted=comments,
                blockers=blockers,
                warnings=warnings,
                suggestions=suggestions,
                files_reviewed=files,
                lines_changed=lines,
                tokens_used=tokens,
                duration_ms=duration,
                categories=cats,
                created_at=event_time,
            )
        s.close()

    print(f"Demo data seeded at {INDEX_DIR}")
    print(f"  8 repos, {sum(random.randint(8, 20) for _ in review_repos)} review events")


if __name__ == "__main__":
    seed()
