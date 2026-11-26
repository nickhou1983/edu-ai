import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "submission_pages",
        sa.Column("analysis_checkpoint", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True),
    )


def downgrade():
    op.drop_column("submission_pages", "analysis_checkpoint")
