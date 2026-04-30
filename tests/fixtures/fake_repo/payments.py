"""Payment processing for the e-commerce platform."""


from fake_repo.config import config
from fake_repo.db_models import Order


def charge_card(amount: float, token: str) -> dict:
    """Charge a payment card via the payment gateway.

    Args:
        amount: Amount to charge in dollars.
        token: Tokenized card details from the frontend.

    Returns:
        Dict with 'success' bool and 'charge_id' on success.
    """
    if not config.STRIPE_API_KEY:
        return {"success": False, "error": "Payment gateway not configured"}
    if amount <= 0:
        return {"success": False, "error": "Invalid amount"}
    # In production, call Stripe API
    return {"success": True, "charge_id": f"ch_{token[:8]}"}


def refund(charge_id: str) -> dict:
    """Issue a refund for a previous charge.

    Args:
        charge_id: The ID of the charge to refund.

    Returns:
        Dict with 'success' bool and 'refund_id' on success.
    """
    if not charge_id:
        return {"success": False, "error": "charge_id is required"}
    # In production, call Stripe refund API
    return {"success": True, "refund_id": f"re_{charge_id}"}


def create_payment_intent(order: Order) -> dict:
    """Create a payment intent for an order.

    Args:
        order: The order to create a payment intent for.

    Returns:
        Dict with intent details.
    """
    return {
        "intent_id": f"pi_order_{order.id}",
        "amount": order.total,
        "currency": "usd",
        "status": "requires_payment",
    }
