import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("reanalysis", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM jobs WHERE reanalysis AND status IN ('queued', 'running') LIMIT 1"
    )).first():
        raise RuntimeError("Finish queued or running worksheet reanalyses before downgrading")
    op.drop_column("jobs", "reanalysis")
