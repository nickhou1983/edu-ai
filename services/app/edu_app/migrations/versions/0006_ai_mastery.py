import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "submissions", sa.Column("ai_mastery", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "mastery_evidence", sa.Column("verification", sa.String(16), server_default="rules", nullable=False),
    )
    op.create_index("ix_evidence_owner_problem", "mastery_evidence", ["owner", "problem_key", "skill"])


def downgrade():
    connection = op.get_bind()
    english_records = sa.table("english_records", sa.column("document", sa.JSON()))
    english_ai = sa.select(1).select_from(english_records).where(
        english_records.c.document["aiMastery"].as_boolean().is_(True),
    ).limit(1)
    if connection.execute(sa.text("SELECT 1 FROM submissions WHERE ai_mastery LIMIT 1")).first() or (
        connection.execute(english_ai).first()
    ):
        raise RuntimeError("Preserve AI homework submissions, English AI records and evidence before downgrading")
    op.drop_index("ix_evidence_owner_problem", table_name="mastery_evidence")
    op.drop_column("mastery_evidence", "verification")
    op.drop_column("submissions", "ai_mastery")
