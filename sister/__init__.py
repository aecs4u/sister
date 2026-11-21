
# Legacy database variables derived from <DB>_POSTGRES_DSN_<SUFFIX> in ../.env.<profile>
from pathlib import Path as _Path
from ._dsn_bridge import bridge_postgres_dsns as _bridge_postgres_dsns

_bridge_postgres_dsns(_Path(__file__).resolve().parents[1], {'CADASTRAL_DATABASE_DSN_LOCALHOST': ('CADASTRAL_POSTGRES_DSN_LOCALHOST', None), 'CADASTRAL_DATABASE_DSN_NEON': ('CADASTRAL_POSTGRES_DSN_PROD', None), 'OPENDATA_DATABASE_DSN_LOCALHOST': ('OPENDATA_POSTGRES_DSN_LOCALHOST', None), 'SISTER_DATABASE_DSN_NEON': ('SISTER_POSTGRES_DSN_PROD', None), 'SISTER_DATABASE_DSN_TEST': ('SISTER_POSTGRES_DSN_TEST', None)})
