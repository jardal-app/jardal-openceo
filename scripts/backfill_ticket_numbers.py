#!/usr/bin/env python3
"""Backfill ticket_number for existing tasks without one.

Assigns sequential numbers starting at 1, ordered by created_at (oldest first).
Updates the counter so new tickets continue from the max assigned.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pymongo import MongoClient

MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://localhost:27017")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "openceo")


def parse_created_at(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        value = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def main():
    client = MongoClient(MONGODB_URL)
    db = client[MONGODB_DATABASE]
    tasks = db["tasks"]
    counters = db["counters"]

    # Find all tasks without a ticket_number
    cursor = tasks.find({"ticket_number": {"$exists": False}})
    no_number = list(cursor)

    if not no_number:
        print("No tasks missing ticket_number. Nothing to do.")
        client.close()
        return

    print(f"Found {len(no_number)} tasks without ticket_number.")

    # Sort by created_at ascending (oldest first) so the first ticket gets #1
    no_number.sort(key=lambda doc: parse_created_at(doc.get("created_at")))

    # Assign sequential numbers
    for idx, doc in enumerate(no_number, start=1):
        tasks.update_one({"_id": doc["_id"]}, {"$set": {"ticket_number": idx}})
        print(f"  Assigned ticket_number {idx} to {doc.get('task_id')} ({doc.get('title', '')[:40]})")

    # Update counter to max ticket_number across all tasks
    max_doc = tasks.find_one(
        {"ticket_number": {"$exists": True}},
        sort=[("ticket_number", -1)],
    )
    max_number = max_doc["ticket_number"] if max_doc else len(no_number)

    counters.update_one(
        {"_id": "ticket_number"},
        {"$set": {"seq": max_number}},
        upsert=True,
    )
    print(f"Updated counter 'ticket_number' to {max_number}.")
    print("Done.")
    client.close()


if __name__ == "__main__":
    main()
