#!/usr/bin/env python
"""Initialize the database schema. Idempotent."""
from lawrag import db

if __name__ == "__main__":
    db.init_schema()
    print("Schema ready (extension + documents + chunks + indexes).")
