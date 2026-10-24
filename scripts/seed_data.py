import argparse
import os
import sys

# Ensure hrms-bot directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import get_table_counts
from app.seed import reset_passwords, seed_database


def main():
    parser = argparse.ArgumentParser(description="Seed synthetic HRMS data and credentials.")
    parser.add_argument("--reset", action="store_true", help="Reset all tables and re-seed from scratch")
    parser.add_argument("--reset-passwords", action="store_true", help="Reset all active employee passwords to shared demo password")
    args = parser.parse_args()

    if args.reset_passwords:
        print("Resetting active employee passwords to shared demo password...")
        try:
            reset_passwords()
        except ValueError as err:
            print(f"Error: {err}")
            sys.exit(1)
        return

    print(f"Seeding HRMS database (reset={args.reset})...")
    seed_database(reset=args.reset)

    print("\n--- Database Row Counts ---")
    counts = get_table_counts()
    for table_name, count in counts.items():
        print(f"  {table_name:<25}: {count:>5} rows")
    print("---------------------------\n")


if __name__ == "__main__":
    main()
