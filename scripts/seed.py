"""Create demo accounts + organisations for local use.   python -m scripts.seed
All data is fictional and the deployment should be labelled a prototype."""
import os

from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import Base, Organisation, OrganisationMembership, User
from app.db.session import SessionLocal, engine

PASSWORD = os.environ.get("SEED_PASSWORD", "demo-password-123")
LAT, LON = 25.3176, 82.9739  # Varanasi, illustrative

USERS = [
    ("admin@example.com", "Admin", "ADMIN", None),
    ("donor@example.com", "Green Kitchen", "DONOR", ("Green Kitchen Canteen", 0.0, 0.0, None)),
    ("ngo1@example.com", "Annapurna Trust", "NGO", ("Annapurna Trust", 0.02, 0.01, 200)),
    ("ngo2@example.com", "Seva Foundation", "NGO", ("Seva Foundation", 0.06, -0.02, 500)),
    ("volunteer1@example.com", "Ravi", "VOLUNTEER", None),
    ("volunteer2@example.com", "Meera", "VOLUNTEER", None),
]


def main() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        for email, name, role, org in USERS:
            if db.scalar(select(User.id).where(User.email == email)):
                continue
            u = User(name=name, email=email, password_hash=hash_password(PASSWORD), role=role)
            db.add(u)
            db.flush()
            if org:
                o = Organisation(name=org[0], type=role, latitude=LAT + org[1], longitude=LON + org[2], capacity=org[3],
                                 service_radius_km=15, verification_status="VERIFIED")
                db.add(o)
                db.flush()
                db.add(OrganisationMembership(user_id=u.id, organisation_id=o.id))
        db.commit()
    print(f"Seeded. Password for all demo users: {PASSWORD}")
    for email, _, role, _ in USERS:
        print(f"  {role:<10} {email}")


if __name__ == "__main__":
    main()
