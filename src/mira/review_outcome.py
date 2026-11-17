"""Finding-delivery evidence, independent of comment-ID lookup."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum


class DeliveryStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"
    NOT_ATTEMPTED = "not_attempted"


@dataclass
class _DeliveryItem:
    confirmed: bool = False
    uncertain_attempts: int = 0
    comment_id: int = 0


class DeliveryReceipt:
    """Accumulate acknowledgements monotonically across existing retry paths."""

    def __init__(self, count: int, *, tracked: bool = True, attempted: bool = True) -> None:
        self._items = [_DeliveryItem() for _ in range(count)]
        self.tracked = tracked
        self.attempted = attempted

    def begin(self, *indices: int) -> None:
        for index in indices:
            self._items[index].uncertain_attempts += 1

    def acknowledge(self, *indices: int) -> None:
        for index in indices:
            self._items[index].confirmed = True

    def reject(self, *indices: int) -> None:
        for index in indices:
            item = self._items[index]
            item.uncertain_attempts = max(0, item.uncertain_attempts - 1)

    def interrupted_copy(self) -> DeliveryReceipt:
        """Detach evidence from a cancelled worker that may still finish its writes."""
        receipt = DeliveryReceipt(0, tracked=self.tracked, attempted=self.attempted)
        receipt._items = [_DeliveryItem(item.confirmed, 1, item.comment_id) for item in self._items]
        return receipt

    @property
    def delivered(self) -> int:
        return sum(item.confirmed for item in self._items)

    @property
    def unknown(self) -> int:
        if not self.attempted:
            return 0
        return sum(
            not item.confirmed and (not self.tracked or item.uncertain_attempts > 0)
            for item in self._items
        )

    @property
    def failed(self) -> int:
        return len(self._items) - self.delivered - self.unknown if self.attempted else 0

    @property
    def status(self) -> DeliveryStatus:
        if not self.attempted:
            return DeliveryStatus.NOT_ATTEMPTED
        if self.delivered == len(self._items):
            return DeliveryStatus.COMPLETE
        if self.delivered:
            return DeliveryStatus.PARTIAL
        return DeliveryStatus.UNKNOWN if self.unknown else DeliveryStatus.FAILED

    @property
    def comment_ids(self) -> list[int]:
        return [item.comment_id for item in self._items]

    def set_comment_id(self, index: int, comment_id: int) -> None:
        if isinstance(comment_id, int) and not isinstance(comment_id, bool) and comment_id > 0:
            self._items[index].comment_id = comment_id


@dataclass(frozen=True)
class ReviewOutcome:
    """Versioned, safe event metadata; no response bodies or exception text."""

    drafted: int | None
    retained: int
    delivered: int
    failed: int
    unknown: int
    status: DeliveryStatus
    coverage: str
    delivery_error: bool = False

    def to_json(self) -> str:
        return json.dumps({"version": 1, **asdict(self)})

    @classmethod
    def from_json(cls, raw: str) -> ReviewOutcome | None:
        """Legacy or malformed metadata supplies no delivery proof."""
        try:
            data = json.loads(raw)
            if (
                not isinstance(data, dict)
                or type(data.get("version")) is not int
                or data["version"] != 1
            ):
                return None
            counts = [data[key] for key in ("retained", "delivered", "failed", "unknown")]
            if any(type(value) is not int or value < 0 for value in counts):
                return None
            drafted = data["drafted"]
            if drafted is not None and (type(drafted) is not int or drafted < counts[0]):
                return None
            status = DeliveryStatus(data["status"])
            retained, delivered, failed, unknown = counts
            if status == DeliveryStatus.NOT_ATTEMPTED:
                if delivered or failed or unknown:
                    return None
            elif delivered + failed + unknown != retained:
                return None
            expected = (
                DeliveryStatus.COMPLETE
                if delivered == retained
                else DeliveryStatus.PARTIAL
                if delivered
                else DeliveryStatus.UNKNOWN
                if unknown
                else DeliveryStatus.FAILED
            )
            if status != DeliveryStatus.NOT_ATTEMPTED and status != expected:
                return None
            if data["coverage"] not in {"complete", "incomplete", "failed", "skipped"}:
                return None
            if type(data.get("delivery_error")) is not bool:
                return None
            return cls(
                drafted,
                retained,
                delivered,
                failed,
                unknown,
                status,
                data["coverage"],
                data["delivery_error"],
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            return None
