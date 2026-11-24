from pathlib import Path

from alembic import command
from alembic.config import Config

from edu_app.db import Database


def migrate(database: Database):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
