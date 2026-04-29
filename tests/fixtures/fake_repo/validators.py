"""Input validation functions for the e-commerce platform."""

import re

_EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def validate_email(email: str) -> list[str]:
    """Validate an email address. Returns list of error messages."""
    errors = []
    if not email:
        errors.append("Email is required")
    elif not _EMAIL_PATTERN.match(email):
        errors.append("Invalid email format")
    return errors


def validate_order(data: dict) -> list[str]:
    """Validate order creation data."""
    errors = []
    if not data:
        errors.append("Order data is required")
        return errors
    items = data.get("items", [])
    if not items:
        errors.append("Order must have at least one item")
    for i, item in enumerate(items):
        if "product_id" not in item:
            errors.append(f"Item {i} missing product_id")
        if "price" not in item:
            errors.append(f"Item {i} missing price")
        elif item["price"] <= 0:
            errors.append(f"Item {i} has invalid price")
        qty = item.get("quantity", 1)
        if qty < 1:
            errors.append(f"Item {i} has invalid quantity")
    return errors


def validate_payment(data: dict) -> list[str]:
    """Validate payment data."""
    errors = []
    if not data:
        errors.append("Payment data is required")
        return errors
    if "order_id" not in data:
        errors.append("order_id is required")
    if "payment_token" not in data:
        errors.append("payment_token is required")
    amount = data.get("amount", 0)
    if amount <= 0:
        errors.append("amount must be positive")
    return errors
