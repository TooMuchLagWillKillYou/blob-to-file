#!/usr/bin/env python3
"""
Export LONGBLOB data from a MySQL table to physical files.

Requirements:
    pip install pymysql python-dotenv

Database credentials are read from a .env file (or the environment):
    MYSQL_HOST=localhost
    MYSQL_PORT=3306
    MYSQL_USER=app
    MYSQL_DATABASE=mydb

Command-line options (--host, --user, ...) override the .env values.
The password is always prompted for interactively.

Examples:
    # Export a single row (id = 42) to an explicit file path
    python blob_to_file.py --table documents --blob-col content \
        --id-col id --id 42 --output ./out/report.pdf

    # Export every row to a folder, naming each file from a column
    python blob_to_file.py --table documents --blob-col content \
        --id-col id --name-col file_name --output-dir ./export
"""

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

import pymysql
from dotenv import load_dotenv

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_$]+$")
DEFAULT_OUTPUT_DIR = "./output"

def quote_ident(name: str) -> str:
    """Safely quote a table/column name (these can't be bound as parameters)."""
    if not IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return f"`{name}`"


def safe_filename(name: str) -> str:
    """Strip any directory parts and unsafe characters from a stored file name."""
    name = os.path.basename(str(name).replace("\\", "/")).strip()
    name = re.sub(r"[^\w.\- ]", "_", name)
    return name or "unnamed"


def write_bytes(path: Path, data: bytes, overwrite: bool) -> None:
    """Write bytes atomically (temp file + rename) so partial files never remain."""
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists (use --overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        # Also covers Ctrl+C, so an interrupted export leaves no .part file behind
        tmp.unlink(missing_ok=True)
        raise


def main() -> int:
    p = argparse.ArgumentParser(description="Export MySQL LONGBLOB values to files.")
    p.add_argument("--env-file", default=".env", help="Credentials file (default .env)")
    p.add_argument("--host", help="Overrides MYSQL_HOST (default localhost)")
    p.add_argument("--port", type=int, help="Overrides MYSQL_PORT (default 3306)")
    p.add_argument("--user", help="Overrides MYSQL_USER")
    p.add_argument("--database", help="Overrides MYSQL_DATABASE")
    p.add_argument("--table", required=True)
    p.add_argument("--blob-col", required=True, help="LONGBLOB column")
    p.add_argument("--id-col", default="id", help="Primary key column")
    p.add_argument("--id", help="Export only the row with this id")
    p.add_argument("--name-col", help="Column holding the original file name")
    p.add_argument("--ext", default=".bin", help="Extension when no name column (default .bin)")
    p.add_argument("--output", help="Output file name or path, relative to --output-dir (single row with --id)")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help=f"Folder for exported files (default {DEFAULT_OUTPUT_DIR})")
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    args = p.parse_args()

    if args.output and args.id is None:
        p.error("--output requires --id")

    # Existing environment variables take precedence over the .env file
    load_dotenv(args.env_file)
    host = args.host or os.environ.get("MYSQL_HOST", "localhost")
    port = args.port or int(os.environ.get("MYSQL_PORT", "3306"))
    user = args.user or os.environ.get("MYSQL_USER")
    database = args.database or os.environ.get("MYSQL_DATABASE")
    if not user:
        p.error("database user not set (MYSQL_USER in .env or --user)")
    if not database:
        p.error("database name not set (MYSQL_DATABASE in .env or --database)")

    password = getpass.getpass("Database password: ")

    cols = [quote_ident(args.id_col), quote_ident(args.blob_col)]
    if args.name_col:
        cols.append(quote_ident(args.name_col))
    sql = f"SELECT {', '.join(cols)} FROM {quote_ident(args.table)}"
    params = ()
    if args.id is not None:
        sql += f" WHERE {quote_ident(args.id_col)} = %s"
        params = (args.id,)

    conn = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        # SSCursor streams rows one by one instead of loading the whole result set
        cursorclass=pymysql.cursors.SSCursor,
    )

    exported = skipped = 0
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur:
                row_id, blob = row[0], row[1]
                if blob is None:
                    print(f"[skip] id={row_id}: blob is NULL", file=sys.stderr)
                    skipped += 1
                    continue

                output_dir = Path(args.output_dir)

                if args.output:
                    # A relative --output is placed inside --output-dir; an absolute one wins
                    target = output_dir / args.output
                elif args.name_col and row[2]:
                    target = output_dir / f"{row_id}_{safe_filename(row[2])}"
                else:
                    target = output_dir / f"{row_id}{args.ext}"

                try:
                    write_bytes(target, bytes(blob), args.overwrite)
                    print(f"[ok]   id={row_id} -> {target} ({len(blob):,} bytes)")
                    exported += 1
                except FileExistsError as e:
                    print(f"[skip] {e}", file=sys.stderr)
                    skipped += 1
    finally:
        conn.close()

    if exported == 0 and skipped == 0:
        print("No rows found.", file=sys.stderr)
        return 1
    print(f"Done: {exported} exported, {skipped} skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())