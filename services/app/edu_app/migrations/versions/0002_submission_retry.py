import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("manual_retries", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("jobs", "manual_retries")
