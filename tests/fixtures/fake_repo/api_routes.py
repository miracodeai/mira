"""API route handlers for the e-commerce platform."""

from typing import Any

from fake_repo.auth_middleware import require_auth
from fake_repo.db_models import Order, OrderItem, OrderStatus, Product
from fake_repo.payments import charge_card, create_payment_intent
from fake_repo.validators import validate_order, validate_payment


@require_auth
async def create_order(request: Any) -> dict:
    """Create a new order from the request body."""
    data = request.json
    errors = validate_order(data)
    if errors:
        return {"status": 400, "errors": errors}

    items = []
    total = 0.0
    for item_data in data.get("items", []):
        product = Product(id=item_data["product_id"], name="", price=item_data["price"])
        order_item = OrderItem(
            id=0,
            order_id=0,
            product_id=product.id,
            quantity=item_data.get("quantity", 1),
            unit_price=product.price,
        )
        items.append(order_item)
        total += order_item.subtotal

    order = Order(id=0, user_id=request.user.id, total=total, items=items)
    return {"status": 201, "order_id": order.id}


@require_auth
async def get_order(request: Any, order_id: int) -> dict:
    """Retrieve an order by ID."""
    # In production, query the database
    order = Order(id=order_id, user_id=request.user.id)
    return {"status": 200, "order": {"id": order.id, "status": order.status.value}}


async def list_products(request: Any) -> dict:
    """List all available products. No auth required."""
    products = [
        Product(id=1, name="Widget", price=9.99, stock=100),
        Product(id=2, name="Gadget", price=24.99, stock=50),
    ]
    return {
        "status": 200,
        "products": [{"id": p.id, "name": p.name, "price": p.price} for p in products],
    }


@require_auth
async def checkout(request: Any) -> dict:
    """Process payment and complete an order."""
    data = request.json
    errors = validate_payment(data)
    if errors:
        return {"status": 400, "errors": errors}

    order_id = data["order_id"]
    order = Order(id=order_id, user_id=request.user.id, total=data.get("amount", 0))

    intent = create_payment_intent(order)
    charge_result = charge_card(order.total, data["payment_token"])

    if charge_result.get("success"):
        order.status = OrderStatus.PAID
        return {"status": 200, "order_id": order.id, "charge_id": charge_result["charge_id"]}
    return {"status": 402, "error": "Payment failed"}
