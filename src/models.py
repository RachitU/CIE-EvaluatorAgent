from pydantic import BaseModel, field_validator
from typing import Literal

class RefinedIdea(BaseModel):
    customer_segment: str
    qualified_problem: str
    consequence: str
    proposed_solution: str


class TIPSValidatedMetrics(BaseModel):
    timely_factor: str
    importance_metric: str
    profitability_pivot: str
    solvability_constraint: str


class TIPSRAGScores(BaseModel):
    T: Literal["GREEN", "YELLOW", "RED"]
    I: Literal["GREEN", "YELLOW", "RED"]
    P: Literal["GREEN", "YELLOW", "RED"]
    S: Literal["GREEN", "YELLOW", "RED"]


class PreEvalOutput(BaseModel):
    problem_statement: str
    customer_segment: str
    consequence: str
    assumptions: list[str]
    proposed_solution: str

    @field_validator("assumptions", mode="before")
    @classmethod
    def fix_assumptions(cls, v):
        if isinstance(v, str):
            return [v]
        return v

    @field_validator(
        "problem_statement",
        "customer_segment",
        "consequence",
        "proposed_solution",
        mode="before"
    )
    @classmethod
    def fix_strings(cls, v):
        if isinstance(v, list):
            # return "; ".join(map(str, v))
            return " ".join(map(str, v))
        return v


class TIPSCOutput(BaseModel):
    refined_idea: RefinedIdea

    solution_alignment: Literal[
        "GREEN",
        "YELLOW",
        "RED"
    ]

    tips_validated_metrics: TIPSValidatedMetrics

    tips_rag_scores: TIPSRAGScores

    tips_coaching: dict[str, str] ={}
    overall_readiness: Literal[
        "STRONG",
        "MODERATE",
        "WEAK"
    ]

    ready_for_dfv: bool

class FollowUpOutput(BaseModel):
    needs_followup: bool
    questions: list[str]


