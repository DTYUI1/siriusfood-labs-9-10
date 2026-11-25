"""Coordinate credential revocations with managed rollback to legacy auth code."""
from sqlalchemy import text

# Stable protocol with scripts/product_release.py; never repurpose this lock id.
AUTH_MUTATION_LOCK = 1397115204


def lock_auth_mutation(db):
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": AUTH_MUTATION_LOCK})
