"""Database models for the e-commerce platform."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class OrderStatus(Enum):
    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"


@dataclass
class User:
    id: int
    email: str
    password_hash: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    is_active: bool = True


@dataclass
class Product:
    id: int
    name: str
    price: float
    stock: int = 0
    description: str = ""


@dataclass
class Order:
    id: int
    user_id: int
    status: OrderStatus = OrderStatus.PENDING
    total: float = 0.0
    created_at: datetime = field(default_factory=datetime.utcnow)
    items: list = field(default_factory=list)


@dataclass
class OrderItem:
    id: int
    order_id: int
    product_id: int
    quantity: int = 1
    unit_price: float = 0.0

    @property
    def subtotal(self) -> float:
        return self.quantity * self.unit_price
