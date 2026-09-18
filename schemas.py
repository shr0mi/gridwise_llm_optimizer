"""Exact request/response contract for the GridWise LLM preliminary.

Mirrors Problem Statement sections 07 (request) and 10 (response) field for
field. Two classes of failure are distinguished, because section 6.1 separates
them:

``SemanticError``  well-formed JSON whose numbers contradict each other
                   (initial energy above capacity, reserve above capacity, ...)
                   -> HTTP 422.
Everything else    missing fields, wrong types, wrong array lengths, malformed
                   JSON -- "structurally invalid" -> HTTP 400.
"""
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DIRECTIVE_TYPES = (
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
)


class SemanticError(ValueError):
    """Well-formed request whose values contradict each other -> 422."""


# --------------------------------------------------------------------------- request


class HourEntry(BaseModel):
    """One hour of the 24-hour horizon (Problem Statement 7.2)."""

    model_config = ConfigDict(extra="ignore")

    hour: int = Field(ge=0, le=23, description="Unique integer from 0 to 23.")
    demand_kwh: float = Field(ge=0, allow_inf_nan=False,
                              description="Campus demand for this hour.")
    solar_kwh: float = Field(
        ge=0, allow_inf_nan=False,
        description="Base solar before operator-note adjustments.")
    tariff_bdt_per_kwh: float = Field(
        ge=0, allow_inf_nan=False,
        description="Grid electricity price for this hour.")


class Battery(BaseModel):
    """Battery energy storage parameters (Problem Statement 7.3)."""

    model_config = ConfigDict(extra="ignore")

    capacity_kwh: float = Field(gt=0, allow_inf_nan=False,
                                description="Maximum stored energy.")
    initial_energy_kwh: float = Field(ge=0, allow_inf_nan=False,
                                      description="Energy at the start of hour 0.")
    minimum_energy_kwh: float = Field(ge=0, allow_inf_nan=False,
                                      description="Base reserve floor.")
    max_charge_kwh_per_hour: float = Field(ge=0, allow_inf_nan=False,
                                           description="Hourly charge limit.")
    max_discharge_kwh_per_hour: float = Field(ge=0, allow_inf_nan=False,
                                              description="Hourly discharge limit.")

    @model_validator(mode="after")
    def _coherent(self) -> "Battery":
        if self.initial_energy_kwh > self.capacity_kwh:
            raise SemanticError("initial_energy_kwh exceeds capacity_kwh")
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise SemanticError("minimum_energy_kwh exceeds capacity_kwh")
        if self.initial_energy_kwh < self.minimum_energy_kwh:
            raise SemanticError("initial_energy_kwh is below minimum_energy_kwh")
        return self


_EXAMPLE_HOURS = [
    {"hour": h,
     "demand_kwh": [180, 170, 165, 160, 158, 162, 175, 195, 215, 230, 240, 248,
                    252, 250, 245, 240, 238, 245, 265, 280, 275, 250, 220, 200][h],
     "solar_kwh": [0, 0, 0, 0, 0, 0, 10, 45, 95, 140, 175, 195,
                   200, 190, 165, 125, 80, 35, 5, 0, 0, 0, 0, 0][h],
     "tariff_bdt_per_kwh": [7, 7, 7, 7, 7, 7, 8, 9, 9, 9, 9, 9,
                            9, 9, 9, 9, 9, 11, 13, 13, 13, 11, 9, 9][h]}
    for h in range(24)
]

_EXAMPLE_REQUEST = {
    "scenario_id": "GRID-101",
    "operator_notes": [
        "Solar output will drop to about 20% from 1 PM to 3 PM.",
        "Do not charge the battery between 2 PM and 4 PM.",
        "The cafeteria menu changes tomorrow.",
    ],
    "hours": _EXAMPLE_HOURS,
    "battery": {
        "capacity_kwh": 500,
        "initial_energy_kwh": 200,
        "minimum_energy_kwh": 50,
        "max_charge_kwh_per_hour": 100,
        "max_discharge_kwh_per_hour": 100,
    },
}


class ScenarioRequest(BaseModel):
    """One 24-hour campus energy scenario plus 1-3 operator notes."""

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={"examples": [_EXAMPLE_REQUEST]},
    )

    scenario_id: str = Field(description="Unique synthetic scenario identifier.")
    operator_notes: List[str] = Field(
        min_length=1, max_length=3,
        description="1-3 non-empty natural-language operator notes.")
    hours: List[HourEntry] = Field(
        min_length=24, max_length=24,
        description="Exactly 24 entries, one per hour 0-23.")
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
    """One machine-checkable reading per operator note (Problem Statement 10.2)."""

    model_config = ConfigDict(json_schema_extra={"examples": [{
        "note_index": 0,
        "applies": True,
        "directive_type": "solar_reduction",
        "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
        "explanation": "Usable solar is scaled to 0.2 of forecast over hours 13-14.",
    }]})

    note_index: int = Field(
        description="Zero-based index of the corresponding operator_notes entry.")
    applies: bool = Field(
        description="true for every non-no_op directive; false only for no_op.")
    directive_type: Literal[DIRECTIVE_TYPES] = Field(  # type: ignore[valid-type]
        description="One of the six supported directive types.")
    structured_adjustment: Optional[dict[str, Any]] = Field(
        description="Exact object required by the directive type, or null for no_op.")
    explanation: str = Field(description="Short explanation of the interpretation.")


class HourPlan(BaseModel):
    """One scheduled hour of the returned plan (Problem Statement 10.3)."""

    hour: int = Field(ge=0, le=23)
    grid_kwh: float = Field(ge=0, description="Grid energy purchased this hour.")
    solar_used_kwh: float = Field(
        ge=0, description="Solar used this hour; never above effective solar.")
    battery_action: Literal["charge", "discharge", "idle"]
    battery_kwh: float = Field(
        ge=0, description="Magnitude of the battery action; 0 when idle.")
    battery_energy_after_kwh: float = Field(
        ge=0, description="Battery energy immediately after this hour.")


class OptimizeResponse(BaseModel):
    """Interpretation plus the final 24-hour schedule (Problem Statement 10.1)."""

    model_config = ConfigDict(json_schema_extra={"examples": [{
        "scenario_id": "GRID-101",
        "directive_interpretation": [
            {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
             "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
             "explanation": "Usable solar is scaled to 0.2 of forecast over hours 13-14."},
            {"note_index": 1, "applies": True, "directive_type": "no_charge_window",
             "structured_adjustment": {"hours": [14, 15]},
             "explanation": "Battery charging is unavailable over hours 14-15."},
            {"note_index": 2, "applies": False, "directive_type": "no_op",
             "structured_adjustment": None,
             "explanation": "This note does not affect today's energy schedule."},
        ],
        "hourly_plan": [{
            "hour": 0, "grid_kwh": 280.0, "solar_used_kwh": 0.0,
            "battery_action": "charge", "battery_kwh": 100.0,
            "battery_energy_after_kwh": 300.0,
        }],
        "total_grid_kwh": 4521.0,
        "total_cost_bdt": 41287.0,
        "peak_grid_kwh": 280.0,
        "plan_summary": "Applied solar_reduction over hours 13-14; no_charge_window "
                        "over hours 14-15. 1 note was irrelevant and ignored.",
    }]})

    scenario_id: str = Field(description="Echoes the request scenario_id.")
    directive_interpretation: List[DirectiveInterpretation] = Field(
        description="Exactly one entry per operator note, in note_index order.")
    hourly_plan: List[HourPlan] = Field(
        min_length=24, max_length=24, description="One entry per hour 0-23.")
    total_grid_kwh: float = Field(description="Sum of grid_kwh across all 24 hours.")
    total_cost_bdt: float = Field(description="Sum of grid_kwh * tariff_bdt_per_kwh.")
    peak_grid_kwh: float = Field(description="Maximum hourly grid_kwh in the plan.")
    plan_summary: str = Field(description="Short human-readable strategy summary.")


class HealthResponse(BaseModel):
    """Readiness response (Problem Statement 6.2)."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"status": "ok"}]})

    status: str = Field(description='Always "ok" when the service is ready.')


class ErrorResponse(BaseModel):
    """Controlled error body. Never carries secrets or stack traces."""

    model_config = ConfigDict(json_schema_extra={"examples": [
        {"error": "invalid request",
         "detail": [{"loc": ["body", "hours"], "msg": "List should have at least 24 items"}]}
    ]})

    error: str
    detail: Optional[Any] = None
