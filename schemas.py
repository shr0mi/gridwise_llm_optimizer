"""Exact request/response contract for the GridWise LLM preliminary."""
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

DIRECTIVE_TYPES = (
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
)


# --------------------------------------------------------------------------- request


class HourEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hour: int = Field(ge=0, le=23)
    demand_kwh: float = Field(ge=0)
    solar_kwh: float = Field(ge=0)
    tariff_bdt_per_kwh: float = Field(ge=0)


class Battery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    capacity_kwh: float = Field(gt=0)
    initial_energy_kwh: float = Field(ge=0)
    minimum_energy_kwh: float = Field(ge=0)
    max_charge_kwh_per_hour: float = Field(ge=0)
    max_discharge_kwh_per_hour: float = Field(ge=0)


class ScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scenario_id: str
    operator_notes: List[str] = Field(min_length=1, max_length=3)
    hours: List[HourEntry] = Field(min_length=24, max_length=24)
    battery: Battery

    @field_validator("operator_notes")
    @classmethod
    def _notes_non_empty(cls, v: List[str]) -> List[str]:
        if any(not isinstance(n, str) or not n.strip() for n in v):
            raise ValueError("operator_notes entries must be non-empty strings")
        return v

    @field_validator("hours")
    @classmethod
    def _hours_are_0_to_23(cls, v: List[HourEntry]) -> List[HourEntry]:
        if sorted(h.hour for h in v) != list(range(24)):
            raise ValueError("hours must contain exactly one entry per hour 0..23")
        return v


# --------------------------------------------------------------------------- response


class DirectiveInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: Literal[DIRECTIVE_TYPES]  # type: ignore[valid-type]
    structured_adjustment: Optional[dict[str, Any]]
    explanation: str


class HourPlan(BaseModel):
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: Literal["charge", "discharge", "idle"]
    battery_kwh: float
    battery_energy_after_kwh: float


class OptimizeResponse(BaseModel):
    scenario_id: str
    directive_interpretation: List[DirectiveInterpretation]
    hourly_plan: List[HourPlan]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
