import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

document = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade():
    op.create_table(
        "submissions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner", sa.String(80), nullable=False),
        sa.Column("digest", sa.LargeBinary(32), nullable=False),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.UniqueConstraint("owner", "digest", name="uq_submission_owner_digest"),
    )
    op.create_index("ix_submission_owner_created", "submissions", ["owner", "created_at"])
    op.create_table(
        "submission_pages",
        sa.Column(
            "submission_id",
            sa.String(36),
            sa.ForeignKey("submissions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("number", sa.Integer, primary_key=True),
        sa.Column("object_key", sa.String(96), nullable=False),
    )
    op.create_table(
        "grading_reports",
        sa.Column(
            "submission_id",
            sa.String(36),
            sa.ForeignKey("submissions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("document", document, nullable=False),
    )
    op.create_table(
        "practice_sets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner", sa.String(80), nullable=False),
        sa.Column("skill", sa.String(32), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("difficulty", sa.String(16), nullable=False),
        sa.Column("count", sa.Integer, nullable=False),
        sa.Column("provenance", document),
        sa.Column("created_at", sa.BigInteger, nullable=False),
    )
    op.create_index("ix_practice_owner_created", "practice_sets", ["owner", "created_at"])
    op.create_table(
        "practice_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "practice_id",
            sa.String(36),
            sa.ForeignKey("practice_sets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("problem_key", sa.String(64), nullable=False),
        sa.Column("spec", document, nullable=False),
        sa.Column("expected", sa.String(256), nullable=False),
        sa.Column("explanation", sa.String(600), nullable=False),
        sa.Column("rules_version", sa.String(32), nullable=False),
        sa.Column("hint_seen", sa.Boolean, nullable=False),
        sa.Column("answer", sa.String(96)),
        sa.Column("score", sa.Integer),
        sa.Column("answered_at", sa.BigInteger),
        sa.Column("counted", sa.Boolean, nullable=False),
        sa.UniqueConstraint("practice_id", "position", name="uq_practice_item_position"),
        sa.UniqueConstraint("practice_id", "problem_key", name="uq_practice_item_problem"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "submission_id", sa.String(36), sa.ForeignKey("submissions.id", ondelete="CASCADE"), unique=True
        ),
        sa.Column(
            "practice_id", sa.String(36), sa.ForeignKey("practice_sets.id", ondelete="CASCADE"), unique=True
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.String(40)),
        sa.Column("available_at", sa.BigInteger, nullable=False),
        sa.Column("dispatched_at", sa.BigInteger),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_until", sa.BigInteger),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.CheckConstraint("(submission_id IS NULL) <> (practice_id IS NULL)", name="ck_job_one_target"),
        sa.CheckConstraint("status IN ('queued', 'running', 'done', 'failed')", name="ck_job_status"),
    )
    op.create_index("ix_job_dispatch", "jobs", ["status", "available_at", "dispatched_at"])
    op.create_table(
        "mastery_evidence",
        sa.Column("source_id", sa.String(36), primary_key=True),
        sa.Column("owner", sa.String(80), nullable=False),
        sa.Column("skill", sa.String(32), nullable=False),
        sa.Column("problem_key", sa.String(64), nullable=False),
        sa.Column("score", sa.Integer, nullable=False),
        sa.Column("occurred_at", sa.BigInteger, nullable=False),
        sa.Column("submission_id", sa.String(36), sa.ForeignKey("submissions.id", ondelete="CASCADE")),
        sa.Column("practice_id", sa.String(36), sa.ForeignKey("practice_sets.id", ondelete="CASCADE")),
        sa.CheckConstraint("score BETWEEN 0 AND 2", name="ck_evidence_score"),
    )
    op.create_index(
        "ix_evidence_owner_skill_problem",
        "mastery_evidence",
        ["owner", "skill", "problem_key", "occurred_at"],
    )
    op.create_index("ix_evidence_submission", "mastery_evidence", ["submission_id"])


def downgrade():
    for table in (
        "mastery_evidence",
        "jobs",
        "practice_items",
        "practice_sets",
        "grading_reports",
        "submission_pages",
        "submissions",
    ):
        op.drop_table(table)
