from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text

from database.models import GiftCode


def assign_unused_gift_code(db: Session, value: int, order_id: str) -> Optional[GiftCode]:
    """
    Pobiera pierwszy nieużyty kod o zadanym nominale i przypisuje mu order_id.
    Zwraca obiekt GiftCode lub None.
    """
    row = db.execute(
        text(
            """
            SELECT id
            FROM gift_codes
            WHERE value = :value AND order_id IS NULL
            ORDER BY id ASC
            LIMIT 1
            """
        ),
        {"value": value},
    ).fetchone()

    if not row:
        return None

    gift_id = row.id

    db.execute(
        text("UPDATE gift_codes SET order_id = :order_id WHERE id = :id"),
        {"order_id": order_id, "id": gift_id},
    )

    # zwracamy obiekt ORM dla wygody (do odczytu code/value)
    gc = db.get(GiftCode, gift_id)
    return gc
