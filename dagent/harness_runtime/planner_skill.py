"""Built-in, version-locked planner skill resources."""

from __future__ import annotations

import hashlib
from importlib.resources import files
from pathlib import Path

from dagent.capabilities.skills import SkillStore
from dagent.schemas import PlannerSkillSnapshot


DAG_GENERATION_SKILL_VERSION = 1
DAG_GENERATION_SKILL_NAME = "generate-dag"


def load_dag_generation_skill() -> PlannerSkillSnapshot:
    skill_root = Path(str(files("dagent.resources").joinpath("skills")))
    view = SkillStore(
        [skill_root],
        managed_root=skill_root / ".managed",
    ).view(
        DAG_GENERATION_SKILL_NAME,
    )
    if view.metadata.get("name") != DAG_GENERATION_SKILL_NAME:
        raise ValueError("Built-in DAG generation skill has an invalid name.")
    if not view.description.strip():
        raise ValueError("Built-in DAG generation skill requires a description.")
    return PlannerSkillSnapshot(
        name=DAG_GENERATION_SKILL_NAME,
        version=DAG_GENERATION_SKILL_VERSION,
        content=view.content,
        sha256=hashlib.sha256(view.content.encode("utf-8")).hexdigest(),
    )
