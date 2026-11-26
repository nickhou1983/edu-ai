from collections.abc import Iterator
from contextlib import contextmanager
from time import time_ns
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.types import JSON

from edu_app.config import Settings

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


def now_ms() -> int:
    return time_ns() // 1_000_000


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint("owner", "digest", name="uq_submission_owner_digest"),
        Index("ix_submission_owner_created", "owner", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String(80))
    digest: Mapped[bytes] = mapped_column(LargeBinary(32))
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class Page(Base):
    __tablename__ = "submission_pages"
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), primary_key=True
    )
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    object_key: Mapped[str] = mapped_column(String(96))
    analysis_checkpoint: Mapped[dict | None] = mapped_column(JSON_DOCUMENT, deferred=True)


class Report(Base):
    __tablename__ = "grading_reports"
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    document: Mapped[dict] = mapped_column(JSON_DOCUMENT)


class Practice(Base):
    __tablename__ = "practice_sets"
    __table_args__ = (Index("ix_practice_owner_created", "owner", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String(80))
    skill: Mapped[str] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(String(16))
    difficulty: Mapped[str] = mapped_column(String(16))
    count: Mapped[int] = mapped_column(Integer)
    provenance: Mapped[list | None] = mapped_column(JSON_DOCUMENT, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)


class PracticeItem(Base):
    __tablename__ = "practice_items"
    __table_args__ = (
        UniqueConstraint("practice_id", "position", name="uq_practice_item_position"),
        UniqueConstraint("practice_id", "problem_key", name="uq_practice_item_problem"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    practice_id: Mapped[str] = mapped_column(ForeignKey("practice_sets.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    problem_key: Mapped[str] = mapped_column(String(64))
    spec: Mapped[dict] = mapped_column(JSON_DOCUMENT)
    expected: Mapped[str] = mapped_column(String(256))
    explanation: Mapped[str] = mapped_column(String(600))
    rules_version: Mapped[str] = mapped_column(String(32))
    hint_seen: Mapped[bool] = mapped_column(default=False)
    answer: Mapped[str | None] = mapped_column(String(96))
    score: Mapped[int | None] = mapped_column(Integer)
    answered_at: Mapped[int | None] = mapped_column(BigInteger)
    counted: Mapped[bool] = mapped_column(default=False)


class EnglishRecord(Base):
    __tablename__ = "english_records"
    __table_args__ = (
        UniqueConstraint("owner", "digest", name="uq_english_owner_digest"),
        Index("ix_english_owner_created", "owner", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(16))
    skill: Mapped[str | None] = mapped_column(String(32))
    digest: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    document: Mapped[dict] = mapped_column(JSON_DOCUMENT)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)


class KnowledgePoint(Base):
    __tablename__ = "knowledge_points"
    __table_args__ = (
        UniqueConstraint("subject", "normalized_title", name="uq_knowledge_subject_title"),
        CheckConstraint("subject IN ('math', 'english')", name="ck_knowledge_subject"),
        CheckConstraint("origin IN ('builtin', 'manual', 'discovered')", name="ck_knowledge_origin"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    subject: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(80))
    normalized_title: Mapped[str] = mapped_column(String(160))
    unit: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(600))
    origin: Mapped[str] = mapped_column(String(16))
    reviewed: Mapped[bool] = mapped_column(default=False)
    created_by: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)


class KnowledgeActivity(Base):
    __tablename__ = "knowledge_activities"
    __table_args__ = (
        UniqueConstraint("owner", "subject", "source_id", name="uq_knowledge_discovery_source"),
        Index("ix_knowledge_activity_owner_created", "owner", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String(80))
    subject: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))
    point_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_points.id"))
    source_id: Mapped[str | None] = mapped_column(String(36))
    document: Mapped[dict] = mapped_column(JSON_DOCUMENT)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "(CASE WHEN submission_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN practice_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN english_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN knowledge_id IS NULL THEN 0 ELSE 1 END) = 1",
            name="ck_job_one_target",
        ),
        CheckConstraint("status IN ('queued', 'running', 'done', 'failed')", name="ck_job_status"),
        Index("ix_job_dispatch", "status", "available_at", "dispatched_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submission_id: Mapped[str | None] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), unique=True
    )
    practice_id: Mapped[str | None] = mapped_column(
        ForeignKey("practice_sets.id", ondelete="CASCADE"), unique=True
    )
    english_id: Mapped[str | None] = mapped_column(
        ForeignKey("english_records.id", ondelete="CASCADE"), unique=True
    )
    knowledge_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_activities.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(String(16), default="queued")
    error: Mapped[str | None] = mapped_column(String(40))
    available_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    dispatched_at: Mapped[int | None] = mapped_column(BigInteger)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    manual_retries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Evidence(Base):
    __tablename__ = "mastery_evidence"
    __table_args__ = (
        CheckConstraint("score BETWEEN 0 AND 2", name="ck_evidence_score"),
        Index("ix_evidence_owner_skill_problem", "owner", "skill", "problem_key", "occurred_at"),
        Index("ix_evidence_submission", "submission_id"),
    )
    source_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(80))
    skill: Mapped[str] = mapped_column(String(32))
    problem_key: Mapped[str] = mapped_column(String(64))
    score: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[int] = mapped_column(BigInteger)
    submission_id: Mapped[str | None] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"))
    practice_id: Mapped[str | None] = mapped_column(ForeignKey("practice_sets.id", ondelete="CASCADE"))
    english_id: Mapped[str | None] = mapped_column(ForeignKey("english_records.id", ondelete="CASCADE"))


class Database:
    def __init__(self, settings: Settings, credential=None):
        url = make_url(settings.dsn)
        if url.get_backend_name() == "sqlite" and url.database not in (None, ":memory:"):
            settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        options = {"pool_pre_ping": True}
        if url.get_backend_name() == "postgresql":
            options["connect_args"] = {"connect_timeout": 5}
            if settings.mode == "azure":
                options["connect_args"]["sslmode"] = "verify-full"
                options["connect_args"]["sslrootcert"] = "/etc/ssl/certs/ca-certificates.crt"
        self.engine = create_engine(url, **options)
        self.sqlite = self.engine.dialect.name == "sqlite"
        if self.sqlite:
            event.listen(self.engine, "connect", self._configure_sqlite)
        if settings.mode == "azure":
            if credential is None:
                raise ValueError("Azure database requires a managed identity credential")

            @event.listens_for(self.engine, "do_connect")
            def supply_password(_dialect, _connection, _args, params):
                params["password"] = credential.get_token(
                    "https://ossrdbms-aad.database.windows.net/.default"
                ).token

        self.session = sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _configure_sqlite(conn, _record):
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self.session.begin() as session:
            if self.sqlite:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            yield session
