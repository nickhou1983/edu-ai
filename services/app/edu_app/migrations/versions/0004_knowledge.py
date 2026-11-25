import re
import unicodedata

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_BUILTINS = (
    ("rational-sum", "math", "有理数加减", "有理数", "将减法转为加上相反数。"),
    ("rational-product", "math", "有理数乘除", "有理数", "判断正负号，再计算绝对值。"),
    ("rational-mixed", "math", "有理数混合运算", "有理数", "先括号，再乘方、乘除，最后加减。"),
    ("like-terms", "math", "合并同类项", "整式", "同类项只对系数做加减。"),
    ("distribution", "math", "去括号与化简", "整式", "因数乘括号中的每一项。"),
    ("linear-equations", "math", "一元一次方程求解", "一元一次方程", "在等式两边进行相同的运算。"),
    ("english-vocabulary", "english", "词汇与词形", "词汇与语法", "根据词义、句意检查词形和拼写。"),
    ("english-grammar", "english", "基础语法", "词汇与语法", "检查主语、时间线索和句子结构。"),
    ("english-reading", "english", "阅读理解", "阅读", "从原文找到支持答案的信息。"),
    ("english-writing", "english", "写作", "表达", "组织内容、完整表达并检查语法。"),
    ("english-listening-text", "english", "听力文本预习", "听说文本预习", "理解对话文本，不评估听力。"),
    ("english-speaking-text", "english", "口语表达草稿", "听说文本预习", "练习书面回应，不评估发音。"),
)


def upgrade():
    points = op.create_table(
        "knowledge_points",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("subject", sa.String(16), nullable=False),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("normalized_title", sa.String(160), nullable=False),
        sa.Column("unit", sa.String(80), nullable=False),
        sa.Column("description", sa.String(600), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("reviewed", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(80)),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("subject", "normalized_title", name="uq_knowledge_subject_title"),
        sa.CheckConstraint("subject IN ('math', 'english')", name="ck_knowledge_subject"),
        sa.CheckConstraint("origin IN ('builtin', 'manual', 'discovered')", name="ck_knowledge_origin"),
    )
    op.bulk_insert(points, [
        dict(id=id, subject=subject, title=title,
             normalized_title=re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", title).casefold()),
             unit=unit, description=description, origin="builtin", reviewed=True, created_by=None, created_at=0)
        for id, subject, title, unit, description in _BUILTINS
    ])
    op.create_table(
        "knowledge_activities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner", sa.String(80), nullable=False),
        sa.Column("subject", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("point_id", sa.String(32), sa.ForeignKey("knowledge_points.id")),
        sa.Column("source_id", sa.String(36)),
        sa.Column("document", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("owner", "subject", "source_id", name="uq_knowledge_discovery_source"),
    )
    op.create_index("ix_knowledge_activity_owner_created", "knowledge_activities", ["owner", "created_at"])
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("ck_job_one_target", type_="check")
        batch.add_column(sa.Column("knowledge_id", sa.String(36)))
        batch.create_foreign_key(
            "fk_job_knowledge", "knowledge_activities", ["knowledge_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_unique_constraint("uq_job_knowledge", ["knowledge_id"])
        batch.create_check_constraint(
            "ck_job_one_target",
            "(CASE WHEN submission_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN practice_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN english_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN knowledge_id IS NULL THEN 0 ELSE 1 END) = 1",
        )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT id FROM knowledge_activities LIMIT 1")).first():
        raise RuntimeError("Export and explicitly remove knowledge activities before downgrading")
    if op.get_bind().execute(sa.text("SELECT id FROM knowledge_points WHERE origin <> 'builtin' LIMIT 1")).first():
        raise RuntimeError("Export and explicitly remove added knowledge points before downgrading")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("ck_job_one_target", type_="check")
        batch.drop_constraint("fk_job_knowledge", type_="foreignkey")
        batch.drop_constraint("uq_job_knowledge", type_="unique")
        batch.drop_column("knowledge_id")
        batch.create_check_constraint(
            "ck_job_one_target",
            "(CASE WHEN submission_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN practice_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN english_id IS NULL THEN 0 ELSE 1 END) = 1",
        )
    op.drop_table("knowledge_activities")
    op.drop_table("knowledge_points")
