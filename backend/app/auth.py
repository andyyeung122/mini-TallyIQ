"""Stand-in for real auth. In production the React app would send a Firebase/IdP
token and this dependency would verify it; here the X-User header names a seeded user."""
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .models import User

SEED_USERS = [
    ("priya", "Priya (AR specialist)", "preparer"),
    ("marcus", "Marcus (Controller)", "approver"),
    ("dana", "Dana (Finance manager)", "preparer,approver"),
]


def seed_users(db: Session) -> None:
    if db.scalar(select(User).limit(1)) is None:
        db.add_all(User(username=u, display_name=d, roles=r) for u, d, r in SEED_USERS)
        db.commit()


def current_user(x_user: str | None = Header(default=None), db: Session = Depends(get_db)) -> User:
    if not x_user:
        raise HTTPException(401, "Choose a user to act as")
    user = db.scalar(select(User).where(User.username == x_user))
    if user is None:
        raise HTTPException(401, f"Unknown user: {x_user}")
    return user


def require_role(user: User, role: str) -> None:
    if not user.has_role(role):
        raise HTTPException(403, f"This action needs the {role} role")
