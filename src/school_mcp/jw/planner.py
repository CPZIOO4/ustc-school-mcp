"""Bounded, deterministic timetable search. No enrolment requests."""
from __future__ import annotations

from itertools import combinations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Slot(BaseModel):
    model_config = ConfigDict(extra='forbid')
    weekday: int = Field(ge=1, le=7, strict=True)
    start_period: int = Field(ge=1, le=20, strict=True)
    end_period: int = Field(ge=1, le=20, strict=True)
    weeks: list[int] = Field(min_length=1, max_length=60)
    campus: str = Field(default='', max_length=100)

    @model_validator(mode='after')
    def valid(self):
        if self.end_period < self.start_period or any(type(w) is not int or not 1 <= w <= 60 for w in self.weeks):
            raise ValueError('节次必须顺序排列，周次为1–60的整数。')
        self.weeks = sorted(set(self.weeks))
        return self


class Candidate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    lesson_id: str = Field(min_length=1, max_length=80)
    course_code: str = Field(min_length=1, max_length=80)
    course_name: str = Field(min_length=1, max_length=200)
    semester_id: int | None = Field(default=None, ge=1, strict=True)
    credits: float = Field(ge=0, le=100, allow_inf_nan=False)
    slots: list[Slot] = Field(default_factory=list, max_length=100)
    schedule_known: bool = False
    required: bool = False
    preference: int = Field(default=0, ge=-100, le=100, strict=True)
    source: Literal['official', 'user_supplied'] = 'user_supplied'


class Constraints(BaseModel):
    model_config = ConfigDict(extra='forbid')
    min_credits: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    max_credits: float = Field(default=30, ge=0, le=100, allow_inf_nan=False)
    target_credits: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    unavailable: list[Slot] = Field(default_factory=list, max_length=100)
    prefer_free_weekdays: list[int] = Field(default_factory=list, max_length=7)
    max_courses: int = Field(default=12, ge=1, le=25, strict=True)
    cross_campus_gap_periods: int = Field(default=1, ge=0, le=10, strict=True)
    limit: int = Field(default=3, ge=1, le=10, strict=True)

    @model_validator(mode='after')
    def valid(self):
        if self.min_credits > self.max_credits or self.target_credits is not None and not self.min_credits <= self.target_credits <= self.max_credits:
            raise ValueError('学分上下限或目标不一致。')
        if any(type(d) is not int or not 1 <= d <= 7 for d in self.prefer_free_weekdays):
            raise ValueError('空闲星期为1–7。')
        return self


def overlap(a: Slot, b: Slot, gap=0):
    if a.weekday != b.weekday or not set(a.weeks).intersection(b.weeks):
        return False
    transfer = gap if a.campus and b.campus and a.campus != b.campus else 0
    return a.start_period <= b.end_period + transfer and b.start_period <= a.end_period + transfer


def generate(candidates: list[Candidate], constraints: Constraints):
    if len(candidates) > 25 or len({c.lesson_id for c in candidates}) != len(candidates):
        return dict(state='needs_input', missing_fields=['unique_candidates_max_25'], next_action='narrow_candidates', plans=[])
    if len({c.semester_id for c in candidates if c.semester_id is not None}) > 1:
        return dict(state='needs_input',missing_fields=['single_semester_candidates'],next_action='separate_semesters',plans=[])
    unknown = [c.lesson_id for c in candidates if not c.schedule_known or not c.slots]
    blocked, eligible = [], []
    for c in candidates:
        reasons = []
        if c.lesson_id in unknown:
            reasons.append('schedule_unknown')
        if any(overlap(s, u) for s in c.slots for u in constraints.unavailable):
            reasons.append('unavailable_time')
        if reasons:
            blocked.append(dict(lesson_id=c.lesson_id, reasons=reasons, required=c.required))
        else:
            eligible.append(c)
    if any(x['required'] for x in blocked):
        return dict(state='needs_input', next_action='resolve_required_course_constraints', plans=[], excluded=blocked)
    conflicts = {}
    for a, b in combinations(eligible, 2):
        reason = 'same_course' if a.course_code == b.course_code else 'time_or_transfer_conflict' if any(overlap(x, y, constraints.cross_campus_gap_periods) for x in a.slots for y in b.slots) else None
        if reason:
            conflicts[frozenset((a.lesson_id, b.lesson_id))] = reason
    results, nodes, truncated = [], 0, False
    target = constraints.target_credits if constraints.target_credits is not None else constraints.max_credits

    def search(index, picked, credits):
        nonlocal nodes, truncated
        nodes += 1
        if nodes > 100000:
            truncated = True
            return
        if credits > constraints.max_credits + 1e-8 or len(picked) > constraints.max_courses:
            return
        if index == len(eligible):
            if not picked or credits + 1e-8 < constraints.min_credits:
                return
            days = {s.weekday for c in picked for s in c.slots}
            score = (-abs(credits - target), -len(days.intersection(constraints.prefer_free_weekdays)), sum(c.preference for c in picked), -len(days))
            results.append((score, list(picked), credits))
            results.sort(key=lambda r: (r[0], tuple(c.lesson_id for c in r[1])), reverse=True)
            del results[constraints.limit:]
            return
        c = eligible[index]
        if not any(frozenset((c.lesson_id, p.lesson_id)) in conflicts for p in picked):
            search(index + 1, picked + [c], credits + c.credits)
        if not c.required:
            search(index + 1, picked, credits)

    search(0, [], 0)
    plans = []
    for score, chosen, credits in results:
        days = sorted({s.weekday for c in chosen for s in c.slots})
        plans.append(dict(lesson_ids=[c.lesson_id for c in chosen], courses=[c.model_dump() for c in chosen], total_credits=round(credits, 6), occupied_weekdays=days,
                          free_weekdays=[d for d in range(1, 8) if d not in days], preference_total=score[2], target_credit_distance=round(-score[0], 6),
                          tradeoffs=['先满足学分目标，再尽量保留指定空闲日，随后按偏好分和到校天数排序。']))
    return dict(state='planned' if plans else 'search_incomplete' if truncated else 'no_feasible_plan', plans=plans, excluded=blocked,
                conflicts=[dict(lesson_ids=sorted(k), reason=v) for k,v in conflicts.items()],
                search_truncated=truncated, search_nodes=min(nodes,100000), optimality_proven=not truncated,
                enrollment_performed=False, eligibility_verified=False,
                limitations=['只依据输入周次与节次；资格、容量、培养方案和考试冲突需另核对。', '空闲日表示该日全学期均无所选课程；跨校区间隔按输入节数计算，未核实交通。'],
                next_action='present_candidates' if plans else 'relax_constraints_or_add_candidates')
