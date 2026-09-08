"""The migration chain must build exactly the schema the models describe."""

from __future__ import annotations

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from hermes.cli import alembic_config
from hermes.config import Settings
from hermes.db import make_engine
from hermes.domain.models import Base


def test_upgrade_head_matches_models(settings: Settings) -> None:
    command.upgrade(alembic_config(settings), "head")
    engine = make_engine(settings.database_url)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True, "render_as_batch": True})
        diff = compare_metadata(ctx, Base.metadata)
    engine.dispose()
    assert diff == [], f"models and migrations differ: {diff}"


def test_downgrade_to_base_is_clean(settings: Settings) -> None:
    cfg = alembic_config(settings)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    engine = make_engine(settings.database_url)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        tables = {d[1].name for d in compare_metadata(ctx, Base.metadata) if d[0] == "add_table"}
    engine.dispose()
    assert tables == set(Base.metadata.tables)
