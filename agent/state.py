from typing import Any, Literal, TypedDict
from pydantic import BaseModel, Field


class Step(BaseModel):
    tool: Literal["web_search", "calculator", "python", "knowledge_base", "dataset_profile", "dataset_aggregate", "dataset_trend"]
    input: str = Field(min_length=1, max_length=6000)
    goal: str = Field(min_length=1)


class Plan(BaseModel):
    steps: list[Step] = Field(min_length=1, max_length=6)


class Review(BaseModel):
    decision: Literal["continue", "retry", "replan", "finish", "escalate"]
    reason: str
    answer: str = ""
    accepted: bool = False


class AnalysisRequest(BaseModel):
    kind: Literal["profile", "aggregate", "trend", "custom"] = "profile"
    metric: str | None = Field(default=None, max_length=100)
    group_by: str | None = Field(default=None, max_length=100)
    date_column: str | None = Field(default=None, max_length=100)


class AgentState(TypedDict, total=False):
    task: str
    plan: list[dict]
    completed: list[dict]
    step_index: int
    retries: dict[str, int]
    errors: list[dict]
    final_answer: str
    decision: str
    reason: str
    result: dict[str, Any]
    tool_calls: int
    replans: int
    fallback_used: bool
    status: str
    events: list[dict]
    dataset_id: str
    dataset_schema: dict
    analysis_request: dict
