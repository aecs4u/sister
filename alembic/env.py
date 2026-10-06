"""Alembic migration environment for sister."""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url
from sqlmodel import SQLModel

from alembic import context

# Add project root to path so sister package is importable
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")
load_dotenv(project_root.parent / ".env", override=False)

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

# Alembic and the application use the same PostgreSQL connection setting.
database_dsn = os.getenv("DATABASE_DSN")
if not database_dsn:
    raise RuntimeError("DATABASE_DSN must be set to a PostgreSQL connection URL")
url = make_url(database_dsn)
if url.get_backend_name() != "postgresql":
    raise RuntimeError("DATABASE_DSN must use PostgreSQL; other database backends are unsupported")
if url.drivername in {
    "postgres",
    "postgresql",
    "postgresql+asyncpg",
    "postgresql+psycopg2",
    "postgresql+psycopg_async",
}:
    url = url.set(drivername="postgresql+psycopg")
# ConfigParser treats '%' specially, including in URL-encoded passwords.
config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))

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
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
