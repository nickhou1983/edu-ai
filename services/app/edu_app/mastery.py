from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from edu_app.db import Evidence
from edu_domain.curriculum import SKILLS


def overview(session: Session, owner: str, curriculum=SKILLS) -> list[dict]:
    # Keep the first verified encounter with a problem, even if it appears in another upload or exercise.
    encounters = (
        select(
            Evidence,
            func.row_number()
            .over(
                partition_by=(Evidence.skill, Evidence.problem_key),
                order_by=(Evidence.occurred_at, Evidence.source_id),
            )
            .label("encounter"),
        )
        .where(Evidence.owner == owner)
        .subquery()
    )
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
        by_skill[row["skill"]].append(
            {
                "itemId": row["source_id"],
                "submissionId": row["submission_id"],
                "practiceId": row["practice_id"],
                "englishId": row["english_id"],
                "score": row["score"],
                "occurredAt": row["occurred_at"],
            }
        )
    skills = []
    for skill in curriculum:
        evidence = by_skill[skill.id]
        if len(evidence) < 3:
            percent, status = None, "insufficient"
        else:
            percent = sum(item["score"] for item in evidence) * 50 // len(evidence)
            status = "priority" if percent < 60 else "reinforce" if percent < 80 else "stable"
        skills.append(
            {
                "id": skill.id,
                "unit": skill.unit,
                "title": skill.title,
                "status": status,
                "percent": percent,
                "sampleCount": len(evidence),
                "evidence": evidence,
            }
        )
    return skills
