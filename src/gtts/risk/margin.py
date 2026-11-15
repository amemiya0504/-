"""Effective-dated futures margin and cost inputs; missing rows never default to zero."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Self

import yaml
from pydantic import Field, field_validator, model_validator

from gtts.data.schema import Schema, _taipei_datetime


class Costs(Schema):
    effective_from: date
    broker_fee_per_contract: Decimal = Field(ge=0)
    tax_rate: Decimal = Field(ge=0, le=1)
    slippage_ticks: int = Field(ge=0)
    final_settlement_fee_per_contract: Decimal | None = Field(default=None, ge=0)
    final_settlement_tax_rate: Decimal | None = Field(default=None, ge=0, le=1)


class Margin(Schema):
    effective_from: datetime
    initial: Decimal = Field(gt=0)
    maintenance: Decimal = Field(gt=0)

    _use_taipei = field_validator("effective_from")(_taipei_datetime)

    @model_validator(mode="after")
    def validate_levels(self) -> Self:
        if self.maintenance > self.initial:
            raise ValueError("maintenance margin must not exceed initial margin")
        return self


class Schedules(Schema):
    costs: dict[str, tuple[Costs, ...]]
    margins: dict[str, tuple[Margin, ...]]

    @classmethod
    def from_yaml(cls, path: Path) -> Self:
        # Parse unquoted YAML decimals as Decimal without a binary-float intermediate.
        class Loader(yaml.SafeLoader):
            pass

        Loader.add_constructor("tag:yaml.org,2002:float", lambda _, node: Decimal(str(node.value)))
        return cls.model_validate(yaml.load(path.read_text(encoding="utf-8"), Loader=Loader))

    @model_validator(mode="after")
    def validate_effective_dates(self) -> Self:
        for schedules in (self.costs, self.margins):
            for instrument, terms in schedules.items():
                dates = [term.effective_from for term in terms]
                if dates != sorted(set(dates)):
                    raise ValueError(
                        f"schedule effective dates must be unique and sorted: {instrument}"
                    )
        return self

    def cost_at(self, instrument_id: str, trade_date: date) -> Costs | None:
        return next(
            (
                row
                for row in reversed(self.costs.get(instrument_id, ()))
                if row.effective_from <= trade_date
            ),
            None,
        )

    def margin_at(self, instrument_id: str, at: datetime) -> Margin | None:
        return next(
            (
                row
                for row in reversed(self.margins.get(instrument_id, ()))
                if row.effective_from <= at
            ),
            None,
        )
