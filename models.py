from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Difficulty = Literal["basic", "medium", "advanced"]


class TopicSpec(BaseModel):
    topic: str = Field(min_length=1, max_length=120)
    difficulty: Difficulty


class TaskItem(BaseModel):
    number: int = Field(ge=1, le=12)
    text: str = Field(min_length=3, max_length=500)
    text_latex: str | None = Field(default=None, max_length=600)
    answer: str = Field(min_length=1, max_length=200)
    eval_expr: str | None = None
    topic: str | None = None
    difficulty: Difficulty | None = None


class Variant(BaseModel):
    variant_id: str = Field(min_length=1, max_length=3)
    tasks: list[TaskItem]

    @field_validator("tasks")
    @classmethod
    def task_count_valid(cls, tasks: list[TaskItem]) -> list[TaskItem]:
        if not 2 <= len(tasks) <= 12:
            raise ValueError("В варианте должно быть от 2 до 12 заданий")
        numbers = sorted(t.number for t in tasks)
        expected = list(range(1, len(tasks) + 1))
        if numbers != expected:
            raise ValueError(f"Задания должны быть пронумерованы {expected}")
        return tasks


class CardSet(BaseModel):
    subject: str
    grade: int = Field(ge=5, le=11)
    topic: str
    difficulty: str
    task_count: int = Field(default=3, ge=2, le=12)
    topics: list[TopicSpec] = Field(default_factory=list)
    variants: list[Variant]

    @field_validator("variants")
    @classmethod
    def variant_count(cls, variants: list[Variant]) -> list[Variant]:
        if len(variants) != 2:
            raise ValueError("Допустимо ровно 2 варианта")
        return variants

    @model_validator(mode="after")
    def topics_and_tasks_aligned(self) -> "CardSet":
        if self.variants and self.task_count != len(self.variants[0].tasks):
            self.task_count = len(self.variants[0].tasks)
        return self
