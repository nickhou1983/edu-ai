from collections import defaultdict

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from edu_app.db import Evidence, KnowledgePoint
from edu_domain.contracts import Subject
from edu_domain.curriculum import SKILLS


def overview(
    session: Session, owner: str, curriculum=SKILLS, *, include_ai: bool = False, subject: Subject = "math",
) -> list[dict]:
    subject_filter = Evidence.english_id.is_not(None) if subject == "english" else Evidence.english_id.is_(None)
    encounters_query = select(Evidence).where(Evidence.owner == owner, subject_filter)
    identity = (Evidence.skill, Evidence.problem_key)
    if include_ai and subject == "math":
        shared_rule_keys = (
            select(Evidence.problem_key)
            .where(Evidence.owner == owner, subject_filter, Evidence.verification == "rules")
            .group_by(Evidence.problem_key)
            .having(func.count(Evidence.skill.distinct()) > 1)
            .subquery()
        )
        # Preserve historical per-skill evidence if the same rule key has multiple meanings.
        shared = shared_rule_keys.c.problem_key.is_not(None)
        encounters_query = (
            encounters_query.outerjoin(shared_rule_keys, Evidence.problem_key == shared_rule_keys.c.problem_key)
            .where(or_(Evidence.verification == "rules", ~shared))
        )
        identity = (case((shared, Evidence.skill)), Evidence.problem_key)
    elif not include_ai:
        encounters_query = encounters_query.where(Evidence.verification == "rules")
    encounters = encounters_query.add_columns(
        func.row_number()
        .over(partition_by=identity, order_by=(Evidence.occurred_at, Evidence.source_id))
        .label("encounter"),
    ).subquery()
    ranked = (
        select(
            encounters,
            func.row_number()
            .over(
                partition_by=encounters.c.skill,
                order_by=(encounters.c.occurred_at.desc(), encounters.c.source_id.desc()),
            )
            .label("recency"),
        )
        .where(encounters.c.encounter == 1)
        .subquery()
    )
    latest = session.execute(
        select(ranked).where(ranked.c.recency <= 10).order_by(ranked.c.recency)
    ).mappings()
    by_skill = defaultdict(list)
    for row in latest:
        item = {
            "itemId": row["source_id"],
            "submissionId": row["submission_id"],
            "practiceId": row["practice_id"],
            "englishId": row["english_id"],
            "score": row["score"],
            "occurredAt": row["occurred_at"],
        }
        if include_ai:
            verification = row["verification"]
            # The historical English "rules" marker used a single AI reference and exact matching.
            item["verification"] = "legacy_match" if subject == "english" and verification == "rules" else verification
        by_skill[row["skill"]].append(item)

    builtin_ids = {skill.id for skill in curriculum}
    extra_ids = by_skill.keys() - builtin_ids if include_ai and subject == "math" else set()
    extra = session.scalars(
        select(KnowledgePoint).where(KnowledgePoint.subject == "math", KnowledgePoint.id.in_(extra_ids))
        .order_by(KnowledgePoint.unit, KnowledgePoint.title, KnowledgePoint.id)
    ).all() if extra_ids else []
    skills = []
    for skill in (*curriculum, *extra):
        evidence = by_skill[skill.id]
        if len(evidence) < 3:
            percent, status = None, "insufficient"
        else:
            percent = sum(item["score"] for item in evidence) * 50 // len(evidence)
            status = "priority" if percent < 60 else "reinforce" if percent < 80 else "stable"
        stats = {
            "id": skill.id,
            "unit": skill.unit,
            "title": skill.title,
            "status": status,
            "percent": percent,
            "sampleCount": len(evidence),
            "evidence": evidence,
        }
        if include_ai and subject == "math":
            stats["practiceMode"] = "rule" if skill.id in builtin_ids else "reference"
        skills.append(stats)
    return skills
