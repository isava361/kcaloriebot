from __future__ import annotations

import argparse

from .legacy import migrate_legacy_database


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import a legacy Go calorie bot database."
    )
    parser.add_argument("source", help="Path to the legacy mydb.db file")
    parser.add_argument("target", help="Path for the new Python database")
    arguments = parser.parse_args()
    report = migrate_legacy_database(arguments.source, arguments.target)
    print(
        f"Imported {report.users} users, {report.entries} entries, and "
        f"{report.favorites} favorites. Skipped {report.skipped_entries} invalid "
        f"entries and {report.skipped_favorites} invalid favorites."
    )


if __name__ == "__main__":
    main()
