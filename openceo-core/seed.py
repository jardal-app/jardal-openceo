#!/usr/bin/env python3
"""Seed default admin user and groups."""
import sys

sys.path.insert(0, "/home/develop/development/Jardal/jardal-openceo/openceo-core")

from pymongo import MongoClient
from settings import get_settings
from auth.service import create_default_user_if_needed


def main():
    settings = get_settings()
    client = MongoClient(settings.MONGODB_URL)
    db = client[settings.MONGODB_DATABASE]
    create_default_user_if_needed(db)
    print(f"Seed done: admin/{settings.DEFAULT_ADMIN_PASSWORD} created if not existed")


if __name__ == "__main__":
    main()
