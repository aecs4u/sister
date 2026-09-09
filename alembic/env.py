"""Alembic migration environment for sister."""

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

from alembic import context

# Add project root to path so sister package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Import all models to register them with SQLModel.metadata
from sister.cadastral import (  # noqa: F401, E402
    CadastralInspection,
    CadastralLegalEntitySearchEntity,
    CadastralLegalEntitySearchGeoSummary,
    CadastralLegalEntitySearchParameter,
    CadastralLegalEntitySearchProperty,
    CadastralLocationParameters,
    CadastralPropertyProperty,
    CadastralProspectOwner,
    CadastralProspectProperty,
    CadastralQuery,
)
from sister.db_models import (  # noqa: F401, E402
    CadastralLocation,
    CadastralSubject,
    DocumentMetadata,
    FeedbackConfig,
    FeedbackUnsubscribe,
    GeographicPlace,
    OwnershipRight,
    PageVisit,
    PageVisitError,
    PageVisitFormElement,
    VisuraDocument,
    VisuraOwner,
    VisuraProperty,
    VisuraRequest,
    VisuraResponse,
    VisuraResult,
)
from sister.visura_xml_models import (  # noqa: F401, E402
    BuildingAddress,
    BuildingClassification,
    BuildingCurrentState,
    BuildingIdentifier,
    BuildingSurface,
    DocumentXmlAttribute,
    DocumentXmlNode,
    BuildingUnit,
    DocumentSubject,
    LandClassification,
    LandParcel,
    OwnershipMutation,
    PropertyGroup,
    PropertyOwner,
    RelatedParcel,
)

config = context.config

# Prefer PostgreSQL when its DSN is configured; retain SQLite as the local default.
database_dsn = os.getenv("DATABSE_DSN")
if database_dsn:
    config.set_main_option("sqlalchemy.url", database_dsn.replace("+asyncpg", "+psycopg2", 1))
else:
    db_path = os.getenv(
        "SISTER_DB_PATH",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "sister.sqlite"),
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
