"""SQL column types for the domain primitives.

Persisting a ``Money`` as two loose columns invites somebody to read the integer
without the currency, and the resulting bug is a number that looks right. These
``TypeDecorator`` subclasses keep each domain type's representation intact across
the boundary.

Every one of them is **lossless by construction**: the stored form is exactly the
in-memory form. ``Money`` stores its minor units, ``Price`` its scaled units. No
conversion happens, so no conversion can be wrong (ADR-042).

Note what is *not* here. There is no ``TradingDayType``: a ``TradingDay`` cannot
be constructed without a calendar (ADR-046), and a column type has no business
holding one. The column stores a plain ``DATE``, and reconstruction happens in a
factory that does have the calendar — see ``factories.py``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID

from sqlalchemy import BigInteger, Dialect, Integer, Numeric, String, TypeDecorator
from sqlalchemy.dialects import postgresql

from dhruva.shared.money import Currency, Money, Price, Quantity, Ratio

if TYPE_CHECKING:
    from dhruva.shared.identity import SurrogateId

__all__ = [
    "MoneyAmount",
    "MoneyCurrency",
    "PriceUnits",
    "QuantityUnits",
    "RatioValue",
    "SurrogateIdType",
]


class MoneyAmount(TypeDecorator[Money]):
    """Stores a :class:`~dhruva.shared.money.Money` amount as ``BIGINT`` minor units.

    Pairs with :class:`MoneyCurrency` on the same table. The two are deliberately
    separate columns rather than a composite type: PostgreSQL composite types are
    awkward to index and to migrate, and the pairing is enforced by the mapper
    instead.

    The currency for reading back is supplied at construction, because a column
    cannot see its sibling. Every table storing money is single-currency in
    practice, and a table that is not must map the pair explicitly.
    """

    impl = BigInteger
    cache_ok = True

    def __init__(self, currency: Currency = Currency.INR) -> None:
        """Bind this column to a currency for reconstruction."""
        super().__init__()
        self._currency = currency

    def process_bind_param(self, value: Money | None, dialect: Dialect) -> int | None:
        """Store the exact minor-unit count. No conversion, so none can be wrong."""
        if value is None:
            return None
        # Only INR exists today, so a checker sees the mismatch branch as
        # unreachable. It is not unreachable in the sense that matters: the day a
        # second currency is added, this is what stops an amount being written
        # into a column bound to the wrong one. Written as an equality against
        # the stored code rather than an identity check on the enum, so it
        # survives that day without a type-checker suppression.
        if str(value.currency) != str(self._currency):
            msg = (
                f"column is bound to {self._currency} but received {value.currency}; "
                f"map the currency column explicitly for multi-currency tables"
            )
            raise ValueError(msg)
        return value.minor_units

    def process_result_value(self, value: int | None, dialect: Dialect) -> Money | None:
        """Reconstruct from minor units."""
        return None if value is None else Money(value, self._currency)


class MoneyCurrency(TypeDecorator[Currency]):
    """Stores a currency code as ``CHAR(3)``.

    Present even though only INR exists, so that no table has to be migrated the
    day a second currency does (ADR-004's reasoning, applied to money).
    """

    impl = String(3)
    cache_ok = True

    def process_bind_param(self, value: Currency | None, dialect: Dialect) -> str | None:
        """Store the ISO code."""
        return None if value is None else str(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Currency | None:
        """Reconstruct the enum, raising on an unknown code rather than guessing."""
        return None if value is None else Currency(value)


class PriceUnits(TypeDecorator[Price]):
    """Stores a :class:`~dhruva.shared.money.Price` as ``BIGINT`` scaled units.

    Eight decimal places, exactly as held in memory. A ``NUMERIC`` column would
    also be exact but would round-trip through ``Decimal``, reintroducing the
    context sensitivity ADR-042 removed.
    """

    impl = BigInteger
    cache_ok = True

    def __init__(self, currency: Currency = Currency.INR) -> None:
        """Bind this column to a currency for reconstruction."""
        super().__init__()
        self._currency = currency

    def process_bind_param(self, value: Price | None, dialect: Dialect) -> int | None:
        """Store the exact scaled-unit count."""
        return None if value is None else value.scaled_units

    def process_result_value(self, value: int | None, dialect: Dialect) -> Price | None:
        """Reconstruct from scaled units."""
        return None if value is None else Price(value, self._currency)


class QuantityUnits(TypeDecorator[Quantity]):
    """Stores a :class:`~dhruva.shared.money.Quantity` as ``INTEGER``.

    The schema should additionally carry a ``CHECK (units >= 0)`` constraint. The
    type refuses a negative quantity in Python; the constraint refuses one that
    arrives by any other route.
    """

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value: Quantity | None, dialect: Dialect) -> int | None:
        """Store the unit count."""
        return None if value is None else value.units

    def process_result_value(self, value: int | None, dialect: Dialect) -> Quantity | None:
        """Reconstruct, which re-asserts the non-negative invariant."""
        return None if value is None else Quantity(value)


class RatioValue(TypeDecorator[Ratio]):
    """Stores a :class:`~dhruva.shared.money.Ratio` as ``NUMERIC(18, 8)``.

    ``NUMERIC`` rather than ``DOUBLE PRECISION``: a rate stored as a float is the
    same defect as money stored as a float, one layer removed.
    """

    impl = Numeric(18, 8)
    cache_ok = True

    def process_bind_param(self, value: Ratio | None, dialect: Dialect) -> Decimal | None:
        """Store the exact fraction."""
        return None if value is None else value.fraction

    def process_result_value(self, value: Decimal | None, dialect: Dialect) -> Ratio | None:
        """Reconstruct from the exact fraction."""
        return None if value is None else Ratio(value)


class SurrogateIdType(TypeDecorator[Any]):
    """Stores a :class:`~dhruva.shared.identity.SurrogateId` as a native ``UUID``.

    Native rather than text: PostgreSQL's ``uuid`` is 16 bytes against 36, indexes
    better, and rejects malformed values at the database rather than at read time.

    The concrete identifier class is supplied at construction, so an
    ``InstrumentId`` column cannot hand back an ``AccountId`` — the two never
    compare equal (S03), and a column that returned the wrong one would make a
    cross-type lookup fail in a way that looks like missing data.
    """

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def __init__(self, id_type: type[SurrogateId]) -> None:
        """Bind this column to one identifier class."""
        super().__init__()
        self._id_type = id_type

    def process_bind_param(self, value: SurrogateId | None, dialect: Dialect) -> UUID | None:
        """Store the underlying UUID."""
        return None if value is None else value.value

    def process_result_value(self, value: UUID | None, dialect: Dialect) -> SurrogateId | None:
        """Reconstruct as the bound identifier class."""
        return None if value is None else self._id_type(value)

    def copy(self, **kwargs: Any) -> Self:
        """Preserve the bound identifier class when SQLAlchemy copies the type."""
        return type(self)(self._id_type)
