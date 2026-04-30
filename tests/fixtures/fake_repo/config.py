"""Application configuration for the e-commerce platform."""

import os


class AppConfig:
    """Central configuration for the application."""

    DB_URL: str = os.environ.get("DB_URL", "postgresql://localhost:5432/ecommerce")
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    STRIPE_API_KEY: str = os.environ.get("STRIPE_API_KEY", "")
    JWT_EXPIRY: int = 3600  # seconds
    MAX_LOGIN_ATTEMPTS: int = 5
    RATE_LIMIT_PER_MINUTE: int = 100
    DEBUG: bool = os.environ.get("DEBUG", "false").lower() == "true"


config = AppConfig()
