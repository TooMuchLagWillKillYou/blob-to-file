#!/usr/bin/env python3
"""
Export LONGBLOB data from a MySQL table to physical files.

Requirements:
    pip install -r requirements.txt

Database credentials are read from a .env file (or the environment):
    MYSQL_HOST=localhost
    MYSQL_PORT=3306
    MYSQL_USER=app
    MYSQL_DATABASE=mydb

Command-line options (--host, --user, ...) override the .env values.
The password is always prompted for interactively.

Any required value that is still missing (user, database, table, blob column)
is prompted for. When that happens the script runs as a wizard and also asks
for the optional values that were not given (Enter keeps the default shown);
otherwise optional values silently fall back to their defaults.

Examples (run from the project root: .env and ./output are resolved against
the current directory):
    # Fully interactive: prompts for everything not in .env
    python src/blob_to_file.py

    # Export a single row (id = 42) to ./output/report.pdf
    python src/blob_to_file.py --table documents --blob-col content \
        --id-col id --id 42 --output report.pdf

    # Export every row to a folder, naming each file from a column
    python src/blob_to_file.py --table documents --blob-col content \
        --id-col id --name-col file_name --output-dir ./export
"""

import argparse
import getpass
import os
import re
import sys
from collections.abc import Callable
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


def ask(label: str, default: str | None = None, required: bool = False,
        validate: Callable[[str], object] | None = None) -> str | None:
    """Prompt for a value; Enter keeps the default (or skips an optional value)."""
    hint = f" [{default}]" if default is not None else ""
    while True:
        value = input(f"{label}{hint}: ").strip()
        if not value:
            if required and default is None:
                print(f"{label} is required.", file=sys.stderr)
                continue
            return default
        if validate:
            try:
                validate(value)
            except ValueError as e:
                print(e, file=sys.stderr)
                continue
        return value


def confirm(label: str) -> bool:
    return input(f"{label} [y/N]: ").strip().lower() in ("y", "yes")


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
    p.add_argument("--table")
    p.add_argument("--blob-col", help="LONGBLOB column")
    p.add_argument("--id-col", help="Primary key column (default id)")
    p.add_argument("--id", help="Export only the row with this id")
    p.add_argument("--name-col", help="Column holding the original file name")
    p.add_argument("--ext", help="Extension when no name column (default .bin)")
    p.add_argument("--output", help="Output file name or path, relative to --output-dir (single row with --id)")
    p.add_argument("--output-dir", help=f"Folder for exported files (default {DEFAULT_OUTPUT_DIR})")
    p.add_argument("--overwrite", action=argparse.BooleanOptionalAction, help="Overwrite existing files")
    args = p.parse_args()

    if args.output and args.id is None:
        p.error("--output requires --id")

    # Precedence: command line > environment > .env file > interactive prompt
    load_dotenv(args.env_file)
    host = args.host or os.environ.get("MYSQL_HOST")
    port = args.port or os.environ.get("MYSQL_PORT")
    user = args.user or os.environ.get("MYSQL_USER")
    database = args.database or os.environ.get("MYSQL_DATABASE")

    # Something required is missing, so the user gets asked for optional values too
    wizard = not all((user, database, args.table, args.blob_col))

    def optional(value, label, default=None, validate=None):
        if value is not None:
            return value
        return ask(label, default, validate=validate) if wizard else default

    host = optional(host, "Host", "localhost")
    port = int(optional(port, "Port", "3306", validate=int))
    user = user or ask("Database user", required=True)
    database = database or ask("Database name", required=True)
    password = getpass.getpass("Database password: ")

    table = args.table or ask("Table", required=True, validate=quote_ident)
    blob_col = args.blob_col or ask("Blob column", required=True, validate=quote_ident)
    id_col = optional(args.id_col, "Id column", "id", validate=quote_ident)
    row_filter = optional(args.id, "Row id (empty = all rows)")
    output = args.output
    if output is None and row_filter is not None:
        output = optional(None, "Output file (empty = name it from id/name column)")
    # The naming options only matter when no explicit output file is given
    name_col = args.name_col
    ext = args.ext
    if output is None:
        name_col = optional(name_col, "File name column (optional)", validate=quote_ident)
        ext = optional(ext, "Extension when no file name", ".bin")
    output_dir = optional(args.output_dir, "Output folder", DEFAULT_OUTPUT_DIR)
    overwrite = args.overwrite if args.overwrite is not None else (wizard and confirm("Overwrite existing files?"))

    cols = [quote_ident(id_col), quote_ident(blob_col)]
    if name_col:
        cols.append(quote_ident(name_col))
    sql = f"SELECT {', '.join(cols)} FROM {quote_ident(table)}"
    params = ()
    if row_filter is not None:
        sql += f" WHERE {quote_ident(id_col)} = %s"
        params = (row_filter,)

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

                if output:
                    # A relative --output is placed inside --output-dir; an absolute one wins
                    target = Path(output_dir) / output
                elif name_col and row[2]:
                    target = Path(output_dir) / f"{row_id}_{safe_filename(row[2])}"
                else:
                    target = Path(output_dir) / f"{row_id}{ext}"

                try:
                    write_bytes(target, bytes(blob), overwrite)
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