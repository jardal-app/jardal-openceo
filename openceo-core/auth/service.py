"""Authentication service: password hashing, permission computation, default seeding."""
from __future__ import annotations

import os
from typing import Any

from passlib.context import CryptContext

from state import GroupDocument, UserDocument

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def compute_user_permissions(user_doc: UserDocument, groups: list[GroupDocument]) -> list[str]:
    """Compute all permissions for a user from their groups."""
    perms: set[str] = set()
    for grp in groups:
        if grp.group_id in user_doc.group_ids:
            perms.update(grp.permissions)
    return list(perms)


def get_groups_for_user(group_ids: list[str], db: Any) -> list[GroupDocument]:
    """Fetch groups by their IDs from the database."""
    if not group_ids:
        return []
    groups = list(db.groups.find({"group_id": {"$in": group_ids}}))
    return [GroupDocument(**g) for g in groups]


def create_default_user_if_needed(db: Any) -> None:
    """Create admin default + default groups if they don't exist."""
    from settings import get_settings

    settings = get_settings()
    admin_username = settings.DEFAULT_ADMIN_USERNAME
    admin_permissions = [
        "read_tickets", "write_tickets", "approve_tickets", "reject_tickets", "review_tickets",
        "qa_review", "manage_users", "manage_groups", "manage_projects", "admin_dashboard", "client_review",
        # Horizon: el espacio personal del CEO no se muestra a los empleados
        # (seccion 9.3 y 9.10). El permiso solo gobierna la vista; el
        # aislamiento real de los datos lo hace el filtro por usuario en la API.
        "personal_space",
    ]

    if db.users.find_one({"username": admin_username}):
        db.groups.update_one(
            {"name": "admin"},
            {"$addToSet": {"permissions": {"$each": admin_permissions}}},
        )
        return

    # Create admin group only if it doesn't exist
    if not db.groups.find_one({"name": "admin"}):
        admin_group = GroupDocument(
            name="admin",
            display_name="Administradores",
            description="Grupo de administradores con todos los permisos",
            permissions=admin_permissions,
            is_default=False,
        )
        db.groups.insert_one(admin_group.model_dump())

    # Create client group only if it doesn't exist
    if not db.groups.find_one({"name": "client"}):
        client_group = GroupDocument(
            name="client",
            display_name="Clientes",
            description="Clientes que pueden revisar tickets asignados",
            permissions=["read_tickets", "client_review", "review_tickets"],
            is_default=True,
        )
        db.groups.insert_one(client_group.model_dump())

    # Get the admin group for the user
    admin_group_doc = db.groups.find_one({"name": "admin"})
    if not admin_group_doc:
        return  # Should not happen

    admin_group = GroupDocument(**admin_group_doc)
    admin_perms = list(admin_group.permissions)
    admin_user = UserDocument(
        username=admin_username,
        email="admin@openceo.jardal.app",
        password_hash=hash_password(settings.DEFAULT_ADMIN_PASSWORD),
        display_name="Administrador",
        is_admin=True,
        group_ids=[admin_group.group_id],
        all_permissions=admin_perms,
    )
    db.users.insert_one(admin_user.model_dump())
