"""Initial schema + PostGIS geography indexes

Revision ID: 0001
Revises:
"""
from alembic import op

from app.core.config import settings
from app.db.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Expression must match app/modules/matching/geo.py so ST_DWithin can use the GiST index.
_GEOG = "CAST(ST_SetSRID(ST_MakePoint(longitude, latitude), 4326) AS geography)"


def upgrade() -> None:
    bind = op.get_bind()
    postgis = settings.use_postgis and bind.dialect.name == "postgresql"
    if postgis:
        op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    # The first revision snapshots the schema defined by the models; later revisions should be explicit
    # (alembic revision --autogenerate -m "...").
    Base.metadata.create_all(bind)
    if postgis:
        op.execute(f"CREATE INDEX ix_donations_geog ON donations USING GIST (({_GEOG}))")
        op.execute(f"CREATE INDEX ix_organisations_geog ON organisations USING GIST (({_GEOG}))")
        op.execute(f"CREATE INDEX ix_volunteer_locations_geog ON volunteer_locations USING GIST (({_GEOG}))")


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
