import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "english_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("skill", sa.String(32)),
        sa.Column("digest", sa.LargeBinary(32)),
        sa.Column("document", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.UniqueConstraint("owner", "digest", name="uq_english_owner_digest"),
    )
    op.create_index("ix_english_owner_created", "english_records", ["owner", "created_at"])
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("ck_job_one_target", type_="check")
        batch.add_column(sa.Column("english_id", sa.String(36)))
        batch.create_foreign_key("fk_job_english", "english_records", ["english_id"], ["id"], ondelete="CASCADE")
        batch.create_unique_constraint("uq_job_english", ["english_id"])
        batch.create_check_constraint(
            "ck_job_one_target",
            "(CASE WHEN submission_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN practice_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN english_id IS NULL THEN 0 ELSE 1 END) = 1",
        )
    with op.batch_alter_table("mastery_evidence") as batch:
        batch.add_column(sa.Column("english_id", sa.String(36)))
        batch.create_foreign_key(
            "fk_evidence_english", "english_records", ["english_id"], ["id"], ondelete="CASCADE"
        )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT id FROM english_records LIMIT 1")).first():
        raise RuntimeError("Export and explicitly remove English records before downgrading to 0002")
    with op.batch_alter_table("mastery_evidence") as batch:
        batch.drop_constraint("fk_evidence_english", type_="foreignkey")
        batch.drop_column("english_id")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("ck_job_one_target", type_="check")
        batch.drop_constraint("fk_job_english", type_="foreignkey")
        batch.drop_constraint("uq_job_english", type_="unique")
        batch.drop_column("english_id")
        batch.create_check_constraint("ck_job_one_target", "(submission_id IS NULL) <> (practice_id IS NULL)")
    op.drop_table("english_records")
