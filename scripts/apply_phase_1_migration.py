"""One-shot migration runner for Phase 1 (admin IA reorg).

Applies the same schema + data changes documented in
migrations/versions/0002_phase_1_ia_reorg.py but using pure stdlib `sqlite3`
because SQLAlchemy / Alembic startup is prohibitively slow on this machine.

After running:
  - alembic_version row exists with version_num = '0002_phase_1_ia_reorg'
  - All Phase 1 schema/data changes are applied
  - Future migrations can use the regular Alembic CLI

Usage:
    python scripts/apply_phase_1_migration.py [--db PATH]

Idempotent: re-running detects an already-applied migration and exits.
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime


def _classify_business_line(name):
    if not name:
        return 'Product'
    n = name.lower()
    if 'choice' in n or 'сургалт' in n or 'training' in n:
        return 'Course'
    if 'consulting' in n or 'audit' in n or 'аудит' in n or 'cpa' in n:
        return 'Service'
    return 'Product'


def _find_or_create_magic_cloud_bu(cur, now):
    row = cur.execute(
        "SELECT id FROM business_line "
        "WHERE LOWER(name) LIKE '%cloud%' "
        "   OR LOWER(name) LIKE '%magic cloud%' "
        "   OR LOWER(name) LIKE '%програм%' "
        "ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if row:
        return row[0]
    row = cur.execute(
        "SELECT id FROM business_line WHERE product_type = 'Product' "
        "ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO business_line "
        "(name, description, action, is_active, sort_order, product_type, "
        " created_at, updated_at) "
        "VALUES (?, ?, 'refer', 1, 99, 'Product', ?, ?)",
        (
            'Migrated Software',
            'Auto-created by phase_1_ia_reorg to hold software rows that had '
            'no Magic Cloud business line. Rename or merge after review.',
            now, now,
        ),
    )
    return cur.lastrowid


def _rebuild_table(cur, old_name, new_create_sql, copy_columns, foreign_key_check_off=True):
    """SQLite-friendly rebuild: create new table, copy rows, drop old, rename.

    `new_create_sql` MUST create a table called `_new_<old_name>`. After
    copying it gets renamed to `<old_name>`.

    `copy_columns` is a comma-separated string listing the columns to copy
    from the old table to the new (must exist in both).
    """
    if foreign_key_check_off:
        cur.execute("PRAGMA foreign_keys = OFF")
    try:
        cur.execute(new_create_sql)
        cur.execute(
            f"INSERT INTO _new_{old_name} ({copy_columns}) "
            f"SELECT {copy_columns} FROM {old_name}"
        )
        cur.execute(f"DROP TABLE {old_name}")
        cur.execute(f"ALTER TABLE _new_{old_name} RENAME TO {old_name}")
    finally:
        if foreign_key_check_off:
            cur.execute("PRAGMA foreign_keys = ON")


def apply_migration(db_path):
    if not os.path.exists(db_path):
        print(f'DB not found at {db_path}', file=sys.stderr)
        return 2
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    # --- preflight: check alembic_version ---
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    )
    if cur.fetchone():
        v = cur.execute("SELECT version_num FROM alembic_version").fetchone()
        if v and v[0] == '0002_phase_1_ia_reorg':
            print('Phase 1 already applied (alembic_version=0002_phase_1_ia_reorg). Nothing to do.')
            return 0
        print(f'alembic_version present but at {v[0] if v else "None"}; expecting baseline.')
    else:
        # Create alembic_version table and stamp at baseline.
        cur.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY)"
        )
        cur.execute("INSERT INTO alembic_version (version_num) VALUES ('0001_baseline')")
        print('Created alembic_version table, stamped at 0001_baseline.')

    now = datetime.utcnow().isoformat(sep=' ', timespec='seconds')

    # --- 1. business_line: add product_type ---
    cur.execute("PRAGMA table_info(business_line)")
    bl_cols = {row[1] for row in cur.fetchall()}
    if 'product_type' not in bl_cols:
        cur.execute(
            "ALTER TABLE business_line ADD COLUMN product_type VARCHAR(20) "
            "NOT NULL DEFAULT 'Product'"
        )
        print('Added business_line.product_type column.')

    # --- 2. backfill product_type per row ---
    bl_rows = cur.execute("SELECT id, name FROM business_line").fetchall()
    for bl_id, bl_name in bl_rows:
        pt = _classify_business_line(bl_name)
        cur.execute("UPDATE business_line SET product_type = ? WHERE id = ?", (pt, bl_id))
    print(f'Backfilled product_type on {len(bl_rows)} business_line rows.')

    # --- 3. create course_link + service_link tables ---
    cur.execute(
        "CREATE TABLE IF NOT EXISTS course_link ("
        " id INTEGER PRIMARY KEY,"
        " course_id INTEGER NOT NULL,"
        " description VARCHAR(200) NOT NULL,"
        " url VARCHAR(500) NOT NULL,"
        " note TEXT,"
        " is_active BOOLEAN NOT NULL DEFAULT 1,"
        " sort_order INTEGER NOT NULL DEFAULT 0,"
        " created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        " FOREIGN KEY (course_id) REFERENCES course (id) ON DELETE CASCADE"
        ")"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS service_link ("
        " id INTEGER PRIMARY KEY,"
        " service_id INTEGER NOT NULL,"
        " description VARCHAR(200) NOT NULL,"
        " url VARCHAR(500) NOT NULL,"
        " note TEXT,"
        " is_active BOOLEAN NOT NULL DEFAULT 1,"
        " sort_order INTEGER NOT NULL DEFAULT 0,"
        " created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        " FOREIGN KEY (service_id) REFERENCES service (id) ON DELETE CASCADE"
        ")"
    )
    print('Created course_link and service_link tables (idempotent).')

    # --- 4. product_link: kind/label -> description, add note ---
    cur.execute("PRAGMA table_info(product_link)")
    pl_cols = {row[1] for row in cur.fetchall()}
    if 'kind' in pl_cols or 'label' in pl_cols:
        if 'description' not in pl_cols:
            cur.execute("ALTER TABLE product_link ADD COLUMN description VARCHAR(200)")
        if 'note' not in pl_cols:
            cur.execute("ALTER TABLE product_link ADD COLUMN note TEXT")
        cur.execute(
            "UPDATE product_link SET description = "
            "CASE "
            "  WHEN kind IS NOT NULL AND kind != '' "
            "       AND label IS NOT NULL AND label != '' "
            "    THEN '[' || kind || '] ' || label "
            "  WHEN label IS NOT NULL AND label != '' THEN label "
            "  WHEN kind IS NOT NULL AND kind != '' THEN kind "
            "  ELSE 'Link' "
            "END "
            "WHERE description IS NULL OR description = ''"
        )
        # Rebuild product_link without `kind` and `label`, with description NOT NULL.
        _rebuild_table(
            cur,
            'product_link',
            (
                "CREATE TABLE _new_product_link ("
                " id INTEGER PRIMARY KEY,"
                " product_id INTEGER NOT NULL,"
                " description VARCHAR(200) NOT NULL,"
                " url VARCHAR(500) NOT NULL,"
                " note TEXT,"
                " is_active BOOLEAN NOT NULL DEFAULT 1,"
                " sort_order INTEGER NOT NULL DEFAULT 0,"
                " created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                " FOREIGN KEY (product_id) REFERENCES product (id) ON DELETE CASCADE"
                ")"
            ),
            'id, product_id, description, url, note, is_active, sort_order, created_at',
        )
        print('Rebuilt product_link with description/note (dropped kind, label).')
    else:
        print('product_link already migrated (no kind/label columns).')

    # --- 5. migrate software -> product, drop software ---
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='software'"
    )
    if cur.fetchone():
        sw_rows = cur.execute(
            "SELECT id, name, description, vendor, is_active, status_note, "
            "       sort_order, created_at "
            "FROM software ORDER BY id ASC"
        ).fetchall()
        if sw_rows:
            target_bl_id = _find_or_create_magic_cloud_bu(cur, now)
            next_num_row = cur.execute(
                "SELECT COALESCE(MAX(product_number), 0) FROM product"
            ).fetchone()
            next_num = (next_num_row[0] or 0) + 1
            for s in sw_rows:
                sw_id, sw_name, sw_desc, sw_vendor, sw_active, sw_note, sw_sort, sw_created = s
                cur.execute(
                    "INSERT INTO product "
                    "(business_line_id, product_number, name, vendor, "
                    " description, is_main_product, is_active, status_note, "
                    " sort_order, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                    (
                        target_bl_id, next_num, sw_name, sw_vendor, sw_desc,
                        1 if sw_active else 0, sw_note, sw_sort or 0,
                        sw_created or now, now,
                    ),
                )
                cur.execute(
                    "INSERT INTO audit_entry "
                    "(actor_username, action, entity_type, entity_id, "
                    " entity_label, detail, created_at) "
                    "VALUES (NULL, 'system_migration', 'software', ?, ?, ?, ?)",
                    (
                        sw_id, sw_name,
                        f'Migrated software row #{sw_id} -> product '
                        f'#{next_num} on business_line {target_bl_id}.',
                        now,
                    ),
                )
                next_num += 1
            print(f'Migrated {len(sw_rows)} software rows to product table.')
        cur.execute("DROP TABLE software")
        print('Dropped software table.')
    else:
        print('software table already gone, skipping.')

    # --- 6. drop 5 obsolete columns from business_line ---
    cur.execute("PRAGMA table_info(business_line)")
    bl_cols_after = {row[1] for row in cur.fetchall()}
    drop_cols = {'signup_form_url', 'signup_phone', 'exam_form_url',
                 'num_products_or_services', 'total_clients_or_users'}
    if drop_cols & bl_cols_after:
        _rebuild_table(
            cur,
            'business_line',
            (
                "CREATE TABLE _new_business_line ("
                " id INTEGER PRIMARY KEY,"
                " name VARCHAR(150) NOT NULL,"
                " description TEXT,"
                " action VARCHAR(20) DEFAULT 'refer',"
                " contact_info VARCHAR(255),"
                " address TEXT,"
                " email VARCHAR(200),"
                " established_year INTEGER,"
                " is_active BOOLEAN DEFAULT 1,"
                " status_note VARCHAR(255),"
                " sort_order INTEGER DEFAULT 0,"
                " product_type VARCHAR(20) NOT NULL DEFAULT 'Product',"
                " created_at DATETIME,"
                " updated_at DATETIME,"
                " CHECK (product_type IN ('Course', 'Service', 'Product'))"
                ")"
            ),
            'id, name, description, action, contact_info, address, email, '
            'established_year, is_active, status_note, sort_order, product_type, '
            'created_at, updated_at',
        )
        print('Dropped 5 obsolete columns from business_line.')
    else:
        print('business_line already migrated (no obsolete columns).')

    # --- 7. stamp at new revision ---
    cur.execute("UPDATE alembic_version SET version_num = '0002_phase_1_ia_reorg'")
    print('Stamped alembic_version at 0002_phase_1_ia_reorg.')

    conn.commit()
    conn.close()
    print('Phase 1 migration complete.')
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default='instance/magic_bot.db',
                        help='Path to SQLite DB (default: instance/magic_bot.db)')
    args = parser.parse_args()
    return apply_migration(args.db)


if __name__ == '__main__':
    sys.exit(main())
